from __future__ import annotations
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import schema as sch
from models import ChatRequest, ChatResponse
from intent_extractor import extract_intent
from sql_builder import build_sql
from presto_connection import run_query
from config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nl2sql")

app = FastAPI(
    title="Procurement NL-to-SQL Chatbot",
    description="Ask natural-language questions across the 9-report unified procurement "
                "schema (ME2L, PO_release, PR_release, Vendor_PO_History, Sap_Purchase, "
                "PR2PO, GateEntry, Material_Doc_List, SES) and get back SQL + results.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        raise HTTPException(status_code=400, detail="question must not be empty")

    try:
        intent = extract_intent(question)
    except Exception as e:
        logger.exception("Intent extraction failed")
        return ChatResponse(question=question, error=f"Could not understand the question: {e}")

    if intent.clarification_needed:
        return ChatResponse(
            question=question,
            report=intent.report,
            intent=intent,
            error=intent.clarification_needed,
        )

    try:
        sql, resolved_range = build_sql(intent)
    except Exception as e:
        logger.exception("SQL build failed")
        return ChatResponse(
            question=question, report=intent.report, intent=intent,
            error=f"Could not build SQL: {e}",
        )

    resolved_range_str = [d.isoformat() for d in resolved_range] if resolved_range else None

    if not req.execute:
        return ChatResponse(
            question=question, report=intent.report, intent=intent, sql=sql,
            resolved_date_range=resolved_range_str,
        )

    try:
        print(f"Executing SQL for question: {question}\nSQL: {sql}")
        columns, rows = run_query(sql, max_rows=intent.limit or settings.MAX_ROWS)
    except Exception as e:
        logger.exception("Query execution failed")
        return ChatResponse(
            question=question, report=intent.report, intent=intent, sql=sql,
            resolved_date_range=resolved_range_str,
            error=f"Query execution failed: {e}",
        )

    return ChatResponse(
        question=question, report=intent.report, intent=intent, sql=sql,
        resolved_date_range=resolved_range_str,
        columns=columns, rows=rows, row_count=len(rows),
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)