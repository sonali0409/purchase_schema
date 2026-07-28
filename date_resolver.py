"""
Resolves natural-language date phrases into concrete (start_date, end_date) pairs.

Indian Financial Year convention: FY starts 1 April, ends 31 March.
e.g. "FY2025" / "last FY" as of July 2026 -> FY2025-26 = 2025-04-01 .. 2026-03-31

All dates returned as datetime.date, inclusive on both ends.
"""
from __future__ import annotations
import re
import calendar
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from typing import Optional, Tuple

MONTH_NAMES = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTH_ABBR = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _fy_bounds(fy_start_year: int) -> Tuple[date, date]:
    """FY that starts 1 Apr of fy_start_year and ends 31 Mar of fy_start_year+1."""
    return date(fy_start_year, 4, 1), date(fy_start_year + 1, 3, 31)


def _current_fy_start_year(today: date) -> int:
    return today.year if today.month >= 4 else today.year - 1


def _quarter_bounds(year: int, quarter_calendar: int) -> Tuple[date, date]:
    """Standard calendar quarter (Q1=Jan-Mar ... Q4=Oct-Dec)."""
    start_month = (quarter_calendar - 1) * 3 + 1
    end_month = start_month + 2
    return date(year, start_month, 1), _month_end(year, end_month)


def _fy_quarter_bounds(fy_start_year: int, fy_quarter: int) -> Tuple[date, date]:
    """FY quarter: Q1=Apr-Jun, Q2=Jul-Sep, Q3=Oct-Dec, Q4=Jan-Mar (of fy_start_year+1)."""
    offsets = {1: (4, fy_start_year), 2: (7, fy_start_year), 3: (10, fy_start_year), 4: (1, fy_start_year + 1)}
    start_month, y = offsets[fy_quarter]
    return date(y, start_month, 1), _month_end(y, start_month + 2)


def _which_calendar_quarter(d: date) -> int:
    return (d.month - 1) // 3 + 1


def _which_fy_quarter(d: date) -> Tuple[int, int]:
    """Returns (fy_start_year, fy_quarter) for a given date."""
    fy_start_year = _current_fy_start_year(d)
    month_in_fy = (d.month - 4) % 12  # 0..11, 0 = April
    fy_quarter = month_in_fy // 3 + 1
    return fy_start_year, fy_quarter


