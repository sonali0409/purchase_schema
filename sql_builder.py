"""
Builds Trino/Presto SQL from an ExtractedIntent, resolving:
  - semantic filter keys -> real column names (per report, via schema.KEY_COLUMNS)
  - date phrases -> concrete BETWEEN ranges (via date_resolver)
  - operation type -> SELECT/COUNT/GROUP BY/trend shape
"""
from __future__ import annotations
from typing import NamedTuple, Optional, Tuple, List
from datetime import date
import re

import schema as sch
from models import ExtractedIntent
from date_resolver import (
    current_fy_period,
    recent_fy_span,
    resolve_period as resolve_date_period,
    time_grain_trunc_expr,
)
from config import settings

EXACT_MATCH_KEYS = {
    "po_number", "pr_number", "plant", "company_code", "release_status",
    "gate_entry_status", "rejected_at_level", "service_entry_sheet", "current_level",
    "requisitioner", "created_by", "purchasing_group", "creation_indicator",
    "pr_processing_status", "pr_deletion_indicator",
}
FUZZY_MATCH_KEYS = {"vendor_name", "material_desc"}

GROUP_BY_KEY_ALIASES = {
    "plant wise": "plant",
    "plant-wise": "plant",
    "plant": "plant",
    "department wise": "department",
    "department-wise": "department",
    "dept wise": "department",
    "dept-wise": "department",
    "department": "department",
    "material description": "material_desc",
    "material desc": "material_desc",
    "material_description": "material_desc",
    "material_desc": "material_desc",
    "material": "material_code",
    "material code": "material_code",
    "material_code": "material_code",
    "vendor wise": "vendor_name",
    "vendor-wise": "vendor_name",
    "processing status wise": "pr_processing_status",
    "processing status-wise": "pr_processing_status",
    "processing_status": "pr_processing_status",
    "processing status": "pr_processing_status",
    "purchasing group wise": "purchasing_group",
    "purchasing grp wise": "purchasing_group",
    "purchasing group": "purchasing_group",
    "purchasing grp": "purchasing_group",
    "purchasing_group": "purchasing_group",
    "requisitioner wise": "requisitioner",
    "requisitioner-wise": "requisitioner",
}

AGGREGATE_COLUMN_ALIASES = {
    "ME2L": {
        "amount": "Net_Order_Value",
        "value": "Net_Order_Value",
        "net_value": "Net_Order_Value",
        "net order value": "Net_Order_Value",
        "net_order_value": "Net_Order_Value",
        "quantity": "Order_Quantity",
        "qty": "Order_Quantity",
        "order quantity": "Order_Quantity",
        "order_quantity": "Order_Quantity",
        "price": "PO_Net_Price",
        "net price": "PO_Net_Price",
        "po_net_price": "PO_Net_Price",
    },
    "PO_release": {
        "amount": "POR_Amount",
        "value": "POR_Amount",
        "por_amount": "POR_Amount",
        "approval days": "PO_No_Of_Days_Approval",
        "approval_days": "PO_No_Of_Days_Approval",
    },
    "Material_Doc_List": {
        "amount": "MTLST_Amt_in_Loc_Cur",
        "value": "MTLST_Amt_in_Loc_Cur",
        "quantity": "MTLST_Quantity",
        "qty": "MTLST_Quantity",
        "grn amount": "GRN_Amount",
        "grn_amount": "GRN_Amount",
        "grn quantity": "GRN_qty",
        "grn_qty": "GRN_qty",
    },
    "Vendor_PO_History": {
        "amount": "GRN_Amount",
        "value": "GRN_Net_Value",
        "net value": "GRN_Net_Value",
        "net_value": "GRN_Net_Value",
        "gross value": "GRN_Gross_Value",
        "gross_value": "GRN_Gross_Value",
        "quantity": "GRN_qty",
        "qty": "GRN_qty",
        "delay days": "Material_Delay_Days",
        "delay_days": "Material_Delay_Days",
    },
    "SES": {
        "amount": "SES_Amount",
        "value": "SES_Amount",
        "ses_amount": "SES_Amount",
    },
    "Sap_Purchase": {
        "quantity": "SAP_Quantity_Requested",
        "qty": "SAP_Quantity_Requested",
        "requested quantity": "SAP_Quantity_Requested",
        "ordered quantity": "SAP_Quantity_Ordered",
    },
}

# Curated columns shown for row-level ("list") results -- the full 70+ column set per
# report is available via all_columns_for_report() for anyone building custom SELECTs,
# but a default listing should stay readable.

DISPLAY_COLUMNS = {
    "ME2L": ["ME2L_Purchasing_Document", "ME2L_Item", "ME2L_Purchase_req_no", "ME2L_Pur_req_Item_no", "PO_Document_Date", "PO_Plant", "Plant_Description", "PO_Material", "PO_Material_Description", "Name_of_Supplier", "Purchasing_Group_Description", "PO_Department_Name", "Order_Quantity", "PO_Net_Price", "Net_Order_Value", "Still_to_be_delivered_qty", "Still_to_be_invoiced_qty"],

    "PO_release": ["POR_PO", "POR_PO_Doc_Type", "POR_Plant_Code", "POR_Plant_Description", "PO_Created_By", "Created_By_Name", "PO_Created_On", "PO_No_Of_Days_Approval", "PO_No_Of_Releases_Required", "POR_Current_Release_Level", "POR_Amount", "GRN_Flag"],

    "PR_release": ["PR_Current_Release_Level", "PR_Releases_Required", "PR_Released", "PR_Rejected", "Rejected_At_Level", "PR_no_of_days_approval", "PR_Created_On", "HOD_Name", "CFO_Name", "MD_Name", "Process_Owner_Name", "VC_Chairman_Name"],

    "Vendor_PO_History": ["VH_PO_No", "VH_PR_No", "VH_Plant", "VH_Vendor_Name", "VH_Material_Desc", "VH_PO_Date", "VH_PO_Qty", "VH_Net_Price", "VH_Department", "VH_Company", "GRN_Date", "Mat_Doc", "Material_Delay_Days"],

    "Sap_Purchase": ["SAP_Purchase_Requisition", "SAP_Purchase_Order", "Requisitioner", "PR_Department", "PR_Plant", "Company_Code", "PR_Processing_Status", "Requisition_date", "SAP_Name_Of_Supplier", "SAP_Short_Text", "SAP_Material"],

    "PR2PO": ["P2P_PR_No", "P2P_PO_No", "P2P_Vendor_Name", "P2P_Material_Description", "P2P_Department", "P2P_Plant", "PR_To_PO_Days", "P2P_Created_On", "PR_Release_Status", "PO_Release_Status", "P2P_PO_Rejection_Text"],

    "GateEntry": ["GTENTRY_Gate_Entry_Number", "GTENTRY_Gate_Entry_Date", "GTENTRY_Gate_Entry_Status", "GTENTRY_Supplier_Name", "GTENTRY_Material", "GTENTRY_Vehicle_Number", "GTENTRY_Po_qty", "GTENTRY_Received_qty", "GTENTRY_Net_Weight", "GTENTRY_Gross_Weight"],

    "Material_Doc_List": ["ME2L_Purchasing_Document", "PO_Material", "PO_Material_Description", "PO_Document_Date", "PO_Plant", "Name_of_Supplier", "Order_Quantity", "Net_Order_Value", "Still_to_be_delivered_qty", "Still_to_be_invoiced_qty"],

    "SES": ["Service_Entry_Sheet", "SES_Date_of_Creation", "SES_Release_Status", "SES_Release_Level", "SES_Name_of_Person", "SES_Amount", "SES_Passing_Levels"]
}

