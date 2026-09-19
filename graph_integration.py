"""Small post-processing adapter for the existing Purchase /chat endpoint."""

from __future__ import annotations

import logging
from typing import Any

from dashboard import generate_dashboard


logger = logging.getLogger(__name__)


def build_graph_block(
    *,
    question: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    intent: Any,
    date_range: Any,
    date_label: str | None = None,
) -> dict[str, Any]:
    """
    Generate a dashboard from the exact Purchase rows already returned.

    This function:
    - does NOT execute SQL
    - does NOT call the LLM
    - does NOT create sample/dummy data
    - passes the exact Purchase result to dashboard.py
    - isolates graph/COS failures from the Purchase API response
    """

    try:
        return generate_dashboard(
            question=question,
            columns=columns,
            rows=rows,
            intent=intent,
            date_range=date_range,
            date_label=date_label,
        )

    except Exception as exc:
        logger.exception(
            "Purchase graph generation failed"
        )

        return {
            "generated": False,
            "error": f"{type(exc).__name__}: {exc}",
        }