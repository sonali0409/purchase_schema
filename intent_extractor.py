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
    "PR2PO": "PR-to-PO conversion tracking (P2P_ prefix): links purchase requisitions to resulting "
            "purchase orders, tracks GRN (goods receipt) quantity/flag, PR-to-PO days, conversion "
            "time/metrics, PR release status, rejected PRs. Use for questions about requisitions "
            "that became orders, purchase order generation from requisitions, and conversion "
            "timelines -- even when phrased generically without the words 'PR2PO' or 'P2P': "
            "'GRN received', 'GRN quantity', 'goods received', 'material received', 'PR to PO "
            "details', 'PR pending for PO', 'pending orders from PRs', 'pending purchase orders', "
            "'PO generated against PR X', 'purchase order from requisition', 'order generated from "
            "PR', 'requisition converted to purchase order', 'requisition to order', 'PR to PO "
            "conversion', 'purchase requisition to order', 'PR to order', 'requisition details' / "
            "'purchase requisition details' about conversion into an order, 'order details in "
            "PR2PO', 'PR2PO details', 'requisitions with orders', 'requisitions that became "
            "orders', 'requisition status' relative to the resulting order, 'requisition creation' "
            "when the question is about the order that resulted from it, 'conversion time', "
            "'conversion metrics', 'PR conversion', 'average conversion days', and 'orders "
            "created'/'count of orders' style questions that also name a PR/requisition or GRN "
            "context rather than a plain PO document.",
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
        "'breakdown by <dimension>' count questions (e.g. 'department wise count of POs' -> "
        "group_by_column='department'). For aggregate questions with a grouping dimension "
        "(e.g. 'plant-wise total PO value', 'sum amount by material description'), use "
        "operation='aggregate' and set group_by_column to the dimension.\n"
        "- Use operation=trend with time_grain set for 'month on month', 'quarter on quarter', "
        "'year on year' questions.\n"
        "- 'month on month'/'mom'/'monthly', 'quarter on quarter'/'qoq'/'quarterly', 'year on "
        "year'/'yoy'/'yearly' describe the TIME GRAIN, not a period. Set operation='trend' and "
        "time_grain, and leave date_phrase null -- NEVER copy the trend wording into "
        "date_phrase, it is not a resolvable date. Only set date_phrase if the question names a "
        "period as well: 'quarter on quarter for last fy' -> time_grain='quarter', "
        "date_phrase='last fy'. A trend with no period stated defaults to the current financial "
        "year downstream, so you do not need to invent one.\n"
        "- PR2PO ONLY -- GRN (Goods Receipt Note) keywords: 'GRN received', 'GRN done', "
        "'goods receipt received', 'material received', 'orders with GRN' all mean the PR2PO "
        "row has a recorded goods receipt -- set filters={'grn_quantity': '> 0'}. Conversely "
        "'GRN not received', 'no GRN', 'pending GRN', 'GRN pending' mean set "
        "filters={'grn_quantity': '= 0'}. If the user states an explicit GRN quantity "
        "comparison instead (e.g. 'GRN quantity less than 5', 'GRN quantity < 5'), set "
        "filters={'grn_quantity': '< 5'} using the operator and number given, not '> 0'. These "
        "GRN examples (grn_quantity as a semantic key with a comparison-operator value) apply "
        "ONLY to the PR2PO report -- do not use grn_quantity for any other report.\n"
        "- filters is a dict of semantic_key -> value using ONLY these semantic keys where "
        "applicable: po_number, pr_number, plant, company_code, vendor_name,name_of_supplier ,material_desc, "
        "material_code, department, release_status, gate_entry_status, rejected_at_level, "
        "requisitioner, service_entry_sheet, current_level. Only include keys the question "
        "actually specifies. If the user gives multiple values for one filter using commas, "
        "'and', or 'or' (e.g. 'sales and admin department', 'PO 5400010477 and 5400010478'), "
        "set that filter value to an array of strings, not one comma-joined string.\n"
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
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}}
                        ]
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
            r"\bpo(?:s)?\b.*\b(?:pending|unrelease(?:d)?|not\s+released)\b"
            r"|\b(?:pending|unrelease(?:d)?|not\s+released)\b.*\bpo(?:s)?\b",
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
            r"\b(?:delay|late|pending)\b.*\b(?:grn|goods?\s+received|goods?\s+receipt(?:\s+note)?)\b"
            r"|"
            r"\b(?:grn|goods?\s+received|goods?\s+receipt(?:\s+note)?)\b.*\b(?:delay|late|pending)\b",
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


