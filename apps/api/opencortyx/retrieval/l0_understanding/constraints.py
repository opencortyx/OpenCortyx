"""L0 — Constraint extraction: time bounds and attribute filters read off the question.

A date in a question is not a keyword. "What happened to Acme after 2023" should REJECT a 2021
record before ranking ever runs, rather than hoping a 2021 record ranks lower than a 2024 one.
The previous engine had an as-of filter, but nothing populated it from the question — and the
timestamps themselves were in no index at all, so time could neither filter nor match.

Constraints extracted here are HYPOTHESES. A constraint that matches nothing must widen, never
empty, the result: a filter that silently removes every candidate is worse than no filter, because
the failure looks like "we have no data on that" rather than "we misread the question".
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from opencortyx.retrieval.contracts import (
    AttributeConstraint, TemporalConstraint,
)

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})

_YEAR = r"(?:19|20)\d{2}"
_MONTH_NAME = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|" \
              r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

# "on 25 May, 2022" / "25 May 2022" / "May 25, 2022"
_DMY = re.compile(rf"\b(\d{{1,2}})\s+{_MONTH_NAME}\.?,?\s+({_YEAR})\b", re.I)
_MDY = re.compile(rf"\b{_MONTH_NAME}\.?\s+(\d{{1,2}}),?\s+({_YEAR})\b", re.I)
_MY = re.compile(rf"\b{_MONTH_NAME}\.?\s+({_YEAR})\b", re.I)
_ISO = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")
_BARE_YEAR = re.compile(rf"\b({_YEAR})\b")

_BETWEEN = re.compile(rf"\bbetween\b.{{0,40}}?\b({_YEAR})\b.{{0,20}}?\band\b.{{0,20}}?\b({_YEAR})\b",
                      re.I | re.S)
_AFTER = re.compile(r"\b(after|since|from|later than|beyond)\b", re.I)
_BEFORE = re.compile(r"\b(before|prior to|until|up to|earlier than)\b", re.I)
_DURING = re.compile(r"\b(in|during|on|throughout|within)\b", re.I)

_COMPARATORS = [
    (re.compile(r"\b(more|greater|larger|higher|above|over|exceed(?:ing|s)?)\s+than\s+"
                r"([\$€£]?\s?[\d,.]+\s?(?:k|m|bn|million|billion)?)", re.I), "gt"),
    (re.compile(r"\b(less|fewer|smaller|lower|below|under)\s+than\s+"
                r"([\$€£]?\s?[\d,.]+\s?(?:k|m|bn|million|billion)?)", re.I), "lt"),
    (re.compile(r"\bat least\s+([\$€£]?\s?[\d,.]+\s?(?:k|m|bn|million|billion)?)", re.I), "gte"),
    (re.compile(r"\bat most\s+([\$€£]?\s?[\d,.]+\s?(?:k|m|bn|million|billion)?)", re.I), "lte"),
]

_MULT = {"k": 1e3, "m": 1e6, "bn": 1e9, "million": 1e6, "billion": 1e9}


def _iso(y: int, m: int = 1, d: int = 1) -> str:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return date(y, 1, 1).isoformat()


def _end_of_year(y: int) -> str:
    return date(y, 12, 31).isoformat()


def _end_of_month(y: int, m: int) -> str:
    nm_y, nm_m = (y + 1, 1) if m == 12 else (y, m + 1)
    from datetime import timedelta
    return (date(nm_y, nm_m, 1) - timedelta(days=1)).isoformat()


def parse_dates(text: str) -> list[tuple[str, str, str]]:
    """Every date-like span, as `(iso_start, iso_end, raw)`. A bare year spans the whole year;
    a month spans that month. Granularity is preserved so "in 2022" does not become 1 January."""
    q = text or ""
    found: list[tuple[str, str, str]] = []
    spans: list[tuple[int, int]] = []

    def overlaps(a, b):
        return any(not (b <= s or a >= e) for s, e in spans)

    for m in _ISO.finditer(q):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        found.append((_iso(y, mo, d), _iso(y, mo, d), m.group(0)))
        spans.append(m.span())
    for m in _DMY.finditer(q):
        if overlaps(*m.span()):
            continue
        d, mon, y = int(m.group(1)), _MONTHS.get(m.group(2).lower()[:3], 1), int(m.group(3))
        found.append((_iso(y, mon, d), _iso(y, mon, d), m.group(0)))
        spans.append(m.span())
    for m in _MDY.finditer(q):
        if overlaps(*m.span()):
            continue
        mon, d, y = _MONTHS.get(m.group(1).lower()[:3], 1), int(m.group(2)), int(m.group(3))
        found.append((_iso(y, mon, d), _iso(y, mon, d), m.group(0)))
        spans.append(m.span())
    for m in _MY.finditer(q):
        if overlaps(*m.span()):
            continue
        mon, y = _MONTHS.get(m.group(1).lower()[:3], 1), int(m.group(2))
        found.append((_iso(y, mon, 1), _end_of_month(y, mon), m.group(0)))
        spans.append(m.span())
    for m in _BARE_YEAR.finditer(q):
        if overlaps(*m.span()):
            continue
        y = int(m.group(1))
        found.append((_iso(y), _end_of_year(y), m.group(0)))
        spans.append(m.span())
    return found


def has_date_range(query: str) -> bool:
    """Whether the question contains "between <date> and <date>".

    Exposed because the intent classifier has to tell two different uses of the same word apart:
    "between 2023 and 2025" is a TIME BOUND, while "between John and Acme" is a RELATION. Reading
    both as relational sent "What changed between 2023 and 2025?" down a graph-expansion plan.
    """
    return bool(_BETWEEN.search(query or ""))


_MONTH_NAMES = ("january|february|march|april|may|june|july|august|september|october|november"
                "|december")
_BETWEEN_MD = re.compile(
    rf"between\s+({_MONTH_NAMES})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s*(\d{{4}}))?\s+and\s+({_MONTH_NAMES})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s*(\d{{4}}))?",
    re.I)
_MONTH_NUM = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def _between_month_day(q: str):
    """A "between <Month D> and <Month D> <YYYY>" range, with the year shared across both ends."""
    m = _BETWEEN_MD.search(q or "")
    if not m:
        return None
    mon1, day1, yr1, mon2, day2, yr2 = m.groups()
    year = yr2 or yr1
    if not year:
        return None
    try:
        import datetime as _dt
        d1 = _dt.date(int(year), _MONTH_NUM[mon1.lower()], int(day1))
        d2 = _dt.date(int(yr2 or year), _MONTH_NUM[mon2.lower()], int(day2))
    except (ValueError, KeyError):
        return None
    lo, hi = sorted([d1, d2])
    return TemporalConstraint("between", lo.isoformat(), hi.isoformat(), m.group(0))


def extract_temporal(query: str) -> list[TemporalConstraint]:
    """Time bounds implied by the question, with the relation its own words used."""
    q = (query or "").strip()
    if not q:
        return []

    md = _between_month_day(q)
    if md:
        return [md]

    mb = _BETWEEN.search(q)
    if mb:
        y1, y2 = int(mb.group(1)), int(mb.group(2))
        lo, hi = min(y1, y2), max(y1, y2)
        return [TemporalConstraint("between", _iso(lo), _end_of_year(hi), mb.group(0))]

    dates = parse_dates(q)
    if not dates:
        return []

    start, end, raw = dates[0]
    # The relation is decided by the words AROUND the first date, not by a default.
    head = q[:q.lower().find(raw.lower())] if raw.lower() in q.lower() else q
    if _AFTER.search(head):
        return [TemporalConstraint("after", start, None, raw)]
    if _BEFORE.search(head):
        return [TemporalConstraint("before", None, end, raw)]
    if len(dates) >= 2:
        s2, e2, r2 = dates[1]
        lo, hi = sorted([start, s2])
        return [TemporalConstraint("between", lo, max(end, e2), f"{raw} .. {r2}")]
    if _DURING.search(head) or True:
        return [TemporalConstraint("during", start, end, raw)]


def _number(raw: str) -> Optional[float]:
    s = (raw or "").strip().lower().replace(",", "").replace("$", "").replace("€", "") \
        .replace("£", "").strip()
    m = re.match(r"^([\d.]+)\s*(k|m|bn|million|billion)?$", s)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    return val * _MULT.get(m.group(2) or "", 1.0)


def extract_attribute_constraints(query: str) -> list[AttributeConstraint]:
    """Numeric comparisons and explicit `field = value` phrasings.

    The attribute NAME is left as whatever the question said. Binding it to a real field is L1's
    job, against the attributes the corpus actually has — a fixed schema map here would be the
    same mistake as a fixed type table.
    """
    q = (query or "")
    out: list[AttributeConstraint] = []
    for pat, op in _COMPARATORS:
        for m in pat.finditer(q):
            raw_val = m.group(m.lastindex or 1)
            val = _number(raw_val)
            if val is None:
                continue
            head = q[:m.start()].strip().split()
            attr = head[-1].lower().strip(".,") if head else "value"
            out.append(AttributeConstraint(attr=attr, op=op, value=val, raw=m.group(0)))
    for m in re.finditer(r"\b(\w+)\s*(?:=|:)\s*[\"']?([\w \-]{2,40})[\"']?", q):
        out.append(AttributeConstraint(attr=m.group(1).lower(), op="eq",
                                       value=m.group(2).strip(), raw=m.group(0)))
    return out


def within(constraint: TemporalConstraint, iso_value: Optional[str]) -> bool:
    """Does a node's date satisfy the constraint? Unknown dates PASS.

    Rejecting a node because its date is unparseable would delete evidence on a technicality; the
    filter exists to remove things known to be out of range, not things we failed to read.
    """
    if not iso_value:
        return True
    v = iso_value[:10]
    if constraint.start and v < constraint.start[:10]:
        return False
    if constraint.end and v > constraint.end[:10]:
        return False
    return True


def to_iso(raw: str) -> Optional[str]:
    """Best-effort ISO date from any of the corpus's date spellings.

    "2:01 pm on 23 January, 2022" is the shape this corpus stores; a naive parser returns None on
    it, and every temporal comparison then silently passes.
    """
    if not raw:
        return None
    s = str(raw)
    m = _ISO.search(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    for pat, order in ((_DMY, "dmy"), (_MDY, "mdy")):
        m = pat.search(s)
        if m:
            if order == "dmy":
                d, mon, y = int(m.group(1)), _MONTHS.get(m.group(2).lower()[:3], 1), int(m.group(3))
            else:
                mon, d, y = _MONTHS.get(m.group(1).lower()[:3], 1), int(m.group(2)), int(m.group(3))
            return _iso(y, mon, d)
    m = _MY.search(s)
    if m:
        return _iso(int(m.group(2)), _MONTHS.get(m.group(1).lower()[:3], 1), 1)
    m = _BARE_YEAR.search(s)
    if m:
        return _iso(int(m.group(1)))
    try:
        return datetime.fromisoformat(s[:19]).date().isoformat()
    except Exception:
        return None
