"""
Uses the Anthropic API (tool-use / forced function-calling) to turn a natural-language
question into a structured ExtractedIntent. Tool-use is used instead of free-text JSON
parsing because it's schema-validated by the API itself, so we don't have to guard
against the model wrapping output in prose or markdown fences.
"""
from __future__ import annotations
import json


from config import settings
from models import ExtractedIntent
import schema as sch
import llm_client


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
        "'last 3 years', 'April 2025 to March 2026', 'last fy', 'this quarter') -- do not resolve "
        "it to actual dates yourself, a downstream deterministic resolver does that.\n"
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


def extract_intent(question: str) -> ExtractedIntent:

    response = llm_client.call_llm_json(_SYSTEM_PROMPT, question, _TOOL_SCHEMA)
    return ExtractedIntent(**response)