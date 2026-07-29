
# """
# Resolves natural-language date phrases into concrete (start_date, end_date) pairs,
# and builds structured Month-over-Month / Quarter-over-Quarter / Year-over-Year
# comparison periods.

# -------------------------------------------------------------------------------
# CALENDAR YEAR vs FINANCIAL YEAR -- handled separately, on purpose
# -------------------------------------------------------------------------------
# - Calendar Year (CY):  1 Jan -> 31 Dec.
#     "2025"            -> CY2025            (2025-01-01 .. 2025-12-31)
#     "this year"       -> current CY
#     "last year"       -> previous CY

# - Financial / Fiscal Year (FY), Indian convention: 1 Apr -> 31 Mar.
#     "FY2025" / "FY 2025-26" -> FY starting 1 Apr 2025, ending 31 Mar 2026
#     "this fy"               -> current FY
#     "last fy"               -> previous FY

#   NOTE: earlier versions of this module treated a bare 4-digit year ("2025")
#   as an FY. That conflated the two concepts and made "2021 to 2024" behave
#   like an FY range. Bare years are now always Calendar Years; use an explicit
#   "FY" prefix to get fiscal-year semantics.

# Quarters are likewise split:
# - Calendar quarter:  Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep, Q4=Oct-Dec.
# - FY quarter:        Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar (next CY).
#   FY quarters are only used when the phrase explicitly says "fy" (e.g.
#   "fy q1 2025" or "q1 fy2025"). A bare "q1 2025" is always a calendar quarter.

# All dates returned are datetime.date, inclusive on both ends.
# -------------------------------------------------------------------------------
# """
# from __future__ import annotations

# import calendar
# import re
# from dataclasses import dataclass
# from datetime import date, timedelta
# from typing import Callable, List, Optional, Tuple

# from dateutil.relativedelta import relativedelta

# DateRange = Tuple[date, date]

# MONTH_NAMES = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
# MONTH_ABBR = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}

# # Words that, if present, mean the phrase is describing a single relative
# # period rather than an "X to Y" range -- so we don't try to split on "-".
# _RANGE_GUARD_WORDS = re.compile(r"\blast\b|\bthis\b|\bcurrent\b")
# _RANGE_SPLIT = re.compile(r"^(.*?)\s+(?:to|-|until|through)\s+(.*)$")


# # ===============================================================================
# # Small value types
# # ===============================================================================

# @dataclass(frozen=True)
# class Period:
#     """A concrete, labelled date span."""
#     start: date
#     end: date
#     label: str = ""

#     def as_tuple(self) -> DateRange:
#         return self.start, self.end

#     def __iter__(self):
#         # lets `start, end = period` keep working, same ergonomics as the old tuple API
#         yield self.start
#         yield self.end


# @dataclass(frozen=True)
# class ComparisonResult:
#     """Structured metadata for a MoM / QoQ / YoY comparison."""
#     kind: str  # "MoM" | "QoQ" | "YoY"
#     current: Period
#     previous: Period

#     def as_dict(self) -> dict:
#         return {
#             "kind": self.kind,
#             "current": {
#                 "start": self.current.start.isoformat(),
#                 "end": self.current.end.isoformat(),
#                 "label": self.current.label,
#             },
#             "previous": {
#                 "start": self.previous.start.isoformat(),
#                 "end": self.previous.end.isoformat(),
#                 "label": self.previous.label,
#             },
#         }


# # ===============================================================================
# # Low-level bound helpers
# # ===============================================================================

# def _month_end(y: int, m: int) -> date:
#     return date(y, m, calendar.monthrange(y, m)[1])


# def _cy_bounds(year: int) -> DateRange:
#     """Calendar year bounds: 1 Jan .. 31 Dec."""
#     return date(year, 4, 1), date(year+1, 3, 31)


# def _fy_bounds(fy_start_year: int) -> DateRange:
#     """FY that starts 1 Apr of fy_start_year and ends 31 Mar of fy_start_year+1."""
#     return date(fy_start_year, 4, 1), date(fy_start_year + 1, 3, 31)


# def _current_fy_start_year(today: date) -> int:
#     return today.year if today.month >= 4 else today.year - 1


# def _quarter_bounds(fy_year: int, quarter: int) -> DateRange:
#     """
#     Financial Year quarter bounds (FY starts in April).
#     """
#     if quarter not in (1, 2, 3, 4):
#         raise ValueError("Quarter must be between 1 and 4.")

#     start_months = {1: 4, 2: 7, 3: 10, 4: 1}
#     start_month = start_months[quarter]

#     year = fy_year if quarter < 4 else fy_year + 1
#     end_month = start_month + 2 if quarter < 4 else 3

#     return date(year, start_month, 1), _month_end(year, end_month)


# def _fy_quarter_bounds(fy_start_year: int, fy_quarter: int) -> DateRange:
#     """FY quarter: Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar (of fy_start_year+1)."""
#     offsets = {
#         1: (4, fy_start_year),
#         2: (7, fy_start_year),
#         3: (10, fy_start_year),
#         4: (1, fy_start_year + 1),
#     }
#     start_month, y = offsets[fy_quarter]
#     return date(y, start_month, 1), _month_end(y, start_month + 2)


# def _which_calendar_quarter(d: date) -> int:
#     if 4 <= d.month <= 6:
#         return 1  # Q1
#     elif 7 <= d.month <= 9:
#         return 2  # Q2
#     elif 10 <= d.month <= 12:
#         return 3  # Q3
#     else:  # January-March
#         return 4  # Q4


# def _which_fy_quarter(d: date) -> Tuple[int, int]:
#     """Returns (fy_start_year, fy_quarter) for a given date."""
#     fy_start_year = _current_fy_start_year(d)
#     month_in_fy = (d.month - 4) % 12  # 0..11, 0 = April
#     fy_quarter = month_in_fy // 3 + 1
#     return fy_start_year, fy_quarter


# def _month_bounds_relative(anchor: date, months_back: int) -> DateRange:
#     """Bounds of the whole month that is `months_back` months before anchor's month
#     (months_back=0 -> anchor's own month)."""
#     first_of_anchor_month = date(anchor.year, anchor.month, 1)
#     target_first = first_of_anchor_month - relativedelta(months=months_back)
#     return date(target_first.year, target_first.month, 1), _month_end(target_first.year, target_first.month)


# # ===============================================================================
# # Labelling (best-effort, used by resolve_period / comparisons)
# # ===============================================================================

# # def _label_for(start: date, end: date) -> str:
# #     if start == end:
# #         return start.isoformat()

# #     # Calendar year: Jan 1 .. Dec 31, same year
# #     if (start.month, start.day) == (1, 1) and (end.month, end.day) == (12, 31) and start.year == end.year:
# #         return str(start.year)

# #     # FY: Apr 1 .. Mar 31 of next year
# #     if (start.month, start.day) == (4, 1) and (end.month, end.day) == (3, 31) and end.year == start.year + 1:
# #         return f"FY{start.year}-{str(end.year)[2:]}"

# #     # Whole calendar quarter
# #     if start.day == 1 and start.month in (1, 4, 7, 10) and start.year == end.year:
# #         if end == _month_end(end.year, start.month + 2):
# #             q = (start.month - 1) // 3 + 1
# #             return f"Q{q} {start.year}"

# #     # Whole single month
# #     if start.day == 1 and start.year == end.year and start.month == end.month and end == _month_end(end.year, end.month):
# #         return f"{calendar.month_name[start.month]} {start.year}"

# #     return f"{start.isoformat()} to {end.isoformat()}"


