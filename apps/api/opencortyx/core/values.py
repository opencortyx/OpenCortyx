"""Values compared by meaning, not spelling: " ACME  Inc " = "acme inc", "9.50" = 9.5,
"2024-01-15T00:00:00" = "2024-01-15", [b, a] = [a, b].

Deliberately conservative: a value that is not clearly a number or a date is compared as text, so
normalising can make two spellings equal but never makes two different values equal.

In core because core needs it too (tombstones hash forgotten names with it) and core may not import
the layers above it; resolution/normalize.py builds identity keys on it.
"""
from __future__ import annotations

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