# Document-level columns only -- used for list_distinct so we dedupe at the PO/PR/document
# grain rather than the line-item grain (line-item columns like item no/qty/price are excluded).
DOC_LEVEL_COLUMNS = {
    "ME2L": ["PO_Number", "PO_Document_Date", "PO_Plant", "Plant_Description", "Name_of_Supplier",
             "PO_Department_Name"],
    "PO_release": ["POR_PO", "POR_Plant_Code", "PO_Release_Status", "Created_By_Name", "PO_Created_On"],
    "PR_release": ["PR_Number", "PR_Plant", "PR_Department", "PR_Release_Status", "PR_Created_On"],
    "Vendor_PO_History": ["VH_PO_No", "VH_Plant", "VH_Vendor_Name", "VH_PO_Date", "VH_Department"],
    "Sap_Purchase": ["SAP_Purchase_Requisition", "Requisitioner", "PR_Department", "PR_Plant",
                     "PR_Processing_Status", "Requisition_date"],
    "PR2PO": ["P2P_PR_No", "P2P_PO_No", "P2P_Vendor_Name", "P2P_Department", "P2P_Created_On"],
    "GateEntry": ["GTENTRY_Gate_Entry_Number", "GTENTRY_Gate_Entry_Date", "GTENTRY_Gate_Entry_Status",
                  "GTENTRY_Supplier_Name"],
    "Material_Doc_List": ["MTLST_Material_Document", "MTLST_Purchase_Order", "MTLST_Posting_Date",
                          "MTLST_Plant", "MTLST_Supplier"],
    "SES": ["Service_Entry_Sheet", "SES_Date_of_Creation", "SES_Release_Status", "SES_Name_of_Person"],
}

# Default document-identity column per report, used for COUNT(DISTINCT ...) when the
# caller didn't specify distinct_key, and as the default group-by tie-break count column.
DEFAULT_DOC_KEY = {
    "ME2L": "po_number", "PO_release": "po_number", "PR2PO": "po_number",
    "Vendor_PO_History": "po_number", "PR_release": "pr_number", "Sap_Purchase": "pr_number",
    "GateEntry": "gate_entry_no", "Material_Doc_List": "po_number", "SES": "service_entry_sheet",
}


def _quote(value) -> str:
    """Escape a value for safe inclusion in a single-quoted SQL literal."""
    s = str(value).replace("'", "''")
    return s


def _qualified_table() -> str:
    return f'{settings.PRESTO_CATALOG}."{settings.PRESTO_SCHEMA}".{sch.TABLE_NAME}'


def _filter_values(value) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        raw_parts = value
    else:
        raw_parts = re.split(r"\s*(?:,|/|\bor\b|\band\b)\s*", str(value), flags=re.IGNORECASE)
    values = []
    for part in raw_parts:
        cleaned = str(part).strip().strip("'\"")
        if cleaned:
            values.append(cleaned)
    return values


def _filter_condition(col: str, key: str, value) -> Optional[str]:
    values = _filter_values(value)
    if not values:
        return None
    if key in FUZZY_MATCH_KEYS:
        clauses = [
            f"lower(CAST({col} AS varchar)) LIKE '%{_quote(v.lower())}%'"
            for v in values
        ]
        return clauses[0] if len(clauses) == 1 else f"({' OR '.join(clauses)})"

    quoted_values = ", ".join(f"'{_quote(v.lower())}'" for v in values)
    if len(values) == 1:
        return f"lower(CAST({col} AS varchar)) = {quoted_values}"
    return f"lower(CAST({col} AS varchar)) IN ({quoted_values})"


_PR_REJECTED_SYNONYMS = {"rejected", "reject", "not approved", "not accepted"}
_PR_APPROVED_SYNONYMS = {"approved", "approve", "accepted", "not rejected"}


def _pr_release_status_override(values: List[str]) -> Optional[str]:
    """PR_release's 'release_status' filter key has historically pointed at the
    PR_Release_Status column, but rejection/approval state actually lives on
    PR_Rejected ('yes'/'no') -- PR_Release_Status doesn't reliably carry a
    'Rejected'/'Approved' value. When the filter value is a rejected/approved
    synonym, redirect to PR_Rejected so we don't filter on a column that never
    holds that value."""
    normalized = [v.strip().lower() for v in values]
    if any(v in _PR_REJECTED_SYNONYMS for v in normalized):
        return "lower(CAST(PR_Rejected AS varchar)) = 'yes'"
    if any(v in _PR_APPROVED_SYNONYMS for v in normalized):
        return "lower(CAST(PR_Rejected AS varchar)) = 'no'"
    return None


def _resolve_filter_column(report: str, key: str) -> Optional[str]:
    key = GROUP_BY_KEY_ALIASES.get(str(key).strip().lower(), key)
    col = sch.key_column(report, key)
    if col:
        return col
    # fall back to a common column with the same name if it happens to exist there
    if key in ("po_number",) and "PO_Number" in sch.COMMON_COLUMNS:
        return "PO_Number"
    if key in ("pr_number",) and "PR_Number" in sch.COMMON_COLUMNS:
        return "PR_Number"
    if key in ("plant",) and "PO_Plant" in sch.COMMON_COLUMNS:
        return "PO_Plant"
    if key in ("company_code",) and "Company_Code" in sch.COMMON_COLUMNS:
        return "Company_Code"
    return None