_GROUP_BY_PHRASE_RULES = (
    (
        re.compile(
            r"\b(?:plant\s*[-\s]?wise|by\s+plant|per\s+plant|plant\s+breakdown)\b",
            re.IGNORECASE,
        ),
        "plant",
    ),
    (
        re.compile(
            r"\b(?:department\s*[-\s]?wise|dept\s*[-\s]?wise|by\s+department|"
            r"by\s+dept|per\s+department|department\s+breakdown)\b",
            re.IGNORECASE,
        ),
        "department",
    ),
    (
        re.compile(
            r"\b(?:material\s+description\s*[-\s]?wise|material\s+desc\s*[-\s]?wise|"
            r"by\s+material\s+description|by\s+material\s+desc|per\s+material\s+description|"
            r"material\s+description\s+breakdown)\b",
            re.IGNORECASE,
        ),
        "material_desc",
    ),
    (
        re.compile(
            r"\b(?:material\s*[-\s]?wise|by\s+material|per\s+material|material\s+breakdown)\b",
            re.IGNORECASE,
        ),
        "material_code",
    ),
    (
            re.compile(
                r"\b(?:vendor\s*[-\s]?wise|by\s+vendor|per\s+vendor|vendor\s+breakdown)\b",
                re.IGNORECASE,
            ),
            "vendor_name",
        ),
)

# PR2PO-only recall safety net: the model sometimes drops the comparison operator off a
# numeric filter (e.g. "quantity < 5" -> filters={"grn_quantity": "5"}), silently turning
# a "less than 5" question into an exact-match-on-5 one. This spots an explicit
# "<keyword> <op> <number>" phrase in the raw question and, for PR2PO only, makes sure the
# operator survives into intent.filters as e.g. "< 5" -- sql_builder.py's PR2PO comparison
# handling (see _COMPARISON_OP_RE) then turns that into a real numeric WHERE condition.
_PR2PO_COMPARISON_PHRASE_RULES = (
    (
        re.compile(
            r"\b(?:grn\s*quantity|grn\s*qty|quantity|qty)\b\s*"
            r"(<=|>=|!=|<>|=|<|>)\s*(\d+(?:\.\d+)?)",
            re.IGNORECASE,
        ),
        "grn_quantity",
    ),
    (
        re.compile(
            r"\b(?:pr[\s\-]?to[\s\-]?po\s*days|conversion\s*days|conversion\s*time|days)\b\s*"
            r"(<=|>=|!=|<>|=|<|>)\s*(\d+(?:\.\d+)?)",
            re.IGNORECASE,
        ),
        "conversion_days",
    ),
)


_COMPARISON_VALUE_ALREADY_HAS_OP_RE = re.compile(r"^\s*(<=|>=|!=|<>|=|<|>)")


# PR2PO-only: which of PR2PO's three date columns (P2P_Created_On / P2P_Delivery_Date /
# P2P_Last_Changed_On) a date question is actually about. Order matters -- "delivery" and
# "modified" are checked before the "created" fallback since a phrase naming one of them
# should never be mistaken for the default. Falls back to "created" when nothing matches,
# matching schema.py's existing default date-filter column for PR2PO.
_PR2PO_DATE_TYPE_PHRASE_RULES = (
    (
        re.compile(
            r"\b(?:delivery\s*date|received\s*date|receipt\s*date|delivery)\b",
            re.IGNORECASE,
        ),
        "delivery",
    ),
    (
        re.compile(
            r"\b(?:last\s*(?:changed|modified)|date\s*modified|modified|changed)\b",
            re.IGNORECASE,
        ),
        "modified",
    ),
    (
        re.compile(r"\b(?:created\s*on|creation|created)\b", re.IGNORECASE),
        "created",
    ),
)