# def _label_for(start: date, end: date) -> str:
#     if start == end:
#         return start.isoformat()

#     # Financial Year: Apr 1 .. Mar 31
#     if (
#         (start.month, start.day) == (4, 1)
#         and (end.month, end.day) == (3, 31)
#         and end.year == start.year + 1
#     ):
#         return f"FY{start.year}-{str(end.year)[2:]}"

#     # Financial Year Quarters
#     if start.day == 1:
#         # FY Q1: Apr-Jun
#         if (
#             start.month == 4
#             and end == _month_end(start.year, 6)
#         ):
#             return f"Q1 FY{start.year}-{str(start.year + 1)[2:]}"

#         # FY Q2: Jul-Sep
#         if (
#             start.month == 7
#             and end == _month_end(start.year, 9)
#         ):
#             return f"Q2 FY{start.year}-{str(start.year + 1)[2:]}"

#         # FY Q3: Oct-Dec
#         if (
#             start.month == 10
#             and end == _month_end(start.year, 12)
#         ):
#             return f"Q3 FY{start.year}-{str(start.year + 1)[2:]}"

#         # FY Q4: Jan-Mar (belongs to previous FY)
#         if (
#             start.month == 1
#             and end == _month_end(start.year, 3)
#         ):
#             fy = start.year - 1
#             return f"Q4 FY{fy}-{str(start.year)[2:]}"

#     # Whole calendar year (optional)
#     if (
#         (start.month, start.day) == (1, 1)
#         and (end.month, end.day) == (12, 31)
#         and start.year == end.year
#     ):
#         return str(start.year)

#     # Whole single month
#     if (
#         start.day == 1
#         and start.year == end.year
#         and start.month == end.month
#         and end == _month_end(end.year, end.month)
#     ):
#         return f"{calendar.month_name[start.month]} {start.year}"

#     return f"{start.isoformat()} to {end.isoformat()}"

# # ===============================================================================
# # Individual phrase parsers. Each takes (p, today) and returns a DateRange or
# # None if the phrase doesn't match its pattern. `resolve()` tries them in order.
# # ===============================================================================

# def _p_today_yesterday(p: str, today: date) -> Optional[DateRange]:
#     if p == "yesterday":
#         y = today - timedelta(days=1)
#         return y, y
#     if p == "today":
#         return today, today
#     return None


# def _p_last_n_days(p: str, today: date) -> Optional[DateRange]:
#     m = re.match(r"last (\d+) days?$", p)
#     if not m:
#         return None
#     n = int(m.group(1))
#     # excludes today, per user-flagged expectation
#     end = today - timedelta(days=1)
#     start = end - timedelta(days=n - 1)
#     return start, end


# def _p_this_last_month(p: str, today: date) -> Optional[DateRange]:
#     if p in ("this month", "current month"):
#         return date(today.year, today.month, 1), _month_end(today.year, today.month)
#     if p == "last month":
#         return _month_bounds_relative(today, 1)
#     return None


# def _p_last_n_months(p: str, today: date) -> Optional[DateRange]:
#     """'last N months', excluding the current (incomplete) month -- i.e. the N
#     complete months ending with last month."""
#     m = re.match(r"last (\d+) months?$", p)
#     if not m:
#         return None
#     n = int(m.group(1))
#     end_start, end = _month_bounds_relative(today, 1)              # last month
#     start, _ = _month_bounds_relative(today, n)                     # n months before "this month"
#     return start, end


# def _p_month_year(p: str, today: date) -> Optional[DateRange]:
#     m = re.match(r"([a-z]+)\s+(\d{4})$", p)
#     if not m:
#         return None
#     name = m.group(1)
#     mo = MONTH_NAMES.get(name) or (MONTH_ABBR.get(name[:3]) if name[:3] in MONTH_ABBR else None)
#     if not mo:
#         return None
#     yr = int(m.group(2))
#     return date(yr, mo, 1), _month_end(yr, mo)


# def _p_bare_month(p: str, today: date) -> Optional[DateRange]:
#     """Bare month name or abbreviation ('march' or 'mar') -> most recent
#     occurrence: this year if that month hasn't passed yet, else last year.
#     Used standalone and as a building block for month ranges like 'jan to mar'."""
#     mo = MONTH_NAMES.get(p) or MONTH_ABBR.get(p)
#     if mo:
#         yr = today.year if mo <= today.month else today.year - 1
#         return date(yr, mo, 1), _month_end(yr, mo)
#     return None


# def _p_this_last_quarter(p: str, today: date) -> Optional[DateRange]:
#     if p in ("this quarter", "current quarter"):
#         q = _which_calendar_quarter(today)
#         return _quarter_bounds(today.year, q)
#     if p == "last quarter":
#         q = _which_calendar_quarter(today)
#         year = today.year
#         q -= 1
#         if q == 0:
#             q, year = 4, year - 1
#         return _quarter_bounds(year, q)
#     return None


# def _p_last_n_quarters(p: str, today: date) -> Optional[DateRange]:
#     m = re.match(r"last (\d+) quarters?$", p)
#     if not m:
#         return None
#     n = int(m.group(1))
#     q = _which_calendar_quarter(today)
#     year = today.year
#     end_q, end_year = q - 1, year
#     if end_q == 0:
#         end_q, end_year = 4, year - 1
#     _, end_date = _quarter_bounds(end_year, end_q)
#     start_q, start_year = end_q, end_year
#     for _ in range(n - 1):
#         start_q -= 1
#         if start_q == 0:
#             start_q, start_year = 4, start_year - 1
#     start_date, _ = _quarter_bounds(start_year, start_q)
#     return start_date, end_date


# def _p_fy_quarter(p: str, today: date) -> Optional[DateRange]:
#     """Explicit FY-quarter forms: 'fy q1 2025', 'fyq1 2025', 'q1 fy2025'."""
#     m = re.match(r"fy\s*q\s*([1-4])\s+(\d{4})$", p)
#     if m:
#         return _fy_quarter_bounds(int(m.group(2)), int(m.group(1)))
#     m = re.match(r"q\s*([1-4])\s*fy\s*(\d{4})$", p)
#     if m:
#         return _fy_quarter_bounds(int(m.group(2)), int(m.group(1)))
#     return None


# def _p_quarter_year(p: str, today: date) -> Optional[DateRange]:
#     """Calendar quarter + year, e.g. 'q1 2025'."""
#     m = re.match(r"q\s*([1-4])\s*(\d{4})$", p)
#     if m:
#         return _quarter_bounds(int(m.group(2)), int(m.group(1)))
#     return None


# def _p_bare_quarter(p: str, today: date) -> Optional[DateRange]:
#     """Bare calendar quarter, e.g. 'q1' -> current year."""
#     m = re.match(r"q\s*([1-4])$", p)
#     if m:
#         return _quarter_bounds(today.year, int(m.group(1)))
#     return None


# def _p_this_last_fy(p: str, today: date) -> Optional[DateRange]:
#     if p in ("this fy", "current fy", "this financial year", "current financial year"):
#         return _fy_bounds(_current_fy_start_year(today))
#     if p in ("last fy", "last financial year", "previous fy"):
#         return _fy_bounds(_current_fy_start_year(today) - 1)
#     return None


# def _p_fy_year(p: str, today: date) -> Optional[DateRange]:
#     """'FY2025' / 'FY 2025-26' style -- requires the 'fy' prefix."""
#     m = re.match(r"fy\s*-?\s*(\d{4})", p)
#     if m:
#         return _fy_bounds(int(m.group(1)))
#     return None


