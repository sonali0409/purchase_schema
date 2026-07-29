# """
# Uses the Anthropic API (tool-use / forced function-calling) to turn a natural-language
# question into a structured ExtractedIntent. Tool-use is used instead of free-text JSON
# parsing because it's schema-validated by the API itself, so we don't have to guard
# against the model wrapping output in prose or markdown fences.
# """
# from __future__ import annotations
# import json


# from config import settings
# from models import ExtractedIntent
# import schema as sch
# import llm_client


# _REPORT_DESCRIPTIONS = {
#     "ME2L": "PO line-item master list: PO/PR numbers, plant, material, vendor, quantities, "
#             "prices, department. Use for general 'PO details', 'materials supplied by vendor X', "
#             "'vendors for material Y', 'PO details for plant/date' questions.",
#     "PO_release": "PO approval workflow: release status, approver levels (L1-L5), release dates, "
#                   "days taken to approve, number of releases required. Use for 'released/unreleased/"
#                   "pending PO', 'days taken to approve PO', 'level wise approval'.",
#     "PR_release": "PR approval workflow: release status, rejection level, approver chain "
#                   "(HOD/CFO/MD/Process Owner/VC Chairman), days to approve. Use for 'released/"
#                   "unreleased/rejected/pending PR', 'PR approval cycle', 'rejected at level N'.",
#     "Vendor_PO_History": "Vendor + PO history combined view (VH_ prefix): PO/PR linkage, vendor, "
#                          "material, dates, quantities. Use for 'delay in material received', "
#                          "'delay in GRN', 'GRN details for PO', 'is GRN created for PO'.",
#     "Sap_Purchase": "Raw SAP purchase requisition data: requisitioner, PR processing status, "
#                     "release info, org/plant/department. Use for 'who raised PR X', 'requisitioner "
#                     "for PR', 'how many PR raised by <person>'.",
#     "PR2PO": "PR-to-PO conversion tracking (P2P_ prefix): links PR to resulting PO, PR-to-PO days, "
#              "GRN quantity/flag. Use for 'PR to PO details', 'PR pending for PO', 'PO generated "
#              "against PR X'.",
#     "GateEntry": "Gate entry / weighbridge records (GTENTRY_ prefix): vehicle, driver, challan, "
#                 "gate in/out times, linked PO/material. Use for 'gate entries with/without PO', "
#                 "'gate entry status for PO'.",
#     "Material_Doc_List": "Material document / GRN postings (MTLST_ prefix + GRN_*): goods receipt "
#                         "quantity/value, posting date, movement type. Use for 'material of PO X', "
#                         "'GRN date/quantity for material'.",
#     "SES": "Service Entry Sheet data: service entry approvals, release levels, amount. Use for "
#            "service-related PO/PR questions and SES release status.",
# }


# def _build_system_prompt() -> str:
#     parts = ["You convert a user's natural-language question about a SAP procurement dataset "
#              "into a structured query intent. The dataset is split into 9 reports (views), each "
#              "with its own date filter column:\n"]
#     for r in sch.REPORT_NAMES:
#         parts.append(f"- {r} (date filter: {sch.date_filter_column(r)}): {_REPORT_DESCRIPTIONS[r]}")
#     parts.append(
#         "\nRules:\n"
#         "- Pick exactly one report that best matches the question's intent.\n"
#         "- Only set date_phrase if the user actually mentioned a date/period. Many valid "
#         "questions have NO date filter (e.g. 'show me details for PO 5400010477') -- leave "
#         "date_phrase null in that case, do not invent one.\n"
#         "- Preserve the user's date phrase close to verbatim (e.g. 'last month', 'March 2021', "
#         "'last 3 years', 'April 2025 to March 2026', 'last fy', 'this quarter') -- do not resolve "
#         "it to actual dates yourself, a downstream deterministic resolver does that.\n"
#         "- For questions asking 'how many <X>' or 'count of <X>' about POs or PRs, prefer "
#         "operation=count_distinct with distinct_key set to 'po_number' or 'pr_number' as "
#         "appropriate -- SAP documents can have multiple line items, so a plain COUNT(*) over-counts.\n"
#         "- Use operation=list_distinct instead of 'list' when the user's phrasing implies "
#         "unique documents rather than every line item (e.g. 'unreleased PO details' listing "
#         "distinct POs) -- but if the user explicitly says something like 'display all POs, not "
#         "only distinct ones', use operation='list'.\n"
#         "- Use operation=group_by_count with group_by_column set for '<dimension> wise' or "
#         "'breakdown by <dimension>' questions (e.g. 'department wise count of POs' -> "
#         "group_by_column='department').\n"
#         "- Use operation=trend with time_grain set for 'month on month', 'quarter on quarter', "
#         "'year on year' questions.\n"
#         "- filters is a dict of semantic_key -> value using ONLY these semantic keys where "
#         "applicable: po_number, pr_number, plant, company_code, vendor_name, material_desc, "
#         "material_code, department, release_status, gate_entry_status, rejected_at_level, "
#         "requisitioner, service_entry_sheet, current_level. Only include keys the question "
#         "actually specifies.\n"
#         "- material_desc / vendor_name filters should carry the raw search text (e.g. 'air "
#         "cooler', 'Brilliance sales') -- these will be matched with case-insensitive partial "
#         "matching downstream, do not guess exact codes.\n"
#         "- If the question is genuinely too ambiguous to build a query (e.g. missing an "
#         "identifier the report requires), set clarification_needed to a short explanation "
#         "instead of guessing."
#     )
#     return "\n".join(parts)


# _TOOL_SCHEMA = {
#     "type": "function",
#     "function": {
#         "name": "extract_intent",
#         "description": "Return the structured query intent for the user's procurement question.",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "report": {
#                     "type": "string",
#                     "enum": [
#                         "GateEntry",
#                         "ME2L",
#                         "Material_Doc_List",
#                         "PO_release",
#                         "PR2PO",
#                         "PR_release",
#                         "SES",
#                         "Sap_Purchase",
#                         "Vendor_PO_History"
#                     ]
#                 },
#                 "operation": {
#                     "type": "string",
#                     "enum": [
#                         "list",
#                         "list_distinct",
#                         "count",
#                         "count_distinct",
#                         "group_by_count",
#                         "aggregate",
#                         "trend"
#                     ]
#                 },
#                 "aggregate_function": {
#                     "type": ["string", "null"],
#                     "enum": [
#                         "SUM",
#                         "AVG",
#                         "MIN",
#                         "MAX",
#                         None
#                     ]
#                 },
#                 "aggregate_column": {
#                     "type": ["string", "null"]
#                 },
#                 "group_by_column": {
#                     "type": ["string", "null"]
#                 },
#                 "time_grain": {
#                     "type": ["string", "null"],
#                     "enum": [
#                         "month",
#                         "quarter",
#                         "year",
#                         None
#                     ]
#                 },
#                 "distinct_key": {
#                     "type": ["string", "null"]
#                 },
#                 "filters": {
#                     "type": "object",
#                     "additionalProperties": {
#                         "type": "string"
#                     }
#                 },
#                 "date_phrase": {
#                     "type": ["string", "null"]
#                 },
#                 "limit": {
#                     "type": ["integer", "null"]
#                 },
#                 "clarification_needed": {
#                     "type": ["string", "null"]
#                 }
#             },
#             "required": [
#                 "report",
#                 "operation",
#                 "filters"
#             ]
#         }
#     }
# }

# _SYSTEM_PROMPT = _build_system_prompt()


# def extract_intent(question: str) -> ExtractedIntent:

#     response = llm_client.call_llm_json(_SYSTEM_PROMPT, question, _TOOL_SCHEMA)
#     return ExtractedIntent(**response)



