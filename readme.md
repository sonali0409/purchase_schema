# Procurement NL-to-SQL Chatbot

FastAPI service that turns natural-language questions about your 9-report unified
procurement schema (`purchase_unified_testing` on Trino/Presto) into SQL and executes it.

## Architecture

```
question -> intent_extractor.py (Anthropic tool-use)  -> ExtractedIntent
         -> sql_builder.py (schema + date_resolver.py) -> SQL string
         -> trino_client.py                            -> columns + rows
```

| File | Responsibility |
|---|---|
| `schema.py` | Auto-generated column catalog for all 9 reports + per-report date filter column + semantic key -> column mapping. Regenerate with `gen_schema.py` if the view's columns change. |
| `date_resolver.py` | Deterministic parsing of date phrases ("last month", "last fy", "Q1 2023", "March 2021", "last 10 days", ranges) into concrete date bounds. Uses **Indian FY (Apr-Mar)**. |
| `intent_extractor.py` | Calls Claude with a schema-aware system prompt + forced tool-use so the question is mapped to a structured `ExtractedIntent` (report, operation, filters, date phrase) — never free-text JSON parsing. |
| `sql_builder.py` | Turns `ExtractedIntent` into SQL: resolves semantic filter keys (`vendor_name`, `plant`, ...) to the real column for the chosen report, applies the date range, and shapes the query for `list` / `count` / `count_distinct` / `group_by_count` / `aggregate` / `trend`. |
| `trino_client.py` | Executes SQL over `trino` DBAPI. |
| `main.py` | FastAPI app: `POST /chat`, `GET /schema`, `GET /health`. |

## The 9 reports and their date filters

| Report | Date filter column |
|---|---|
| ME2L | `PO_Document_Date` |
| PO_release | `PO_Created_On` |
| PR_release | `PR_Created_On` |
| Vendor_PO_History | `VH_PO_Date` |
| Sap_Purchase | `Requisition_date` (was not specified in your list — inferred from `Requisitioner`/PR fields; adjust in `schema.py`'s `DATE_FILTER` dict if you intended a different column) |
| PR2PO | `P2P_Created_On` |
| GateEntry | `GTENTRY_Gate_Entry_Date` |
| Material_Doc_List | `MTLST_Posting_Date` |
| SES | `SES_Date_of_Creation` |

## Design decisions worth knowing about

- **Distinct vs raw counts.** Your test doc flagged several "count isn't distinct count" bugs.
  `count_distinct` is the default the LLM is told to prefer for "how many PO/PR" questions —
  it does `COUNT(DISTINCT PO_Number)` (or the relevant doc key), not `COUNT(*)`, since a
  document can have multiple line items.
- **`list` vs `list_distinct`.** `list_distinct` returns one row per document (a curated set
  of document-level columns, see `DOC_LEVEL_COLUMNS` in `sql_builder.py`) for "show me
  unreleased PO details" style questions. If a question explicitly says "display all POs,
  not only distinct ones" the extractor is told to use plain `list` instead.
- **"last N days" excludes today** — matches your flagged expectation ("date filter should be
  from 13 June to 23 June, shouldn't include current day").
- **"last year" is ambiguous in your doc** — sometimes tested as calendar year, sometimes as
  FY. This build defaults `"last year"` to **calendar year** and keeps `"last fy"` / `"this fy"`
  as separate, explicit FY phrases (Apr–Mar). If your users mean FY when they say "last year",
  either say so in the prompt update in `intent_extractor.py`, or remap `"last year"` in
  `date_resolver.py` to call `_fy_bounds` instead.
- **Fuzzy filters** (`vendor_name`, `material_desc`, e.g. "Brilliance sales", "air cooler") use
  case-insensitive partial match (`LIKE '%...%'`), not exact match — matches the docx note
  "material description mapped to column short_text" / "data is present in column short text".
- **No date filter by default.** The extractor is explicitly told not to invent a date phrase
  when the user didn't give one (per "PO 5400010477 -- date filter not needed").
- **SQL safety:** filter values are escaped (single quotes doubled) before being inlined; there's
  no user-supplied SQL fragment path. If you want parameterized queries instead of inlined
  literals, swap the f-string literals in `sql_builder.py` for `trino`'s parameter binding.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in TRINO_* and ANTHROPIC_API_KEY
uvicorn main:app --reload
```

## Usage

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "Show me vendors for air cooler in last 3 years", "execute": true}'
```

Set `"execute": false` to get back just the generated SQL without running it against Trino
(useful for reviewing/debugging query generation before wiring up a live connection).

`GET /schema` returns the full column catalog per report — useful for a front-end to show
what fields are available, or for you to sanity-check the auto-generated column groupings
in `schema.py` (I split your 333 columns by SAP-report prefix, e.g. `ME2L_*`, `VH_*`, `P2P_*`,
plus a manual assignment for the ~114 non-prefixed shared columns — worth a quick skim since
a few of those, like `Sap_Purchase`'s date filter, were assumptions on my part).

## Known gaps to close before production

1. **`Sap_Purchase` date filter column** wasn't given in your spec — I used `Requisition_date`
   as the closest match; confirm or correct it in `schema.py`.
2. **Group-by dimensions** are currently limited to whatever semantic key maps to a real column
   per report (e.g. `department`, `plant`) — extend `KEY_COLUMNS` in `schema.py` if you need
   more grouping dimensions (e.g. vendor-wise, material-wise).
3. **The LLM extractor is not yet tested against a live API key** in this environment — I've
   validated the deterministic half (schema, date resolution, SQL shape) against hand-built
   intents standing in for the LLM's output, matching the questions in your test doc. Once you
   plug in `ANTHROPIC_API_KEY`, it's worth running the actual question list from
   `purchase_question.docx` end-to-end and spot-checking the extracted intents.
4. **Trino connection** is untested here (no live cluster access) — confirm host/catalog/schema
   values and that `purchase_unified_testing` is queryable with the configured user.