# def _p_last_n_fy(p: str, today: date) -> Optional[DateRange]:
#     m = re.match(r"last (\d+) (?:fy|financial years?)$", p)
#     if not m:
#         return None
#     n = int(m.group(1))
#     cur = _current_fy_start_year(today)
#     start, _ = _fy_bounds(cur - n)
#     _, end = _fy_bounds(cur - 1)
#     return start, end


# def _p_this_last_year(p: str, today: date) -> Optional[DateRange]:
#     if p in ("this year", "current year"):
#         return _cy_bounds(today.year)
#     if p == "last year":
#         return _cy_bounds(today.year - 1)
#     return None


# def _p_bare_year(p: str, today: date) -> Optional[DateRange]:
#     """Bare 4-digit year -> Calendar Year (NOT fiscal year; use 'FY2025' for that)."""
#     m = re.match(r"(\d{4})$", p)
#     if m:
#         return _cy_bounds(int(m.group(1)))
#     return None


# def _p_last_n_years(p: str, today: date) -> Optional[DateRange]:
#     m = re.match(r"last (\d+) years?$", p)
#     if not m:
#         return None
#     n = int(m.group(1))
#     end = date(today.year - 1, 12, 31)
#     start = date(today.year - n, 1, 1)
#     return start, end


# def _p_iso_date(p: str, today: date) -> Optional[DateRange]:
#     m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", p)
#     if m:
#         d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
#         return d, d
#     return None


# def _p_dd_mon_yyyy(p: str, today: date) -> Optional[DateRange]:
#     m = re.match(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})$", p)
#     if m and m.group(2)[:3] in MONTH_ABBR:
#         d = date(int(m.group(3)), MONTH_ABBR[m.group(2)[:3]], int(m.group(1)))
#         return d, d
#     return None

# def _between_months_with_or_without_year(p: str, today: date) -> Optional[DateRange]:
#     """Handles phrases like 'jan to mar', 'jan 2025 to mar 2025', 'jan 2025 to mar'."""
#     m = re.match(r"([a-z]+)(?:\s+(\d{4}))?\s+(?:to|-|until|through)\s+([a-z]+)(?:\s+(\d{4}))?$", p)
#     if not m:
#         return None
#     start_month_name, start_year_str, end_month_name, end_year_str = m.groups()
#     start_month = MONTH_NAMES.get(start_month_name) or MONTH_ABBR.get(start_month_name[:3])
#     end_month = MONTH_NAMES.get(end_month_name) or MONTH_ABBR.get(end_month_name[:3])
#     if not start_month or not end_month:
#         return None

#     # Determine the years for the start and end months
#     if start_year_str:
#         start_year = int(start_year_str)
#     else:
#         # If no year is provided, assume the most recent occurrence of the month
#         start_year = today.year if start_month <= today.month else today.year - 1

#     if end_year_str:
#         end_year = int(end_year_str)
#     else:
#         # If no year is provided, assume the same year as the start month unless it has passed
#         end_year = start_year if end_month >= start_month else start_year + 1

#     # Ensure that the range is valid (start date should be before or equal to end date)
#     if (start_year, start_month) > (end_year, end_month):
#         return None

#     start_date = date(start_year, start_month, 1)
#     end_date = _month_end(end_year, end_month)
#     return start_date, end_date



# # Order matters: more specific patterns (with a year attached) must be tried
# # before their "bare" counterparts.
# _PARSERS: List[Callable[[str, date], Optional[DateRange]]] = [
#     _p_today_yesterday,
#     _p_last_n_days,
#     _p_this_last_month,
#     _p_last_n_months,
#     _p_month_year,
#     _p_fy_quarter,
#     _p_quarter_year,
#     _p_this_last_quarter,
#     _p_last_n_quarters,
#     _p_bare_quarter,
#     _p_this_last_fy,
#     _p_fy_year,
#     _p_last_n_fy,
#     _p_this_last_year,
#     _p_last_n_years,
#     _p_bare_year,
#     _p_iso_date,
#     _p_dd_mon_yyyy,
#     _p_bare_month,  # last: bare month names are the loosest pattern
#     _between_months_with_or_without_year
# ]


# # ===============================================================================
# # Public API
# # ===============================================================================

# def resolve(phrase: Optional[str], today: Optional[date] = None) -> Optional[DateRange]:
#     """
#     Resolve a natural-language date phrase to (start_date, end_date), inclusive.
#     Returns None if no phrase given / no date filter should be applied.
#     Raises ValueError on an unrecognized but non-empty phrase (caller should decide
#     whether to fall back to "no date filter" or surface an error).
#     """
#     if not phrase or not phrase.strip():
#         return None
#     p = phrase.strip().lower()
#     p = re.sub(r"\s+", " ", p)
#     today = today or date.today()

#     # ---- explicit "X to Y" / "X - Y" range, each side parsed recursively ----
#     range_match = _RANGE_SPLIT.match(p)
#     if range_match and not _RANGE_GUARD_WORDS.search(p):
#         left, right = range_match.groups()
#         left_r = resolve(left, today)
#         right_r = resolve(right, today)
#         if left_r and right_r:
#             return left_r[0], right_r[1]

#     for parser in _PARSERS:
#         result = parser(p, today)
#         print(f"Trying parser {parser.__name__} on phrase {p!r}: result={result}")
#         if result is not None:
#             return result

#     raise ValueError(
#         f"Unrecognized date phrase: {phrase!r}. Supported forms include 'today', 'yesterday', "
#         f"'last 7 days', 'this month', 'last month', 'last 3 months', 'March 2021', 'q1 2025', "
#         f"'last quarter', 'last 2 quarters', 'this fy', 'last fy', 'FY2025', '2025', "
#         f"'last 3 years', '2021 to 2024', '2025-01-31'. Note that 'month on month' / "
#         f"'quarter on quarter' / 'year on year' are time grains, not periods."
#     )


# def resolve_period(phrase: Optional[str], today: Optional[date] = None) -> Optional[Period]:
#     """Same as resolve(), but returns a labelled Period instead of a bare tuple."""
#     r = resolve(phrase, today)
#     if r is None:
#         return None
#     start, end = r
#     return Period(start, end, _label_for(start, end))


# def current_fy_range(today: Optional[date] = None) -> DateRange:
#     """Bounds of the financial year (1 Apr .. 31 Mar) that `today` falls inside."""
#     today = today or date.today()
#     return _fy_bounds(_current_fy_start_year(today))


# def current_fy_period(today: Optional[date] = None) -> Period:
#     """Current FY as a labelled Period, e.g. FY2026-27 for any date in Apr 2026 .. Mar 2027.

#     Used as the implicit window for trend queries where the user named a grain
#     ("month over month") but no period -- an unbounded trend scans the whole table
#     and buries the recent months the question is actually about.
#     """
#     start, end = current_fy_range(today)
#     return Period(start, end, _label_for(start, end))


# def _fy_label(fy_start_year: int) -> str:
#     return f"FY{fy_start_year}-{str(fy_start_year + 1)[2:]}"


# def recent_fy_span(years: int = 3, today: Optional[date] = None) -> Period:
#     """The last `years` financial years, ending with (and including) the current one.

#     A year-grain trend confined to a single FY is one bucket, which can't express a
#     year-over-year change -- so YoY needs a window spanning several FYs. years=1 gives
#     exactly current_fy_period().
#     """
#     if years < 1:
#         raise ValueError("years must be >= 1")
#     today = today or date.today()
#     current = _current_fy_start_year(today)
#     first = current - (years - 1)
#     start, _ = _fy_bounds(first)
#     _, end = _fy_bounds(current)
#     label = _fy_label(current) if years == 1 else f"{_fy_label(first)} to {_fy_label(current)}"
#     return Period(start, end, label)


# # ===============================================================================
# # MoM / QoQ / YoY comparisons
# # ===============================================================================

# def month_over_month(today: Optional[date] = None, include_current: bool = False) -> ComparisonResult:
#     """
#     MoM comparison.
#     include_current=False (default): compares the last two *complete* months
#         (last month vs the month before that) -- avoids biasing on a partial
#         current month.
#     include_current=True: compares this (partial) month vs last month.
#     """
#     today = today or date.today()
#     offset = 0 if include_current else 1
#     cur_start, cur_end = _month_bounds_relative(today, offset)
#     prev_start, prev_end = _month_bounds_relative(today, offset + 1)
#     return ComparisonResult(
#         kind="MoM",
#         current=Period(cur_start, cur_end, _label_for(cur_start, cur_end)),
#         previous=Period(prev_start, prev_end, _label_for(prev_start, prev_end)),
#     )


# def quarter_over_quarter(today: Optional[date] = None, include_current: bool = False) -> ComparisonResult:
#     """
#     QoQ comparison (calendar quarters).
#     include_current=False (default): last complete quarter vs the one before it.
#     include_current=True: this (partial) quarter vs last quarter.
#     """
#     today = today or date.today()
#     q = _which_calendar_quarter(today)
#     year = today.year

#     if include_current:
#         cur_q, cur_year = q, year
#     else:
#         cur_q, cur_year = q - 1, year
#         if cur_q == 0:
#             cur_q, cur_year = 4, year - 1

#     prev_q, prev_year = cur_q - 1, cur_year
#     if prev_q == 0:
#         prev_q, prev_year = 4, cur_year - 1

#     cur_start, cur_end = _quarter_bounds(cur_year, cur_q)
#     prev_start, prev_end = _quarter_bounds(prev_year, prev_q)
#     return ComparisonResult(
#         kind="QoQ",
#         current=Period(cur_start, cur_end, _label_for(cur_start, cur_end)),
#         previous=Period(prev_start, prev_end, _label_for(prev_start, prev_end)),
#     )


# def year_over_year(
#     today: Optional[date] = None,
#     include_current: bool = False,
#     fiscal: bool = False,
# ) -> ComparisonResult:
#     """
#     YoY comparison.
#     fiscal=False (default): calendar years.
#     fiscal=True: financial years (Apr-Mar).
#     include_current=False (default): last complete year vs the year before it.
#     include_current=True: this (partial) year vs last year.
#     """
#     today = today or date.today()

#     if fiscal:
#         cur_fy = _current_fy_start_year(today) - (0 if include_current else 1)
#         prev_fy = cur_fy - 1
#         cur_start, cur_end = _fy_bounds(cur_fy)
#         prev_start, prev_end = _fy_bounds(prev_fy)
#     else:
#         cur_year = today.year - (0 if include_current else 1)
#         prev_year = cur_year - 1
#         cur_start, cur_end = _cy_bounds(cur_year)
#         prev_start, prev_end = _cy_bounds(prev_year)

#     return ComparisonResult(
#         kind="YoY",
#         current=Period(cur_start, cur_end, _label_for(cur_start, cur_end)),
#         previous=Period(prev_start, prev_end, _label_for(prev_start, prev_end)),
#     )


# def resolve_comparison(phrase: str, today: Optional[date] = None) -> ComparisonResult:
#     """
#     Parse a comparison phrase into a ComparisonResult.
#     Recognised phrases (case-insensitive, flexible spacing):
#         "mom", "mom current"
#         "qoq", "qoq current"
#         "yoy", "yoy current", "yoy fy", "yoy fy current"
#     "current" includes the in-progress current period instead of comparing
#     the last two complete periods.
#     """
#     p = re.sub(r"\s+", " ", phrase.strip().lower())
#     tokens = set(p.split())
#     include_current = "current" in tokens
#     fiscal = "fy" in tokens

#     if "mom" in tokens:
#         return month_over_month(today, include_current=include_current)
#     if "qoq" in tokens:
#         return quarter_over_quarter(today, include_current=include_current)
#     if "yoy" in tokens:
#         return year_over_year(today, include_current=include_current, fiscal=fiscal)

#     raise ValueError(f"Unrecognized comparison phrase: {phrase!r}")


# # ===============================================================================
# # SQL helpers
# # ===============================================================================

# def time_grain_trunc_expr(column: str, grain: str, fiscal: bool = False) -> str:
#     """Trino date_trunc expression for month/quarter/year trend queries.

#     fiscal=True only changes the *year* grain, where it buckets by financial year
#     (Apr-Mar) instead of calendar year. That matters because a calendar-year bucket
#     over a fiscal window splits the first and last FY into partial buckets, which makes
#     the year-over-year change read as a huge jump that's really just 9 months vs 12.
#     Month and quarter need no fiscal variant: FY quarters (Apr-Jun, Jul-Sep, ...) have
#     exactly the same boundaries as calendar quarters, only a different number.
#     """
#     grain = grain.lower()
#     if grain not in ("month", "quarter", "year"):
#         raise ValueError(f"Unsupported time grain: {grain}")
#     parsed = f"TRY(date_parse(CAST({column} AS VARCHAR), '%Y%m%d'))"
#     if grain == "year" and fiscal:
#         # Shift back 3 months so Apr-Mar falls inside one calendar year, truncate, then
#         # shift forward again so each bucket is stamped with its own 1 Apr start date.
#         return f"date_add('month', 3, date_trunc('year', date_add('month', -3, {parsed})))"
#     return f"date_trunc('{grain}', {parsed})"


