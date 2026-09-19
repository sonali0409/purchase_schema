from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import schema as sch
from models import ChatRequest, ChatResponse
from intent_extractor import extract_intent
from sql_builder import (
    build_sql,
    build_kpi_sql,
    resolve_intent_date_range,
    _filter_values,
    _resolve_filter_column,
)
from presto_connection import run_query
from config import settings
from graph_integration import build_graph_block


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nl2sql")


app = FastAPI(
    title="Procurement NL-to-SQL Chatbot",
    description=(
        "Ask natural-language questions across the 9-report unified procurement "
        "schema (ME2L, PO_release, PR_release, Vendor_PO_History, Sap_Purchase, "
        "PR2PO, GateEntry, Material_Doc_List, SES) and get back SQL + results."
    ),
    version="1.0.0",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _separate_filter_group_columns(question: str, intent) -> list:
    if "separately" not in question.lower():
        return []

    cols = []

    for key, value in (intent.filters or {}).items():
        if len(_filter_values(value)) < 2:
            continue

        col = _resolve_filter_column(intent.report, key)

        if col and col not in cols:
            cols.append(col)

    return cols


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/schema")
def get_schema():
    """Returns the report catalog: columns per report and each report's date filter column."""

    return {
        "table": sch.TABLE_NAME,
        "reports": {
            r: {
                "date_filter_column": sch.date_filter_column(r),
                "column_count": len(sch.all_columns_for_report(r)),
                "columns": sch.all_columns_for_report(r),
            }
            for r in sch.REPORT_NAMES
        },
    }


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    question = req.question.strip()

    if not question:
        raise HTTPException(
            status_code=400,
            detail="question must not be empty",
        )

    # ---------------------------------------------------------
    # 1. Extract intent
    # ---------------------------------------------------------
    try:
        intent = extract_intent(question)

    except Exception as e:
        logger.exception("Intent extraction failed")

        return ChatResponse(
            question=question,
            error=f"Could not understand the question: {e}",
        )

    print(f"Extracted intent: {intent}")

    # ---------------------------------------------------------
    # 2. Handle clarification
    # ---------------------------------------------------------
    if intent.clarification_needed:
        return ChatResponse(
            question=question,
            report=intent.report,
            intent=intent,
            error=intent.clarification_needed,
        )

    # ---------------------------------------------------------
    # 3. Resolve date range + build SQL
    # ---------------------------------------------------------
    try:
        # Resolve the date window once so KPI and generic paths
        # use the same date range.
        date_info = resolve_intent_date_range(intent)

        if intent.kpi_id:
            # Fixed KPI calculation
            sql = build_kpi_sql(
                intent.kpi_id,
                intent.filters,
                date_info.range,
                operation=intent.operation,
                distinct_key=intent.distinct_key,
                time_grain=intent.time_grain,
                group_by_column=intent.group_by_column,
                extra_group_columns=_separate_filter_group_columns(
                    question,
                    intent,
                ),
            )

            resolved_range = date_info.range

        else:
            sql, resolved_range = build_sql(
                intent,
                date_range=date_info.range,
                extra_group_columns=_separate_filter_group_columns(
                    question,
                    intent,
                ),
            )

    except Exception as e:
        logger.exception("SQL build failed")

        return ChatResponse(
            question=question,
            report=intent.report,
            intent=intent,
            error=f"Could not build SQL: {e}",
        )

    # Convert dates to strings for API response
    resolved_range_str = (
        [d.isoformat() for d in resolved_range]
        if resolved_range
        else None
    )

    # ---------------------------------------------------------
    # 4. Return SQL only when execute=False
    # ---------------------------------------------------------
    if not req.execute:
        return ChatResponse(
            question=question,
            report=intent.report,
            intent=intent,
            sql=sql,
            resolved_date_range=resolved_range_str,
            resolved_date_label=date_info.label,
            date_range_defaulted=date_info.defaulted,
        )

    # ---------------------------------------------------------
    # 5. Execute SQL
    # ---------------------------------------------------------
    try:
        print(
            f"Executing SQL for question: {question}\n"
            f"SQL: {sql}"
        )

        columns, rows = run_query(
            sql,
            max_rows=intent.limit or settings.MAX_ROWS,
        )

    except Exception as e:
        logger.exception("Query execution failed")

        return ChatResponse(
            question=question,
            report=intent.report,
            intent=intent,
            sql=sql,
            resolved_date_range=resolved_range_str,
            resolved_date_label=date_info.label,
            date_range_defaulted=date_info.defaulted,
            error=f"Query execution failed: {e}",
        )

    # ---------------------------------------------------------
    # 6. Generate dashboard / graph from the EXACT SQL result
    # ---------------------------------------------------------
    graph = build_graph_block(
        question=question,
        columns=columns,
        rows=rows,
        intent=intent,
        date_range=date_info,
        date_label=date_info.label,
    )

    # ---------------------------------------------------------
    # 7. Return Purchase result + graph URL
    # ---------------------------------------------------------
    return ChatResponse(
        question=question,
        report=intent.report,
        intent=intent,
        sql=sql,
        resolved_date_range=resolved_range_str,
        resolved_date_label=date_info.label,
        date_range_defaulted=date_info.defaulted,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        graph=graph,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )