"""L0, after the plan: the model picks the relationship a question asks about, among the ones the
graph has. Added to the plan the rules made, never replacing it.

Palantir's agents reach the ontology through tools - find an object, follow a link type, search
text - and the model picks the link type to follow from the ones that exist. Here the same choice
refines the plan: the relationship types this corpus has (the vocabulary) closest to the question
by meaning are the options, with "none of these", and the model's pick becomes an edge type the
graph surface follows (policy.py runs the graph whenever the plan names one).

Without a model, or when it picks "none", the plan is exactly what the rules made.
"""
from __future__ import annotations

import math
from typing import Optional

from opencortyx.retrieval.contracts import QueryPlan

#: Relationship types offered to the model, closest to the question first.
CHOICES = 5
#: How much more likely than the runner-up a choice must be before it changes the plan.
MIN_MARGIN = 1.0
NONE = "none of these"
_cache: dict = {}


def _phrase(edge_type: str) -> str:
    return edge_type.replace("_", " ").lower()


def _cos(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


async def _vec(embed, text: str):
    hit = _cache.get(text)
    if hit is None:
        hit = await embed(text)
        if len(_cache) > 2000:
            _cache.clear()
        _cache[text] = hit
    return hit


async def relationship(workspace_id, plan: QueryPlan, vocab, embed) -> Optional[str]:
    """The edge type the question asks about, or None. Sets it on `plan` and re-plans execution."""
    from opencortyx.core.inference import choice
    from opencortyx.retrieval.l0_understanding.policy import plan_execution
    if vocab is None or not getattr(vocab, "edge_types", None) or embed is None:
        return None
    spec = await choice.workspace_model(workspace_id)
    if spec is None:
        return None
    question = plan.raw_query or plan.normalized_query
    qv = await _vec(embed, question)
    ranked = []
    for name in vocab.edge_types:
        v = await _vec(embed, _phrase(name))
        if v:
            ranked.append((_cos(qv, v), name))
    ranked.sort(reverse=True)
    names = [n for _, n in ranked[:CHOICES]]
    if not names:
        return None
    ask = "The relationship the question asks about is"
    c = await choice.choose(f"Question: {question}\n{ask}", [_phrase(n) for n in names] + [NONE],
                            model_spec=spec, baseline=f"Question: \n{ask}")
    plan.notes.append(f"relationship choice: {c.option if c else 'no model'}")
    if c is None or c.index >= len(names) or c.margin < MIN_MARGIN:
        return None
    edge = names[c.index]
    plan.edge_types = {edge: 1.0, **{k: v for k, v in (plan.edge_types or {}).items() if k != edge}}
    plan.strategies["graph"] = max(plan.strategies.get("graph", 0.0), 0.9)
    plan.max_hops = max(plan.max_hops or 1, 1)
    plan.policy = plan_execution(plan, confidence=plan.confidence)
    return edge