"""
Resolves natural-language date phrases into concrete (start_date, end_date) pairs,
and builds structured Month-over-Month / Quarter-over-Quarter / Year-over-Year
comparison periods.

-------------------------------------------------------------------------------
CALENDAR YEAR vs FINANCIAL YEAR -- handled separately, on purpose
-------------------------------------------------------------------------------
- Calendar Year (CY):  1 Jan -> 31 Dec.
    "2025"            -> CY2025            (2025-01-01 .. 2025-12-31)
    "this year"       -> current CY
    "last year"       -> previous CY

- Financial / Fiscal Year (FY), Indian convention: 1 Apr -> 31 Mar.
    "FY2025" / "FY 2025-26" -> FY starting 1 Apr 2025, ending 31 Mar 2026
    "this fy"               -> current FY
    "last fy"               -> previous FY

  NOTE: earlier versions of this module treated a bare 4-digit year ("2025")
  as an FY. That conflated the two concepts and made "2021 to 2024" behave
  like an FY range. Bare years are now always Calendar Years; use an explicit
  "FY" prefix to get fiscal-year semantics.

Quarters are likewise split:
- Calendar quarter:  Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep, Q4=Oct-Dec.
- FY quarter:        Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar (next CY).
  FY quarters are only used when the phrase explicitly says "fy" (e.g.
  "fy q1 2025" or "q1 fy2025"). A bare "q1 2025" is always a calendar quarter.

All dates returned are datetime.date, inclusive on both ends.
-------------------------------------------------------------------------------
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, List, Optional, Tuple

from dateutil.relativedelta import relativedelta

DateRange = Tuple[date, date]

MONTH_NAMES = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTH_ABBR = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}

# Words that, if present, mean the phrase is describing a single relative
# period rather than an "X to Y" range -- so we don't try to split on "-".
_RANGE_GUARD_WORDS = re.compile(r"\blast\b|\bthis\b|\bcurrent\b")
_RANGE_SPLIT = re.compile(r"^(.*?)\s+(?:to|-|until|through|till)\s+(.*)$")

# "between X and Y" -- unambiguous syntax (the word "between" itself signals a
# range), so it's checked ahead of, and isn't itself subject to, the guard
# words above: "between last month and this month" should still split.
_BETWEEN_SPLIT = re.compile(r"^between\s+(.*?)\s+and\s+(.*)$")

# Open-ended "X till date" / "X to date" / "X till now" -- resolves the left
# side for its start and pins the end to `today`.
_TILL_DATE_SUFFIX = re.compile(r"^(.*?)\s+(?:till date|to date|till now|until now|till today)$")


# ===============================================================================
# Small value types
# ===============================================================================

@dataclass(frozen=True)
class Period:
    """A concrete, labelled date span."""
    start: date
    end: date
    label: str = ""

    def as_tuple(self) -> DateRange:
        return self.start, self.end

    def __iter__(self):
        # lets `start, end = period` keep working, same ergonomics as the old tuple API
        yield self.start
        yield self.end


@dataclass(frozen=True)
class ComparisonResult:
    """Structured metadata for a MoM / QoQ / YoY comparison."""
    kind: str  # "MoM" | "QoQ" | "YoY"
    current: Period
    previous: Period

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "current": {
                "start": self.current.start.isoformat(),
                "end": self.current.end.isoformat(),
                "label": self.current.label,
            },
            "previous": {
                "start": self.previous.start.isoformat(),
                "end": self.previous.end.isoformat(),
                "label": self.previous.label,
            },
        }


# ===============================================================================
# Low-level bound helpers
# ===============================================================================

def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _cy_bounds(year: int) -> DateRange:
    """Calendar year bounds: 1 Jan .. 31 Dec."""
    return date(year, 4, 1), date(year+1, 3, 31)


def _fy_bounds(fy_start_year: int) -> DateRange:
    """FY that starts 1 Apr of fy_start_year and ends 31 Mar of fy_start_year+1."""
    return date(fy_start_year, 4, 1), date(fy_start_year + 1, 3, 31)


def _current_fy_start_year(today: date) -> int:
    return today.year if today.month >= 4 else today.year - 1


def _quarter_bounds(fy_year: int, quarter: int) -> DateRange:
    """
    Financial Year quarter bounds (FY starts in April).
    """
    if quarter not in (1, 2, 3, 4):
        raise ValueError("Quarter must be between 1 and 4.")

    start_months = {1: 4, 2: 7, 3: 10, 4: 1}
    start_month = start_months[quarter]

    year = fy_year if quarter < 4 else fy_year + 1
    end_month = start_month + 2 if quarter < 4 else 3

    return date(year, start_month, 1), _month_end(year, end_month)


def _fy_quarter_bounds(fy_start_year: int, fy_quarter: int) -> DateRange:
    """FY quarter: Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar (of fy_start_year+1)."""
    offsets = {
        1: (4, fy_start_year),
        2: (7, fy_start_year),
        3: (10, fy_start_year),
        4: (1, fy_start_year + 1),
    }
    start_month, y = offsets[fy_quarter]
    return date(y, start_month, 1), _month_end(y, start_month + 2)


def _which_calendar_quarter(d: date) -> int:
    if 4 <= d.month <= 6:
        return 1  # Q1
    elif 7 <= d.month <= 9:
        return 2  # Q2
    elif 10 <= d.month <= 12:
        return 3  # Q3
    else:  # January-March
        return 4  # Q4


def _which_fy_quarter(d: date) -> Tuple[int, int]:
    """Returns (fy_start_year, fy_quarter) for a given date."""
    fy_start_year = _current_fy_start_year(d)
    month_in_fy = (d.month - 4) % 12  # 0..11, 0 = April
    fy_quarter = month_in_fy // 3 + 1
    return fy_start_year, fy_quarter


def _month_bounds_relative(anchor: date, months_back: int) -> DateRange:
    """Bounds of the whole month that is `months_back` months before anchor's month
    (months_back=0 -> anchor's own month)."""
    first_of_anchor_month = date(anchor.year, anchor.month, 1)
    target_first = first_of_anchor_month - relativedelta(months=months_back)
    return date(target_first.year, target_first.month, 1), _month_end(target_first.year, target_first.month)


# ===============================================================================
# Labelling (best-effort, used by resolve_period / comparisons)
# ===============================================================================

# def _label_for(start: date, end: date) -> str:
#     if start == end:
#         return start.isoformat()

#     # Calendar year: Jan 1 .. Dec 31, same year
#     if (start.month, start.day) == (1, 1) and (end.month, end.day) == (12, 31) and start.year == end.year:
#         return str(start.year)

#     # FY: Apr 1 .. Mar 31 of next year
#     if (start.month, start.day) == (4, 1) and (end.month, end.day) == (3, 31) and end.year == start.year + 1:
#         return f"FY{start.year}-{str(end.year)[2:]}"

#     # Whole calendar quarter
#     if start.day == 1 and start.month in (1, 4, 7, 10) and start.year == end.year:
#         if end == _month_end(end.year, start.month + 2):
#             q = (start.month - 1) // 3 + 1
#             return f"Q{q} {start.year}"

#     # Whole single month
#     if start.day == 1 and start.year == end.year and start.month == end.month and end == _month_end(end.year, end.month):
#         return f"{calendar.month_name[start.month]} {start.year}"

#     return f"{start.isoformat()} to {end.isoformat()}"


def _label_for(start: date, end: date) -> str:
    if start == end:
        return start.isoformat()

    # Financial Year: Apr 1 .. Mar 31
    if (
        (start.month, start.day) == (4, 1)
        and (end.month, end.day) == (3, 31)
        and end.year == start.year + 1
    ):
        return f"FY{start.year}-{str(end.year)[2:]}"

    # Financial Year Quarters
    if start.day == 1:
        # FY Q1: Apr-Jun
        if (
            start.month == 4
            and end == _month_end(start.year, 6)
        ):
            return f"Q1 FY{start.year}-{str(start.year + 1)[2:]}"

        # FY Q2: Jul-Sep
        if (
            start.month == 7
            and end == _month_end(start.year, 9)
        ):
            return f"Q2 FY{start.year}-{str(start.year + 1)[2:]}"

        # FY Q3: Oct-Dec
        if (
            start.month == 10
            and end == _month_end(start.year, 12)
        ):
            return f"Q3 FY{start.year}-{str(start.year + 1)[2:]}"

        # FY Q4: Jan-Mar (belongs to previous FY)
        if (
            start.month == 1
            and end == _month_end(start.year, 3)
        ):
            fy = start.year - 1
            return f"Q4 FY{fy}-{str(start.year)[2:]}"

    # Whole calendar year (optional)
    if (
        (start.month, start.day) == (1, 1)
        and (end.month, end.day) == (12, 31)
        and start.year == end.year
    ):
        return str(start.year)

    # Whole single month
    if (
        start.day == 1
        and start.year == end.year
        and start.month == end.month
        and end == _month_end(end.year, end.month)
    ):
        return f"{calendar.month_name[start.month]} {start.year}"

    return f"{start.isoformat()} to {end.isoformat()}"

# ===============================================================================
# Individual phrase parsers. Each takes (p, today) and returns a DateRange or
# None if the phrase doesn't match its pattern. `resolve()` tries them in order.
# ===============================================================================

def _p_today_yesterday(p: str, today: date) -> Optional[DateRange]:
    if p == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if p == "today":
        return today, today
    return None


def _p_last_n_days(p: str, today: date) -> Optional[DateRange]:
    m = re.match(r"last (\d+) days?$", p)
    if not m:
        return None
    n = int(m.group(1))
    # excludes today, per user-flagged expectation
    end = today - timedelta(days=1)
    start = end - timedelta(days=n - 1)
    return start, end


def _p_this_last_month(p: str, today: date) -> Optional[DateRange]:
    if p in ("this month", "current month"):
        return date(today.year, today.month, 1), _month_end(today.year, today.month)
    if p == "last month":
        return _month_bounds_relative(today, 1)
    return None


def _p_last_n_months(p: str, today: date) -> Optional[DateRange]:
    """'last N months', excluding the current (incomplete) month -- i.e. the N
    complete months ending with last month."""
    m = re.match(r"last (\d+) months?$", p)
    if not m:
        return None
    n = int(m.group(1))
    end_start, end = _month_bounds_relative(today, 1)              # last month
    start, _ = _month_bounds_relative(today, n)                     # n months before "this month"
    return start, end


def _p_month_year(p: str, today: date) -> Optional[DateRange]:
    m = re.match(r"([a-z]+)\s+(\d{4})$", p)
    if not m:
        return None
    name = m.group(1)
    mo = MONTH_NAMES.get(name) or (MONTH_ABBR.get(name[:3]) if name[:3] in MONTH_ABBR else None)
    if not mo:
        return None
    yr = int(m.group(2))
    return date(yr, mo, 1), _month_end(yr, mo)


def _p_bare_month(p: str, today: date) -> Optional[DateRange]:
    """Bare month name or abbreviation ('march' or 'mar') -> most recent
    occurrence: this year if that month hasn't passed yet, else last year.
    Used standalone and as a building block for month ranges like 'jan to mar'."""
    mo = MONTH_NAMES.get(p) or MONTH_ABBR.get(p)
    if mo:
        yr = today.year if mo <= today.month else today.year - 1
        return date(yr, mo, 1), _month_end(yr, mo)
    return None


def _p_this_last_quarter(p: str, today: date) -> Optional[DateRange]:
    if p in ("this quarter", "current quarter"):
        q = _which_calendar_quarter(today)
        return _quarter_bounds(today.year, q)
    if p == "last quarter":
        q = _which_calendar_quarter(today)
        year = today.year
        q -= 1
        if q == 0:
            q, year = 4, year - 1
        return _quarter_bounds(year, q)
    return None


def _p_last_n_quarters(p: str, today: date) -> Optional[DateRange]:
    m = re.match(r"last (\d+) quarters?$", p)
    if not m:
        return None
    n = int(m.group(1))
    q = _which_calendar_quarter(today)
    year = today.year
    end_q, end_year = q - 1, year
    if end_q == 0:
        end_q, end_year = 4, year - 1
    _, end_date = _quarter_bounds(end_year, end_q)
    start_q, start_year = end_q, end_year
    for _ in range(n - 1):
        start_q -= 1
        if start_q == 0:
            start_q, start_year = 4, start_year - 1
    start_date, _ = _quarter_bounds(start_year, start_q)
    return start_date, end_date


def _p_fy_quarter(p: str, today: date) -> Optional[DateRange]:
    """Explicit FY-quarter forms: 'fy q1 2025', 'fyq1 2025', 'q1 fy2025'."""
    m = re.match(r"fy\s*q\s*([1-4])\s+(\d{4})$", p)
    if m:
        return _fy_quarter_bounds(int(m.group(2)), int(m.group(1)))
    m = re.match(r"q\s*([1-4])\s*fy\s*(\d{4})$", p)
    if m:
        return _fy_quarter_bounds(int(m.group(2)), int(m.group(1)))
    return None


def _p_quarter_year(p: str, today: date) -> Optional[DateRange]:
    """Calendar quarter + year, e.g. 'q1 2025'."""
    m = re.match(r"q\s*([1-4])\s*(\d{4})$", p)
    if m:
        return _quarter_bounds(int(m.group(2)), int(m.group(1)))
    return None


def _p_bare_quarter(p: str, today: date) -> Optional[DateRange]:
    """Bare calendar quarter, e.g. 'q1' -> current year."""
    m = re.match(r"q\s*([1-4])$", p)
    if m:
        return _quarter_bounds(today.year, int(m.group(1)))
    return None


def _p_this_last_fy(p: str, today: date) -> Optional[DateRange]:
    if p in ("this fy", "current fy", "this financial year", "current financial year"):
        return _fy_bounds(_current_fy_start_year(today))
    if p in ("last fy", "last financial year", "previous fy"):
        return _fy_bounds(_current_fy_start_year(today) - 1)
    return None


def _p_fy_year(p: str, today: date) -> Optional[DateRange]:
    """'FY2025' / 'FY 2025-26' style -- requires the 'fy' prefix."""
    m = re.match(r"fy\s*-?\s*(\d{4})", p)
    if m:
        return _fy_bounds(int(m.group(1)))
    return None


def _p_last_n_fy(p: str, today: date) -> Optional[DateRange]:
    m = re.match(r"last (\d+) (?:fy|financial years?)$", p)
    if not m:
        return None
    n = int(m.group(1))
    cur = _current_fy_start_year(today)
    start, _ = _fy_bounds(cur - n)
    _, end = _fy_bounds(cur - 1)
    return start, end


def _p_this_last_year(p: str, today: date) -> Optional[DateRange]:
    if p in ("this year", "current year"):
        return _cy_bounds(today.year)
    if p == "last year":
        return _cy_bounds(today.year - 1)
    return None


def _p_bare_year(p: str, today: date) -> Optional[DateRange]:
    """Bare 4-digit year -> Calendar Year (NOT fiscal year; use 'FY2025' for that)."""
    m = re.match(r"(\d{4})$", p)
    if m:
        return _cy_bounds(int(m.group(1)))
    return None


def _p_last_n_years(p: str, today: date) -> Optional[DateRange]:
    m = re.match(r"last (\d+) years?$", p)
    if not m:
        return None
    n = int(m.group(1))
    end = date(today.year - 1, 12, 31)
    start = date(today.year - n, 1, 1)
    return start, end


def _p_iso_date(p: str, today: date) -> Optional[DateRange]:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", p)
    if m:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d, d
    return None


def _p_dd_mon_yyyy(p: str, today: date) -> Optional[DateRange]:
    m = re.match(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})$", p)
    if m and m.group(2)[:3] in MONTH_ABBR:
        d = date(int(m.group(3)), MONTH_ABBR[m.group(2)[:3]], int(m.group(1)))
        return d, d
    return None

def _between_months_with_or_without_year(p: str, today: date) -> Optional[DateRange]:
    """Handles phrases like 'jan to mar', 'jan 2025 to mar 2025', 'jan 2025 to mar'."""
    m = re.match(r"([a-z]+)(?:\s+(\d{4}))?\s+(?:to|-|until|through)\s+([a-z]+)(?:\s+(\d{4}))?$", p)
    if not m:
        return None
    start_month_name, start_year_str, end_month_name, end_year_str = m.groups()
    start_month = MONTH_NAMES.get(start_month_name) or MONTH_ABBR.get(start_month_name[:3])
    end_month = MONTH_NAMES.get(end_month_name) or MONTH_ABBR.get(end_month_name[:3])
    if not start_month or not end_month:
        return None

    # Determine the years for the start and end months
    if start_year_str:
        start_year = int(start_year_str)
    else:
        # If no year is provided, assume the most recent occurrence of the month
        start_year = today.year if start_month <= today.month else today.year - 1

    if end_year_str:
        end_year = int(end_year_str)
    else:
        # If no year is provided, assume the same year as the start month unless it has passed
        end_year = start_year if end_month >= start_month else start_year + 1

    # Ensure that the range is valid (start date should be before or equal to end date)
    if (start_year, start_month) > (end_year, end_month):
        return None

    start_date = date(start_year, start_month, 1)
    end_date = _month_end(end_year, end_month)
    return start_date, end_date



