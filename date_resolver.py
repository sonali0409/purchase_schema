"""
Resolves natural-language date phrases into concrete (start_date, end_date) pairs,
and builds structured Month-over-Month / Quarter-over-Quarter / Year-over-Year
comparison periods.

-------------------------------------------------------------------------------
FINANCIAL YEAR (FY) ONLY -- Indian convention, 1 Apr -> 31 Mar
-------------------------------------------------------------------------------
- Financial Year: 1 Apr -> 31 Mar of next calendar year.
    "FY2025"                 -> 2025-04-01 .. 2026-03-31
    "this fy" / "current fy" -> current FY
    "last fy"                -> previous FY
    "2025"                   -> FY2025 (2025-04-01 .. 2026-03-31)

- FY quarter: Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar (of next CY).
    "q1 2025"       -> 2025-04-01 .. 2025-06-30
    "q4 2025"       -> 2026-01-01 .. 2026-03-31

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

# ADDED: flip to True to see which parser matched which phrase (debugging only).
# Kept OFF by default so it never slows down the hot path. Re-enable temporarily
# when investigating a "phrase didn't match" bug.
DEBUG = False

MONTH_NAMES = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTH_ABBR = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}

# Words that, if present, mean the phrase is describing a single relative
# period rather than an "X to Y" range -- so we don't try to split on "-".
# Example: "last 3 months" or "this year" shouldn't be range-split.
_RANGE_GUARD_WORDS = re.compile(r"\blast\b|\bthis\b|\bcurrent\b")

# CHANGED: strip an optional leading "from" so
#   "from 13 march 2025 till date"  ->  ("13 march 2025", "date")
# Without this, the splitter sees left="from march 2026" (fails to parse)
# and right="date" (fails to parse), and the whole phrase becomes unrecognised.
_RANGE_SPLIT = re.compile(r"^(?:from\s+)?(.*?)\s+(?:to|until|through|till)\s+(.*)$")

# "between X and Y" -- unambiguous syntax (the word "between" itself signals a
# range), so it's checked ahead of, and isn't itself subject to, the guard
# words above.
_BETWEEN_SPLIT = re.compile(r"^between\s+(.*?)\s+and\s+(.*)$")

# ADDED: "X and Y" for bare conjunction ranges like "q1 and q2", "may and june 2023".
# Tried after the specific parsers so unambiguous phrases win first.
_AND_SPLIT = re.compile(r"^(.+?)\s+and\s+(.+)$")

# CHANGED: allow optional leading "from" so "from march 2026 till date" works.
# Open-ended phrases pin the right side to today.
_TILL_DATE_SUFFIX = re.compile(
    r"^(?:from\s+)?(.*?)\s+(?:till date|to date|till now|until now|till today)$"
)


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
    """Last day of the given month, e.g. _month_end(2026, 2) -> 2026-02-28."""
    return date(y, m, calendar.monthrange(y, m)[1])


def _fy_bounds(fy_start_year: int) -> DateRange:
    """FY that starts 1 Apr of fy_start_year and ends 31 Mar of fy_start_year+1.

    Example: _fy_bounds(2026) -> (2026-04-01, 2027-03-31)
    """
    return date(fy_start_year, 4, 1), date(fy_start_year + 1, 3, 31)


# Kept as an alias so existing imports of _cy_bounds still work.
# In FY-only semantics, a bare 4-digit year means its FY.
def _cy_bounds(year: int) -> DateRange:
    """Alias for _fy_bounds. Kept for backwards compatibility."""
    return _fy_bounds(year)


def _current_fy_start_year(today: date) -> int:
    """Which FY does `today` fall inside? Returns that FY's start year.

    FY runs Apr -> Mar. So a date in Apr-Dec of year Y belongs to FY Y;
    a date in Jan-Mar of year Y belongs to FY (Y-1).

    Example: 2026-09-22 -> 2026 (FY2026-27)
             2026-02-15 -> 2025 (FY2025-26)
    """
    return today.year if today.month >= 4 else today.year - 1


def _quarter_bounds(fy_year: int, quarter: int) -> DateRange:
    """FY quarter bounds: Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar (next CY).

    Example: _quarter_bounds(2025, 1) -> (2025-04-01, 2025-06-30)
             _quarter_bounds(2025, 4) -> (2026-01-01, 2026-03-31)
    """
    if quarter not in (1, 2, 3, 4):
        raise ValueError("Quarter must be between 1 and 4.")
    start_months = {1: 4, 2: 7, 3: 10, 4: 1}
    start_month = start_months[quarter]
    year = fy_year if quarter < 4 else fy_year + 1
    end_month = start_month + 2 if quarter < 4 else 3
    return date(year, start_month, 1), _month_end(year, end_month)


def _fy_quarter_bounds(fy_start_year: int, fy_quarter: int) -> DateRange:
    """Same as _quarter_bounds -- kept for API compatibility."""
    return _quarter_bounds(fy_start_year, fy_quarter)


def _which_calendar_quarter(d: date) -> int:
    """Name kept for compatibility, but returns the FY quarter number.

    Given a date, returns which FY quarter it falls in (1..4).
    """
    return _which_fy_quarter(d)[1]


def _which_fy_quarter(d: date) -> Tuple[int, int]:
    """Returns (fy_start_year, fy_quarter) for a given date.

    month_in_fy: how many months since April. April=0, May=1, ..., March=11.
    fy_quarter:  0-2 months -> Q1, 3-5 -> Q2, 6-8 -> Q3, 9-11 -> Q4.
    """
    fy_start_year = _current_fy_start_year(d)
    month_in_fy = (d.month - 4) % 12  # 0..11, 0 = April
    fy_quarter = month_in_fy // 3 + 1
    return fy_start_year, fy_quarter


def _month_bounds_relative(anchor: date, months_back: int) -> DateRange:
    """Bounds of the whole month that is `months_back` months before anchor's month
    (months_back=0 -> anchor's own month).

    Example (anchor = 2026-09-22):
        _month_bounds_relative(anchor, 0) -> (2026-09-01, 2026-09-30)
        _month_bounds_relative(anchor, 1) -> (2026-08-01, 2026-08-31)
        _month_bounds_relative(anchor, 3) -> (2026-06-01, 2026-06-30)
    """
    first_of_anchor_month = date(anchor.year, anchor.month, 1)
    target_first = first_of_anchor_month - relativedelta(months=months_back)
    return date(target_first.year, target_first.month, 1), _month_end(target_first.year, target_first.month)


# ADDED: Monday..Sunday of the week N weeks back. weeks_back=0 means this week.
# weekday() returns 0 for Monday, so subtracting it snaps to Monday.
def _week_bounds(anchor: date, weeks_back: int = 0) -> DateRange:
    monday = anchor - timedelta(days=anchor.weekday()) - timedelta(weeks=weeks_back)
    return monday, monday + timedelta(days=6)


# ===============================================================================
# Labelling
# ===============================================================================

def _label_for(start: date, end: date) -> str:
    """Human-readable label for a date span, used by resolve_period / comparisons.

    Priority order matters -- the first matching shape wins, so more specific
    patterns (FY quarter, FY) are checked before looser ones (single month).
    """
    if start == end:
        return start.isoformat()

    # FY: Apr 1 .. Mar 31 of next year
    if (start.month, start.day) == (4, 1) and (end.month, end.day) == (3, 31) and end.year == start.year + 1:
        return f"FY{start.year}-{str(end.year)[2:]}"

    # FY Q1: Apr-Jun
    if start.day == 1 and start.month == 4 and end == _month_end(start.year, 6):
        return f"Q1 FY{start.year}-{str(start.year + 1)[2:]}"

    # FY Q2: Jul-Sep
    if start.day == 1 and start.month == 7 and end == _month_end(start.year, 9):
        return f"Q2 FY{start.year}-{str(start.year + 1)[2:]}"

    # FY Q3: Oct-Dec
    if start.day == 1 and start.month == 10 and end == _month_end(start.year, 12):
        return f"Q3 FY{start.year}-{str(start.year + 1)[2:]}"

    # FY Q4: Jan-Mar (belongs to previous FY)
    if start.day == 1 and start.month == 1 and end == _month_end(start.year, 3):
        fy = start.year - 1
        return f"Q4 FY{fy}-{str(start.year)[2:]}"

    # Single whole month
    if (start.day == 1 and start.year == end.year and start.month == end.month
            and end == _month_end(end.year, end.month)):
        return f"{calendar.month_name[start.month]} {start.year}"

    return f"{start.isoformat()} to {end.isoformat()}"


# ===============================================================================
# Phrase parsers
#
# Each parser takes (phrase, today) and returns a DateRange or None.
# resolve() tries them in order and returns the first non-None result.
# ===============================================================================

def _p_today_yesterday(p: str, today: date) -> Optional[DateRange]:
    """Handle 'today' and 'yesterday'."""
    if p == "yesterday":
        y = today - timedelta(days=1)
        return y, y
    if p == "today":
        return today, today
    return None


# ADDED: this week / last week
def _p_this_last_week(p: str, today: date) -> Optional[DateRange]:
    """Handle 'this week', 'current week', 'last week' (Mon-Sun)."""
    if p in ("this week", "current week"):
        return _week_bounds(today, 0)
    if p == "last week":
        return _week_bounds(today, 1)
    return None


def _p_last_n_days(p: str, today: date) -> Optional[DateRange]:
    """Handle 'last N days' (excludes today, includes N-1 complete days)."""
    m = re.match(r"last (\d+) days?$", p)
    if not m:
        return None
    n = int(m.group(1))
    # excludes today, per user-flagged expectation
    end = today - timedelta(days=1)
    start = end - timedelta(days=n - 1)
    return start, end


def _p_this_last_month(p: str, today: date) -> Optional[DateRange]:
    """Handle 'this month' / 'current month' / 'last month'."""
    if p in ("this month", "current month"):
        return date(today.year, today.month, 1), _month_end(today.year, today.month)
    if p == "last month":
        return _month_bounds_relative(today, 1)
    return None


def _p_last_n_months(p: str, today: date) -> Optional[DateRange]:
    """'last N months' = N complete months ending last month.

    For a question asked in Sep, 'last 3 months' = Jun, Jul, Aug.
    Excludes the current (incomplete) month intentionally.
    """
    m = re.match(r"last (\d+) months?$", p)
    if not m:
        return None
    n = int(m.group(1))
    _, end = _month_bounds_relative(today, 1)     # last complete month
    start, _ = _month_bounds_relative(today, n)   # N months back
    return start, end


def _p_month_year(p: str, today: date) -> Optional[DateRange]:
    """Handle 'march 2025', 'mar 2025' (month + year)."""
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
    """Handle a bare month name like 'march' or 'mar'.

    Returns the most recent occurrence: this year if that month hasn't
    passed yet in `today`'s year, else last year.
    """
    mo = MONTH_NAMES.get(p) or MONTH_ABBR.get(p)
    if mo:
        yr = today.year if mo <= today.month else today.year - 1
        return date(yr, mo, 1), _month_end(yr, mo)
    return None


def _p_this_last_quarter(p: str, today: date) -> Optional[DateRange]:
    """Handle 'this quarter' / 'current quarter' / 'last quarter' (FY quarters)."""
    if p in ("this quarter", "current quarter"):
        fy_year, q = _which_fy_quarter(today)
        return _quarter_bounds(fy_year, q)
    if p == "last quarter":
        fy_year, q = _which_fy_quarter(today)
        q -= 1
        if q == 0:
            q, fy_year = 4, fy_year - 1
        return _quarter_bounds(fy_year, q)
    return None


def _p_last_n_quarters(p: str, today: date) -> Optional[DateRange]:
    """Handle 'last N quarters' = N complete FY quarters ending last quarter."""
    m = re.match(r"last (\d+) quarters?$", p)
    if not m:
        return None
    n = int(m.group(1))
    fy_year, q = _which_fy_quarter(today)
    # end = previous complete FY quarter
    end_q, end_fy = q - 1, fy_year
    if end_q == 0:
        end_q, end_fy = 4, fy_year - 1
    _, end_date = _quarter_bounds(end_fy, end_q)
    start_q, start_fy = end_q, end_fy
    for _ in range(n - 1):
        start_q -= 1
        if start_q == 0:
            start_q, start_fy = 4, start_fy - 1
    start_date, _ = _quarter_bounds(start_fy, start_q)
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
    """Quarter + year, e.g. 'q1 2025'. Interpreted as FY quarter."""
    m = re.match(r"q\s*([1-4])\s*(\d{4})$", p)
    if m:
        return _quarter_bounds(int(m.group(2)), int(m.group(1)))
    return None


def _p_bare_quarter(p: str, today: date) -> Optional[DateRange]:
    """Bare quarter, e.g. 'q1' -> current FY's Q1."""
    m = re.match(r"q\s*([1-4])$", p)
    if m:
        fy_year = _current_fy_start_year(today)
        return _quarter_bounds(fy_year, int(m.group(1)))
    return None


def _p_this_last_fy(p: str, today: date) -> Optional[DateRange]:
    """Handle 'this fy' / 'current fy' / 'last fy' and variants."""
    if p in ("this fy", "current fy", "this financial year", "current financial year"):
        return _fy_bounds(_current_fy_start_year(today))
    if p in ("last fy", "last financial year", "previous fy"):
        return _fy_bounds(_current_fy_start_year(today) - 1)
    return None


def _p_fy_year(p: str, today: date) -> Optional[DateRange]:
    """Handle 'FY2025', 'FY 2025-26' style."""
    m = re.match(r"fy\s*-?\s*(\d{4})", p)
    if m:
        return _fy_bounds(int(m.group(1)))
    return None


def _p_last_n_fy(p: str, today: date) -> Optional[DateRange]:
    """Handle 'last N fy' or 'last N financial years' = N complete FYs ending last FY."""
    m = re.match(r"last (\d+) (?:fy|financial years?)$", p)
    if not m:
        return None
    n = int(m.group(1))
    cur = _current_fy_start_year(today)
    start, _ = _fy_bounds(cur - n)
    _, end = _fy_bounds(cur - 1)
    return start, end


def _p_this_last_year(p: str, today: date) -> Optional[DateRange]:
    """Handle 'this year' / 'current year' / 'last year'.

    In FY-only semantics, these map to FY, not calendar year.
    """
    if p in ("this year", "current year"):
        return _fy_bounds(_current_fy_start_year(today))
    if p == "last year":
        return _fy_bounds(_current_fy_start_year(today) - 1)
    return None


def _p_bare_year(p: str, today: date) -> Optional[DateRange]:
    """Bare 4-digit year -> its FY. Example: '2025' -> 2025-04-01 .. 2026-03-31."""
    m = re.match(r"(\d{4})$", p)
    if m:
        return _fy_bounds(int(m.group(1)))
    return None


def _p_last_n_years(p: str, today: date) -> Optional[DateRange]:
    """Handle 'last N years' = N complete FYs ending last FY."""
    m = re.match(r"last (\d+) years?$", p)
    if not m:
        return None
    n = int(m.group(1))
    cur = _current_fy_start_year(today)
    start, _ = _fy_bounds(cur - n)
    _, end = _fy_bounds(cur - 1)
    return start, end


def _p_iso_date(p: str, today: date) -> Optional[DateRange]:
    """Handle an ISO date like '2025-01-31' -> single-day range."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", p)
    if m:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d, d
    return None


def _p_dd_mon_yyyy(p: str, today: date) -> Optional[DateRange]:
    """Handle a single-day phrase like '31 jan 2025' or '5 mar 2024'."""
    m = re.match(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})$", p)
    if m and m.group(2)[:3] in MONTH_ABBR:
        d = date(int(m.group(3)), MONTH_ABBR[m.group(2)[:3]], int(m.group(1)))
        return d, d
    return None


# ADDED: "1 march 2026 to 31 march 2026", "2025-01-01 to 2025-12-31".
# Both sides are parsed via resolve() so any single-date form works.
def _p_date_to_date(p: str, today: date) -> Optional[DateRange]:
    m = re.match(
        r"^(\d{1,2}\s+[a-z]+\s+\d{4}|\d{4}-\d{2}-\d{2})"
        r"\s+(?:to|until|through|till|-)\s+"
        r"(\d{1,2}\s+[a-z]+\s+\d{4}|\d{4}-\d{2}-\d{2})$",
        p,
    )
    if not m:
        return None
    left = resolve(m.group(1), today)
    right = resolve(m.group(2), today)
    if left and right:
        return left[0], right[1]
    return None


def _between_months_with_or_without_year(p: str, today: date) -> Optional[DateRange]:
    """Handles phrases like 'jan to mar', 'jan 2025 to mar 2025', 'jan 2025 to mar'."""
    m = re.match(r"([a-z]+)(?:\s+(\d{4}))?\s+(?:to|-|until|through|till)\s+([a-z]+)(?:\s+(\d{4}))?$", p)
    if not m:
        return None
    start_month_name, start_year_str, end_month_name, end_year_str = m.groups()
    start_month = MONTH_NAMES.get(start_month_name) or MONTH_ABBR.get(start_month_name[:3])
    end_month = MONTH_NAMES.get(end_month_name) or MONTH_ABBR.get(end_month_name[:3])
    if not start_month or not end_month:
        return None

    if start_year_str:
        start_year = int(start_year_str)
    else:
        start_year = today.year if start_month <= today.month else today.year - 1

    if end_year_str:
        end_year = int(end_year_str)
    else:
        end_year = start_year if end_month >= start_month else start_year + 1

    if (start_year, start_month) > (end_year, end_month):
        return None

    return date(start_year, start_month, 1), _month_end(end_year, end_month)


# ADDED: "q1 and q2 2025" / "q1 and q2" / "may and june 2023" / "may and june".
# Each side is parsed by resolve(), so "q1" resolves to current FY's Q1,
# "june 2023" resolves to June 2023, and the two are merged into one range.
def _p_conjunction(p: str, today: date) -> Optional[DateRange]:
    m = _AND_SPLIT.match(p)
    if not m:
        return None
    left_phrase, right_phrase = m.group(1).strip(), m.group(2).strip()
    left = resolve(left_phrase, today)
    right = resolve(right_phrase, today)
    if left and right:
        return left[0], right[1]
    return None


# ===============================================================================
# Parser registry (order matters: more specific patterns first)
# ===============================================================================

_PARSERS: List[Callable[[str, date], Optional[DateRange]]] = [
    _p_today_yesterday,
    _p_this_last_week,               # ADDED
    _p_last_n_days,
    _p_this_last_month,
    _p_last_n_months,
    _p_date_to_date,                 # ADDED -- before single-date parsers
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
    _between_months_with_or_without_year,
    _p_bare_month,
    _p_conjunction,                  # ADDED -- loosest, tried last
]


# ===============================================================================
# Deterministic phrase spotting (recall safety net for the LLM intent extractor)
# ===============================================================================
#
# detect_phrase() is NOT a replacement for the LLM's date_phrase extraction --
# the model still does the semantic work of deciding whether a question is
# about a date at all. This exists to catch one specific, high-value failure
# mode: the model returning date_phrase=null when the question plainly
# contains one of these explicit, unambiguous forms.
#
# Deliberately excluded: bare 4-digit years, bare quarters ("q1"), and bare
# month names. Those are exactly the shapes that risk matching a PO number,
# plant code, or quantity instead of an actual date when searched against
# arbitrary free text.

_MONTH_ALT = "|".join(sorted(set(MONTH_NAMES) | set(MONTH_ABBR), key=len, reverse=True))

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
    rf"|(?:{_MONTH_ALT})\s+\d{{4}}"
    r")"
)

# A bare 4-digit year is excluded from _ATOMIC_PERIOD (too easily a material/PO/plant
# code), but "2021 to 2024" / "between 2021 and 2024" is a much safer signal: the
# explicit connector between two bare years is what makes it unambiguous.
_ATOMIC_PERIOD_OR_BARE_YEAR = _ATOMIC_PERIOD[:-1] + r"|(?:19|20)\d{2}" + ")"

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
    """Best-effort deterministic scan for a date phrase embedded in free text.

    Returns the matched substring (original casing preserved) or None.
    Each candidate is verified by running it back through resolve() so this
    spotter never has its own drifted notion of validity.
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
    Raises ValueError on an unrecognized but non-empty phrase.

    The caller (question pipeline) should use resolve_or_default() if it wants
    the "no phrase -> Current FY" behaviour; use resolve() when you want to
    notice bugs by seeing the ValueError.
    """
    if not phrase or not phrase.strip():
        return None
    p = phrase.strip().lower()
    p = re.sub(r"\s+", " ", p)
    today = today or date.today()

    # CHANGED: strip a leading "from " once, so nothing downstream sees it.
    # Example: "from march 2026 to date" -> "march 2026 to date"
    if p.startswith("from "):
        p = p[5:].strip()

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

    # ---- "X and Y" range (e.g. "q1 and q2 2025") ----  # ADDED
    and_match = _AND_SPLIT.match(p)
    if and_match and not _RANGE_GUARD_WORDS.search(p):
        left, right = and_match.groups()
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
        if result is not None:
            if DEBUG:                                    # ADDED
                print(f"[date_resolver] {parser.__name__} matched {p!r} -> {result}")
            return result

    raise ValueError(
        f"Unrecognized date phrase: {phrase!r}. Supported forms include 'today', "
        f"'yesterday', 'this week', 'last week', 'last 7 days', 'this month', "
        f"'last month', 'last 3 months', 'March 2021', 'q1 2025', 'q1 and q2 2025', "
        f"'last quarter', 'last 2 quarters', 'this fy', 'last fy', 'FY2025', '2025', "
        f"'last 3 years', '2021 to 2024', '1 march 2026 to 31 march 2026', "
        f"'2025-01-31', '2021 till 2024', 'between jan 2025 and mar 2025', "
        f"'FY2024 till date', 'march 2025 to date', 'from march 2026 till date', "
        f"'may and june 2023'."
    )


def resolve_period(phrase: Optional[str], today: Optional[date] = None) -> Optional[Period]:
    """Same as resolve(), but returns a labelled Period instead of a bare tuple."""
    r = resolve(phrase, today)
    if r is None:
        return None
    start, end = r
    return Period(start, end, _label_for(start, end))


# ADDED: default-aware entry point. Missing/empty/unrecognized -> Current FY.
def resolve_or_default(phrase: Optional[str], today: Optional[date] = None) -> DateRange:
    """Resolve a phrase; on missing/unrecognized input, return Current FY.

    This is what the question pipeline should call. It always returns a valid
    (start, end) tuple -- never None -- so callers don't need to special-case
    "no date phrase in the question".
    """
    if not phrase or not phrase.strip():
        return current_fy_range(today)
    try:
        r = resolve(phrase, today)
    except ValueError:
        return current_fy_range(today)
    return r if r is not None else current_fy_range(today)


def current_fy_range(today: Optional[date] = None) -> DateRange:
    """Bounds of the financial year (1 Apr .. 31 Mar) that `today` falls inside."""
    today = today or date.today()
    return _fy_bounds(_current_fy_start_year(today))


def current_fy_period(today: Optional[date] = None) -> Period:
    """Current FY as a labelled Period.

    Used as the implicit window for trend queries where the user named a grain
    ("month over month") but no period -- an unbounded trend scans the whole
    table and buries the recent months the question is actually about.
    """
    start, end = current_fy_range(today)
    return Period(start, end, _label_for(start, end))


def _fy_label(fy_start_year: int) -> str:
    return f"FY{fy_start_year}-{str(fy_start_year + 1)[2:]}"


def recent_fy_span(years: int = 3, today: Optional[date] = None) -> Period:
    """The last `years` financial years, ending with (and including) the current one.

    A year-grain trend confined to a single FY is one bucket, which can't express a
    year-over-year change -- so YoY needs a window spanning several FYs.
    years=1 gives exactly current_fy_period().
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
    QoQ comparison (FY quarters).
    include_current=False (default): last complete FY quarter vs the one before it.
    include_current=True: this (partial) FY quarter vs last quarter.
    """
    today = today or date.today()
    fy_year, q = _which_fy_quarter(today)

    if include_current:
        cur_q, cur_fy = q, fy_year
    else:
        cur_q, cur_fy = q - 1, fy_year
        if cur_q == 0:
            cur_q, cur_fy = 4, fy_year - 1

    prev_q, prev_fy = cur_q - 1, cur_fy
    if prev_q == 0:
        prev_q, prev_fy = 4, cur_fy - 1

    cur_start, cur_end = _quarter_bounds(cur_fy, cur_q)
    prev_start, prev_end = _quarter_bounds(prev_fy, prev_q)
    return ComparisonResult(
        kind="QoQ",
        current=Period(cur_start, cur_end, _label_for(cur_start, cur_end)),
        previous=Period(prev_start, prev_end, _label_for(prev_start, prev_end)),
    )


def year_over_year(
    today: Optional[date] = None,
    include_current: bool = False,
    fiscal: bool = True,     # CHANGED: default to fiscal, since we're FY-only
) -> ComparisonResult:
    """
    YoY comparison (FY based, since we're FY-only).
    include_current=False (default): last complete FY vs the FY before it.
    include_current=True: this (partial) FY vs last FY.
    """
    today = today or date.today()

    cur_fy = _current_fy_start_year(today) - (0 if include_current else 1)
    prev_fy = cur_fy - 1
    cur_start, cur_end = _fy_bounds(cur_fy)
    prev_start, prev_end = _fy_bounds(prev_fy)

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

    if "mom" in tokens:
        return month_over_month(today, include_current=include_current)
    if "qoq" in tokens:
        return quarter_over_quarter(today, include_current=include_current)
    if "yoy" in tokens:
        return year_over_year(today, include_current=include_current, fiscal=True)

    raise ValueError(f"Unrecognized comparison phrase: {phrase!r}")


# ===============================================================================
# Grain vocabulary
# ===============================================================================

# ADDED: grain vocabulary. Maps every alias a user might say to a canonical grain.
# This is NOT a date range -- it identifies the time bucket the SQL should group by.
# Callers should normalize grains through normalize_grain() before building SQL.
_GRAIN_ALIASES = {
    "mom": "MoM", "month on month": "MoM", "monthly": "MoM",
    "month wise": "MoM", "month-wise": "MoM", "month over month": "MoM",
    "qoq": "QoQ", "quarter on quarter": "QoQ", "quarterly": "QoQ",
    "quarter wise": "QoQ", "quarter-wise": "QoQ", "quarter over quarter": "QoQ",
    "yoy": "YoY", "year on year": "YoY", "yearly": "YoY",
    "year wise": "YoY", "year-wise": "YoY", "year over year": "YoY",
}


def normalize_grain(phrase: Optional[str]) -> Optional[str]:
    """Return 'MoM' / 'QoQ' / 'YoY' if the phrase names a comparison grain, else None.

    Example:
        normalize_grain("year wise") -> "YoY"
        normalize_grain("qoq")       -> "QoQ"
        normalize_grain("march")     -> None
    """
    if not phrase:
        return None
    return _GRAIN_ALIASES.get(re.sub(r"\s+", " ", phrase.strip().lower()))


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