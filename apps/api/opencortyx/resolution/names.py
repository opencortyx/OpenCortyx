"""Named things that are one thing under a near miss of their name: "Kristin Hannah" and "Kristen
Hannah", "Hilton Garden Inn" and "Hilton Gardens Inn". Swapped letters ("Douro" / "Duoro") are not
caught: word by word they score below the bar.

Exact names already share one node: a named thing's id is its type and normalised name
(`ingest/extraction/text_reader.entity_id`), whichever reader or extractor found it, and the
normalised name drops case, punctuation, articles and legal suffixes ("The Nightingale" is
"nightingale"). What normalising cannot reach is a different spelling. Those, of the same type, are
linked here with SAME_AS - the edge name resolution already writes and retrieval follows - carrying
their score, so a wrong match is one edge to remove rather than two things welded into one.

The bar is the `name_match_threshold` setting (0.85 by default; `.env`:
OPENCORTYX_SETTING_NAME_MATCH_THRESHOLD; 1.0 turns it off). Similarity is difflib's ratio over the
normalised names, word by word: a near miss has the same numbers and the same number of words, each
word close to its counterpart ("Kristin Hannah" / "Kristen Hannah"). "25 minutes" and "20 minutes" are
two durations, "this week" and "this weekend" two different words - not misspellings - and a short
word is matched exactly: one letter more on "ACL" is "EACL", another conference.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, Optional

from opencortyx.core.observability import get_logger
from opencortyx.stores.graph.query import normalize_name

log = get_logger(__name__)

#: Most existing names of one type a pass compares against.
MAX_PER_TYPE = 20000
EDGE = "SAME_AS"


def named(node) -> bool:
    """A thing whose name is its identity (an entity read from text or named by an extractor)."""
    return (getattr(node, "properties", None) or {}).get("__identity") == "entity"


_NUM = re.compile(r"\d+")
#: Words shorter than this are matched exactly: a letter changed on a short word is another word.
MIN_FUZZY_WORD = 5


def score(a: str, b: str) -> float:
    """How alike two normalised names are: 0 when their numbers or word counts differ, else the
    least alike pair of words they hold in order."""
    wa, wb = a.split(), b.split()
    if _NUM.findall(a) != _NUM.findall(b) or len(wa) != len(wb):
        return 0.0
    if any(x != y and min(len(x), len(y)) < MIN_FUZZY_WORD for x, y in zip(wa, wb)):
        return 0.0
    return min(SequenceMatcher(None, x, y).ratio() for x, y in zip(wa, wb))


def near_misses(new: Iterable, existing: Iterable, threshold: float) -> list[tuple]:
    """(new node, existing node, score) for each pair of the same type whose normalised names differ
    but score at least `threshold`. Each unordered pair once."""
    pool: dict = {}
    for o in existing:
        key = normalize_name(o.label)
        if key:
            pool.setdefault(o.type, []).append((key, o))
    out, seen = [], set()
    for n in new:
        a = normalize_name(n.label)
        if not a:
            continue
        for b, o in pool.get(n.type, []):
            if o.id == n.id or a == b:
                continue
            # the ratio is at most 2*min/(sum of lengths): a pair that far apart cannot reach the bar
            if 2 * min(len(a), len(b)) / (len(a) + len(b)) < threshold:
                continue
            s = score(a, b)
            pair = frozenset((str(n.id), str(o.id)))
            if s >= threshold and pair not in seen:
                seen.add(pair)
                out.append((n, o, s))
    return out


async def link_similar(workspace_id, nodes: Iterable, repo=None, threshold: Optional[float] = None) -> int:
    """Link each of `nodes` (just written) to the existing named things of its type whose name is a
    near miss of its own. Returns the edges written."""
    from opencortyx.core.config import runtime as rc
    from opencortyx.core.substrate import NodeRepo
    bar = rc.name_match_threshold() if threshold is None else threshold
    new = [n for n in nodes if n is not None and named(n)]
    if bar >= 1.0 or not new:
        return 0
    repo = repo or NodeRepo()
    existing = []
    for t in {n.type for n in new}:
        existing += [o for o in await repo.find(workspace_id, types=[t], limit=MAX_PER_TYPE) if named(o)]
    pairs = near_misses(new, existing, bar)
    if pairs:
        # one direction for a pair, whichever node is new: written again, it is the same edge
        ends = [sorted((n.id, o.id), key=str) for n, o, _ in pairs]
        await repo.add_edges(workspace_id=workspace_id, edges=[
            {"source_id": a, "target_id": b, "type": EDGE, "properties": {"score": round(s, 3), "by": "name_match"}}
            for (a, b), (_, _, s) in zip(ends, pairs)])
        log.info("names.linked", workspace_id=str(workspace_id), pairs=len(pairs), threshold=bar)
    return len(pairs)