# Order matters: more specific patterns (with a year attached) must be tried
# before their "bare" counterparts.
_PARSERS: List[Callable[[str, date], Optional[DateRange]]] = [
    _p_today_yesterday,
    _p_last_n_days,
    _p_this_last_month,
    _p_last_n_months,
    _p_month_year,
    _p_fy_quarter,
    _p_quarter_year,
    _p_this_last_quarter,
    _p_last_n_quarters,
    _p_bare_quarter,
    _p_this_last_fy,
    _p_fy_year,
    _p_last_n_fy,
    _p_this_last_year,
    _p_last_n_years,
    _p_bare_year,
    _p_iso_date,
    _p_dd_mon_yyyy,
    _p_bare_month,  # last: bare month names are the loosest pattern
    _between_months_with_or_without_year
]


# ===============================================================================
# Deterministic phrase spotting (recall safety net for the LLM intent extractor)
# ===============================================================================
#
# detect_phrase() is NOT a replacement for the LLM's date_phrase extraction --
# the model still does the semantic work of deciding whether a question is
# about a date at all, and it can catch creative phrasing these fixed patterns
# don't cover. This exists to catch one specific, high-value failure mode: the
# model returning date_phrase=null (or something unparseable) when the question
# plainly contains one of these explicit, unambiguous forms.
#
# Deliberately excluded: bare 4-digit years, bare quarters ("q1"), and bare
# month names. Those are exactly the shapes that risk matching a PO number,
# plant code, or quantity instead of an actual date when searched against
# arbitrary free text -- a miss there is lower stakes than a false positive
# here, so they're left to the LLM's semantic judgement.

# Explicit alternation of real month names/abbreviations only -- using a bare
# [a-zA-Z]+ here would greedily grab whatever word precedes a 4-digit number
# ("for 2021", "code 2025"), which is exactly the false-positive shape this
# spotter is trying to avoid.
_MONTH_ALT = "|".join(
    sorted(set(MONTH_NAMES) | set(MONTH_ABBR), key=len, reverse=True)
)

_ATOMIC_PERIOD = (
    r"(?:"
    r"today|yesterday"
    r"|last\s+\d+\s+days?"
    r"|(?:this|current)\s+month|last\s+month|last\s+\d+\s+months?"
    r"|(?:this|current)\s+quarter|last\s+quarter|last\s+\d+\s+quarters?"
    r"|q\s*[1-4]\s*fy\s*\d{4}|fy\s*q\s*[1-4]\s*\d{4}|q\s*[1-4]\s*\d{4}"
    r"|(?:this|current)\s+fy|last\s+fy|fy\s*-?\s*\d{4}|last\s+\d+\s+(?:fy|financial\s+years?)"
    r"|(?:this|current)\s+year|last\s+year|last\s+\d+\s+years?"
    r"|\d{4}-\d{2}-\d{2}"
    rf"|\d{{1,2}}\s+(?:{_MONTH_ALT})\s+\d{{4}}"
    rf"|(?:{_MONTH_ALT})\s+\d{{4}}"  # month name + year, e.g. "march 2025"
    r")"
)

# A bare 4-digit year is excluded from _ATOMIC_PERIOD (too easily a material/PO/plant
# code), but "2021 to 2024" / "between 2021 and 2024" is a much safer signal: the
# explicit connector between two bare years is what makes it unambiguous, so it's
# only allowed here, inside range shapes -- never as a standalone spotted phrase.
_ATOMIC_PERIOD_OR_BARE_YEAR = _ATOMIC_PERIOD[:-1] + r"|(?:19|20)\d{2}" + ")"

# Ordered longest-shape-first: a range/open-ended match is more informative
# than the bare period it's built from, so it should win when both match.
_SPOTTER_PATTERNS: List[re.Pattern] = [
    re.compile(
        rf"\bbetween\s+{_ATOMIC_PERIOD_OR_BARE_YEAR}\s+and\s+{_ATOMIC_PERIOD_OR_BARE_YEAR}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_ATOMIC_PERIOD_OR_BARE_YEAR}\s+(?:to|till|-|until|through)\s+{_ATOMIC_PERIOD_OR_BARE_YEAR}\b",
        re.IGNORECASE,
    ),
    re.compile(rf"\b{_ATOMIC_PERIOD}\s+(?:till date|to date|till now|until now|till today)\b", re.IGNORECASE),
    re.compile(rf"\b{_ATOMIC_PERIOD}\b", re.IGNORECASE),
]


def detect_phrase(question: str) -> Optional[str]:
    """
    Best-effort deterministic scan for a date phrase embedded in a free-text
    question. Returns the matched substring (original casing preserved) or
    None if no unambiguous date phrase is present.

    Each candidate match is verified by running it back through resolve() --
    that keeps this spotter from having its own, possibly-drifted notion of
    what counts as valid; resolve()'s parsers remain the single source of
    truth, this just finds where in the sentence to look.
    """
    if not question:
        return None
    for pattern in _SPOTTER_PATTERNS:
        m = pattern.search(question)
        if not m:
            continue
        candidate = m.group(0).strip()
        try:
            if resolve(candidate) is not None:
                return candidate
        except ValueError:
            continue
    return None


# ===============================================================================
# Public API
# ===============================================================================

def resolve(phrase: Optional[str], today: Optional[date] = None) -> Optional[DateRange]:
    """
    Resolve a natural-language date phrase to (start_date, end_date), inclusive.
    Returns None if no phrase given / no date filter should be applied.
    Raises ValueError on an unrecognized but non-empty phrase (caller should decide
    whether to fall back to "no date filter" or surface an error).
    """
    if not phrase or not phrase.strip():
        return None
    p = phrase.strip().lower()
    p = re.sub(r"\s+", " ", p)
    today = today or date.today()

    # ---- open-ended "X till date" / "X to date" / "X till now" -- start from
    # X, end pinned to today ----
    till_date_match = _TILL_DATE_SUFFIX.match(p)
    if till_date_match:
        left = till_date_match.group(1).strip()
        left_r = resolve(left, today)
        if left_r:
            return left_r[0], today

    # ---- "between X and Y" range, each side parsed recursively ----
    between_match = _BETWEEN_SPLIT.match(p)
    if between_match:
        left, right = between_match.groups()
        left_r = resolve(left, today)
        right_r = resolve(right, today)
        if left_r and right_r:
            return left_r[0], right_r[1]

    # ---- explicit "X to Y" / "X - Y" / "X till Y" range, each side parsed recursively ----
    range_match = _RANGE_SPLIT.match(p)
    if range_match and not _RANGE_GUARD_WORDS.search(p):
        left, right = range_match.groups()
        left_r = resolve(left, today)
        right_r = resolve(right, today)
        if left_r and right_r:
            return left_r[0], right_r[1]

    for parser in _PARSERS:
        result = parser(p, today)
        print(f"Trying parser {parser.__name__} on phrase {p!r}: result={result}")
        if result is not None:
            return result

    raise ValueError(
        f"Unrecognized date phrase: {phrase!r}. Supported forms include 'today', 'yesterday', "
        f"'last 7 days', 'this month', 'last month', 'last 3 months', 'March 2021', 'q1 2025', "
        f"'last quarter', 'last 2 quarters', 'this fy', 'last fy', 'FY2025', '2025', "
        f"'last 3 years', '2021 to 2024', '2025-01-31', '2021 till 2024', "
        f"'between jan 2025 and mar 2025', 'FY2024 till date', 'march 2025 to date'. "
        f"Note that 'month on month' / 'quarter on quarter' / 'year on year' are time "
        f"grains, not periods."
    )