def _resolve_aggregate_column(report: str, key: str) -> Optional[str]:
    if not key:
        return None
    normalized_key = str(key).strip()
    alias = AGGREGATE_COLUMN_ALIASES.get(report, {}).get(normalized_key.lower())
    if alias:
        return alias
    resolved = _resolve_filter_column(report, normalized_key)
    if resolved:
        return resolved
    if normalized_key in sch.all_columns_for_report(report):
        return normalized_key
    return None


class ResolvedDateRange(NamedTuple):
    """Outcome of working out which window a question should be filtered to."""
    range: Optional[Tuple[date, date]]
    label: Optional[str]
    defaulted: bool  # True when the user named no period and we supplied one


def resolve_intent_date_range(intent: ExtractedIntent) -> ResolvedDateRange:
    """Single source of truth for a question's date window, shared by the generic
    and KPI SQL paths.

    Precedence:
      1. Whatever the user actually said (intent.date_phrase).
      2. For trend questions only, the current financial year -- "month over month
         how many PRs were created" names a grain but no window, and an unbounded
         trend is both slow and not what was asked. Year-grain trends widen to the
         last few FYs, since one FY is a single bucket and can't show a YoY change.
      3. Otherwise no date filter at all.
    """
    date_phrase = intent.date_phrase
    # Treat serialized null-like strings from the LLM as no date filter.
    if isinstance(date_phrase, str) and date_phrase.strip().lower() in {"null", "none", "n/a", "na"}:
        date_phrase = None

    if date_phrase:
        period = resolve_date_period(date_phrase)
        if period:
            return ResolvedDateRange(period.as_tuple(), period.label, False)
        return ResolvedDateRange(None, None, False)

    if intent.operation == "trend" and settings.TREND_DEFAULT_TO_CURRENT_FY:
        if (intent.time_grain or "").lower() == "year":
            default = recent_fy_span(settings.TREND_DEFAULT_FY_SPAN_YEAR_GRAIN)
        else:
            default = current_fy_period()
        return ResolvedDateRange(default.as_tuple(), default.label, True)

    return ResolvedDateRange(None, None, False)


def build_where_clause(
    intent: ExtractedIntent,
    date_range: Optional[Tuple[date, date]] = None,
) -> Tuple[List[str], Optional[Tuple[date, date]]]:
    """date_range=None means 'work it out from the intent' (see
    resolve_intent_date_range); pass one explicitly to reuse an already-resolved window."""
    conditions: List[str] = []
    for key, value in (intent.filters or {}).items():
        values = _filter_values(value)
        if not values:
            continue
        if intent.report == "PR_release" and key == "release_status":
            override = _pr_release_status_override(values)
            if override:
                conditions.append(override)
                continue
        col = _resolve_filter_column(intent.report, key)
        if not col:
            continue  # this filter concept doesn't apply to the chosen report; skip silently
        condition = _filter_condition(col, key, values)
        if condition:
            conditions.append(condition)

    resolved_range = date_range if date_range is not None else resolve_intent_date_range(intent).range
    if resolved_range:
        date_col = sch.date_filter_column(intent.report)
        start, end = resolved_range
        conditions.append(
            f"TRY(date_parse(CAST({date_col} AS VARCHAR), '%Y%m%d')) BETWEEN DATE '{start.isoformat()}' "
            f"AND DATE '{end.isoformat()}'"
        )
    return conditions, resolved_range


def _build_trend_sql(from_sql: str, period_expr: str, metric_expr: str, metric_alias: str) -> str:
    """Per-period series + the previous period's value and the change against it.

    A "month over month" question is a comparison, not just a monthly breakdown, so
    the per-period aggregate goes in a CTE and LAG() carries the prior period onto each
    row. Rows whose date column doesn't parse land in a NULL period and are dropped --
    the WHERE runs before the window functions, so they can't shift the comparison.
    """
    indented_from = "\n".join("  " + line for line in from_sql.splitlines())
    prev = f"LAG({metric_alias}) OVER (ORDER BY period)"
    return (
        "WITH periods AS (\n"
        f"  SELECT {period_expr} AS period,\n"
        f"         {metric_expr} AS {metric_alias}\n"
        f"{indented_from}\n"
        f"  GROUP BY {period_expr}\n"
        ")\n"
        "SELECT period,\n"
        f"       {metric_alias},\n"
        f"       {prev} AS prev_{metric_alias},\n"
        f"       {metric_alias} - {prev} AS change_vs_prev,\n"
        f"       CASE WHEN {prev} > 0\n"
        f"            THEN ROUND(100.0 * ({metric_alias} - {prev}) / {prev}, 2)\n"
        "       END AS pct_change_vs_prev\n"
        "FROM periods\n"
        "WHERE period IS NOT NULL\n"
        "ORDER BY period"
    )


