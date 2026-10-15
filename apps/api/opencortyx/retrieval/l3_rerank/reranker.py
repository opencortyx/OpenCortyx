"""L3 — Evidence-aware reranking with diversity.

    candidates + features ─► intent-shaped linear score ─► MMR diversification ─► ordered list

TWO STAGES, DELIBERATELY SEPARATE

`score()` answers "how well does this candidate answer the question?". `diversify()` answers
"given what is already selected, what does this candidate ADD?". Merging them produces a ranker
that cannot express redundancy: twenty near-identical passages each score highly and each is
individually correct, and the resulting context answers one facet of the question twenty times.

WEIGHTS ARE PER-INTENT, NOT PER-TYPE

The weights below vary by what the QUESTION is, never by what a node IS. An exact lookup should
trust a literal identifier match over semantic similarity; a multi-hop question should trust path
quality over surface cosine. That is a property of the question.

This is the seam for a learned ranker. `score_features` consumes a feature dict and returns a
float; replacing it with LambdaMART or a trained neural ranker changes nothing else in the
pipeline, because the features are already named, bounded and produced per candidate.

ABSTENTION IS DECIDED HERE, ON A NAMED FEATURE

The previous engine abstained on `max(relevance)` — the single float that anything could
overwrite. When a graph-walk score replaced a cosine, the gate compared 0.016 against a 0.3 floor
and refused questions whose answers had been retrieved. Here the gate reads `semantic`, which no
other stage writes.
"""
from __future__ import annotations

from typing import Iterable, Optional

from opencortyx.retrieval.contracts import (
    Candidate, Intent, QueryPlan,
)
from opencortyx.retrieval.l3_rerank.features import (
    _ONLY_WHEN_ASSESSED, FEATURE_NAMES, extract,
)

#: The features every candidate always has. Used to decide whether a candidate still
#: needs extracting — see `rerank`.
_ALWAYS_ASSESSED = tuple(n for n in FEATURE_NAMES if n not in _ONLY_WHEN_ASSESSED)

#: Baseline weights. Every feature carries a non-zero weight so no signal is silently ignored.
_BASE = {
    "semantic": 1.00,
    "attribute": 0.70,
    "attr_targeted": 0.45,
    "lexical": 0.40,
    "exact": 0.60,
    "agreement": 0.55,
    "path_quality": 0.50,
    "path_brevity": 0.15,
    "temporal_fit": 0.30,
    "constraint_fit": 0.40,
    "type_fit": 0.25,
    "text_present": 0.20,
    "informative": 0.85,
    "connectivity": 0.20,
    "evidence_quality": 0.45,
    "cross_encoder": 1.20,
}

#: Per-INTENT overrides. Keyed on the question's shape, never on a node's type.
_BY_INTENT: dict[Intent, dict[str, float]] = {
    Intent.EXACT_LOOKUP: {"exact": 1.30, "lexical": 0.85, "attr_targeted": 0.90,
                          "semantic": 0.55, "path_quality": 0.15},
    Intent.KEYWORD:      {"lexical": 1.00, "semantic": 0.75, "path_quality": 0.15},
    Intent.SEMANTIC:     {"semantic": 1.10, "attribute": 0.80},
    Intent.ENTITY:       {"exact": 0.90, "attribute": 0.85, "path_quality": 0.45,
                          "type_fit": 0.45},
    Intent.RELATIONSHIP: {"path_quality": 1.00, "path_brevity": 0.35, "agreement": 0.70,
                          "semantic": 0.75, "connectivity": 0.55},
    Intent.MULTI_HOP:    {"path_quality": 1.20, "path_brevity": 0.40, "agreement": 0.75,
                          "semantic": 0.65, "connectivity": 0.60},
    Intent.TEMPORAL:     {"temporal_fit": 1.00, "attribute": 0.95, "attr_targeted": 0.70,
                          "semantic": 0.70},
    Intent.AGGREGATION:  {"constraint_fit": 0.95, "temporal_fit": 0.70, "agreement": 0.70,
                          "type_fit": 0.55},
}

#: Below this best-`semantic`, nothing was retrieved that resembles the question.
ABSTAIN_FLOOR = 0.28

#: MMR trade-off. 1.0 = pure relevance, 0.0 = pure novelty.
MMR_LAMBDA = 0.72


def weights_for(plan: Optional[QueryPlan]) -> dict[str, float]:
    w = dict(_BASE)
    if plan is not None:
        w.update(_BY_INTENT.get(plan.intent, {}))
    return w