# """
# Uses the Anthropic API (tool-use / forced function-calling) to turn a natural-language
# question into a structured ExtractedIntent. Tool-use is used instead of free-text JSON
# parsing because it's schema-validated by the API itself, so we don't have to guard
# against the model wrapping output in prose or markdown fences.
# """
# from __future__ import annotations
# import json
# import re
# from typing import Optional


# from config import settings
# from models import ExtractedIntent
# import schema as sch
# import llm_client
# import sql_builder as sqb


# _REPORT_DESCRIPTIONS = {
#     "ME2L": "PO line-item master list: PO/PR numbers, plant, material, vendor, quantities, "
#             "prices, department. Use for general 'PO details', 'materials supplied by vendor X', "
#             "'vendors for material Y', 'PO details for plant/date' questions.",
#     "PO_release": "PO approval workflow: release status, approver levels (L1-L5), release dates, "
#                   "days taken to approve, number of releases required. Use for 'released/unreleased/"
#                   "pending PO', 'days taken to approve PO', 'level wise approval'.",
#     "PR_release": "PR approval workflow: release status, rejection level, approver chain "
#                   "(HOD/CFO/MD/Process Owner/VC Chairman), days to approve. Use for 'released/"
#                   "unreleased/rejected/pending PR', 'PR approval cycle', 'rejected at level N'.",
#     "Vendor_PO_History": "Vendor + PO history combined view (VH_ prefix): PO/PR linkage, vendor, "
#                          "material, dates, quantities. Use for 'delay in material received', "
#                          "'delay in GRN', 'GRN details for PO', 'is GRN created for PO'.",
#     "Sap_Purchase": "Raw SAP purchase requisition data: requisitioner, PR processing status, "
#                     "release info, org/plant/department. Use for 'who raised PR X', 'requisitioner "
#                     "for PR', 'how many PR raised by <person>'.",
#     "PR2PO": "PR-to-PO conversion tracking (P2P_ prefix): links PR to resulting PO, PR-to-PO days, "
#              "GRN quantity/flag. Use for 'PR to PO details', 'PR pending for PO', 'PO generated "
#              "against PR X'.",
#     "GateEntry": "Gate entry / weighbridge records (GTENTRY_ prefix): vehicle, driver, challan, "
#                 "gate in/out times, linked PO/material. Use for 'gate entries with/without PO', "
#                 "'gate entry status for PO'.",
#     "Material_Doc_List": "Material document / GRN postings (MTLST_ prefix + GRN_*): goods receipt "
#                         "quantity/value, posting date, movement type. Use for 'material of PO X', "
#                         "'GRN date/quantity for material'.",
#     "SES": "Service Entry Sheet data: service entry approvals, release levels, amount. Use for "
#            "service-related PO/PR questions and SES release status.",
# }


# def _build_system_prompt() -> str:
#     parts = ["You convert a user's natural-language question about a SAP procurement dataset "
#              "into a structured query intent. The dataset is split into 9 reports (views), each "
#              "with its own date filter column:\n"]
#     for r in sch.REPORT_NAMES:
#         parts.append(f"- {r} (date filter: {sch.date_filter_column(r)}): {_REPORT_DESCRIPTIONS[r]}")
#     parts.append(
#         "\nRules:\n"
#         "- Pick exactly one report that best matches the question's intent.\n"
#         "- Only set date_phrase if the user actually mentioned a date/period. Many valid "
#         "questions have NO date filter (e.g. 'show me details for PO 5400010477') -- leave "
#         "date_phrase null in that case, do not invent one.\n"
#         "- Preserve the user's date phrase close to verbatim (e.g. 'last month', 'March 2021', "
#         "'last 3 years', 'April 2025 to March 2026', 'last fy', 'this quarter','this month','last quarter') -- do not resolve "
#         "it to actual dates yourself, a downstream deterministic resolver does that.\n"
#         "- If the user asks for a any year or month either capitalized or in small letters, preserve the case as it is. For example, if the user asks for 'last FY' then preserve it as 'last FY' and if the user asks for 'last fy' then preserve it as 'last fy'.\n"
#         "- For questions asking 'how many <X>' or 'count of <X>' about POs or PRs, prefer "
#         "operation=count_distinct with distinct_key set to 'po_number' or 'pr_number' as "
#         "appropriate -- SAP documents can have multiple line items, so a plain COUNT(*) over-counts.\n"
#         "- Use operation=list_distinct instead of 'list' when the user's phrasing implies "
#         "unique documents rather than every line item (e.g. 'unreleased PO details' listing "
#         "distinct POs) -- but if the user explicitly says something like 'display all POs, not "
#         "only distinct ones', use operation='list'.\n"
#         "- Use operation=group_by_count with group_by_column set for '<dimension> wise' or "
#         "'breakdown by <dimension>' questions (e.g. 'department wise count of POs' -> "
#         "group_by_column='department').\n"
#         "- Use operation=trend with time_grain set for 'month on month', 'quarter on quarter', "
#         "'year on year' questions.\n"
#         "- 'month on month'/'mom'/'monthly', 'quarter on quarter'/'qoq'/'quarterly', 'year on "
#         "year'/'yoy'/'yearly' describe the TIME GRAIN, not a period. Set operation='trend' and "
#         "time_grain, and leave date_phrase null -- NEVER copy the trend wording into "
#         "date_phrase, it is not a resolvable date. Only set date_phrase if the question names a "
#         "period as well: 'quarter on quarter for last fy' -> time_grain='quarter', "
#         "date_phrase='last fy'. A trend with no period stated defaults to the current financial "
#         "year downstream, so you do not need to invent one.\n"
#         "- filters is a dict of semantic_key -> value using ONLY these semantic keys where "
#         "applicable: po_number, pr_number, plant, company_code, vendor_name, material_desc, "
#         "material_code, department, release_status, gate_entry_status, rejected_at_level, "
#         "requisitioner, service_entry_sheet, current_level. Only include keys the question "
#         "actually specifies.\n"
#         "- material_desc / vendor_name filters should carry the raw search text (e.g. 'air "
#         "cooler', 'Brilliance sales') -- these will be matched with case-insensitive partial "
#         "matching downstream, do not guess exact codes.\n"
#         "- If the question is genuinely too ambiguous to build a query (e.g. missing an "
#         "identifier the report requires), set clarification_needed to a short explanation "
#         "instead of guessing."
#     )
#     parts.append(
#         "\nFixed KPI calculations:\n"
#         "A small set of questions match a pre-built, exact KPI calculation instead of the "
#         "generic operation above -- these need composite conditions (e.g. 'released AND no PO "
#         "yet'), NULL checks, or cross-column date differences that operation/filters can't "
#         "express. If the question clearly matches one of these, set kpi_id to the matching id "
#         "and still set report to the report listed for it. Still extract filters and date_phrase "
#         "normally when the question mentions them (e.g. plant, department, vendor_name, "
#         "po_number, pr_number). If the user asks for a trend such as 'month on month', "
#         "'quarter on quarter', or 'year on year', set operation='trend' and time_grain to "
#         "the requested grain. If the user asks for 'count', 'total count', or 'how many', "
#         "set operation='count_distinct' with the right distinct_key when possible; otherwise "
#         "leave operation='list'. aggregate_function/aggregate_column/group_by_column are "
#         "ignored when kpi_id is set. "
#         "If the question does not clearly match one of these, leave kpi_id null and use the "
#         "generic operation-based extraction as usual -- most questions (plain lists, counts, "
#         "group-bys, trends, simple status/aggregate lookups) do NOT need a kpi_id.\n"
#     )
#     for kid, (report, _fn, desc) in sqb.KPI_REGISTRY.items():
#         parts.append(f"- {kid} (report: {report}): {desc}")
#     for kid, reason in sqb.UNSUPPORTED_KPIS.items():
#         parts.append(
#             f"- {kid}: NOT CURRENTLY AVAILABLE -- {reason} If the question asks for this "
#             f"specifically, set clarification_needed explaining it isn't available yet instead "
#             f"of guessing at a report/column."
#         )
#     return "\n".join(parts)


# _TOOL_SCHEMA = {
#     "type": "function",
#     "function": {
#         "name": "extract_intent",
#         "description": "Return the structured query intent for the user's procurement question.",
#         "parameters": {
#             "type": "object",
#             "properties": {
#                 "report": {
#                     "type": "string",
#                     "enum": [
#                         "GateEntry",
#                         "ME2L",
#                         "Material_Doc_List",
#                         "PO_release",
#                         "PR2PO",
#                         "PR_release",
#                         "SES",
#                         "Sap_Purchase",
#                         "Vendor_PO_History"
#                     ]
#                 },
#                 "operation": {
#                     "type": "string",
#                     "enum": [
#                         "list",
#                         "list_distinct",
#                         "count",
#                         "count_distinct",
#                         "group_by_count",
#                         "aggregate",
#                         "trend"
#                     ]
#                 },
#                 "aggregate_function": {
#                     "type": ["string", "null"],
#                     "enum": [
#                         "SUM",
#                         "AVG",
#                         "MIN",
#                         "MAX",
#                         None
#                     ]
#                 },
#                 "aggregate_column": {
#                     "type": ["string", "null"]
#                 },
#                 "group_by_column": {
#                     "type": ["string", "null"]
#                 },
#                 "time_grain": {
#                     "type": ["string", "null"],
#                     "enum": [
#                         "month",
#                         "quarter",
#                         "year",
#                         None
#                     ]
#                 },
#                 "distinct_key": {
#                     "type": ["string", "null"]
#                 },
#                 "filters": {
#                     "type": "object",
#                     "additionalProperties": {
#                         "type": "string"
#                     }
#                 },
#                 "date_phrase": {
#                     "type": ["string", "null"]
#                 },
#                 "limit": {
#                     "type": ["integer", "null"]
#                 },
#                 "clarification_needed": {
#                     "type": ["string", "null"]
#                 },
#                 "kpi_id": {
#                     "type": ["string", "null"],
#                     "enum": list(sqb.KPI_REGISTRY.keys()) + list(sqb.UNSUPPORTED_KPIS.keys()) + [None]
#                 }
#             },
#             "required": [
#                 "report",
#                 "operation",
#                 "filters"
#             ]
#         }
#     }
# }

# _SYSTEM_PROMPT = _build_system_prompt()


# _DATE_PHRASE_ALIASES = (
#     (re.compile(r"\b(?:in|for|during)?\s*this\s+months?\b", re.IGNORECASE), "this month"),
#     (re.compile(r"\b(?:in|for|during)?\s*current\s+months?\b", re.IGNORECASE), "current month"),
# )

# # _KPI_PHRASE_RULES = (
# #     (
# #         re.compile(r"\bpr(?:s)?\b.*\bpending\b.*\bpo\b|\bpending\b.*\bpr(?:s)?\b.*\bpo\b", re.IGNORECASE),
# #         "pr-pending-for-po",
# #     ),
# #     (
# #         re.compile(r"\bpo(?:s)?\b.*\bpending\b|\bpending\b.*\bpo(?:s)?\b", re.IGNORECASE),
# #         "po-pending",
# #     ),
# #     (
# #         re.compile(
# #             r"\b(?:pr(?:s)?(?:\s+\w+){0,2}?\s+reject(?:ed|ion)?|reject(?:ed|ion)?(?:\s+\w+){0,2}?\s+pr(?:s)?)\b",
# #             re.IGNORECASE,
# #         ),
# #         "pr-rejection",
# #     ),
# #     # PO Release
# #     (
# #         re.compile(
# #             r"\b(?:po(?:s)?\s*release(?:d)?|release(?:d)?\s*po(?:s)?)\b",
# #             re.IGNORECASE,
# #         ),
# #         "po-release",
# #     ),
# #     # PO Unrelease / Pending
# #     (
# #         re.compile(
# #             r"\b(?:po(?:s)?\s*(?:unrelease(?:d)?|pending)|(?:unrelease(?:d)?|pending)\s*po(?:s)?)\b",
# #             re.IGNORECASE,
# #         ),
# #         "po-pending",
# #     ),
# #     (
# #         re.compile(r"\bpo(?:s)?\b.*\bapproval\b.*\bcycle\b|\bpo(?:s)?\b.*\bdays?\b.*\bapprov", re.IGNORECASE),
# #         "po-approval-cycle-time",
# #     ),
# #     (
# #         re.compile(r"\bpr(?:s)?\b.*\bapproval\b.*\bcycle\b|\bpr(?:s)?\b.*\bdays?\b.*\bapprov", re.IGNORECASE),
# #         "pr-approval-cycle-time",
# #     ),
# #     (
# #         re.compile(r"\bdelay\b.*\bgrn\b|\bgrn\b.*\bdelay\b", re.IGNORECASE),
# #         "delay-in-grn",
# #     ),
# #     (
# #         re.compile(r"\bgate\s+entr(?:y|ies)\b.*\bwithout\b.*\bpo\b", re.IGNORECASE),
# #         "gate-entry-without-po",
# #     ),
# #     (
# #         re.compile(r"\bgate\s+entr(?:y|ies)\b.*\bwith\b.*\bpo\b", re.IGNORECASE),
# #         "gate-entry-with-po",
# #     ),
# #     (
# #         re.compile(r"\bpo(?:s)?\b.*\bapproval\b.*\bdelay\b|\bpo(?:s)?\b.*\bdays?\b.*\bapprov", re.IGNORECASE),
# #         "po-approval-delay-level-wise",
# #     ),

# #     (
# #         re.compile(
# #             r"\b(?:pr|purchase\s+requisition)\b.*\b(?:creation|created|create|creating)\b"
# #             r"|\b(?:creation|created|create|creating)\b.*\b(?:pr|purchase\s+requisition)\b",
# #             re.IGNORECASE,
# #         ),
# #     "pr-created",
# #     )
# # )


# _KPI_PHRASE_RULES = (

#     # ------------------------------------------------------------------
#     # PR Pending for PO
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bpr(?:s)?\b.*\bpending\b.*\bpo\b"
#             r"|\bpending\b.*\bpr(?:s)?\b.*\bpo\b",
#             re.IGNORECASE,
#         ),
#         "pr-pending-for-po",
#     ),

#     # ------------------------------------------------------------------
#     # PR Rejection
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\b(?:pr(?:s)?(?:\s+\w+){0,2}?\s+reject(?:ed|ion)?"
#             r"|reject(?:ed|ion)?(?:\s+\w+){0,2}?\s+pr(?:s)?)\b",
#             re.IGNORECASE,
#         ),
#         "pr-rejection",
#     ),

#     # ------------------------------------------------------------------
#     # PR Created
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\b(?:pr|purchase\s+requisition)\b.*\b(?:creation|created|create|creating)\b"
#             r"|\b(?:creation|created|create|creating)\b.*\b(?:pr|purchase\s+requisition)\b",
#             re.IGNORECASE,
#         ),
#         "pr-created",
#     ),

#     # ------------------------------------------------------------------
#     # PR Release Status
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bpr(?:s)?\b.*\b(?:release(?:d)?|unrelease(?:d)?|status)\b"
#             r"|\b(?:release(?:d)?|unrelease(?:d)?|status)\b.*\bpr(?:s)?\b",
#             re.IGNORECASE,
#         ),
#         "pr-release-status",
#     ),

#     # ------------------------------------------------------------------
#     # PR Approval Cycle Time
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bpr(?:s)?\b.*\bapproval\b.*\bcycle\b"
#             r"|\bpr(?:s)?\b.*\bdays?\b.*\bapprov",
#             re.IGNORECASE,
#         ),
#         "pr-approval-cycle-time",
#     ),

#     # ------------------------------------------------------------------
#     # PR PO Details
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bpr\b.*\bpo\b.*\bdetails?\b"
#             r"|\bdetails?\b.*\bpr\b.*\bpo\b"
#             r"|\bpr\s*po\b",
#             re.IGNORECASE,
#         ),
#         "pr-po-details",
#     ),

#     # ------------------------------------------------------------------
#     # PO Pending
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bpo(?:s)?\b.*\bpending\b"
#             r"|\bpending\b.*\bpo(?:s)?\b",
#             re.IGNORECASE,
#         ),
#         "po-pending",
#     ),

#     # ------------------------------------------------------------------
#     # PO Released
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\b(?:po(?:s)?\s*release(?:d)?|release(?:d)?\s*po(?:s)?)\b",
#             re.IGNORECASE,
#         ),
#         "po-release",
#     ),

#     # ------------------------------------------------------------------
#     # PO Approval Cycle Time
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bpo(?:s)?\b.*\bapproval\b.*\bcycle\b"
#             r"|\bpo(?:s)?\b.*\bdays?\b.*\bapprov",
#             re.IGNORECASE,
#         ),
#         "po-approval-cycle-time",
#     ),

#     # ------------------------------------------------------------------
#     # PO Approval Delay Level Wise
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bpo(?:s)?\b.*\bapproval\b.*\bdelay\b"
#             r"|\blevel\b.*\bapproval\b.*\bpo\b"
#             r"|\bapproval\b.*\blevel\b.*\bpo\b"
#             r"|\blevel\s*wise\b.*\bpo\b"
#             r"|\bpo\b.*\blevel\s*wise\b",
#             re.IGNORECASE,
#         ),
#         "po-approval-delay-level-wise",
#     ),

#     # ------------------------------------------------------------------
#     # PO Status / GRN
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bpo(?:s)?\b.*\bgrn\b"
#             r"|\bgrn\b.*\bpo(?:s)?\b"
#             r"|\bmaterial\s+document\b"
#             r"|\bgrn\s+status\b",
#             re.IGNORECASE,
#         ),
#         "po-status-grn",
#     ),

#     # ------------------------------------------------------------------
#     # Material PO Delay
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bmaterial\b.*\bpo\b.*\bdelay\b"
#             r"|\bdelay\b.*\bmaterial\b.*\bpo\b"
#             r"|\bdelivery\b.*\bdelay\b",
#             re.IGNORECASE,
#         ),
#         "material-po-delay",
#     ),

#     # ------------------------------------------------------------------
#     # Vendor Wise PO
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bvendor\b.*\bpo\b"
#             r"|\bpo\b.*\bvendor\b"
#             r"|\bvendor\s*wise\b",
#             re.IGNORECASE,
#         ),
#         "vendor-wise-po",
#     ),

#     # ------------------------------------------------------------------
#     # Material Wise Vendor
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bmaterial\b.*\bvendor\b"
#             r"|\bvendor\b.*\bmaterial\b"
#             r"|\bmaterial\s*wise\b",
#             re.IGNORECASE,
#         ),
#         "material-wise-vendor",
#     ),

#     # ------------------------------------------------------------------
#     # Delay in GRN
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bdelay\b.*\bgrn\b"
#             r"|\bgrn\b.*\bdelay\b",
#             re.IGNORECASE,
#         ),
#         "delay-in-grn",
#     ),

#     # ------------------------------------------------------------------
#     # Gate Entry Daily
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bgate\s+entr(?:y|ies)\b.*\bdaily\b"
#             r"|\bdaily\b.*\bgate\s+entr(?:y|ies)\b"
#             r"|\bgate\s+entry\s+count\b",
#             re.IGNORECASE,
#         ),
#         "gate-entry-daily",
#     ),

#     # ------------------------------------------------------------------
#     # Gate Entry With PO
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bgate\s+entr(?:y|ies)\b.*\bwith\b.*\bpo\b",
#             re.IGNORECASE,
#         ),
#         "gate-entry-with-po",
#     ),

#     # ------------------------------------------------------------------
#     # Gate Entry Without PO
#     # ------------------------------------------------------------------
#     (
#         re.compile(
#             r"\bgate\s+entr(?:y|ies)\b.*\bwithout\b.*\bpo\b",
#             re.IGNORECASE,
#         ),
#         "gate-entry-without-po",
#     ),
# )

# _COUNT_PHRASE_RE = re.compile(
#     r"\b(?:count|total\s+count|how\s+many(?:\s+of)?|no\.?\s+of)\b",
#     re.IGNORECASE,
# )

# _TREND_PHRASE_RULES = (
#     (
#         re.compile(
#             r"\b(?:month[\s\-/]*(?:on|over|vs\.?)[\s\-/]*month|m-?o-?m|mom|monthly|"
#             r"(?:by|per|each)\s+month|month\s*[-\s]?wise)\b",
#             re.IGNORECASE,
#         ),
#         "month",
#     ),
#     (
#         re.compile(
#             r"\b(?:quarter[\s\-/]*(?:on|over|vs\.?)[\s\-/]*quarter|q-?o-?q|qoq|quarterly|"
#             r"(?:by|per|each)\s+quarter|quarter\s*[-\s]?wise)\b",
#             re.IGNORECASE,
#         ),
#         "quarter",
#     ),
#     (
#         re.compile(
#             r"\b(?:year[\s\-/]*(?:on|over|vs\.?)[\s\-/]*year|y-?o-?y|yoy|yearly|annually|"
#             r"(?:by|per|each)\s+year|year\s*[-\s]?wise)\b",
#             re.IGNORECASE,
#         ),
#         "year",
#     ),
# )


# # A grain is not a period. The model reliably copies spelled-out grain wording
# # ("quarter on quarter", "year on year") into date_phrase, where the date resolver
# # rightly rejects it -- "Unrecognized date phrase: 'quarter on quarter'". Strip the grain
# # wording out of the phrase and keep only a period if the question actually named one:
# #   "quarter on quarter"             -> None        (falls back to the current-FY default)
# #   "quarter on quarter for last fy" -> "last fy"
# _TREND_WORDING_RE = re.compile(
#     "|".join(pattern.pattern for pattern, _ in _TREND_PHRASE_RULES), re.IGNORECASE
# )

# # Connective/analysis words that can be left dangling at either end once the grain
# # wording is removed. Only trimmed from the edges, never from the middle, so ranges
# # like "2021 to 2024" and "April 2025 to March 2026" are untouched.
# _DATE_PHRASE_EDGE_FILLER = {
#     "for", "in", "of", "on", "by", "at", "during", "the", "over", "across", "basis",
#     "trend", "trends", "trending", "comparison", "comparisons", "compare", "comparing",
#     "analysis", "breakdown", "wise", "and", "vs", "versus", "growth", "change", "count",
# }


# def _sanitize_date_phrase(date_phrase: Optional[str]) -> Optional[str]:
#     """Drop trend-grain wording from a date phrase; return None if nothing real is left."""
#     if not date_phrase or not date_phrase.strip():
#         return None
#     cleaned = _TREND_WORDING_RE.sub(" ", date_phrase)
#     tokens = [t for t in cleaned.split() if t]
#     while tokens and tokens[0].strip(".,-/").lower() in _DATE_PHRASE_EDGE_FILLER:
#         tokens.pop(0)
#     while tokens and tokens[-1].strip(".,-/").lower() in _DATE_PHRASE_EDGE_FILLER:
#         tokens.pop()
#     return " ".join(tokens).strip(" ,/") or None


# _KPI_DEFAULT_DISTINCT_KEYS = {
#     "pr-pending-for-po": "pr_number",
#     "pr-rejection": "pr_number",
#     "po-pending": "po_number",
#     "po-release": "po_number",
#     "po-approval-cycle-time": "po_number",
#     "pr-approval-cycle-time": "pr_number",
#     "delay-in-grn": "po_number",
#     "gate-entry-without-po": "gate_entry_no",
#     "gate-entry-with-po": "gate_entry_no",
# }


# def _apply_deterministic_overrides(question: str, intent: ExtractedIntent) -> ExtractedIntent:
#     """Patch high-value KPI/date phrases that are too important to leave to model drift."""
#     updates = {}
#     requested_time_grain = None
#     for pattern, time_grain in _TREND_PHRASE_RULES:
#         if pattern.search(question):
#             requested_time_grain = time_grain
#             break
    
#     # for pattern, date_phrase in _DATE_PHRASE_ALIASES:
#     #     if pattern.search(question):
#     #         updates["date_phrase"] = date_phrase
#     #         break

#     # The grain belongs in time_grain, never in date_phrase -- see _sanitize_date_phrase.
#     if intent.date_phrase:
#         cleaned_date_phrase = _sanitize_date_phrase(intent.date_phrase)
#         if cleaned_date_phrase != intent.date_phrase:
#             updates["date_phrase"] = cleaned_date_phrase

#     matched_kpi = None
#     for pattern, kpi_id in _KPI_PHRASE_RULES:
#         if pattern.search(question):
#             matched_kpi = kpi_id
#             break

#     if matched_kpi:
#         report, _fn, _desc = sqb.KPI_REGISTRY[matched_kpi]
#         operation = intent.operation
#         distinct_key = intent.distinct_key
#         if requested_time_grain:
#             operation = "trend"
#             distinct_key = distinct_key or _KPI_DEFAULT_DISTINCT_KEYS.get(matched_kpi)
#         elif _COUNT_PHRASE_RE.search(question):
#             operation = "count_distinct"
#             distinct_key = distinct_key or _KPI_DEFAULT_DISTINCT_KEYS.get(matched_kpi)
#         elif operation not in ("count", "count_distinct"):
#             operation = "list"
#         updates.update(
#             {
#                 "kpi_id": matched_kpi,
#                 "report": report,
#                 "operation": operation,
#                 "aggregate_function": None,
#                 "aggregate_column": None,
#                 "group_by_column": None,
#                 "time_grain": requested_time_grain if operation == "trend" else None,
#                 "distinct_key": distinct_key if operation in ("count", "count_distinct", "trend") else None,
#             }
#         )
#     elif requested_time_grain and intent.operation != "trend":
#         # No KPI matched, but the question explicitly asked for a grain ("mom how many
#         # PRs were created"). Without this the trend was dropped on the floor and the
#         # model's count_distinct survived, collapsing the series into one grand total.
#         updates["operation"] = "trend"
#         updates["time_grain"] = requested_time_grain
#         if intent.operation == "aggregate" and intent.aggregate_function and intent.aggregate_column:
#             # "mom total PO value" -- trend the aggregate, don't replace it with a count.
#             updates["distinct_key"] = intent.distinct_key
#         else:
#             updates["aggregate_function"] = None
#             updates["aggregate_column"] = None
#             updates["distinct_key"] = intent.distinct_key or sqb.DEFAULT_DOC_KEY.get(intent.report)
#     elif requested_time_grain and not intent.time_grain:
#         # Already operation='trend' but the model left time_grain null.
#         updates["time_grain"] = requested_time_grain

#     if not updates:
#         return intent
#     if hasattr(intent, "model_copy"):
#         return intent.model_copy(update=updates)
#     return intent.copy(update=updates)


# def extract_intent(question: str) -> ExtractedIntent:

#     response = llm_client.call_llm_json(_SYSTEM_PROMPT, question, _TOOL_SCHEMA)
#     intent = ExtractedIntent(**response)
#     return _apply_deterministic_overrides(question, intent)


"""
Uses the Anthropic API (tool-use / forced function-calling) to turn a natural-language
question into a structured ExtractedIntent. Tool-use is used instead of free-text JSON
parsing because it's schema-validated by the API itself, so we don't have to guard
against the model wrapping output in prose or markdown fences.
"""
from __future__ import annotations
import json
import logging
import re
from typing import Optional


from config import settings
from models import ExtractedIntent
import schema as sch
import llm_client
import sql_builder as sqb
import date_resolver

logger = logging.getLogger("nl2sql.intent_extractor")


_REPORT_DESCRIPTIONS = {
    "ME2L": "PO line-item master list: PO/PR numbers, plant, material, vendor, quantities, "
            "prices, department. Use for general 'PO details', 'materials supplied by vendor X', "
            "'vendors for material Y', 'PO details for plant/date' questions.",
    "PO_release": "PO approval workflow: release status, approver levels (L1-L5), release dates, "
                  "days taken to approve, number of releases required. Use for 'released/unreleased/"
                  "pending PO', 'days taken to approve PO', 'level wise approval'.",
    "PR_release": "PR approval workflow: release status, rejection level, approver chain "
                  "(HOD/CFO/MD/Process Owner/VC Chairman), days to approve. Use for 'released/"
                  "unreleased/rejected/pending PR', 'PR approval cycle', 'rejected at level N'.",
    "Vendor_PO_History": "Vendor + PO history combined view (VH_ prefix): PO/PR linkage, vendor, "
                         "material, dates, quantities. Use for 'delay in material received', "
                         "'delay in GRN', 'GRN details for PO', 'is GRN created for PO'.",
    "Sap_Purchase": "Raw SAP purchase requisition data: requisitioner, PR processing status, "
                    "release info, org/plant/department. Use for 'who raised PR X', 'requisitioner "
                    "for PR', 'how many PR raised by <person>'.",
    "PR2PO": "PR-to-PO conversion tracking (P2P_ prefix): links PR to resulting PO, PR-to-PO days, "
             "GRN quantity/flag. Use for 'PR to PO details', 'PR pending for PO', 'PO generated "
             "against PR X'.",
    "GateEntry": "Gate entry / weighbridge records (GTENTRY_ prefix): vehicle, driver, challan, "
                "gate in/out times, linked PO/material. Use for 'gate entries with/without PO', "
                "'gate entry status for PO'.",
    "Material_Doc_List": "Material document / GRN postings (MTLST_ prefix + GRN_*): goods receipt "
                        "quantity/value, posting date, movement type. Use for 'material of PO X', "
                        "'GRN date/quantity for material'.",
    "SES": "Service Entry Sheet data: service entry approvals, release levels, amount. Use for "
           "service-related PO/PR questions and SES release status.",
}


def _build_system_prompt() -> str:
    parts = ["You convert a user's natural-language question about a SAP procurement dataset "
             "into a structured query intent. The dataset is split into 9 reports (views), each "
             "with its own date filter column:\n"]
    for r in sch.REPORT_NAMES:
        parts.append(f"- {r} (date filter: {sch.date_filter_column(r)}): {_REPORT_DESCRIPTIONS[r]}")
    parts.append(
        "\nRules:\n"
        "- Pick exactly one report that best matches the question's intent.\n"
        "- Only set date_phrase if the user actually mentioned a date/period. Many valid "
        "questions have NO date filter (e.g. 'show me details for PO 5400010477') -- leave "
        "date_phrase null in that case, do not invent one.\n"
        "- Preserve the user's date phrase close to verbatim (e.g. 'last month', 'March 2021', "
        "'last 3 years', 'April 2025 to March 2026', 'last fy', 'this quarter','this month','last quarter') -- do not resolve "
        "it to actual dates yourself, a downstream deterministic resolver does that.\n"
        "- If the user asks for a any year or month either capitalized or in small letters, preserve the case as it is. For example, if the user asks for 'last FY' then preserve it as 'last FY' and if the user asks for 'last fy' then preserve it as 'last fy'.\n"
        "- For questions asking 'how many <X>' or 'count of <X>' about POs or PRs, prefer "
        "operation=count_distinct with distinct_key set to 'po_number' or 'pr_number' as "
        "appropriate -- SAP documents can have multiple line items, so a plain COUNT(*) over-counts.\n"
        "- Use operation=list_distinct instead of 'list' when the user's phrasing implies "
        "unique documents rather than every line item (e.g. 'unreleased PO details' listing "
        "distinct POs) -- but if the user explicitly says something like 'display all POs, not "
        "only distinct ones', use operation='list'.\n"
        "- Use operation=group_by_count with group_by_column set for '<dimension> wise' or "
        "'breakdown by <dimension>' questions (e.g. 'department wise count of POs' -> "
        "group_by_column='department').\n"
        "- Use operation=trend with time_grain set for 'month on month', 'quarter on quarter', "
        "'year on year' questions.\n"
        "- 'month on month'/'mom'/'monthly', 'quarter on quarter'/'qoq'/'quarterly', 'year on "
        "year'/'yoy'/'yearly' describe the TIME GRAIN, not a period. Set operation='trend' and "
        "time_grain, and leave date_phrase null -- NEVER copy the trend wording into "
        "date_phrase, it is not a resolvable date. Only set date_phrase if the question names a "
        "period as well: 'quarter on quarter for last fy' -> time_grain='quarter', "
        "date_phrase='last fy'. A trend with no period stated defaults to the current financial "
        "year downstream, so you do not need to invent one.\n"
        "- filters is a dict of semantic_key -> value using ONLY these semantic keys where "
        "applicable: po_number, pr_number, plant, company_code, vendor_name, material_desc, "
        "material_code, department, release_status, gate_entry_status, rejected_at_level, "
        "requisitioner, service_entry_sheet, current_level. Only include keys the question "
        "actually specifies.\n"
        "- material_desc / vendor_name filters should carry the raw search text (e.g. 'air "
        "cooler', 'Brilliance sales') -- these will be matched with case-insensitive partial "
        "matching downstream, do not guess exact codes.\n"
        "- If the question is genuinely too ambiguous to build a query (e.g. missing an "
        "identifier the report requires), set clarification_needed to a short explanation "
        "instead of guessing."
    )
    parts.append(
        "\nFixed KPI calculations:\n"
        "A small set of questions match a pre-built, exact KPI calculation instead of the "
        "generic operation above -- these need composite conditions (e.g. 'released AND no PO "
        "yet'), NULL checks, or cross-column date differences that operation/filters can't "
        "express. If the question clearly matches one of these, set kpi_id to the matching id "
        "and still set report to the report listed for it. Still extract filters and date_phrase "
        "normally when the question mentions them (e.g. plant, department, vendor_name, "
        "po_number, pr_number). If the user asks for a trend such as 'month on month', "
        "'quarter on quarter', or 'year on year', set operation='trend' and time_grain to "
        "the requested grain. If the user asks for 'count', 'total count', or 'how many', "
        "set operation='count_distinct' with the right distinct_key when possible; otherwise "
        "leave operation='list'. aggregate_function/aggregate_column/group_by_column are "
        "ignored when kpi_id is set. "
        "If the question does not clearly match one of these, leave kpi_id null and use the "
        "generic operation-based extraction as usual -- most questions (plain lists, counts, "
        "group-bys, trends, simple status/aggregate lookups) do NOT need a kpi_id.\n"
    )
    for kid, (report, _fn, desc) in sqb.KPI_REGISTRY.items():
        parts.append(f"- {kid} (report: {report}): {desc}")
    for kid, reason in sqb.UNSUPPORTED_KPIS.items():
        parts.append(
            f"- {kid}: NOT CURRENTLY AVAILABLE -- {reason} If the question asks for this "
            f"specifically, set clarification_needed explaining it isn't available yet instead "
            f"of guessing at a report/column."
        )
    return "\n".join(parts)


_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "extract_intent",
        "description": "Return the structured query intent for the user's procurement question.",
        "parameters": {
            "type": "object",
            "properties": {
                "report": {
                    "type": "string",
                    "enum": [
                        "GateEntry",
                        "ME2L",
                        "Material_Doc_List",
                        "PO_release",
                        "PR2PO",
                        "PR_release",
                        "SES",
                        "Sap_Purchase",
                        "Vendor_PO_History"
                    ]
                },
                "operation": {
                    "type": "string",
                    "enum": [
                        "list",
                        "list_distinct",
                        "count",
                        "count_distinct",
                        "group_by_count",
                        "aggregate",
                        "trend"
                    ]
                },
                "aggregate_function": {
                    "type": ["string", "null"],
                    "enum": [
                        "SUM",
                        "AVG",
                        "MIN",
                        "MAX",
                        None
                    ]
                },
                "aggregate_column": {
                    "type": ["string", "null"]
                },
                "group_by_column": {
                    "type": ["string", "null"]
                },
                "time_grain": {
                    "type": ["string", "null"],
                    "enum": [
                        "month",
                        "quarter",
                        "year",
                        None
                    ]
                },
                "distinct_key": {
                    "type": ["string", "null"]
                },
                "filters": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "string"
                    }
                },
                "date_phrase": {
                    "type": ["string", "null"]
                },
                "limit": {
                    "type": ["integer", "null"]
                },
                "clarification_needed": {
                    "type": ["string", "null"]
                },
                "kpi_id": {
                    "type": ["string", "null"],
                    "enum": list(sqb.KPI_REGISTRY.keys()) + list(sqb.UNSUPPORTED_KPIS.keys()) + [None]
                }
            },
            "required": [
                "report",
                "operation",
                "filters"
            ]
        }
    }
}