# PR2PO-only grouping overrides. PR2PO has no material_code column (only
# P2P_Material_Group), so "material wise" needs to resolve to "material_group" instead
# of the generic _GROUP_BY_PHRASE_RULES default of "material_code" -- and PR2PO also
# supports a "purchasing group" dimension the generic rules above don't cover at all.
_PR2PO_GROUP_BY_PHRASE_RULES = (
    (
        re.compile(
            r"\b(?:purchasing\s*group\s*[-\s]?wise|by\s+purchasing\s+group|"
            r"per\s+purchasing\s+group|purchasing\s+group\s+breakdown|"
            r"purch\s*group\s*[-\s]?wise|by\s+purch\s+group|per\s+purch\s+group|"
            r"purchasing\s+group|purch\s+group)\b",
            re.IGNORECASE,
        ),
        "purchasing_group",
    ),
    (
        re.compile(
            r"\b(?:material\s+group\s*[-\s]?wise|by\s+material\s+group|"
            r"per\s+material\s+group|material\s+group\s+breakdown|material\s+group)\b",
            re.IGNORECASE,
        ),
        "material_group",
    ),
)


# PR2PO-only: generic structural words that show up describing a dimension in the
# question (e.g. "plant location", "vendor name", "material group wise") but that the
# model sometimes mistakes for an actual filter VALUE of that dimension. Used only to
# reject a candidate filter value, never to reject a filter key.
_PR2PO_HALLUCINATION_BLOCKLIST = {
    "location", "wise", "group", "status", "type", "name", "code", "level",
    "number", "value", "details", "detail", "breakdown", "distribution",
}


def _validate_pr2po_filters(question: str, filters: dict) -> dict:
    """PR2PO-only recall/precision guard (see FIX #7): drop any filter whose value the
    model appears to have invented rather than read off the question -- either the value
    never appears in the question at all (e.g. a PO number nobody mentioned), or it's
    just a generic structural word (e.g. "location" out of "plant location") describing
    the dimension rather than naming an actual value for it. Filters that ARE mentioned
    are always kept, comparison operators (FIX #3/#5) included -- only the operand after
    the operator is checked against the question."""
    if not filters:
        return filters
    q_lower = question.lower()
    validated = {}
    for key, value in filters.items():
        if value in (None, ""):
            continue
        text = str(value).strip()
        op_match = _COMPARISON_VALUE_ALREADY_HAS_OP_RE.match(text)
        operand = text[op_match.end():].strip() if op_match else text
        if not operand:
            continue
        operand_lower = operand.lower()
        if operand_lower in _PR2PO_HALLUCINATION_BLOCKLIST:
            continue
        if re.search(r"[^a-z]", operand_lower):
            # contains digits/punctuation/spaces (a code, a number, or multi-word free
            # text like a vendor name) -- require the exact operand text in the question.
            found = operand_lower in q_lower
        else:
            # a single alphabetic word -- require it as a standalone token, not merely a
            # substring caught inside a longer, unrelated word.
            found = re.search(r"\b" + re.escape(operand_lower) + r"\b", q_lower) is not None
        if found:
            validated[key] = value
    return validated


# PR2PO-only (FIX #8): a filter value's own dimension name, or a generic label like
# "status"/"code", often rides along in the user's wording ("F & A department",
# "Vendor Code 001") but isn't part of what's actually stored in the column. Trimmed
# only from the front or back of the value, in order, and never down to nothing --
# the middle of a value (e.g. the "&" in "F & A") is never touched.
_PR2PO_FILTER_VALUE_DESCRIPTORS = {
    "department", "group", "code", "status", "name", "type", "level", "category",
}

_PR2PO_FILTER_KEY_WORDS = {
    "department": {"department"},
    "vendor_name": {"vendor", "name"},
    "plant": {"plant"},
    "material_desc": {"material", "description", "desc"},
    "material_group": {"material", "group"},
    "purchasing_group": {"purchasing", "group"},
    "po_number": {"po", "number"},
    "pr_number": {"pr", "number"},
}