def score_features(features: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted sum over NAMED features, normalised by the weight mass actually used.

    Normalising means adding a feature does not inflate every score, so thresholds stay
    comparable across releases.

    AN ABSENT FEATURE IS NOT A ZERO. A feature missing from the dict was NOT ASSESSED, and its
    weight is left out of the normalising mass entirely; a feature present as `0.0` was assessed
    and scored nothing. Treating the two alike penalises a candidate for a measurement nobody
    made — and that matters the moment a feature is computed for only part of the pool, which is
    exactly what a cost-bounded cross-encoder does. Scoring the top 30 and reading "absent" as
    "worst" would make being outside the cross-encoder's budget a demotion, turning a COST bound
    into a ranking decision.
    """
    total = mass = 0.0
    for name in FEATURE_NAMES:
        w = weights.get(name, 0.0)
        if w <= 0 or name not in features:
            continue
        total += w * float(features.get(name, 0.0))
        mass += w
    return total / mass if mass else 0.0


def rerank(candidates: Iterable[Candidate], plan: Optional[QueryPlan] = None,
           limit: int = 0) -> list[Candidate]:
    """Score every candidate on its evidence. Does not truncate unless asked."""
    w = weights_for(plan)
    out = []
    for c in candidates:
        if not c.features or not all(k in c.features for k in _ALWAYS_ASSESSED):
            c.features.update(extract(c, plan))
        c.rerank_score = score_features(c.features, w)
        out.append(c)
    out.sort(key=lambda c: c.rerank_score or 0.0, reverse=True)
    return out[:limit] if limit and limit > 0 else out


def _overlap(a: Candidate, b: Candidate) -> float:
    """Redundancy between two candidates, from their text and their provenance.

    Token overlap catches near-duplicate passages. Shared provenance catches the subtler case: two
    nodes derived from the SAME source are the same evidence twice, even when their wording
    differs entirely.

    Only between nodes of the same type, though. Two chunks of one document are one piece of
    evidence; a project's lead and its risk are two facts that happen to share the project as
    parent and as graph seed. Counted as redundant, "who leads it and what is its biggest risk?"
    kept the risk, dropped the lead, and the answer named the lead of another project instead.
    """
    ta = {w for w in (a.text or "").lower().split() if len(w) > 3}
    tb = {w for w in (b.text or "").lower().split() if len(w) > 3}
    jac = len(ta & tb) / len(ta | tb) if (ta and tb) else 0.0
    if (a.node_type or "").lower() != (b.node_type or "").lower():
        return jac

    def keys(c: Candidate, names) -> set:
        s = set()
        for key in names:
            v = (c.properties or {}).get(key)
            if v:
                s.add(f"{key}={v}")
        return s

    derive_a = keys(a, ("source_ref", "parent_id", "derived_from", "extracted_from"))
    derive_b = keys(b, ("source_ref", "parent_id", "derived_from", "extracted_from"))
    for p in a.paths:
        if p.seed_id:
            derive_a.add(f"seed={p.seed_id}")
    for p in b.paths:
        if p.seed_id:
            derive_b.add(f"seed={p.seed_id}")

    contain_a = keys(a, ("session", "dialogue_id", "document_id"))
    contain_b = keys(b, ("session", "dialogue_id", "document_id"))

    derived = (len(derive_a & derive_b) / len(derive_a | derive_b)) if (derive_a and derive_b) else 0.0
    contained = (len(contain_a & contain_b) / len(contain_a | contain_b)) \
        if (contain_a and contain_b) else 0.0

    return max(jac, 0.9 * derived, 0.25 * contained)


def diversify(ranked: list[Candidate], limit: int, lambda_: float = MMR_LAMBDA) -> list[Candidate]:
    """Maximal Marginal Relevance: relevance minus redundancy against what is already chosen.

    The failure this prevents is a context window filled with one facet of the answer. Session
    collapse in the previous engine was a crude approximation — it deduplicated by parent, which
    also discarded genuinely distinct evidence that happened to share a parent.
    """
    if limit <= 0 or not ranked:
        return ranked
    selected: list[Candidate] = []
    pool = list(ranked)
    while pool and len(selected) < limit:
        best, best_val = None, None
        for c in pool:
            rel = c.rerank_score if c.rerank_score is not None else c.fusion_score
            red = max((_overlap(c, s) for s in selected), default=0.0)
            val = lambda_ * rel - (1.0 - lambda_) * red
            if best_val is None or val > best_val:
                best, best_val = c, val
        selected.append(best)
        pool.remove(best)
    return selected


def should_abstain(candidates: Iterable[Candidate], floor: float = ABSTAIN_FLOOR) -> bool:
    """True when nothing resembling the question was retrieved.

    Reads `semantic` only — a named feature no other stage writes — so a graph score can never
    make the system refuse a question whose answer it had.
    """
    best = 0.0
    for c in candidates:
        best = max(best, float(c.features.get("semantic", 0.0)) if c.features
                   else c.best("cosine"))
    return best < floor


def rank_paths(graph, plan: Optional[QueryPlan] = None, limit: int = 10) -> list:
    """Rank PATHS as objects in their own right, not as a feature of their endpoint.

    `path_quality` and `path_brevity` are properties of a NODE: they answer "was this candidate
    reached over a good route". That is the wrong question for a relationship or multi-hop
    question, where the ROUTE IS THE ANSWER — "which companies connect to John through Germany"
    is answered by the connection, and returning only its endpoint discards what was asked for.

    A path is scored by its own traversal quality (`Path.score`, already the product of edge
    relevances modulated by the nodes landed on) lifted by how well its TERMINAL ranks. A route
    to a strong candidate is better evidence than the same route to a weak one, and a strong
    candidate reached by a poor route is weaker evidence than its own score suggests.

    Deduplicated by route, because the same path is reachable from several candidates that share
    a prefix, and listing one connection five times is not five pieces of evidence.
    """
    if graph is None or not getattr(graph, "candidates", None):
        return []

    best_by_route: dict = {}
    for cand in graph.candidates.values():
        terminal_strength = float(cand.final_score or 0.0)
        for path in cand.paths:
            if not path.steps:
                continue
            key = path.as_tuple()
            score = float(path.score) * (0.5 + 0.5 * min(1.0, terminal_strength))
            current = best_by_route.get(key)
            if current is None or score > current[0]:
                best_by_route[key] = (score, path, cand)

    ordered = sorted(best_by_route.values(), key=lambda t: -t[0])
    return [(round(score, 6), path, cand) for score, path, cand in ordered[:limit]]