def resolve(phrase: Optional[str], today: Optional[date] = None) -> Optional[Tuple[date, date]]:
    """
    Resolve a natural-language date phrase to (start_date, end_date), inclusive.
    Returns None if no phrase given / no date filter should be applied.
    Raises ValueError on an unrecognized but non-empty phrase (caller should decide
    whether to fall back to "no date filter" or surface an error).
    """
    if not phrase or not phrase.strip():
        return None
    p = phrase.strip().lower()
    today = today or date.today()

    # ---- explicit "X to Y" / "X - Y" range, each side parsed recursively ----
    range_match = re.match(r'^(.*?)\s+(?:to|-|until|through)\s+(.*)$', p)
    if range_match and not re.search(r'\blast\b|\bthis\b|\bcurrent\b', p):
        left, right = range_match.groups()
        left_r = resolve(left, today)
        right_r = resolve(right, today)
        if left_r and right_r:
            return left_r[0], right_r[1]

    # ---- yesterday / today ----
    if p == 'yesterday':
        y = today - timedelta(days=1)
        return y, y
    if p == 'today':
        return today, today

    # ---- last N days ----
    m = re.match(r'last (\d+) days?', p)
    if m:
        n = int(m.group(1))
        # "last N days" excludes today per user-flagged expectation (e.g. "shouldn't include current day")
        end = today - timedelta(days=1)
        start = end - timedelta(days=n - 1)
        return start, end

    # ---- last N months ----
    m = re.match(r'last (\d+) months?', p)
    if m:
        n = int(m.group(1))
        end = today
        start = today - relativedelta(months=n)
        return start, end

    # ---- this / current month ----
    if p in ('this month', 'current month'):
        return date(today.year, today.month, 1), _month_end(today.year, today.month)

    # ---- last month ----
    if p == 'last month':
        first_of_this = date(today.year, today.month, 1)
        last_month_end = first_of_this - timedelta(days=1)
        return date(last_month_end.year, last_month_end.month, 1), last_month_end

    # ---- explicit "Month Year" e.g. "March 2021", "april 2026" ----
    m = re.match(r'([a-z]+)\s+(\d{4})$', p)
    if m and m.group(1) in MONTH_NAMES:
        mo = MONTH_NAMES[m.group(1)]
        yr = int(m.group(2))
        return date(yr, mo, 1), _month_end(yr, mo)

    # ---- bare month name, assume current year (or most recent past occurrence) ----
    if p in MONTH_NAMES:
        mo = MONTH_NAMES[p]
        yr = today.year if mo <= today.month else today.year - 1
        return date(yr, mo, 1), _month_end(yr, mo)

    # ---- this / current quarter (calendar) ----
    if p in ('this quarter', 'current quarter'):
        q = _which_calendar_quarter(today)
        return _quarter_bounds(today.year, q)

    # ---- last quarter (calendar) ----
    if p == 'last quarter':
        q = _which_calendar_quarter(today)
        year = today.year
        q -= 1
        if q == 0:
            q = 4
            year -= 1
        return _quarter_bounds(year, q)

    # ---- last N quarters (calendar) ----
    m = re.match(r'last (\d+) quarters?', p)
    if m:
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

    # ---- q1/q2/q3/q4 YYYY (calendar quarter) ----
    m = re.match(r'q([1-4])\s*(\d{4})', p)
    if m:
        return _quarter_bounds(int(m.group(2)), int(m.group(1)))

    # ---- this / current FY ----
    if p in ('this fy', 'current fy', 'this financial year', 'current financial year'):
        return _fy_bounds(_current_fy_start_year(today))

    # ---- last FY ----
    if p in ('last fy', 'last financial year', 'previous fy'):
        return _fy_bounds(_current_fy_start_year(today) - 1)

    # ---- FY2025 / FY 2025-26 style ----
    m = re.match(r'fy\s*-?\s*(\d{4})', p)
    if m:
        return _fy_bounds(int(m.group(1)))

    # ---- last 2 fy / last N fy ----
    m = re.match(r'last (\d+) (?:fy|financial years?)', p)
    if m:
        n = int(m.group(1))
        cur = _current_fy_start_year(today)
        start, _ = _fy_bounds(cur - n)
        _, end = _fy_bounds(cur - 1)
        return start, end

    # ---- this / last N months (calendar year), e.g. "in 2025", "this year", "current year" ----
    if p in ('this year', 'current year'):
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if p == 'last year':
        # Ambiguous in the docx (flagged as a bug when treated as calendar year in one place,
        # FY in another) -- default to calendar year here since "in 2025" style phrases are
        # handled separately below; callers needing FY semantics should pass "last fy" instead.
        y = today.year - 1
        return date(y, 1, 1), date(y, 12, 31)

    # ---- bare 4-digit year ----
    m = re.match(r'(\d{4})$', p)
    if m:
        fy = int(m.group(1))
        return date(fy, 4, 1), date(fy + 1, 3, 31)

    # ---- last N years (calendar) ----
    m = re.match(r'last (\d+) years?', p)
    if m:
        n = int(m.group(1))
        end = date(today.year - 1, 12, 31)
        start = date(today.year - n, 1, 1)
        return start, end

    # ---- ISO date or DD Mon YYYY, e.g. "2025-08-25", "25 Aug 2025" ----
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})$', p)
    if m:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d, d
    m = re.match(r'(\d{1,2})\s+([a-z]+)\s+(\d{4})$', p)
    if m and m.group(2)[:3] in MONTH_ABBR:
        d = date(int(m.group(3)), MONTH_ABBR[m.group(2)[:3]], int(m.group(1)))
        return d, d

    raise ValueError(f"Unrecognized date phrase: {phrase!r}")


def time_grain_trunc_expr(column: str, grain: str) -> str:
    """Trino date_trunc expression for month/quarter/year trend queries."""
    grain = grain.lower()
    if grain not in ('month', 'quarter', 'year'):
        raise ValueError(f"Unsupported time grain: {grain}")
    return f"date_trunc('{grain}', CAST({column} AS date))"