_SYSTEM_PROMPT = _build_system_prompt()


_DATE_PHRASE_ALIASES = (
    (re.compile(r"\b(?:in|for|during)?\s*this\s+months?\b", re.IGNORECASE), "this month"),
    (re.compile(r"\b(?:in|for|during)?\s*current\s+months?\b", re.IGNORECASE), "current month"),
)

# _KPI_PHRASE_RULES = (
#     (
#         re.compile(r"\bpr(?:s)?\b.*\bpending\b.*\bpo\b|\bpending\b.*\bpr(?:s)?\b.*\bpo\b", re.IGNORECASE),
#         "pr-pending-for-po",
#     ),
#     (
#         re.compile(r"\bpo(?:s)?\b.*\bpending\b|\bpending\b.*\bpo(?:s)?\b", re.IGNORECASE),
#         "po-pending",
#     ),
#     (
#         re.compile(
#             r"\b(?:pr(?:s)?(?:\s+\w+){0,2}?\s+reject(?:ed|ion)?|reject(?:ed|ion)?(?:\s+\w+){0,2}?\s+pr(?:s)?)\b",
#             re.IGNORECASE,
#         ),
#         "pr-rejection",
#     ),
#     # PO Release
#     (
#         re.compile(
#             r"\b(?:po(?:s)?\s*release(?:d)?|release(?:d)?\s*po(?:s)?)\b",
#             re.IGNORECASE,
#         ),
#         "po-release",
#     ),
#     # PO Unrelease / Pending
#     (
#         re.compile(
#             r"\b(?:po(?:s)?\s*(?:unrelease(?:d)?|pending)|(?:unrelease(?:d)?|pending)\s*po(?:s)?)\b",
#             re.IGNORECASE,
#         ),
#         "po-pending",
#     ),
#     (
#         re.compile(r"\bpo(?:s)?\b.*\bapproval\b.*\bcycle\b|\bpo(?:s)?\b.*\bdays?\b.*\bapprov", re.IGNORECASE),
#         "po-approval-cycle-time",
#     ),
#     (
#         re.compile(r"\bpr(?:s)?\b.*\bapproval\b.*\bcycle\b|\bpr(?:s)?\b.*\bdays?\b.*\bapprov", re.IGNORECASE),
#         "pr-approval-cycle-time",
#     ),
#     (
#         re.compile(r"\bdelay\b.*\bgrn\b|\bgrn\b.*\bdelay\b", re.IGNORECASE),
#         "delay-in-grn",
#     ),
#     (
#         re.compile(r"\bgate\s+entr(?:y|ies)\b.*\bwithout\b.*\bpo\b", re.IGNORECASE),
#         "gate-entry-without-po",
#     ),
#     (
#         re.compile(r"\bgate\s+entr(?:y|ies)\b.*\bwith\b.*\bpo\b", re.IGNORECASE),
#         "gate-entry-with-po",
#     ),
#     (
#         re.compile(r"\bpo(?:s)?\b.*\bapproval\b.*\bdelay\b|\bpo(?:s)?\b.*\bdays?\b.*\bapprov", re.IGNORECASE),
#         "po-approval-delay-level-wise",
#     ),

