"""Aggregation: COUNT THE GRAPH, do not retrieve it and hope the model counts correctly.

    "How many transactions did each company have?"
            │
    parse ──┼──► count · target=transactions · per=company
            │
    ground ─┼──► the corpus's own type names, matched as text  (no such type -> no aggregation)
            │
    execute ┴──► one grouped query ──► rows ──► a single rendered block

WHY THIS IS A DIFFERENT CATEGORY AND NOT A RETRIEVAL SETTING

Every other question is answered by finding the right evidence. A counting question is answered by
a computation over ALL the evidence, and those two need opposite things from retrieval. Ranking
exists to put the best few things first; a count is wrong unless it sees everything. Retrieving
the top 400 nodes and asking a model to count them produces a confident number that is wrong by
however much the budget truncated — and nothing in the pipeline can detect that, because from the
outside it looks exactly like a successful retrieval.

So `Intent.AGGREGATION` was detected long before anything acted on it, and the question still went
down the stuffing path. This is what acts on it.

WHAT MAKES IT SAFE

An aggregation is only executed when the question's nouns bind to types the corpus ACTUALLY HAS.
"How many transactions per company" against a conversation graph grounds nothing, and grounding
nothing must mean "fall back to ordinary retrieval", never "return zero". A zero from a query
that matched no rows and a zero that is the true answer are indistinguishable to the reader, and
only one of them is honest.

Nothing here consults a table of node types. The nouns are matched as TEXT against the discovered
vocabulary (`vocabulary.match_type`), exactly as everywhere else, so a corpus that calls its
things something else aggregates over its own names with no code change.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Optional


from opencortyx.core.substrate import CONVERSATION_FLAG, NodeRepo, in_scope
from opencortyx.retrieval.contracts import Intent, QueryPlan
from opencortyx.retrieval.l0_understanding.vocabulary import (
    TypeVocabulary, match_type,
)
from opencortyx.retrieval.common.text_norm import singularize

MAX_GROUPS = 50

_FUNCTIONS = (
    (re.compile(r"\b(how many|number of|count of|count the)\b", re.I), "count"),
    (re.compile(r"\b(total|sum of)\b", re.I), "sum"),
    (re.compile(r"\b(average|avg|mean)\b", re.I), "avg"),
    (re.compile(r"\b(largest|maximum|max|highest|most)\b", re.I), "max"),
    (re.compile(r"\b(smallest|minimum|min|lowest|fewest|least)\b", re.I), "min"),
)

#: The thing being counted: the noun right after the counting phrase.
_TARGET = re.compile(
    r"\b(?:how many|number of|count of|count the|total|sum of|average|avg|mean)\s+"
    r"(?:the\s+)?([a-z][\w\-]*)", re.I)

#: The thing counted PER: "each company", "per company", "by company", "for each company".
_GROUP = re.compile(r"\b(?:per|each|every|by)\s+(?:the\s+)?([a-z][\w\-]*)", re.I)

_NOISE = frozenset({"the", "a", "an", "of", "many", "much", "one", "ones", "thing", "things",
                    "time", "times", "there", "them", "it", "they"}
                   # a unit of time asks for a duration ("how many days ago", "how many weeks in
                   # total"), never for the number of nodes of a type an extractor called Day
                   | {u + s for u in ("second", "minute", "hour", "day", "week", "month", "year")
                      for s in ("", "s")})


@dataclass
class AggregationSpec:
    """A computation read off the question and bound to the corpus's own type names."""
    function: str = "count"
    target_type: str = ""                # corpus type being aggregated
    group_type: str = ""                 # corpus type to group by; "" = a single scalar
    target_noun: str = ""                # the question's own word, for rendering
    group_noun: str = ""
    grounded: bool = False               # both nouns bound to real types
    why: str = ""

    def describe(self) -> str:
        base = f"{self.function}({self.target_type or self.target_noun or '?'})"
        return f"{base} per {self.group_type}" if self.group_type else base


@dataclass
class AggregationRow:
    key: str
    value: float
    key_id: Optional[uuid.UUID] = None


@dataclass
class AggregationResult:
    """The computed answer, plus enough provenance to defend it."""
    spec: AggregationSpec
    rows: list[AggregationRow] = field(default_factory=list)
    scalar: Optional[float] = None
    groups_total: int = 0                # how many groups exist, not how many are listed
    truncated: bool = False
    ms: float = 0.0
    #: The types are real and the query ran, but nothing connects them, so there is no
    #: computation to report. See `execute` — this is NOT a count of zero.
    inconclusive: bool = False
    note: str = ""

    @property
    def usable(self) -> bool:
        return not self.inconclusive and (self.scalar is not None or bool(self.rows))

    def render(self) -> str:
        s = self.spec
        if self.scalar is not None:
            return f"{s.function} of {s.target_type}: {self.scalar:g}"
        head = f"{s.function} of {s.target_type} per {s.group_type} " \
               f"({self.groups_total} {s.group_type.lower()}(s))"
        lines = [f"  {r.key or '(unlabelled)'}: {r.value:g}" for r in self.rows]
        if self.truncated:
            lines.append(f"  … {self.groups_total - len(self.rows)} more not listed "
                         f"(all {self.groups_total} were counted)")
        return "\n".join([head] + lines)