def _normalize_pr2po_filter_values(filters: dict) -> dict:
    """PR2PO-only: strip a leading/trailing descriptor word off each filter value --
    run after _validate_pr2po_filters so hallucinated filters are already gone and only
    real, but over-worded, values are being cleaned."""
    normalized = {}
    for key, value in (filters or {}).items():
        if isinstance(value, str) and value.strip():
            descriptors = _PR2PO_FILTER_VALUE_DESCRIPTORS | _PR2PO_FILTER_KEY_WORDS.get(key, set())
            tokens = value.split()
            start = 0
            while start < len(tokens) - 1 and re.sub(r"[^\w&]", "", tokens[start]).lower() in descriptors:
                start += 1
            end = len(tokens)
            while end > start + 1 and re.sub(r"[^\w&]", "", tokens[end - 1]).lower() in descriptors:
                end -= 1
            cleaned = " ".join(tokens[start:end]).strip()
            normalized[key] = cleaned if cleaned else value
        else:
            normalized[key] = value
    return normalized


# PR2PO-only (FIX #9): recall safety net for "average conversion time by vendor"-style
# questions where the model recognizes operation="aggregate" but leaves
# aggregate_function/aggregate_column null. Keyword -> (function, column); checked in
# this order so a more specific word never loses to a less specific one appearing later
# in the same question.
_PR2PO_AGGREGATE_KEYWORDS = (
    (re.compile(r"\b(?:average|avg)\b", re.IGNORECASE), ("AVG", "PR_To_PO_Days")),
    (re.compile(r"\b(?:sum|total)\b", re.IGNORECASE), ("SUM", "P2P_GRN_Quantity")),
    (re.compile(r"\b(?:minimum|min)\b", re.IGNORECASE), ("MIN", "PR_To_PO_Days")),
    (re.compile(r"\b(?:maximum|max)\b", re.IGNORECASE), ("MAX", "PR_To_PO_Days")),
)


def _detect_pr2po_aggregate(question: str):
    """PR2PO-only: return (aggregate_function, aggregate_column) for the first
    recognized keyword in the question, or (None, None) if none matched."""
    for pattern, (agg_fn, agg_col) in _PR2PO_AGGREGATE_KEYWORDS:
        if pattern.search(question):
            return agg_fn, agg_col
    return None, None


# PR2PO-only (FIX #10 safety net): bare GRN wording with no explicit number -- e.g. "GRN
# received", "GRN not received" -- that the LLM doesn't reliably turn into a grn_quantity
# filter on its own even with the FIX #10 system-prompt instruction, and that FIX #3's
# comparison-operator regex can't catch since there's no number in the question at all.
# Negative phrasing is checked first so "GRN not received" is never mistaken for the
# positive "GRN received" pattern.
_PR2PO_GRN_KEYWORD_RULES = (
    (
        re.compile(
            r"\b(?:grn\s*not\s*received|no\s*grn|grn\s*pending|pending\s*grn|"
            r"grn\s*not\s*done)\b",
            re.IGNORECASE,
        ),
        "= 0",
    ),
    (
        re.compile(
            r"\b(?:grn\s*received|grn\s*done|goods\s*receipt\s*received|"
            r"material\s*received|orders?\s+with\s+grn)\b",
            re.IGNORECASE,
        ),
        "> 0",
    ),
)


