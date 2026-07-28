"""
Builds Trino/Presto SQL from an ExtractedIntent, resolving:
  - semantic filter keys -> real column names (per report, via schema.KEY_COLUMNS)
  - date phrases -> concrete BETWEEN ranges (via date_resolver)
  - operation type -> SELECT/COUNT/GROUP BY/trend shape
"""
from __future__ import annotations
from typing import Optional, Tuple, List
from datetime import date

import schema as sch
from models import ExtractedIntent
from date_resolver import resolve as resolve_date, time_grain_trunc_expr
from config import settings

EXACT_MATCH_KEYS = {
    "po_number", "pr_number", "plant", "company_code", "release_status",
    "gate_entry_status", "rejected_at_level", "service_entry_sheet", "current_level",
}
FUZZY_MATCH_KEYS = {"vendor_name", "material_desc", "requisitioner"}

# Curated columns shown for row-level ("list") results -- the full 70+ column set per
# report is available via all_columns_for_report() for anyone building custom SELECTs,
# but a default listing should stay readable.
DISPLAY_COLUMNS = {
    "ME2L": ["PO_Number", "PO_Item", "PR_Number", "PR_Item", "PO_Document_Date", "PO_Plant",
             "Plant_Description", "PO_Material", "PO_Material_Description", "Name_of_Supplier",
             "Purchasing_Group_Description", "PO_Department_Name", "Order_Quantity",
             "PO_Net_Price", "Net_Order_Value", "Still_to_be_delivered_qty",
             "Still_to_be_invoiced_qty"],
    "PO_release": ["POR_PO", "POR_PO_Doc_Type", "POR_Plant_Code", "POR_Plant_Description",
                   "PO_Release_Status", "PO_Created_By", "Created_By_Name", "PO_Created_On",
                   "PO_No_Of_Days_Approval", "PO_No_Of_Releases_Required",
                   "POR_Current_Release_Level", "POR_Amount"],
    "PR_release": ["PR_Number", "PR_Plant", "PR_Department", "PR_Release_Status", "PR_Rejected",
                   "Rejected_At_Level", "PR_Current_Release_Level", "PR_Releases_Required",
                   "PR_no_of_days_approval", "PR_Created_On", "HOD_Name", "CFO_Name", "MD_Name"],
    "Vendor_PO_History": ["VH_PO_No", "VH_PR_No", "VH_Plant", "VH_Vendor_Name", "VH_Material_Desc",
                          "VH_PO_Date", "VH_PO_Qty", "VH_Net_Price", "VH_Department", "VH_Company"],
    "Sap_Purchase": ["SAP_Purchase_Requisition", "SAP_Purchase_Order", "Requisitioner",
                     "PR_Department", "PR_Plant", "Company_Code", "PR_Processing_Status",
                     "Requisition_date", "SAP_Name_Of_Supplier", "SAP_Short_Text", "SAP_Material"],
    "PR2PO": ["P2P_PR_No", "P2P_PO_No", "P2P_Vendor_Name", "P2P_Material_Description",
              "P2P_Department", "P2P_Plant", "PR_To_PO_Days", "P2P_Created_On",
              "PR_Release_Status", "P2P_PO_Rejection_Text"],
    "GateEntry": ["GTENTRY_Gate_Entry_Number", "GTENTRY_Gate_Entry_Date", "GTENTRY_Gate_Entry_Status",
                  "GTENTRY_Supplier_Name", "GTENTRY_Material", "GTENTRY_Vehicle_Number",
                  "GTENTRY_Po_qty", "GTENTRY_Received_qty", "GTENTRY_Net_Weight",
                  "GTENTRY_Gross_Weight"],
    "Material_Doc_List": ["MTLST_Material_Document", "MTLST_Purchase_Order", "MTLST_Material",
                          "MTLST_Posting_Date", "MTLST_Quantity", "MTLST_Plant", "MTLST_Supplier",
                          "MTLST_Movement_Type", "GRN_qty", "GRN_Amount", "Material_Delay_Days",
                          "GRN_Days"],
    "SES": ["Service_Entry_Sheet", "SES_Date_of_Creation", "SES_Release_Status",
            "SES_Release_Level", "SES_Name_of_Person", "SES_Amount", "SES_Passing_Levels"],
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


def _resolve_filter_column(report: str, key: str) -> Optional[str]:
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


def build_where_clause(intent: ExtractedIntent) -> Tuple[List[str], Optional[Tuple[date, date]]]:
    conditions: List[str] = []
    for key, value in (intent.filters or {}).items():
        if value in (None, ""):
            continue
        col = _resolve_filter_column(intent.report, key)
        if not col:
            continue  # this filter concept doesn't apply to the chosen report; skip silently
        if key in FUZZY_MATCH_KEYS:
            conditions.append(f"lower(CAST({col} AS varchar)) LIKE '%{_quote(str(value).lower())}%'")
        else:
            conditions.append(f"lower(CAST({col} AS varchar)) = '{_quote(value.lower())}'")

    resolved_range = None
    if intent.date_phrase:
        resolved_range = resolve_date(intent.date_phrase)
        if resolved_range:
            date_col = sch.date_filter_column(intent.report)
            start, end = resolved_range
            conditions.append(
                f"TRY(date_parse(CAST({date_col} AS VARCHAR), '%Y%m%d')) BETWEEN DATE '{start.isoformat()}' "
                f"AND DATE '{end.isoformat()}'"
            )
    return conditions, resolved_range


def build_sql(intent: ExtractedIntent) -> Tuple[str, Optional[Tuple[date, date]]]:
    if intent.report not in sch.REPORT_NAMES:
        raise ValueError(f"Unknown report: {intent.report}")

    table = _qualified_table()
    conditions, resolved_range = build_where_clause(intent)
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
        sql = (f"SELECT {group_col} AS group_value, COUNT(DISTINCT {distinct_col}) AS record_count\n"
               f"FROM {table}{where_sql}\n"
               f"GROUP BY {group_col}\nORDER BY record_count DESC")

    elif op == "aggregate":
        if not intent.aggregate_function or not intent.aggregate_column:
            raise ValueError("aggregate requires aggregate_function and aggregate_column")
        agg_col = _resolve_filter_column(intent.report, intent.aggregate_column) or intent.aggregate_column
        func = intent.aggregate_function.upper()
        if func not in ("SUM", "AVG", "MIN", "MAX"):
            raise ValueError(f"Unsupported aggregate function: {func}")
        sql = f"SELECT {func}({agg_col}) AS result\nFROM {table}{where_sql}"

    elif op == "trend":
        if not intent.time_grain:
            raise ValueError("trend requires time_grain")
        date_col = sch.date_filter_column(intent.report)
        trunc_expr = time_grain_trunc_expr(date_col, intent.time_grain)
        distinct_semkey = intent.distinct_key or DEFAULT_DOC_KEY.get(intent.report, "po_number")
        distinct_col = _resolve_filter_column(intent.report, distinct_semkey) or "PO_Number"
        sql = (f"SELECT {trunc_expr} AS period, COUNT(DISTINCT {distinct_col}) AS record_count\n"
               f"FROM {table}{where_sql}\n"
               f"GROUP BY {trunc_expr}\nORDER BY period")

    else:
        raise ValueError(f"Unsupported operation: {op}")

    return sql, resolved_range