#     (
#         re.compile(
#             r"\b(?:pr|purchase\s+requisition)\b.*\b(?:creation|created|create|creating)\b"
#             r"|\b(?:creation|created|create|creating)\b.*\b(?:pr|purchase\s+requisition)\b",
#             re.IGNORECASE,
#         ),
#     "pr-created",
#     )
# )


_KPI_PHRASE_RULES = (

    # ------------------------------------------------------------------
    # PR Pending for PO
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bpr(?:s)?\b.*\bpending\b.*\bpo\b"
            r"|\bpending\b.*\bpr(?:s)?\b.*\bpo\b",
            re.IGNORECASE,
        ),
        "pr-pending-for-po",
    ),

    # ------------------------------------------------------------------
    # PR Rejection
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\b(?:pr(?:s)?(?:\s+\w+){0,2}?\s+reject(?:ed|ion)?"
            r"|reject(?:ed|ion)?(?:\s+\w+){0,2}?\s+pr(?:s)?)\b",
            re.IGNORECASE,
        ),
        "pr-rejection",
    ),

    # ------------------------------------------------------------------
    # PR Created
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\b(?:pr|purchase\s+requisition)\b.*\b(?:creation|created|create|creating)\b"
            r"|\b(?:creation|created|create|creating)\b.*\b(?:pr|purchase\s+requisition)\b",
            re.IGNORECASE,
        ),
        "pr-created",
    ),

    # ------------------------------------------------------------------
    # PR Release Status
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bpr(?:s)?\b.*\b(?:release(?:d)?|unrelease(?:d)?|status)\b"
            r"|\b(?:release(?:d)?|unrelease(?:d)?|status)\b.*\bpr(?:s)?\b",
            re.IGNORECASE,
        ),
        "pr-release-status",
    ),

    # ------------------------------------------------------------------
    # PR Approval Cycle Time
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bpr(?:s)?\b.*\bapproval\b.*\bcycle\b"
            r"|\bpr(?:s)?\b.*\bdays?\b.*\bapprov",
            re.IGNORECASE,
        ),
        "pr-approval-cycle-time",
    ),

    # ------------------------------------------------------------------
    # PR PO Details
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bpr\b.*\bpo\b.*\bdetails?\b"
            r"|\bdetails?\b.*\bpr\b.*\bpo\b"
            r"|\bpr\s*po\b",
            re.IGNORECASE,
        ),
        "pr-po-details",
    ),

    # ------------------------------------------------------------------
    # PO Pending
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bpo(?:s)?\b.*\bpending\b"
            r"|\bpending\b.*\bpo(?:s)?\b",
            re.IGNORECASE,
        ),
        "po-pending",
    ),

    # ------------------------------------------------------------------
    # PO Released
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\b(?:po(?:s)?\s*release(?:d)?|release(?:d)?\s*po(?:s)?)\b",
            re.IGNORECASE,
        ),
        "po-release",
    ),

    # ------------------------------------------------------------------
    # PO Approval Cycle Time
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bpo(?:s)?\b.*\bapproval\b.*\bcycle\b"
            r"|\bpo(?:s)?\b.*\bdays?\b.*\bapprov",
            re.IGNORECASE,
        ),
        "po-approval-cycle-time",
    ),

    # ------------------------------------------------------------------
    # PO Approval Delay Level Wise
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bpo(?:s)?\b.*\bapproval\b.*\bdelay\b"
            r"|\blevel\b.*\bapproval\b.*\bpo\b"
            r"|\bapproval\b.*\blevel\b.*\bpo\b"
            r"|\blevel\s*wise\b.*\bpo\b"
            r"|\bpo\b.*\blevel\s*wise\b",
            re.IGNORECASE,
        ),
        "po-approval-delay-level-wise",
    ),

    # ------------------------------------------------------------------
    # PO Status / GRN
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bpo(?:s)?\b.*\bgrn\b"
            r"|\bgrn\b.*\bpo(?:s)?\b"
            r"|\bmaterial\s+document\b"
            r"|\bgrn\s+status\b",
            re.IGNORECASE,
        ),
        "po-status-grn",
    ),

    # ------------------------------------------------------------------
    # Material PO Delay
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bmaterial\b.*\bpo\b.*\bdelay\b"
            r"|\bdelay\b.*\bmaterial\b.*\bpo\b"
            r"|\bdelivery\b.*\bdelay\b",
            re.IGNORECASE,
        ),
        "material-po-delay",
    ),

    # ------------------------------------------------------------------
    # Vendor Wise PO
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bvendor\b.*\bpo\b"
            r"|\bpo\b.*\bvendor\b"
            r"|\bvendor\s*wise\b",
            re.IGNORECASE,
        ),
        "vendor-wise-po",
    ),

    # ------------------------------------------------------------------
    # Material Wise Vendor
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bmaterial\b.*\bvendor\b"
            r"|\bvendor\b.*\bmaterial\b"
            r"|\bmaterial\s*wise\b",
            re.IGNORECASE,
        ),
        "material-wise-vendor",
    ),

    # ------------------------------------------------------------------
    # Delay in GRN
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bdelay\b.*\bgrn\b"
            r"|\bgrn\b.*\bdelay\b",
            re.IGNORECASE,
        ),
        "delay-in-grn",
    ),

    # ------------------------------------------------------------------
    # Gate Entry Daily
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bgate\s+entr(?:y|ies)\b.*\bdaily\b"
            r"|\bdaily\b.*\bgate\s+entr(?:y|ies)\b"
            r"|\bgate\s+entry\s+count\b",
            re.IGNORECASE,
        ),
        "gate-entry-daily",
    ),

    # ------------------------------------------------------------------
    # Gate Entry With PO
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bgate\s+entr(?:y|ies)\b.*\bwith\b.*\bpo\b",
            re.IGNORECASE,
        ),
        "gate-entry-with-po",
    ),

    # ------------------------------------------------------------------
    # Gate Entry Without PO
    # ------------------------------------------------------------------
    (
        re.compile(
            r"\bgate\s+entr(?:y|ies)\b.*\bwithout\b.*\bpo\b",
            re.IGNORECASE,
        ),
        "gate-entry-without-po",
    ),
)