def build_sql(
    intent: ExtractedIntent,
    date_range: Optional[Tuple[date, date]] = None,
    extra_group_columns: Optional[List[str]] = None,
) -> Tuple[str, Optional[Tuple[date, date]]]:
    if intent.report not in sch.REPORT_NAMES:
        raise ValueError(f"Unknown report: {intent.report}")
    table = _qualified_table()
    conditions, resolved_range = build_where_clause(intent, date_range)
    where_sql = f"\nWHERE {' AND '.join(conditions)}" if conditions else ""
    op = intent.operation

    if op == "count":
        sql = f"SELECT COUNT(*) AS record_count\nFROM {table}{where_sql}"

    elif op == "count_distinct":
        distinct_semkey = intent.distinct_key or DEFAULT_DOC_KEY.get(intent.report, "po_number")
        distinct_col = _resolve_filter_column(intent.report, distinct_semkey) or "PO_Number"
        sql = f"SELECT COUNT(DISTINCT {distinct_col}) AS distinct_count\nFROM {table}{where_sql}"

    elif op == "list_distinct":
        cols = DOC_LEVEL_COLUMNS.get(intent.report, DISPLAY_COLUMNS[intent.report])
        col_list = ",\n       ".join(cols)
        date_col = sch.date_filter_column(intent.report)
        limit = intent.limit or settings.MAX_ROWS
        sql = (f"SELECT DISTINCT {col_list}\nFROM {table}{where_sql}\n"
               f"ORDER BY {date_col} DESC\nLIMIT {limit}")

    elif op == "list":
        cols = DISPLAY_COLUMNS.get(intent.report, sch.all_columns_for_report(intent.report))
        col_list = ",\n       ".join(cols)
        date_col = sch.date_filter_column(intent.report)
        limit = intent.limit or settings.MAX_ROWS
        sql = (f"SELECT {col_list}\nFROM {table}{where_sql}\n"
               f"ORDER BY {date_col} DESC\nLIMIT {limit}")

    elif op == "group_by_count":
        if not intent.group_by_column:
            raise ValueError("group_by_count requires group_by_column")
        group_col = _resolve_filter_column(intent.report, intent.group_by_column)
        if not group_col:
            raise ValueError(
                f"'{intent.group_by_column}' is not a recognized grouping dimension for "
                f"report '{intent.report}'"
            )
        distinct_semkey = intent.distinct_key or DEFAULT_DOC_KEY.get(intent.report, "po_number")
        distinct_col = _resolve_filter_column(intent.report, distinct_semkey) or "PO_Number"
        sql = _grouped_count_sql(
            f"FROM {table}{where_sql}",
            f"COUNT(DISTINCT {distinct_col})",
            "record_count",
            group_col,
            extra_group_columns,
        )

    elif op == "aggregate":
        if not intent.aggregate_function or not intent.aggregate_column:
            raise ValueError("aggregate requires aggregate_function and aggregate_column")
        agg_col = _resolve_aggregate_column(intent.report, intent.aggregate_column)
        if not agg_col:
            raise ValueError(
                f"'{intent.aggregate_column}' is not a recognized aggregate column for "
                f"report '{intent.report}'"
            )
        func = intent.aggregate_function.upper()
        if func not in ("SUM", "AVG", "MIN", "MAX"):
            raise ValueError(f"Unsupported aggregate function: {func}")
        if intent.group_by_column:
            group_col = _resolve_filter_column(intent.report, intent.group_by_column)
            if not group_col:
                raise ValueError(
                    f"'{intent.group_by_column}' is not a recognized grouping dimension for "
                    f"report '{intent.report}'"
                )
            group_cols = _combined_group_columns(group_col, extra_group_columns)
            select_cols = ", ".join(
                f"{col} AS group_value_{idx}" for idx, col in enumerate(group_cols, start=1)
            )
            group_by = ", ".join(group_cols)
            sql = (f"SELECT {select_cols}, {func}({agg_col}) AS result\n"
                   f"FROM {table}{where_sql}\n"
                   f"GROUP BY {group_by}\nORDER BY result DESC")
        else:
            sql = f"SELECT {func}({agg_col}) AS result\nFROM {table}{where_sql}"

    elif op == "trend":
        if not intent.time_grain:
            raise ValueError("trend requires time_grain")
        date_col = sch.date_filter_column(intent.report)
        trunc_expr = time_grain_trunc_expr(date_col, intent.time_grain)
        # A trend can measure either a document count or an aggregate over a value
        # column ("mom total PO value") -- keep whichever the intent asked for.
        if intent.aggregate_function and intent.aggregate_column:
            func = intent.aggregate_function.upper()
            if func not in ("SUM", "AVG", "MIN", "MAX"):
                raise ValueError(f"Unsupported aggregate function: {func}")
            agg_col = _resolve_aggregate_column(intent.report, intent.aggregate_column)
            if not agg_col:
                raise ValueError(
                    f"'{intent.aggregate_column}' is not a recognized aggregate column for "
                    f"report '{intent.report}'"
                )
            metric_expr, metric_alias = f"{func}({agg_col})", "metric_value"
        else:
            distinct_semkey = intent.distinct_key or DEFAULT_DOC_KEY.get(intent.report, "po_number")
            distinct_col = _resolve_filter_column(intent.report, distinct_semkey) or "PO_Number"
            metric_expr, metric_alias = f"COUNT(DISTINCT {distinct_col})", "record_count"
        sql = _build_trend_sql(f"FROM {table}{where_sql}", trunc_expr, metric_expr, metric_alias)

    else:
        raise ValueError(f"Unsupported operation: {op}")

    return sql, resolved_range


# =============================================================================
# Fixed purchase-KPI catalog
#
# The generic ExtractedIntent/filters path above only supports single-column
# equality or fuzzy-match conditions. Several of the KPIs below need composite
# conditions (e.g. "PO_Release_Status = RELEASED AND PO_Number IS NULL") or
# derived date-diffs across multiple columns that path can't express, so they're
# hand-written SQL templates here instead, reusing the same _qualified_table()/
# _quote() helpers as the rest of this file. Wired up in main.py's /kpi/{kpi_id}
# endpoint; nothing above this line was changed.
#
# GAPS (flagged, not guessed around):
#   - "SES Approval Cycle" has no implementation. schema.py's SES report has no
#     "days to approve" column (only SES_Date_of_Creation, SES_Release_Status,
#     SES_Release_Level, SES_Passing_Levels, SES_Hod_Id/Name). See
#     UNSUPPORTED_KPIS below -- needs the exact source column confirmed first.
#   - "PR Pending for PO" and "Material/PO Delay" both reference PO_Number
#     across report types. Valid per COMMON_COLUMNS, but relies on the unified
#     view populating PO_Number consistently outside its "home" report --
#     worth a spot-check against real data.
#   - "Material/PO Delay" spec says GRN_Date - Delivery_Date, but there's no
#     column literally named Delivery_Date -- closest match is
#     Vendor_Delivery_Date (Material_Doc_List), used below.
# =============================================================================

_KPI_ME2L_DIMS = {"plant": "PO_Plant", "department": "PO_Department_Name",
                  "vendor_name": "Name_of_Supplier", "po_number": "PO_Number",
                  "pr_number": "PR_Number", "material_code": "PO_Material"}
_KPI_PR_RELEASE_DIMS = {"plant": "PR_Plant", "department": "PR_Department", "pr_number": "PR_Number","rejected_at_level": "Rejected_At_Level", "release_status": "PR_Release_Status", "current_level": "PR_Current_Release_Level", "hod_name": "HOD_Name", "cfo_name": "CFO_Name", "md_name": "MD_Name", "process_owner_name": "Process_Owner_Name", "vc_chairman_name": "VC_Chairman_Name"}
_KPI_PO_RELEASE_DIMS = {"plant": "POR_Plant_Code", "po_number": "POR_PO","po_deletion_indicator": "POR_Deletion_Indicator", "release_status": "PO_Release_Status","po_approval_days": "PO_No_Of_Days_Approval","po_no_of_releases_required": "PO_No_Of_Releases_Required"}
_KPI_MATDOC_DIMS = {"plant": "MTLST_Plant", "po_number": "MTLST_Purchase_Order",
                    "vendor_name": "MTLST_Supplier", "material_code": "MTLST_Material"}