_GROUP_BY_KEY_ALIASES = {
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
}


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
    "pr-release-status": "pr_number",
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

    requested_group_by = None
    for pattern, group_by_column in _GROUP_BY_PHRASE_RULES:
        if pattern.search(question):
            requested_group_by = group_by_column
            break

    # PR2PO-only: override/extend the generic grouping match above. PR2PO has no
    # material_code column, so a bare "material wise" must resolve to "material_group"
    # instead; "purchasing group wise" isn't recognized by the generic rules at all.
    if intent.report == "PR2PO":
        for pattern, group_by_column in _PR2PO_GROUP_BY_PHRASE_RULES:
            if pattern.search(question):
                requested_group_by = group_by_column
                break
        else:
            if requested_group_by == "material_code":
                requested_group_by = "material_group"

    current_group_by = intent.group_by_column
    if current_group_by:
        normalized_group_by = _GROUP_BY_KEY_ALIASES.get(str(current_group_by).strip().lower())
        if normalized_group_by and normalized_group_by != current_group_by:
            updates["group_by_column"] = normalized_group_by
            current_group_by = normalized_group_by
    if requested_group_by and not current_group_by:
        updates["group_by_column"] = requested_group_by
        current_group_by = requested_group_by
    if (
        current_group_by
        and intent.operation == "group_by_count"
        and intent.aggregate_function
        and intent.aggregate_column
    ):
        updates["operation"] = "aggregate"
    
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
        elif _COUNT_PHRASE_RE.search(question) or current_group_by:
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
                "group_by_column": current_group_by,
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

    # PR2PO-only: preserve a comparison operator the model dropped from a numeric filter.
    # Gated on the EFFECTIVE report (post KPI-match override) so this never touches
    # filters for ME2L, Sap_Purchase, PO_release, PR_release, or any other report.
    effective_report = updates.get("report", intent.report)
    if effective_report == "PR2PO":
        for pattern, semantic_key in _PR2PO_COMPARISON_PHRASE_RULES:
            match = pattern.search(question)
            if not match:
                continue
            op, number = match.groups()
            current_filters = updates.get("filters", intent.filters) or {}
            existing_value = str(current_filters.get(semantic_key, ""))
            if not _COMPARISON_VALUE_ALREADY_HAS_OP_RE.match(existing_value):
                updates["filters"] = {**current_filters, semantic_key: f"{op} {number}"}
            break

    # PR2PO-only: tag which date column ("created" | "delivery" | "modified") the question
    # is actually about, so sql_builder.py can filter on the right one instead of always
    # defaulting to P2P_Created_On. Gated on the same effective_report as the comparison-
    # operator fix above, so no other report is affected.
    if effective_report == "PR2PO":
        detected_date_type = "created"
        for pattern, date_type in _PR2PO_DATE_TYPE_PHRASE_RULES:
            if pattern.search(question):
                detected_date_type = date_type
                break
        if detected_date_type != (intent.date_type or "created"):
            updates["date_type"] = detected_date_type

    # PR2PO-only (FIX #5 continued): once date_type has pinned down which date column
    # the question is about, strip the descriptive words that named that column out of
    # date_phrase, so "delivery date in 2015" resolves to just "2015" instead of tripping
    # the date resolver on words that aren't part of the date itself.
    if effective_report == "PR2PO":
        current_date_type = updates.get("date_type", intent.date_type)
        current_date_phrase = updates.get("date_phrase", intent.date_phrase)
        if current_date_type and current_date_phrase:
            descriptive_words = [
                "delivery date", "delivery",
                "last modified", "modified", "changed", "last changed",
                "created", "creation", "created on",
                "received", "goods receipt", "goods received",
                "in", "on", "at",
            ]
            cleaned_phrase = current_date_phrase
            for word in descriptive_words:
                cleaned_phrase = cleaned_phrase.lower().replace(word, "").strip()
            if cleaned_phrase and cleaned_phrase != current_date_phrase:
                updates["date_phrase"] = cleaned_phrase

    # PR2PO-only: bare-year recall safety net. Two failure modes seen in practice:
    # (a) the model names a year in a plain "... in 2014 ..." question but returns
    # date_phrase=null entirely (the generic spotter above only recognizes date_resolver's
    # fixed patterns, and missed these); (b) the model instead stuffs the year into an
    # unrelated filter value, so it survives as a meaningless filter rather than a date.
    # Only ever fills date_phrase when it's still empty -- never overrides a phrase the
    # model or an earlier fix already set.
    if effective_report == "PR2PO":
        current_date_phrase = updates.get("date_phrase", intent.date_phrase)
        if not current_date_phrase:
            current_filters = updates.get("filters", intent.filters) or {}
            year_filter_key = next(
                (
                    key for key, value in current_filters.items()
                    if re.fullmatch(r"(?:19|20)\d{2}", str(value).strip())
                ),
                None,
            )
            if year_filter_key:
                updates["date_phrase"] = str(current_filters[year_filter_key]).strip()
                updates["filters"] = {
                    k: v for k, v in current_filters.items() if k != year_filter_key
                }
            else:
                year_match = re.search(r"\bin\s+((?:19|20)\d{2})\b", question, re.IGNORECASE)
                if year_match:
                    updates["date_phrase"] = year_match.group(1)

    # PR2PO-only: date-phrase hallucination guard (Q42-style). The model occasionally
    # returns a date_phrase built from a relative-period word ("last quarter") that the
    # question never actually said. Since date_resolver.py can't be touched to add a
    # "was this really said" check, catch it here: if the phrase names a period unit the
    # question doesn't mention at all, the phrase was invented -- drop it rather than
    # filter on a window the user never asked for.
    if effective_report == "PR2PO":
        current_date_phrase = updates.get("date_phrase", intent.date_phrase)
        if current_date_phrase:
            phrase_lower = current_date_phrase.lower()
            q_lower = question.lower()
            for period_word in ("quarter", "month", "week"):
                if period_word in phrase_lower and period_word not in q_lower:
                    updates["date_phrase"] = None
                    break

    # PR2PO-only: run last, after FIX #3 (comparison operators), FIX #5 (grouping) and
    # FIX #6 (date type) have all had their say, so it validates the final filter set
    # rather than an intermediate one. Drops any filter the model invented instead of
    # reading off the question (FIX #7), then trims descriptive words that rode along
    # with an otherwise-real value (FIX #8) -- in that order, since a hallucinated
    # filter should be removed outright rather than normalized.
    if effective_report == "PR2PO":
        current_filters = updates.get("filters", intent.filters)
        validated_filters = _validate_pr2po_filters(question, current_filters)
        normalized_filters = _normalize_pr2po_filter_values(validated_filters)
        if normalized_filters != current_filters:
            updates["filters"] = normalized_filters

    # PR2PO-only (FIX #9): fill in aggregate_function/aggregate_column when the model
    # recognized an "aggregate" question (e.g. "average conversion time by vendor") but
    # left the function/column null -- see _detect_pr2po_aggregate. Never overrides a
    # function the model (or an earlier override above) already set.
    if effective_report == "PR2PO":
        effective_operation = updates.get("operation", intent.operation)
        effective_agg_fn = updates.get("aggregate_function", intent.aggregate_function)
        if effective_operation == "aggregate" and not effective_agg_fn:
            detected_fn, detected_col = _detect_pr2po_aggregate(question)
            if detected_fn:
                updates["aggregate_function"] = detected_fn
                updates["aggregate_column"] = detected_col

    # PR2PO-only (FIX #10 safety net): catch bare "GRN received"/"GRN not received"
    # wording the LLM missed. Runs LAST, after _validate_pr2po_filters, since the
    # synthesized "> 0"/"= 0" value has no literal "0" in the question for that
    # validator to find -- adding it before validation would get it removed as a
    # hallucination. Never overrides a grn_quantity the model (or FIX #3) already set.
    if effective_report == "PR2PO":
        current_filters = updates.get("filters", intent.filters) or {}
        if "grn_quantity" not in current_filters:
            for pattern, grn_value in _PR2PO_GRN_KEYWORD_RULES:
                if pattern.search(question):
                    updates["filters"] = {**current_filters, "grn_quantity": grn_value}
                    break

    if not updates:
        return intent
    if hasattr(intent, "model_copy"):
        return intent.model_copy(update=updates)
    return intent.copy(update=updates)


def extract_intent(question: str) -> ExtractedIntent:

    response = llm_client.call_llm_json(_SYSTEM_PROMPT, question, _TOOL_SCHEMA)
    intent = ExtractedIntent(**response)
    return _apply_deterministic_overrides(question, intent)
