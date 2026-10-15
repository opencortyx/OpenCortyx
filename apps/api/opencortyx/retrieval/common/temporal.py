"""Temporal helpers — robust date normalization (absolute AND relative), a recency score, and
date-expression detection used by the temporal query router and the coded temporal extractors
(extract_temporal_pattern / py.date_normalizer).

`normalize_date` understands the many ways people write dates:
  • ISO            2026-04-01
  • numeric M/Y    02/2026, 4-2026
  • month + year   "february 2026", "Feb 2026"
  • month + 'YY    "feb 26"  → February 2026
  • relative       "last monday", "last month", "yesterday", "3 weeks ago", "next friday"
All resolve to an ISO `YYYY-MM-DD` string (relative ones are resolved against a reference date,
default today). `find_date_expressions` scans free text and returns the spans it recognised, each
with a granularity (day|month|year) so the router can build a date WINDOW (e.g. all of Feb 2026).
"""
import calendar
import re
from datetime import date, datetime, timedelta

try:
    from dateutil import parser as _dp
except Exception:
    _dp = None

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WD_ALT = "|".join(_WEEKDAYS)


def _clamp(y: int, m: int, d: int) -> date:
    return date(y, m, min(d, calendar.monthrange(y, m)[1]))


def _shift_months(d: date, n: int) -> date:
    z = d.month - 1 + n
    return _clamp(d.year + z // 12, z % 12 + 1, d.day)


def _nearest_weekday(ref: date, target: int, kw: str) -> date:
    cur = ref.weekday()
    if kw in ("last", "past"):
        delta = (cur - target) % 7 or 7
        return ref - timedelta(days=delta)
    if kw == "next":
        delta = (target - cur) % 7 or 7
        return ref + timedelta(days=delta)
    return ref + timedelta(days=(target - cur) % 7)        # "this <weekday>"


def _relative(s: str, ref: date):
    """Resolve a RELATIVE date phrase against `ref`. Returns (date, granularity) or None."""
    if s in ("today", "now"):
        return ref, "day"
    if s == "yesterday":
        return ref - timedelta(days=1), "day"
    if s == "tomorrow":
        return ref + timedelta(days=1), "day"
    if s in ("last week", "past week", "a week ago"):
        return ref - timedelta(weeks=1), "day"
    if s == "next week":
        return ref + timedelta(weeks=1), "day"
    if s in ("last month", "past month", "a month ago"):
        return _shift_months(ref, -1), "month"
    if s == "next month":
        return _shift_months(ref, 1), "month"
    if s in ("last year", "past year", "a year ago"):
        return _clamp(ref.year - 1, ref.month, ref.day), "year"
    if s == "next year":
        return _clamp(ref.year + 1, ref.month, ref.day), "year"
    m = re.fullmatch(rf"(last|next|this|past)\s+({_WD_ALT})", s)
    if m:
        return _nearest_weekday(ref, _WEEKDAYS[m.group(2)], m.group(1)), "day"
    m = re.fullmatch(r"(\d+)\s+(day|week|month|year)s?\s+ago", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "day":
            return ref - timedelta(days=n), "day"
        if unit == "week":
            return ref - timedelta(weeks=n), "day"
        if unit == "month":
            return _shift_months(ref, -n), "month"
        return _clamp(ref.year - n, ref.month, ref.day), "year"
    return None


def _absolute(s: str, ref: date):
    """Resolve an ABSOLUTE date string. Returns (date, granularity) or None."""
    # ISO YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return _clamp(int(m.group(1)), int(m.group(2)), int(m.group(3))), "day"
        except ValueError:
            return None
    # numeric month/year — 02/2026, 4-2026  (no day → month granularity, day 1)
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{4})", s)
    if m and 1 <= int(m.group(1)) <= 12:
        return date(int(m.group(2)), int(m.group(1)), 1), "month"
    # month name + number — "february 2026", "feb 26"
    m = re.fullmatch(rf"({_MONTH_ALT})\.?\s+(\d{{1,4}})", s)
    if m:
        mon, num = _MONTHS[m.group(1)], int(m.group(2))
        if len(m.group(2)) == 4:                            # explicit year
            return date(num, mon, 1), "month"
        if num > 12:                                        # 2-digit > 12 → year ("feb 26" → 2026)
            return date(2000 + num, mon, 1), "month"
        return _clamp(ref.year, mon, num), "day"            # small number → day of current year
    # bare month name → that month of the current year
    if s in _MONTHS:
        return date(ref.year, _MONTHS[s], 1), "month"
    # bare 4-digit year
    if re.fullmatch(r"(19|20)\d{2}", s):
        return date(int(s), 1, 1), "year"
    # last resort: dateutil fuzzy (fills missing parts from the 1st of the ref month)
    if _dp:
        try:
            dt = _dp.parse(s, fuzzy=True, default=datetime(ref.year, ref.month, 1))
            return dt.date(), "day"
        except Exception:
            return None
    return None


def normalize_date(text, ref: date | None = None) -> str | None:
    """Normalize a single date expression (absolute or relative) → ISO 'YYYY-MM-DD', or None."""
    res = _normalize(text, ref)
    return res[0].isoformat() if res else None


def _normalize(text, ref: date | None = None):
    if not text:
        return None
    ref = ref or date.today()
    s = re.sub(r"\s+", " ", str(text).strip().lower()).strip(" .,!?")
    return _relative(s, ref) or _absolute(s, ref)


# Patterns scanned in free text (the spans, not full-string). Order matters: relative phrases and
# explicit formats first so they win over a bare number.
_SCAN = [
    re.compile(r"\b(?:last|next|this|past)\s+(?:" + _WD_ALT + r"|week|month|year)\b", re.I),
    re.compile(r"\b(?:yesterday|today|tomorrow)\b", re.I),
    re.compile(r"\b\d+\s+(?:day|week|month|year)s?\s+ago\b", re.I),
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[/\-.]\d{4}\b"),
    re.compile(r"\b(?:" + _MONTH_ALT + r")\.?\s+\d{1,4}\b", re.I),
]


def find_date_expressions(text, ref: date | None = None) -> list:
    """Scan free text → [{phrase, date, granularity}] for every date expression recognised.
    Powers temporal-intent routing (a date mention makes a query temporal) and is a deterministic
    normalization fallback for the coded temporal extractors."""
    out, seen = [], set()
    for rx in _SCAN:
        for m in rx.finditer(text or ""):
            span = m.group(0)
            key = span.lower()
            if key in seen:
                continue
            res = _normalize(span, ref)
            if res:
                seen.add(key)
                out.append({"phrase": span, "date": res[0].isoformat(), "granularity": res[1]})
    return out


def recency_score(iso: str | None, floor_year: int = 2000) -> float:
    """0..1 — newer dates score higher. Used as a small ranking boost."""
    if not iso:
        return 0.0
    try:
        d = date.fromisoformat(iso)
        span = (date.today().year - floor_year) or 1
        return max(0.0, min(1.0, (d.year - floor_year) / span))
    except Exception:
        return 0.0