_KPI_GATEENTRY_DIMS = {"vendor_name": "GTENTRY_Supplier_Name", "material_code": "GTENTRY_Material"}
_KPI_SAP_PURCHASE_DIMS = {"plant": "PR_Plant", "department": "PR_Department", "pr_number": "SAP_Purchase_Requisition","requisitioner": "Requisitioner", "company_code": "Company_Code","pr_deletion_indicator": "PR_Deletion_Indicator", "pr_processing_status": "PR_Processing_Status", "created_by": "PR_Created_By", "purchasing_group": "SAP_Purchasing_Group", "creation_indicator": "SAP_Creation_Indicator"}

_KPI_COUNT_COLUMNS = {
    "pr-created": "PR_Number",
    "pr-approval-cycle-time": "PR_Number",
    "pr-release-status": "PR_Number",
    "pr-pending-for-po": "PR_Number",
    "pr-rejection": "PR_Number",
    "pr-po-details": "PR_Number",
    "po-approval-cycle-time": "POR_PO",
    "po-approval-delay-level-wise": "POR_PO",
    "po-pending": "PO_Number",
    "po-release": "PO_Number",
    "po-status-grn": "MTLST_Purchase_Order",
    "material-po-delay": "PO_Number",
    "vendor-wise-po": "PO_Number",
    "material-wise-vendor": "Name_of_Supplier",
    "delay-in-grn": "MTLST_Purchase_Order",
    "gate-entry-daily": "GTENTRY_Gate_Entry_Number",
    "gate-entry-with-po": "GTENTRY_Gate_Entry_Number",
    "gate-entry-without-po": "GTENTRY_Gate_Entry_Number",
}

# A KPI's "home" report in KPI_REGISTRY is not always the report whose date column its
# builder function filters on -- e.g. po-pending is registered under Vendor_PO_History but
# its rows are filtered on Material_Doc_List's MTLST_Posting_Date. Trend queries must bucket
# by the SAME column they filter on, or the periods don't line up with the window.
# Keep this in sync with the date_filter_column(...) call inside each builder fn below.
_KPI_DATE_REPORT = {
    "pr-release-status": "PR_release",
    "pr-pending-for-po": "PR_release",
    "po-pending": "Material_Doc_List",
    "po-release": "Material_Doc_List",
    "po-status-grn": "Material_Doc_List",
    "material-po-delay": "Material_Doc_List",
    "delay-in-grn": "Material_Doc_List",
}

_KPI_DIMENSION_COLUMNS = {
    "pr-approval-cycle-time": _KPI_PR_RELEASE_DIMS,
    "pr-release-status": _KPI_PR_RELEASE_DIMS,
    "pr-pending-for-po": _KPI_PR_RELEASE_DIMS,
    "pr-rejection": _KPI_PR_RELEASE_DIMS,
    "pr-created": _KPI_SAP_PURCHASE_DIMS,
    "pr-po-details": _KPI_ME2L_DIMS,
    "po-approval-cycle-time": _KPI_PO_RELEASE_DIMS,
    "po-approval-delay-level-wise": _KPI_PO_RELEASE_DIMS,
    "po-pending": _KPI_MATDOC_DIMS,
    "po-release": _KPI_MATDOC_DIMS,
    "po-status-grn": _KPI_MATDOC_DIMS,
    "material-po-delay": _KPI_MATDOC_DIMS,
    "vendor-wise-po": _KPI_ME2L_DIMS,
    "material-wise-vendor": _KPI_ME2L_DIMS,
    "delay-in-grn": _KPI_MATDOC_DIMS,
    "gate-entry-daily": _KPI_GATEENTRY_DIMS,
    "gate-entry-with-po": _KPI_GATEENTRY_DIMS,
    "gate-entry-without-po": _KPI_GATEENTRY_DIMS,
}

_KPI_DISTINCT_KEY_COLUMNS = {
    "pr_number": "PR_Number",
    "po_number": "PO_Number",
    "gate_entry_no": "GTENTRY_Gate_Entry_Number",
    "service_entry_sheet": "Service_Entry_Sheet",
}


def _kpi_date_condition(date_col: str, date_range: Optional[Tuple[date, date]]) -> Optional[str]:
    if not date_range:
        return None
    start, end = date_range
    return (f"TRY(date_parse(CAST({date_col} AS VARCHAR), '%Y%m%d')) BETWEEN "
            f"DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'")


def _kpi_dim_conditions(filters: dict, colmap: dict) -> List[str]:
    conds = []
    for key, value in (filters or {}).items():
        values = _filter_values(value)
        if not values:
            continue
        if colmap is _KPI_PR_RELEASE_DIMS and key == "release_status":
            override = _pr_release_status_override(values)
            if override:
                conds.append(override)
                continue
        col = colmap.get(key)
        if not col:
            continue
        condition = _filter_condition(col, key, values)
        if condition:
            conds.append(condition)
    return conds


def _kpi_where(conds: List[str]) -> str:
    return f"\nWHERE {' AND '.join(conds)}" if conds else ""


