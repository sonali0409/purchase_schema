# from __future__ import annotations
# from typing import Optional, Dict, Any, List
# from pydantic import BaseModel, Field


# class ExtractedIntent(BaseModel):
#     """Structured intent returned by the LLM extractor for a single NL question."""
#     report: str = Field(..., description="One of the 9 report names")
#     operation: str = Field(
#         ...,
#         description="list | list_distinct | count | count_distinct | group_by_count | "
#                     "aggregate | trend",
#     )
#     aggregate_function: Optional[str] = Field(
#         None, description="SUM | AVG | MIN | MAX -- only used when operation == 'aggregate'"
#     )
#     aggregate_column: Optional[str] = None
#     group_by_column: Optional[str] = Field(
#         None, description="Semantic key (e.g. 'department', 'plant', 'vendor_name') to group by"
#     )
#     time_grain: Optional[str] = Field(
#         None, description="month | quarter | year -- only used when operation == 'trend'"
#     )
#     distinct_key: Optional[str] = Field(
#         None, description="Semantic key to COUNT(DISTINCT ...) on, e.g. 'po_number'"
#     )
#     filters: Dict[str, Any] = Field(
#         default_factory=dict,
#         description="Semantic key -> value, e.g. {'po_number': '5000011648', 'plant': '1300'}",
#     )
#     date_phrase: Optional[str] = Field(
#         None, description="Raw date phrase as the user said it, e.g. 'last month', 'March 2021'. "
#                           "Null if no date filter should be applied."
#     )
#     limit: Optional[int] = Field(None, description="Row limit if the user asked for top N")
#     clarification_needed: Optional[str] = Field(
#         None, description="If the question is too ambiguous to answer, explain what's missing"
#     )


# class ChatRequest(BaseModel):
#     question: str
#     execute: bool = Field(True, description="If false, only return the generated SQL, don't run it")


# class ChatResponse(BaseModel):
#     question: str
#     report: Optional[str] = None
#     intent: Optional[ExtractedIntent] = None
#     sql: Optional[str] = None
#     resolved_date_range: Optional[List[str]] = None
#     columns: Optional[List[str]] = None
#     rows: Optional[List[Dict[str, Any]]] = None
#     row_count: Optional[int] = None
#     error: Optional[str] = None



from __future__ import annotations
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class ExtractedIntent(BaseModel):
    """Structured intent returned by the LLM extractor for a single NL question."""
    report: str = Field(..., description="One of the 9 report names")
    operation: str = Field(
        ...,
        description="list | list_distinct | count | count_distinct | group_by_count | "
                    "aggregate | trend",
    )
    aggregate_function: Optional[str] = Field(
        None, description="SUM | AVG | MIN | MAX -- only used when operation == 'aggregate'"
    )
    aggregate_column: Optional[str] = None
    group_by_column: Optional[str] = Field(
        None, description="Semantic key (e.g. 'department', 'plant', 'vendor_name') to group by"
    )
    time_grain: Optional[str] = Field(
        None, description="month | quarter | year -- only used when operation == 'trend'"
    )
    distinct_key: Optional[str] = Field(
        None, description="Semantic key to COUNT(DISTINCT ...) on, e.g. 'po_number'"
    )
    filters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Semantic key -> value, e.g. {'po_number': '5000011648', 'plant': '1300'}",
    )
    date_phrase: Optional[str] = Field(
        None, description="Raw date phrase as the user said it, e.g. 'last month', 'March 2021'. "
                          "Null if no date filter should be applied."
    )
    limit: Optional[int] = Field(None, description="Row limit if the user asked for top N")
    clarification_needed: Optional[str] = Field(
        None, description="If the question is too ambiguous to answer, explain what's missing"
    )
    date_type: Optional[str] = Field(
        None, description="PR2PO ONLY: which of PR2PO's date columns a date_phrase should "
                          "filter on -- 'created' (P2P_Created_On, the default), "
                          "'delivery' (P2P_Delivery_Date), or 'modified' (P2P_Last_Changed_On). "
                          "Ignored for every other report."
    )
    kpi_id: Optional[str] = Field(
        None, description="Set ONLY when the question matches one of the fixed KPI calculations "
                          "in sql_builder.KPI_REGISTRY (e.g. 'pr-pending-for-po', 'po-pending', "
                          "'material-po-delay') -- these need composite conditions/NULL checks/"
                          "date-diffs the generic operation above can't express, so they're built "
                          "from a hand-written SQL template instead. filters and date_phrase are "
                          "still used normally when kpi_id is set; operation/aggregate_*/"
                          "group_by_column/time_grain/distinct_key are ignored in that case. Null "
                          "for ordinary questions."
    )


class ChatRequest(BaseModel):
    question: str
    execute: bool = Field(True, description="If false, only return the generated SQL, don't run it")


class ChatResponse(BaseModel):
    question: str
    report: Optional[str] = None
    intent: Optional[ExtractedIntent] = None
    sql: Optional[str] = None
    resolved_date_range: Optional[List[str]] = None
    # Human label for the applied window, e.g. 'FY2026-27', 'March 2021'.
    resolved_date_label: Optional[str] = None
    # True when the user named no period and the window was defaulted (current FY for
    # trend questions) rather than taken from the question itself.
    date_range_defaulted: bool = False
    columns: Optional[List[str]] = None
    rows: Optional[List[Dict[str, Any]]] = None
    row_count: Optional[int] = None
    error: Optional[str] = None