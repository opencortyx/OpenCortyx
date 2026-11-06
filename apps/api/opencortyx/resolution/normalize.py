"""Identity keys for resolution, built on core/values.py's `norm` (values compared by meaning, not
spelling: " ACME  Inc " = "acme inc", "9.50" = 9.5, [b, a] = [a, b])."""
from __future__ import annotations

import hashlib
import re

from opencortyx.core.values import norm


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


def identity_blocks(values) -> list[str]:
    """Every block a similar identity is filed and looked up under: the first four characters of
    the whole identity (as `identity_block`), and of its last word. One block alone misses a match
    whose first word is spelled differently ("Catherine Zeta" / "Katherine Zeta"); the second finds
    it, and `similarity` still decides."""
    text = identity_text(values)
    if text is None:
        return []
    out = [identity_block(values)]
    last = text.replace("", " ").split()
    if len(last) > 1:
        out.append(hashlib.sha256(("block:last:" + last[-1][:4]).encode()).hexdigest())
    return out


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
