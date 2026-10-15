"""L0 — Execution policy: which retrievers actually RUN for this question.

    intent + confidence ──► surfaces to execute ──► the rest are SKIPPED (with a reason)
                                    │
                              underfilled? ──► widen and run them after all

WEIGHTS AND EXECUTION ARE DIFFERENT DECISIONS, AND CONFLATING THEM COST BOTH

`planner.py` assigns every surface a WEIGHT with a hard floor of 0.15, on the rule that a plan is
a hypothesis and a wrong hypothesis must cost precision rather than recall. That rule is right,
and it is about FUSION: how loudly a surface that ran gets to vote.

It says nothing about whether the surface should have run. And because nothing else said anything
either, the orchestrator ran all of them, always. An exact lookup — "what is the email of John",
answerable by resolving one node and reading one field — paid for a full ANN sweep and a graph
walk, then down-weighted their results to 0.15 and threw them away. The classification was
computed, traced, and then ignored by the thing it was supposed to govern.

So this module decides EXECUTION, and `strategies` continues to decide VOTING. A surface that
runs is weighted by the floor rule; a surface that does not run is recorded in `skipped` with the
reason, because a silently absent retriever is indistinguishable from one that found nothing.

THE SAFETY NET, WITHOUT WHICH THIS WOULD BE A RECALL BUG

Narrowing is only safe if being wrong is recoverable, so two guards apply:

  1. NARROW ONLY WHEN CONFIDENT. Below `MIN_CONFIDENCE_TO_NARROW` every surface runs. The
     `SEMANTIC` default sits at 0.35 precisely because nothing in the question was recognised —
     that is the case that most needs breadth, and it gets it.

  2. WIDEN WHEN UNDERFILLED. If the narrow plan returns fewer than `widen_below` candidates, the
     skipped surfaces run after all. The cost of being wrong is therefore a second pass of
     latency, never an unanswerable question — which is the same bargain the weight floor makes,
     applied one level up.

WHY THE SETS BELOW ARE NOT A LOOKUP TABLE OF THE FORBIDDEN KIND

They are keyed on INTENT — a property of the question, computed from its language — never on a
node type or an edge name. Which relations get walked and which types rank highly are still
decided against the query, in L2 and L4. This says only "a counting question should not pay for
a dense sweep", which is a statement about the question, not about the graph's schema.
"""
from __future__ import annotations

from opencortyx.retrieval.contracts import (
    ExecutionPolicy, Intent, QueryPlan,
)

#: Below this the classification is not trustworthy enough to act on, and everything runs.
MIN_CONFIDENCE_TO_NARROW = 0.5

#: Fewer fused candidates than this and the skipped surfaces run after all.
DEFAULT_WIDEN_BELOW = 10

_EXECUTE: dict[Intent, set[str]] = {
    Intent.EXACT_LOOKUP: {"entity", "identifier", "bm25", "attr_embedding", "fuzzy", "metadata"},
    Intent.KEYWORD:      {"bm25", "identifier", "attr_embedding", "fuzzy"},
    Intent.SEMANTIC:     {"attr_embedding", "bm25", "identifier", "fuzzy",
                          "metadata", "entity"},
    Intent.ENTITY:       {"entity", "attr_embedding", "fuzzy", "graph",
                          "identifier", "bm25"},
    Intent.RELATIONSHIP: {"entity", "graph", "attr_embedding", "fuzzy", "bm25"},
    Intent.MULTI_HOP:    {"entity", "graph", "attr_embedding", "fuzzy", "bm25"},
    Intent.TEMPORAL:     {"metadata", "attr_embedding", "bm25", "identifier",
                          "fuzzy", "entity"},
    Intent.AGGREGATION:  {"metadata", "entity", "graph", "attr_embedding", "bm25"},
}

for _surfaces in _EXECUTE.values():
    _surfaces.add("ontology")

#: Every surface the orchestrator can run. A surface absent from an intent's set is skipped.
ALL_SURFACES = frozenset().union(*_EXECUTE.values())

_GRAPH_OVERRIDABLE = frozenset({Intent.SEMANTIC, Intent.KEYWORD, Intent.TEMPORAL})

#: Why each surface is skipped, phrased as the reason the QUESTION does not need it. These are
#: read back in the trace, where "skipped" without a reason is indistinguishable from "crashed".
_REASONS = {
    "graph": "the answer is not carried by an edge for this question shape",
    "bm25": "lexical recall is near-useless for this question shape",
    "attr_embedding": "no attribute surface is implicated",
    "metadata": "no constraint to filter on",
    "entity": "no entity binding is needed",
    "identifier": "no identifier-shaped token in the question",
    "fuzzy": "no mention to vary",
    "ontology": "the question implicated no type this corpus has",
}


def plan_execution(plan: QueryPlan, *, widen_below: int = DEFAULT_WIDEN_BELOW,
                   confidence: float = 1.0) -> ExecutionPolicy:
    """Decide which surfaces run. Pure — the orchestrator applies the result.

    `confidence` is the classifier's, passed in rather than re-derived so that this module has
    exactly one job. A low-confidence plan narrows nothing.
    """
    narrow = confidence >= MIN_CONFIDENCE_TO_NARROW
    if narrow:
        run = set(_EXECUTE.get(plan.intent, ALL_SURFACES))
        reason = f"intent={plan.intent.value} conf={confidence:.2f}"

        # Signals in the question override the intent's default set. The intent classifier reads
        # question SHAPE; these read its CONTENT, and content wins when the two disagree.
        if plan.intent is Intent.SEMANTIC and not _is_relational_plan(plan):
            run.discard("graph")
        if plan.edge_types and plan.intent in _GRAPH_OVERRIDABLE:
            run.add("graph")
        if plan.temporal or plan.attributes:
            # An explicit constraint is filterable whatever the question's shape. "Tell me about
            # JP Morgan after 2023" is an entity question that still has a time bound to apply.
            run.add("metadata")
    else:
        run = set(ALL_SURFACES)
        reason = (f"intent confidence {confidence:.2f} below {MIN_CONFIDENCE_TO_NARROW}; "
                  f"running every surface")

    if not plan.mentions:
        run.discard("fuzzy")
        run.discard("entity")           # nothing to resolve
    if not plan.temporal and not plan.attributes:
        run.discard("metadata")
    if not plan.node_types:
        run.discard("ontology")         # nothing to search within

    skipped = {s: _REASONS.get(s, "not implicated by this question")
               for s in ALL_SURFACES - run}
    return ExecutionPolicy(run=run, skipped=skipped, widen_below=widen_below, reason=reason)


def _is_relational_plan(plan: QueryPlan) -> bool:
    from opencortyx.retrieval.l0_understanding.intent import is_relational
    return bool(plan.edge_types) or is_relational(plan.normalized_query or plan.raw_query)


def widen(policy: ExecutionPolicy, found: int) -> tuple[bool, str]:
    """Should the skipped surfaces run after all? Returns `(widen, why)`.

    This is what makes narrowing safe rather than a recall gamble. Being wrong about the intent
    costs a second pass; it never costs the answer.
    """
    if not policy.skipped:
        return False, ""
    if found >= policy.widen_below:
        return False, ""
    return True, (f"only {found} candidate(s) from {len(policy.run)} surface(s), "
                  f"below {policy.widen_below} — running {len(policy.skipped)} skipped surface(s)")
