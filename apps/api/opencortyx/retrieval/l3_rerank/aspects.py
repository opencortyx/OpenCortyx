"""L3 — aspect coverage: rank a SET, not a list of passages.

THE MEASURED FAILURE

    "What recipes has Joanna made?"     seven recipes, seven turns
    "What mediums does Nate use?"       Gamecube, PC, PlayStation - three turns
    "Which countries did James visit?"  Canada, Greenland - two turns

Each turn names ONE item, so each scores middling on relevance and none reaches the top 30 as a
group. Nine of the fourteen questions still targeted have this shape.

**Relevance cannot express "these three together are complete."** It scores each passage against
the query independently, which is the right objective for a question with one answer and the
wrong one for a question whose answer is a set. Ranking harder does not help: the passages are
individually mediocre and collectively correct.

WHAT THIS DOES INSTEAD

Greedy selection for aspect COVERAGE, the xQuAD / IA-Select formulation:

    value(c) = relevance(c) + LAMBDA * novelty(c | already selected)

where novelty is the share of a candidate's own content that no selected candidate has
contributed yet. A turn naming a recipe nobody has named is promoted; a fourth turn naming a
recipe already covered is not. MMR's redundancy penalty is the same idea with the sign flipped -
this rewards new ground rather than merely punishing repetition, which matters when every
candidate is weakly relevant and the differences between them are what carry the answer.

WHY IT IS GATED, AND ON WHAT

`Cardinality` is decided in L0 and, until now, consumed by nothing - the fifth declared-and-unused
construct found in this engine. Its own docstring states the case exactly:

    "a SET answer that returns four of five items is wrong in a way a ONE answer cannot be"

So this runs for SET and COUNT questions and leaves ONE alone. A single-answer question wants the
best passage first, and rewarding novelty there would promote a worse passage for saying
something new. Applying diversification to every query is how a fix for one question shape
becomes a regression for the others.

Type-free throughout: it asks what a candidate's text CONTRIBUTES, never what the node is.
"""
from __future__ import annotations

from typing import Optional

from opencortyx.retrieval.contracts import Cardinality, QueryPlan

LAMBDA = 0.35

WINDOW = 200

#: How many positions to reorder. Beyond this the ordering no longer affects what is packaged.
DEPTH = 60


def applies(plan: Optional[QueryPlan]) -> bool:
    return bool(plan) and plan.cardinality in (Cardinality.SET, Cardinality.COUNT)


def _content(text: str) -> set:
    from opencortyx.retrieval.l2_graph.expansion import content_terms
    return content_terms(text or "")


def diversify_aspects(ranked: list, plan: Optional[QueryPlan],
                      lambda_: float = LAMBDA, depth: int = DEPTH,
                      window: int = WINDOW) -> list:
    """Reorder the head of `ranked` to cover more distinct items. Tail order is untouched."""
    if not applies(plan) or len(ranked) < 2:
        return ranked

    asked = _content(plan.normalized_query or plan.raw_query)
    pool = list(ranked)
    out: list = []
    covered: set = set()

    while pool and len(out) < depth:
        best_i, best_v = 0, float("-inf")
        for i, cand in enumerate(pool[:window]):
            own = _content(getattr(cand, "text", "") or getattr(cand, "label", "")) - asked
            novelty = (len(own - covered) / len(own)) if own else 0.0
            value = (cand.final_score or 0.0) + lambda_ * novelty
            if value > best_v:
                best_i, best_v = i, value
        chosen = pool.pop(best_i)
        covered |= (_content(getattr(chosen, "text", "")
                             or getattr(chosen, "label", "")) - asked)
        out.append(chosen)
    return out + pool