_COUNT_PHRASE_RE = re.compile(
    r"\b(?:count|total\s+count|how\s+many(?:\s+of)?|no\.?\s+of)\b",
    re.IGNORECASE,
)

_TREND_PHRASE_RULES = (
    (
        re.compile(
            r"\b(?:month[\s\-/]*(?:on|over|vs\.?)[\s\-/]*month|m-?o-?m|mom|monthly|"
            r"(?:by|per|each)\s+month|month\s*[-\s]?wise)\b",
            re.IGNORECASE,
        ),
        "month",
    ),
    (
        re.compile(
            r"\b(?:quarter[\s\-/]*(?:on|over|vs\.?)[\s\-/]*quarter|q-?o-?q|qoq|quarterly|"
            r"(?:by|per|each)\s+quarter|quarter\s*[-\s]?wise)\b",
            re.IGNORECASE,
        ),
        "quarter",
    ),
    (
        re.compile(
            r"\b(?:year[\s\-/]*(?:on|over|vs\.?)[\s\-/]*year|y-?o-?y|yoy|yearly|annually|"
            r"(?:by|per|each)\s+year|year\s*[-\s]?wise)\b",
            re.IGNORECASE,
        ),
        "year",
    ),
)


# A grain is not a period. The model reliably copies spelled-out grain wording
# ("quarter on quarter", "year on year") into date_phrase, where the date resolver
# rightly rejects it -- "Unrecognized date phrase: 'quarter on quarter'". Strip the grain
# wording out of the phrase and keep only a period if the question actually named one:
#   "quarter on quarter"             -> None        (falls back to the current-FY default)
#   "quarter on quarter for last fy" -> "last fy"
_TREND_WORDING_RE = re.compile(
    "|".join(pattern.pattern for pattern, _ in _TREND_PHRASE_RULES), re.IGNORECASE
)