def kpi_pr_approval_cycle_time(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_PR_RELEASE_DIMS)
    dc = _kpi_date_condition(sch.date_filter_column("PR_release"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT PR_Number, PR_Department, PR_Plant, PR_Created_On,\n"
            f"       PR_L1_Released_On, PR_L2_Released_On, PR_L3_Released_On, PR_L4_Released_On,\n"
            f"       PR_no_of_days_approval AS pr_approval_days\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
            f"ORDER BY PR_Created_On DESC\nLIMIT 500")


def kpi_pr_release_status(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_PR_RELEASE_DIMS)
    dc = _kpi_date_condition(sch.date_filter_column("PR_release"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT PR_Release_Status, COUNT(DISTINCT PR_Number) AS pr_count\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
            f"GROUP BY PR_Release_Status")


def kpi_pr_pending_for_po(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_PR_RELEASE_DIMS)
    conds.append("upper(CAST(PR_Release_Status AS varchar)) = 'RELEASED'")
    conds.append("(PO_Number IS NULL OR CAST(PO_Number AS varchar) = '')")
    dc = _kpi_date_condition(sch.date_filter_column("PR_release"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT PR_Number, PR_Department, PR_Plant, PR_Release_Status, PO_Number\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
            f"ORDER BY PR_Created_On DESC\nLIMIT 500")


def kpi_pr_rejection(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_PR_RELEASE_DIMS)
    conds.append("lower(CAST(PR_Rejected AS varchar)) = 'yes'")
    dc = _kpi_date_condition(sch.date_filter_column("PR_release"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT PR_Number, PR_Department, PR_Plant, Rejected_At_Level, "
            f"PR_Rejection_Text, PR_Created_On\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
            f"ORDER BY PR_Created_On DESC\nLIMIT 500")


def kpi_pr_po_details(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_ME2L_DIMS)
    dc = _kpi_date_condition(sch.date_filter_column("ME2L"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT PR_Number, PO_Number, Name_of_Supplier\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
            f"ORDER BY PO_Document_Date DESC\nLIMIT 500")


def kpi_po_approval_cycle_time(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_PO_RELEASE_DIMS)
    dc = _kpi_date_condition(sch.date_filter_column("PO_release"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT POR_PO, POR_Plant_Code, PO_Created_On,\n"
            f"       PO_No_Of_Days_Approval AS po_approval_days\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
            f"ORDER BY PO_Created_On DESC\nLIMIT 500")


_KPI_LEVEL_COLS = ["PO_Created_On", "PO_L1_Released_On", "PO_L2_Released_On",
                   "PO_L3_Released_On", "PO_L4_Released_On", "PO_L5_Released_On"]


def kpi_po_approval_delay_level_wise(filters, date_range):
    # Single-scan, unpivoted format: one row per approval level with the days
    # that level took (previous level's release date -> this level's release
    # date). Previously this UNION ALL'd 5 separate SELECTs, each re-scanning
    # purchase_unified_testing_new -- against the real (view-backed) table that
    # multiplies into 161 Presto stages and fails with QUERY_HAS_TOO_MANY_STAGES.
    # CROSS JOIN UNNEST explodes the 5 levels per row from a single table scan
    # instead, so the PO filter and the table scan each happen exactly once.
    #
    # The base table is line-item grain (multiple rows per PO, per
    # DOC_LEVEL_COLUMNS["PO_release"]/DEFAULT_DOC_KEY["PO_release"] == POR_PO
    # being the PO's document identity, distinct from the line-item rows it
    # repeats across). Exploding 5 levels straight off those line-item rows
    # multiplies into row_count-per-PO x 5 duplicate level rows. So the base
    # table is first collapsed to exactly one row per POR_PO -- GROUP BY on
    # that same document-identity key, MAX() over the approval-date columns
    # since those are header-level values repeated identically across a PO's
    # line items (MAX also just passes through the single real value and
    # ignores NULLs from any partially-joined line-item rows) -- and only
    # that deduplicated one-row-per-PO result is fed into CROSS JOIN UNNEST.
    conds = _kpi_dim_conditions(filters, _KPI_PO_RELEASE_DIMS)
    dc = _kpi_date_condition(sch.date_filter_column("PO_release"), date_range)
    if dc:
        conds.append(dc)
    where_clause = _kpi_where(conds)

    start_cols = _KPI_LEVEL_COLS[0:5]   # PO_Created_On, PO_L1..L4_Released_On
    end_cols = _KPI_LEVEL_COLS[1:6]     # PO_L1..L5_Released_On
    start_array = ", ".join(start_cols)
    end_array = ", ".join(end_cols)
    level_cols_agg = ",\n".join(f"           MAX({col}) AS {col}" for col in _KPI_LEVEL_COLS)

    return (
        "WITH po_header AS (\n"
        "    SELECT POR_PO,\n"
        f"{level_cols_agg}\n"
        f"    FROM {_qualified_table()}\n"
        f"    {where_clause}\n"
        "    GROUP BY POR_PO\n"
        "),\n"
        "level_delays AS (\n"
        "    SELECT POR_PO, current_level,\n"
        "           date_diff('day', TRY(date_parse(CAST(start_raw AS VARCHAR), '%Y%m%d')), "
        "TRY(date_parse(CAST(end_raw AS VARCHAR), '%Y%m%d'))) AS approval_days\n"
        "    FROM po_header\n"
        "    CROSS JOIN UNNEST(\n"
        "        ARRAY[1, 2, 3, 4, 5],\n"
        f"        ARRAY[{start_array}],\n"
        f"        ARRAY[{end_array}]\n"
        "    ) AS t(current_level, start_raw, end_raw)\n"
        ")\n"
        "SELECT POR_PO, current_level, approval_days\n"
        "FROM level_delays\n"
        "WHERE approval_days IS NOT NULL\n"
        "ORDER BY POR_PO, current_level\nLIMIT 500"
    )


def kpi_po_pending(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_MATDOC_DIMS)
    conds.append("PO_Number IS NOT NULL")
    conds.append("(Mat_Doc IS NULL OR CAST(Mat_Doc AS varchar) = '')")
    dc = _kpi_date_condition(sch.date_filter_column("Material_Doc_List"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT PO_Number, MTLST_Purchase_Order, MTLST_Plant, MTLST_Supplier, Mat_Doc\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
            f"ORDER BY MTLST_Posting_Date DESC\nLIMIT 500")

def kpi_po_release(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_MATDOC_DIMS)
    conds.append("PO_Number IS NOT NULL")
    conds.append("(Mat_Doc IS NOT NULL AND CAST(Mat_Doc AS varchar) != '')")
    dc = _kpi_date_condition(sch.date_filter_column("Material_Doc_List"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT PO_Number, MTLST_Purchase_Order, MTLST_Plant, MTLST_Supplier, Mat_Doc\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
            f"ORDER BY MTLST_Posting_Date DESC\nLIMIT 500")


def kpi_po_status_grn(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_MATDOC_DIMS)
    dc = _kpi_date_condition(sch.date_filter_column("Material_Doc_List"), date_range)
    if dc:
        conds.append(dc)
    return (
        "SELECT MTLST_Purchase_Order, Mat_Doc,\n"
        "       CASE WHEN Mat_Doc IS NOT NULL AND CAST(Mat_Doc AS varchar) != '' "
        "THEN 'GRN Created' ELSE 'GRN Not Created' END AS grn_status\n"
        f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
        f"ORDER BY MTLST_Posting_Date DESC\nLIMIT 500"
    )


def kpi_material_po_delay(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_MATDOC_DIMS)
    dc = _kpi_date_condition(sch.date_filter_column("Material_Doc_List"), date_range)
    if dc:
        conds.append(dc)
    return (
        "SELECT PO_Number, MTLST_Purchase_Order, Mat_Doc,\n"
        "       CASE WHEN Mat_Doc IS NULL OR CAST(Mat_Doc AS varchar) = '' "
        "THEN true ELSE false END AS is_pending,\n"
        "       GRN_Date, Vendor_Delivery_Date,\n"
        "       date_diff('day', TRY(date_parse(CAST(Vendor_Delivery_Date AS VARCHAR), '%Y%m%d')), "
        "TRY(date_parse(CAST(GRN_Date AS VARCHAR), '%Y%m%d'))) AS delay_days\n"
        f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
        f"ORDER BY MTLST_Posting_Date DESC\nLIMIT 500"
    )


def kpi_vendor_wise_po(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_ME2L_DIMS)
    dc = _kpi_date_condition(sch.date_filter_column("ME2L"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT Name_of_Supplier, PO_Number\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
            f"ORDER BY Name_of_Supplier\nLIMIT 500")


def kpi_material_wise_vendor(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_ME2L_DIMS)
    dc = _kpi_date_condition(sch.date_filter_column("ME2L"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT PO_Material_Description, Name_of_Supplier\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
            f"ORDER BY PO_Material_Description\nLIMIT 500")


def kpi_delay_in_grn(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_MATDOC_DIMS)
    dc = _kpi_date_condition(sch.date_filter_column("Material_Doc_List"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT MTLST_Purchase_Order, MTLST_Supplier, Material_Delay_Days\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
            f"ORDER BY Material_Delay_Days DESC\nLIMIT 500")


def kpi_gate_entry_daily(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_GATEENTRY_DIMS)
    dc = _kpi_date_condition(sch.date_filter_column("GateEntry"), date_range)
    if dc:
        conds.append(dc)
    return (
        "SELECT TRY(date_parse(CAST(GTENTRY_Gate_Entry_Date AS VARCHAR), '%Y%m%d')) AS entry_date,\n"
        "       COUNT(GTENTRY_Gate_Entry_Number) AS gate_entry_count\n"
        f"FROM {_qualified_table()}{_kpi_where(conds)}\n"
        "GROUP BY TRY(date_parse(CAST(GTENTRY_Gate_Entry_Date AS VARCHAR), '%Y%m%d'))\n"
        "ORDER BY entry_date DESC"
    )


def kpi_gate_entry_with_po(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_GATEENTRY_DIMS)
    conds.append("PO_Number IS NOT NULL")
    dc = _kpi_date_condition(sch.date_filter_column("GateEntry"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT COUNT(GTENTRY_Gate_Entry_Number) AS gate_entry_count\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}")

def kpi_pr_created(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_SAP_PURCHASE_DIMS)
    # Deletion_Indicator is '' (blank) for a live PR and 'X' for a deleted one --
    # it is never SQL NULL, but the IS NULL check is kept as a defensive fallback.
    # Parenthesized so it doesn't get swallowed by the surrounding AND-joined conds
    # (unparenthesized "A AND B OR C AND D" parses as "(A AND B) OR (C AND D)").
    conds.append("(PR_Deletion_Indicator IS NULL OR CAST(PR_Deletion_Indicator AS varchar) != 'X')")
    dc = _kpi_date_condition(sch.date_filter_column("Sap_Purchase"), date_range)
    if dc:
        conds.append(dc)
    # SAP purchase data is item-level: a single PR can have multiple line items,
    # so COUNT(PR_Number) would over-count. COUNT(DISTINCT ...) counts PRs, not rows.
    return (f"SELECT COUNT(DISTINCT PR_Number) AS pr_created_count\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}")

def kpi_gate_entry_without_po(filters, date_range):
    conds = _kpi_dim_conditions(filters, _KPI_GATEENTRY_DIMS)
    conds.append("PO_Number IS NULL")
    dc = _kpi_date_condition(sch.date_filter_column("GateEntry"), date_range)
    if dc:
        conds.append(dc)
    return (f"SELECT COUNT(GTENTRY_Gate_Entry_Number) AS gate_entry_count\n"
            f"FROM {_qualified_table()}{_kpi_where(conds)}")


# kpi_id -> (source report, builder fn, human description)
KPI_REGISTRY = {
    "pr-approval-cycle-time": ("PR_release", kpi_pr_approval_cycle_time,
                               "PR approval cycle time in days"),
    "pr-release-status": ("PR2PO", kpi_pr_release_status,
                          "RELEASED / UNRELEASED counts"),
    "pr-pending-for-po": ("PR2PO", kpi_pr_pending_for_po,
                          "Released PRs with no PO created yet"),
    "pr-rejection": ("PR_release", kpi_pr_rejection, "Rejected PRs"),
    "pr-created": ("Sap_Purchase", kpi_pr_created, "PRs created in the window"),
    "pr-po-details": ("ME2L", kpi_pr_po_details, "PR / PO / supplier detail rows"),
    "po-approval-cycle-time": ("PO_release", kpi_po_approval_cycle_time,
                               "PO approval cycle time in days"),
    "po-approval-delay-level-wise": ("PO_release", kpi_po_approval_delay_level_wise,
                                     "Days elapsed between each approval level"),
    "po-pending": ("Vendor_PO_History", kpi_po_pending, "POs with no material doc / GRN yet"),
    "po-release": ("Vendor_PO_History", kpi_po_release, "POs with  material doc / GRN created"),
    "po-status-grn": ("Vendor_PO_History", kpi_po_status_grn, "GRN created / not created"),
    "material-po-delay": ("Vendor_PO_History", kpi_material_po_delay,
                          "Pending flag + GRN vs delivery-date delay"),
    "vendor-wise-po": ("ME2L", kpi_vendor_wise_po, "PO numbers per vendor"),
    "material-wise-vendor": ("ME2L", kpi_material_wise_vendor, "Vendor per material"),
    "delay-in-grn": ("Vendor_PO_History", kpi_delay_in_grn, "Material_Delay_Days per PO"),
    "gate-entry-daily": ("GateEntry", kpi_gate_entry_daily, "Gate entry count per day"),
    "gate-entry-with-po": ("GateEntry", kpi_gate_entry_with_po, "Gate entries linked to a PO"),
    "gate-entry-without-po": ("GateEntry", kpi_gate_entry_without_po,
                             "Gate entries with no PO reference"),
}

# KPIs whose builder fn (above) already computes and returns the actual metric
# (approval duration in days) as detail rows -- unlike the other KPIs in
# KPI_REGISTRY, there is no separate "count" reading of these questions.
# _kpi_count_sql's count/count_distinct/group_by wrapping would discard the
# duration calculation entirely and substitute COUNT(DISTINCT POR_PO), so
# intent_extractor must force operation='list' for these regardless of
# "how many days" / "level wise" phrasing or an LLM-supplied group_by_column.
DURATION_DETAIL_KPIS = {"po-approval-cycle-time", "po-approval-delay-level-wise"}

# Flagged, not guessed around -- see module docstring above.
UNSUPPORTED_KPIS = {
    "ses-approval-cycle": (
        "SES report has no 'days to approve' column in schema.py -- only "
        "SES_Date_of_Creation, SES_Release_Status, SES_Release_Level, "
        "SES_Passing_Levels, SES_Hod_Id/Name are available. Confirm the exact "
        "source column before this can be wired up."
    ),
}


def _combined_group_columns(primary_group_col: Optional[str], extra_group_cols: Optional[List[str]] = None) -> List[str]:
    group_cols = []
    for col in [primary_group_col] + (extra_group_cols or []):
        if col and col not in group_cols:
            group_cols.append(col)
    return group_cols


def _grouped_count_sql(
    from_sql: str,
    select_expr: str,
    alias: str,
    primary_group_col: Optional[str],
    extra_group_cols: Optional[List[str]] = None,
) -> str:
    group_cols = _combined_group_columns(primary_group_col, extra_group_cols)
    if not group_cols:
        return f"SELECT {select_expr} AS {alias}\n{from_sql}"
    select_cols = ", ".join(f"{col} AS group_value_{idx}" for idx, col in enumerate(group_cols, start=1))
    group_by = ", ".join(group_cols)
    return (f"SELECT {select_cols}, {select_expr} AS {alias}\n"
            f"{from_sql}\n"
            f"GROUP BY {group_by}\nORDER BY {alias} DESC")


def _kpi_count_sql(
    kpi_id: str,
    detail_sql: str,
    operation: str,
    distinct_key: Optional[str],
    group_by_column: Optional[str] = None,
    extra_group_columns: Optional[List[str]] = None,
) -> str:
    if operation not in ("count", "count_distinct"):
        return detail_sql

    if operation == "count_distinct":
        count_col = _KPI_DISTINCT_KEY_COLUMNS.get(distinct_key or "")
        count_col = count_col or _KPI_COUNT_COLUMNS.get(kpi_id)
        if not count_col:
            raise ValueError(f"No count column configured for KPI '{kpi_id}'")
        select_expr = f"COUNT(DISTINCT {count_col})"
        alias = "distinct_count"
    else:
        select_expr = "COUNT(*)"
        alias = "record_count"

    from_sql = _kpi_from_sql(kpi_id, detail_sql)
    group_col = _kpi_group_column(kpi_id, group_by_column) if group_by_column else None
    return _grouped_count_sql(from_sql, select_expr, alias, group_col, extra_group_columns)


def _kpi_group_column(kpi_id: str, group_by_column: str) -> str:
    normalized_key = GROUP_BY_KEY_ALIASES.get(str(group_by_column).strip().lower(), group_by_column)
    colmap = _KPI_DIMENSION_COLUMNS.get(kpi_id, {})
    group_col = colmap.get(normalized_key)
    if not group_col:
        report = KPI_REGISTRY.get(kpi_id, (None,))[0]
        if report:
            group_col = _resolve_filter_column(report, normalized_key)
    if not group_col:
        raise ValueError(
            f"'{group_by_column}' is not a recognized grouping dimension for KPI '{kpi_id}'"
        )
    return group_col


def kpi_supports_group_by(kpi_id: str, group_by_column: str) -> bool:
    """Non-raising check: does this KPI have a real column for this grouping
    dimension? Used by intent_extractor to drop an unsupported group_by_column
    before it reaches _kpi_group_column's ValueError."""
    try:
        _kpi_group_column(kpi_id, group_by_column)
        return True
    except ValueError:
        return False


def _kpi_from_sql(kpi_id: str, detail_sql: str) -> str:
    from_index = detail_sql.upper().find("\nFROM ")
    if from_index == -1:
        raise ValueError(f"KPI '{kpi_id}' SQL cannot be converted")

    from_sql = detail_sql[from_index + 1:]
    upper_from = from_sql.upper()
    order_index = upper_from.find("\nORDER BY ")
    if order_index != -1:
        from_sql = from_sql[:order_index]
        upper_from = from_sql.upper()
    group_index = upper_from.find("\nGROUP BY ")
    if group_index != -1:
        from_sql = from_sql[:group_index]

    return from_sql


def _kpi_trend_sql(
    kpi_id: str,
    report: str,
    detail_sql: str,
    time_grain: Optional[str],
    distinct_key: Optional[str],
) -> str:
    if not time_grain:
        raise ValueError("trend requires time_grain")

    count_col = _KPI_DISTINCT_KEY_COLUMNS.get(distinct_key or "")
    count_col = count_col or _KPI_COUNT_COLUMNS.get(kpi_id)
    if not count_col:
        raise ValueError(f"No count column configured for KPI '{kpi_id}'")

    date_col = sch.date_filter_column(_KPI_DATE_REPORT.get(kpi_id, report))
    period_expr = time_grain_trunc_expr(date_col, time_grain)
    from_sql = _kpi_from_sql(kpi_id, detail_sql)

    return _build_trend_sql(from_sql, period_expr, f"COUNT(DISTINCT {count_col})", "record_count")


def build_kpi_sql(
    kpi_id: str,
    filters: dict,
    date_range: Optional[Tuple[date, date]],
    operation: str = "list",
    distinct_key: Optional[str] = None,
    time_grain: Optional[str] = None,
    group_by_column: Optional[str] = None,
    extra_group_columns: Optional[List[str]] = None,
) -> str:
    if kpi_id in UNSUPPORTED_KPIS:
        raise ValueError(UNSUPPORTED_KPIS[kpi_id])
    if kpi_id not in KPI_REGISTRY:
        raise ValueError(f"Unknown KPI '{kpi_id}'")
    report, builder_fn, _ = KPI_REGISTRY[kpi_id]
    detail_sql = builder_fn(filters, date_range)
    if operation == "trend":
        return _kpi_trend_sql(kpi_id, report, detail_sql, time_grain, distinct_key)
    return _kpi_count_sql(
        kpi_id,
        detail_sql,
        operation,
        distinct_key,
        group_by_column,
        extra_group_columns,
    )