def parse(plan: QueryPlan, vocab: Optional[TypeVocabulary]) -> Optional[AggregationSpec]:
    """Read the computation out of the question and bind its nouns to real types.

    Returns `None` when this is not an aggregation, and an ungrounded spec when it is one the
    corpus cannot answer — the caller needs to tell those apart, because the second case must
    fall back to ordinary retrieval rather than reporting a count of nothing.
    """
    if plan.intent is not Intent.AGGREGATION:
        return None
    q = plan.normalized_query or plan.raw_query or ""
    if not q:
        return None

    function = "count"
    for pat, name in _FUNCTIONS:
        if pat.search(q):
            function = name
            break

    m = _TARGET.search(q)
    target_noun = (m.group(1).lower() if m else "")
    g = _GROUP.search(q)
    group_noun = (g.group(1).lower() if g else "")
    if target_noun in _NOISE:
        target_noun = ""
    if group_noun in _NOISE:
        group_noun = ""

    spec = AggregationSpec(function=function, target_noun=target_noun, group_noun=group_noun)
    if not target_noun:
        spec.why = "no countable noun in the question"
        return spec

    nodes = (vocab.node_types if vocab else None)
    hit = match_type(target_noun, nodes)
    if not hit:
        spec.why = f"this corpus has no type resembling {singularize(target_noun)!r}"
        return spec
    spec.target_type = hit[0]

    if group_noun:
        ghit = match_type(group_noun, nodes)
        if not ghit:
            spec.why = f"this corpus has no type resembling {singularize(group_noun)!r}"
            return spec
        spec.group_type = ghit[0]

    spec.grounded = True
    spec.why = f"grounded: {spec.describe()}"
    return spec


async def execute(workspace_id: uuid.UUID, spec: AggregationSpec, *,
                  limit: int = MAX_GROUPS, scope: str | None = None,
                  collection_ids=None) -> AggregationResult:
    """Run the computation over every node of the types, not over a retrieved sample.

    `scope` is the question's corpus, as retrieval's own scope filter reads it. Counting ignored it,
    so "how many sessions were there?" asked of the knowledge base counted the app's own captured
    chats, and any type name the app writes as memory competed with the corpus's for a count. A node
    outside the scope is not counted, as a group or as a member. So with `collection_ids`: only the
    collections asked about are counted, and a count of none there is no answer (the type is simply
    not in them) - retrieval reads the collections instead."""
    import time
    t0 = time.perf_counter()
    res = AggregationResult(spec=spec)
    if not spec.grounded:
        return res
    repo = NodeRepo()
    scope = (scope or "all").strip().lower()
    live = {"superseded": "true"}
    if scope == "knowledge":
        live[CONVERSATION_FLAG] = "true"            # excluded when EITHER key matches

    cols = {str(c) for c in collection_ids} if collection_ids else None

    def in_cols(node) -> bool:
        return cols is None or str(node.collection_id or "") in cols

    def counted(node) -> bool:
        return (str((node.properties or {}).get("superseded", "")).lower() != "true"
                and in_scope(node, scope) and in_cols(node))

    if not spec.group_type:
        if scope == "conversations" or cols:        # "flag is true" cannot be a props_not
            nodes = await repo.find(workspace_id, exact_types=[spec.target_type])
            res.scalar = float(sum(1 for n in nodes if counted(n)))
            if cols and not res.scalar:
                res.inconclusive = True
                res.note = f"no {spec.target_type} in these collections; this is not a count of zero"
        else:
            res.scalar = float(await repo.count(workspace_id, exact_types=[spec.target_type], props_not=live))
        res.ms = round((time.perf_counter() - t0) * 1000, 1)
        return res
    groups = [g for g in await repo.find(workspace_id, exact_types=[spec.group_type], props_not=live)
              if in_scope(g, scope) and in_cols(g)]
    members: dict[str, set] = {str(g.id): set() for g in groups}
    for start in range(0, len(groups), 200):
        batch = [g.id for g in groups[start:start + 200]]
        for edge, node in await repo.neighbors(workspace_id, batch, direction="both", limit=1_000_000):
            if node.type != spec.target_type or not counted(node):
                continue
            group_id = str(edge.source_id) if str(edge.target_id) == str(node.id) else str(edge.target_id)
            if group_id in members:
                members[group_id].add(str(node.id))
    labels = {str(g.id): g.label or "" for g in groups}
    rows = sorted(((gid, labels[gid], len(tids)) for gid, tids in members.items() if tids),
                  key=lambda r: (-r[2], r[1]))
    res.groups_total = len(rows)
    if not rows:
        res.inconclusive = True
        res.note = (f"no edge connects {spec.target_type} to {spec.group_type} directly; "
                    f"this is not a count of zero")
        res.ms = round((time.perf_counter() - t0) * 1000, 1)
        return res
    res.truncated = len(rows) > limit
    for gid, glabel, n in rows[:limit]:
        res.rows.append(AggregationRow(key=glabel, value=float(n), key_id=uuid.UUID(gid)))
    res.ms = round((time.perf_counter() - t0) * 1000, 1)
    return res