# Connective/analysis words that can be left dangling at either end once the grain
# wording is removed. Only trimmed from the edges, never from the middle, so ranges
# like "2021 to 2024" and "April 2025 to March 2026" are untouched.
_DATE_PHRASE_EDGE_FILLER = {
    "for", "in", "of", "on", "by", "at", "during", "the", "over", "across", "basis",
    "trend", "trends", "trending", "comparison", "comparisons", "compare", "comparing",
    "analysis", "breakdown", "wise", "and", "vs", "versus", "growth", "change", "count",
}


def _sanitize_date_phrase(date_phrase: Optional[str]) -> Optional[str]:
    """Drop trend-grain wording from a date phrase; return None if nothing real is left."""
    if not date_phrase or not date_phrase.strip():
        return None
    cleaned = _TREND_WORDING_RE.sub(" ", date_phrase)
    tokens = [t for t in cleaned.split() if t]
    while tokens and tokens[0].strip(".,-/").lower() in _DATE_PHRASE_EDGE_FILLER:
        tokens.pop(0)
    while tokens and tokens[-1].strip(".,-/").lower() in _DATE_PHRASE_EDGE_FILLER:
        tokens.pop()
    return " ".join(tokens).strip(" ,/") or None


_KPI_DEFAULT_DISTINCT_KEYS = {
    "pr-pending-for-po": "pr_number",
    "pr-rejection": "pr_number",
    "po-pending": "po_number",
    "po-release": "po_number",
    "po-approval-cycle-time": "po_number",
    "pr-approval-cycle-time": "pr_number",
    "delay-in-grn": "po_number",
    "gate-entry-without-po": "gate_entry_no",
    "gate-entry-with-po": "gate_entry_no",
}


def _apply_deterministic_overrides(question: str, intent: ExtractedIntent) -> ExtractedIntent:
    """Patch high-value KPI/date phrases that are too important to leave to model drift."""
    updates = {}
    requested_time_grain = None
    for pattern, time_grain in _TREND_PHRASE_RULES:
        if pattern.search(question):
            requested_time_grain = time_grain
            break
    
    # for pattern, date_phrase in _DATE_PHRASE_ALIASES:
    #     if pattern.search(question):
    #         updates["date_phrase"] = date_phrase
    #         break

    # The grain belongs in time_grain, never in date_phrase -- see _sanitize_date_phrase.
    if intent.date_phrase:
        cleaned_date_phrase = _sanitize_date_phrase(intent.date_phrase)
        if cleaned_date_phrase != intent.date_phrase:
            updates["date_phrase"] = cleaned_date_phrase

    # ------------------------------------------------------------------
    # Date-phrase recall safety net.
    #
    # The model occasionally returns date_phrase=null (or an unparseable
    # phrase) for a question that plainly names a date -- a silent failure,
    # since a dropped date filter means the query runs over ALL data instead
    # of the intended window. date_resolver.detect_phrase() is a deterministic,
    # regex-based spotter over the raw question that only recognizes explicit,
    # unambiguous forms (see date_resolver.py); it's a recall safety net for
    # the model, not a replacement -- a phrase the model *did* extract and that
    # resolves cleanly is trusted as-is, even if it's phrased in a way the
    # spotter's fixed patterns wouldn't have caught on their own.
    # ------------------------------------------------------------------
    current_date_phrase = updates.get("date_phrase", intent.date_phrase)
    date_phrase_resolvable = False
    if current_date_phrase:
        try:
            date_phrase_resolvable = date_resolver.resolve(current_date_phrase) is not None
        except ValueError:
            date_phrase_resolvable = False

    if not current_date_phrase or not date_phrase_resolvable:
        spotted = date_resolver.detect_phrase(question)
        if spotted and spotted != current_date_phrase:
            if not current_date_phrase:
                logger.info(
                    "date_phrase filled by spotter (model returned null) -- question=%r spotted=%r",
                    question, spotted,
                )
            else:
                logger.warning(
                    "date_phrase overridden by spotter (model phrase unparseable) -- "
                    "question=%r model_said=%r spotted=%r",
                    question, current_date_phrase, spotted,
                )
            updates["date_phrase"] = spotted

    matched_kpi = None
    for pattern, kpi_id in _KPI_PHRASE_RULES:
        if pattern.search(question):
            matched_kpi = kpi_id
            break

    if matched_kpi:
        report, _fn, _desc = sqb.KPI_REGISTRY[matched_kpi]
        operation = intent.operation
        distinct_key = intent.distinct_key
        if requested_time_grain:
            operation = "trend"
            distinct_key = distinct_key or _KPI_DEFAULT_DISTINCT_KEYS.get(matched_kpi)
        elif _COUNT_PHRASE_RE.search(question):
            operation = "count_distinct"
            distinct_key = distinct_key or _KPI_DEFAULT_DISTINCT_KEYS.get(matched_kpi)
        elif operation not in ("count", "count_distinct"):
            operation = "list"
        updates.update(
            {
                "kpi_id": matched_kpi,
                "report": report,
                "operation": operation,
                "aggregate_function": None,
                "aggregate_column": None,
                "group_by_column": None,
                "time_grain": requested_time_grain if operation == "trend" else None,
                "distinct_key": distinct_key if operation in ("count", "count_distinct", "trend") else None,
            }
        )
    elif requested_time_grain and intent.operation != "trend":
        # No KPI matched, but the question explicitly asked for a grain ("mom how many
        # PRs were created"). Without this the trend was dropped on the floor and the
        # model's count_distinct survived, collapsing the series into one grand total.
        updates["operation"] = "trend"
        updates["time_grain"] = requested_time_grain
        if intent.operation == "aggregate" and intent.aggregate_function and intent.aggregate_column:
            # "mom total PO value" -- trend the aggregate, don't replace it with a count.
            updates["distinct_key"] = intent.distinct_key
        else:
            updates["aggregate_function"] = None
            updates["aggregate_column"] = None
            updates["distinct_key"] = intent.distinct_key or sqb.DEFAULT_DOC_KEY.get(intent.report)
    elif requested_time_grain and not intent.time_grain:
        # Already operation='trend' but the model left time_grain null.
        updates["time_grain"] = requested_time_grain

    if not updates:
        return intent
    if hasattr(intent, "model_copy"):
        return intent.model_copy(update=updates)
    return intent.copy(update=updates)


def extract_intent(question: str) -> ExtractedIntent:

    response = llm_client.call_llm_json(_SYSTEM_PROMPT, question, _TOOL_SCHEMA)
    intent = ExtractedIntent(**response)
    return _apply_deterministic_overrides(question, intent)