def resolve_period(phrase: Optional[str], today: Optional[date] = None) -> Optional[Period]:
    """Same as resolve(), but returns a labelled Period instead of a bare tuple."""
    r = resolve(phrase, today)
    if r is None:
        return None
    start, end = r
    return Period(start, end, _label_for(start, end))


def current_fy_range(today: Optional[date] = None) -> DateRange:
    """Bounds of the financial year (1 Apr .. 31 Mar) that `today` falls inside."""
    today = today or date.today()
    return _fy_bounds(_current_fy_start_year(today))


def current_fy_period(today: Optional[date] = None) -> Period:
    """Current FY as a labelled Period, e.g. FY2026-27 for any date in Apr 2026 .. Mar 2027.

    Used as the implicit window for trend queries where the user named a grain
    ("month over month") but no period -- an unbounded trend scans the whole table
    and buries the recent months the question is actually about.
    """
    start, end = current_fy_range(today)
    return Period(start, end, _label_for(start, end))


def _fy_label(fy_start_year: int) -> str:
    return f"FY{fy_start_year}-{str(fy_start_year + 1)[2:]}"


def recent_fy_span(years: int = 3, today: Optional[date] = None) -> Period:
    """The last `years` financial years, ending with (and including) the current one.

    A year-grain trend confined to a single FY is one bucket, which can't express a
    year-over-year change -- so YoY needs a window spanning several FYs. years=1 gives
    exactly current_fy_period().
    """
    if years < 1:
        raise ValueError("years must be >= 1")
    today = today or date.today()
    current = _current_fy_start_year(today)
    first = current - (years - 1)
    start, _ = _fy_bounds(first)
    _, end = _fy_bounds(current)
    label = _fy_label(current) if years == 1 else f"{_fy_label(first)} to {_fy_label(current)}"
    return Period(start, end, label)


# ===============================================================================
# MoM / QoQ / YoY comparisons
# ===============================================================================

def month_over_month(today: Optional[date] = None, include_current: bool = False) -> ComparisonResult:
    """
    MoM comparison.
    include_current=False (default): compares the last two *complete* months
        (last month vs the month before that) -- avoids biasing on a partial
        current month.
    include_current=True: compares this (partial) month vs last month.
    """
    today = today or date.today()
    offset = 0 if include_current else 1
    cur_start, cur_end = _month_bounds_relative(today, offset)
    prev_start, prev_end = _month_bounds_relative(today, offset + 1)
    return ComparisonResult(
        kind="MoM",
        current=Period(cur_start, cur_end, _label_for(cur_start, cur_end)),
        previous=Period(prev_start, prev_end, _label_for(prev_start, prev_end)),
    )


def quarter_over_quarter(today: Optional[date] = None, include_current: bool = False) -> ComparisonResult:
    """
    QoQ comparison (calendar quarters).
    include_current=False (default): last complete quarter vs the one before it.
    include_current=True: this (partial) quarter vs last quarter.
    """
    today = today or date.today()
    q = _which_calendar_quarter(today)
    year = today.year

    if include_current:
        cur_q, cur_year = q, year
    else:
        cur_q, cur_year = q - 1, year
        if cur_q == 0:
            cur_q, cur_year = 4, year - 1

    prev_q, prev_year = cur_q - 1, cur_year
    if prev_q == 0:
        prev_q, prev_year = 4, cur_year - 1

    cur_start, cur_end = _quarter_bounds(cur_year, cur_q)
    prev_start, prev_end = _quarter_bounds(prev_year, prev_q)
    return ComparisonResult(
        kind="QoQ",
        current=Period(cur_start, cur_end, _label_for(cur_start, cur_end)),
        previous=Period(prev_start, prev_end, _label_for(prev_start, prev_end)),
    )


def year_over_year(
    today: Optional[date] = None,
    include_current: bool = False,
    fiscal: bool = False,
) -> ComparisonResult:
    """
    YoY comparison.
    fiscal=False (default): calendar years.
    fiscal=True: financial years (Apr-Mar).
    include_current=False (default): last complete year vs the year before it.
    include_current=True: this (partial) year vs last year.
    """
    today = today or date.today()

    if fiscal:
        cur_fy = _current_fy_start_year(today) - (0 if include_current else 1)
        prev_fy = cur_fy - 1
        cur_start, cur_end = _fy_bounds(cur_fy)
        prev_start, prev_end = _fy_bounds(prev_fy)
    else:
        cur_year = today.year - (0 if include_current else 1)
        prev_year = cur_year - 1
        cur_start, cur_end = _cy_bounds(cur_year)
        prev_start, prev_end = _cy_bounds(prev_year)

    return ComparisonResult(
        kind="YoY",
        current=Period(cur_start, cur_end, _label_for(cur_start, cur_end)),
        previous=Period(prev_start, prev_end, _label_for(prev_start, prev_end)),
    )


def resolve_comparison(phrase: str, today: Optional[date] = None) -> ComparisonResult:
    """
    Parse a comparison phrase into a ComparisonResult.
    Recognised phrases (case-insensitive, flexible spacing):
        "mom", "mom current"
        "qoq", "qoq current"
        "yoy", "yoy current", "yoy fy", "yoy fy current"
    "current" includes the in-progress current period instead of comparing
    the last two complete periods.
    """
    p = re.sub(r"\s+", " ", phrase.strip().lower())
    tokens = set(p.split())
    include_current = "current" in tokens
    fiscal = "fy" in tokens

    if "mom" in tokens:
        return month_over_month(today, include_current=include_current)
    if "qoq" in tokens:
        return quarter_over_quarter(today, include_current=include_current)
    if "yoy" in tokens:
        return year_over_year(today, include_current=include_current, fiscal=fiscal)

    raise ValueError(f"Unrecognized comparison phrase: {phrase!r}")


# ===============================================================================
# SQL helpers
# ===============================================================================

def time_grain_trunc_expr(column: str, grain: str, fiscal: bool = False) -> str:
    """Trino date_trunc expression for month/quarter/year trend queries.

    fiscal=True only changes the *year* grain, where it buckets by financial year
    (Apr-Mar) instead of calendar year. That matters because a calendar-year bucket
    over a fiscal window splits the first and last FY into partial buckets, which makes
    the year-over-year change read as a huge jump that's really just 9 months vs 12.
    Month and quarter need no fiscal variant: FY quarters (Apr-Jun, Jul-Sep, ...) have
    exactly the same boundaries as calendar quarters, only a different number.
    """
    grain = grain.lower()
    if grain not in ("month", "quarter", "year"):
        raise ValueError(f"Unsupported time grain: {grain}")
    parsed = f"TRY(date_parse(CAST({column} AS VARCHAR), '%Y%m%d'))"
    if grain == "year" and fiscal:
        # Shift back 3 months so Apr-Mar falls inside one calendar year, truncate, then
        # shift forward again so each bucket is stamped with its own 1 Apr start date.
        return f"date_add('month', 3, date_trunc('year', date_add('month', -3, {parsed})))"
    return f"date_trunc('{grain}', {parsed})"