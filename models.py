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


class ChatRequest(BaseModel):
    question: str
    execute: bool = Field(True, description="If false, only return the generated SQL, don't run it")


class ChatResponse(BaseModel):
    question: str
    report: Optional[str] = None
    intent: Optional[ExtractedIntent] = None
    sql: Optional[str] = None
    resolved_date_range: Optional[List[str]] = None
    columns: Optional[List[str]] = None
    rows: Optional[List[Dict[str, Any]]] = None
    row_count: Optional[int] = None
    error: Optional[str] = None