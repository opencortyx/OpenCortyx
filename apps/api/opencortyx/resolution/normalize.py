"""Values compared by meaning, not spelling: " ACME  Inc " = "acme inc", "9.50" = 9.5,
"2024-01-15T00:00:00" = "2024-01-15", [b, a] = [a, b].

Deliberately conservative: a value that is not clearly a number or a date is compared as text, so
normalising can make two spellings equal but never makes two different values equal.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime

_NUMBER = re.compile(r"^[-+]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][\d:.]+([+-]\d{2}:?\d{2}|Z)?)?$")


def _number(n) -> str:
    return str(int(n)) if float(n).is_integer() else repr(float(n))


def norm(value) -> str | None:
    """The canonical text of a value, or None when there is no value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, (list, tuple, set)):
        items = sorted(n for n in (norm(v) for v in value) if n is not None)
        return "[" + ",".join(items) + "]" if items else None
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    text = " ".join(unicodedata.normalize("NFKC", str(value)).split())
    if not text:
        return None
    if _NUMBER.match(text):
        return _number(float(text.replace(",", "")))
    if _ISO_DATE.match(text):                   # before case-folding, which would turn the "T" into "t"
        try:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
            midnight = (when.hour, when.minute, when.second, when.microsecond) == (0, 0, 0, 0)
            return when.date().isoformat() if midnight else when.isoformat()
        except ValueError:
            pass
    return text.casefold()


def identity_text(values) -> str | None:
    """The normalised identity values as one string, or None when any is missing."""
    parts = [norm(v) for v in values]
    if not parts or any(p is None for p in parts):
        return None
    return "\x1f".join(parts)


def identity_block(values) -> str | None:
    """A coarse key for similar-identity matching: nodes are only compared with nodes that share it
    (the first four characters of the identity), so matching is never all-against-all."""
    text = identity_text(values)
    if text is None:
        return None
    return hashlib.sha256(("block:" + text.replace(" ", "")[:4]).encode()).hexdigest()


_DIGITS = re.compile(r"\d+")
#: Words shorter than this are matched exactly: a letter changed on a short word is another word.
MIN_FUZZY_WORD = 5


def similarity(a: str, b: str) -> float:
    """0–1 similarity of two identity strings (1.0 when equal), field by field and word by word: two
    identities are near only when every field has the same numbers and the same number of words,
    and each word is close to its counterpart - the least close pair is the score. "Kristin Hannah"
    and "Kristen Hannah" are near; "25 minutes" and "20 minutes" are two durations, "this week" and
    "this weekend" two different words, "ACL" and "EACL" two conferences (short words exactly)."""
    from difflib import SequenceMatcher
    if a == b:
        return 1.0
    fa, fb = a.split("\x1f"), b.split("\x1f")
    if len(fa) != len(fb):
        return 0.0
    worst = 1.0
    for x, y in zip(fa, fb):
        wx, wy = x.split(), y.split()
        if _DIGITS.findall(x) != _DIGITS.findall(y) or len(wx) != len(wy) or not wx:
            return 0.0
        for p, q in zip(wx, wy):
            if p == q:
                continue
            if min(len(p), len(q)) < MIN_FUZZY_WORD:
                return 0.0
            worst = min(worst, SequenceMatcher(None, p, q).ratio())
    return worst


def identity_key(values) -> str | None:
    """A stable hash of normalised identity values, or None when any of them is missing: an
    incomplete identity never matches anything."""
    parts = [norm(v) for v in values]
    if not parts or any(p is None for p in parts):
        return None
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
