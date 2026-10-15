"""L3 — Feature extraction: turn a candidate's evidence into calibrated ranking signals.

    Candidate(evidence[], paths[], properties)  ─►  {feature: value in [0,1]}

WHY FEATURES AND NOT ONE SCORE

The previous engine ranked on a single `relevance` float. When anything overwrote it the ranking
inverted silently and no stage could tell: a graph-walk score of 0.0961 replaced a cosine of
0.8401 and moved a node from rank 4 to rank 1328. A feature vector cannot fail that way — each
signal is separately named, separately bounded, and separately inspectable, so a wrong ranking is
attributable to a feature rather than to "the score".

It is also the only shape a learned ranker can consume later. LambdaMART or a neural ranker needs
per-candidate features with stable semantics; producing them now means the hand-weighted scorer
can be swapped for a trained one without re-plumbing retrieval.

EVERY FEATURE IS DERIVED, NONE IS LOOKED UP

There is no node-type table and no edge-weight table. `type_fit` compares type NAMES as text
against what the question asked for; `path_quality` reads the scores L2 computed against the
query. A corpus with unfamiliar type names loses a small bonus rather than being mis-ranked.
"""
from __future__ import annotations

import math

from typing import Optional

from opencortyx.retrieval import experiments
from opencortyx.retrieval.contracts import Candidate, QueryPlan
from opencortyx.retrieval.l0_understanding.constraints import (
    to_iso, within,
)
from opencortyx.retrieval.l2_graph.edge_scoring import endpoint_bonus
from opencortyx.retrieval.l2_graph.expansion import content_terms

#: Names of every feature produced. Declared so a scorer can never silently read a feature that
#: was never computed — reading a missing key as 0.0 is how a signal disappears unnoticed.
FEATURE_NAMES = (
    "semantic",         # best cosine from any embedding surface
    "attribute",        # best cosine from an ATTRIBUTE surface specifically
    "attr_targeted",    # the attribute that fired is the one the question asked for
    "lexical",          # best normalised lexical rank
    "exact",            # an identifier matched literally
    "agreement",        # how many independent surfaces proposed this
    "path_quality",     # best path score from L2
    "path_brevity",     # shorter routes preferred
    "temporal_fit",     # satisfies the question's time bounds
    "constraint_fit",   # satisfies the question's attribute constraints
    "type_fit",         # node type resembles the requested answer type
    "text_present",     # has usable text at all
    "informative",      # says something the QUESTION did not already say
    "connectivity",     # how well this sits inside the RETRIEVED subgraph
    "evidence_quality", # how RELIABLE the best evidence is, not merely how much there is
    "cross_encoder",    # a learned (query, text) relevance score; ABSENT when not assessed
)

_ONLY_WHEN_ASSESSED = frozenset({
    "cross_encoder", "semantic", "attribute", "attr_targeted", "lexical",
    "path_quality", "path_brevity",
})

_SCALE_RELIABILITY = {
    "exact": 1.00,
    "binding": 0.95,     # a resolved entity, carrying its own confidence
    "cosine": 0.80,
    "bm25": 0.75,
    "path": 0.60,
    "rrf": 0.50,
    "ratio": 0.45,       # trigram similarity — a guess, and known to be one
    "constant": 0.30,    # membership, not relevance: the surface fired, that is all it says
}

#: Rank at which a lexical hit is considered worthless. Beyond this the normalised value floors,
#: rather than continuing to shrink toward an indistinguishable epsilon.
_LEXICAL_HORIZON = 200.0


def _rank_to_score(rank: Optional[int], horizon: float = _LEXICAL_HORIZON) -> float:
    if rank is None:
        return 0.0
    return max(0.0, 1.0 - (float(rank) / horizon))


def _candidate_date(props: dict) -> Optional[str]:
    """The node's date, whatever it is called and however it is spelled.

    Key discovery rather than a fixed field name: an ingest that writes `event_date` instead of
    `date_time` must still be temporally filterable.
    """
    if not props:
        return None
    for key, value in props.items():
        k = str(key).lower()
        if not isinstance(value, str):
            continue
        if "date" in k or "time" in k or k in ("valid_from", "timestamp", "when"):
            iso = to_iso(value)
            if iso:
                return iso
    return None


def extract(cand: Candidate, plan: Optional[QueryPlan] = None) -> dict[str, float]:
    """Compute every feature for one candidate. Values are bounded to [0,1]."""
    f = {name: 0.0 for name in FEATURE_NAMES if name not in _ONLY_WHEN_ASSESSED}

    if any(e.scale == "cosine" for e in cand.evidence):
        f["semantic"] = min(1.0, max(0.0, cand.best("cosine")))

    attr_ev = [e for e in cand.evidence if e.surface == "attr_embedding"]
    if attr_ev:
        best_attr = max(attr_ev, key=lambda e: e.raw_score)
        f["attribute"] = min(1.0, max(0.0, best_attr.raw_score))
        wanted = (plan.answer_type_hint if plan else "") or ""
        if wanted:
            f["attr_targeted"] = 0.0
            detail = (best_attr.detail or "").split("|")[0].lower()
            b = wanted.lower()
            if detail and (detail == b or detail in b or b in detail):
                f["attr_targeted"] = 1.0

    if experiments.on("attr_targeted_own") and plan is not None:
        own = _own_attribute_match(cand, plan)
        if own is not None:
            f["attr_targeted"] = max(own, f.get("attr_targeted", 0.0))

    lex_ranks = [e.rank for e in cand.evidence if e.surface in ("bm25", "fuzzy")]
    if lex_ranks:
        f["lexical"] = _rank_to_score(min(lex_ranks))
    f["exact"] = 1.0 if any(e.scale == "exact" for e in cand.evidence) else 0.0

    # ── agreement across independent surfaces ───────────────────────────────────────────────
    n = len(cand.surfaces)
    # Saturating rather than linear: the step from one surface to two is far more informative
    # than the step from four to five.
    f["agreement"] = min(1.0, (n - 1) / 3.0) if n > 1 else 0.0

    # ── path evidence from L2 ───────────────────────────────────────────────────────────────
    if cand.paths:
        best_path = max(cand.paths, key=lambda p: p.score)
        f["path_quality"] = min(1.0, max(0.0, best_path.score))
        f["path_brevity"] = 1.0 / float(max(best_path.length, 1))

    # ── constraint satisfaction ─────────────────────────────────────────────────────────────
    iso = _candidate_date(cand.properties)
    if plan and plan.temporal:
        # Unknown dates PASS but earn no bonus: absence of evidence is not evidence of fit.
        ok = all(within(tc, iso) for tc in plan.temporal)
        f["temporal_fit"] = 1.0 if (ok and iso) else (0.5 if ok else 0.0)
    if plan and plan.attributes:
        hits = 0
        for ac in plan.attributes:
            val = (cand.properties or {}).get(ac.attr)
            if val is None:
                continue
            if ac.op == "eq" and str(val).lower() == str(ac.value).lower():
                hits += 1
            elif ac.op in ("gt", "gte", "lt", "lte"):
                try:
                    v = float(str(val).replace(",", ""))
                    t = float(ac.value)
                    if ((ac.op == "gt" and v > t) or (ac.op == "gte" and v >= t)
                            or (ac.op == "lt" and v < t) or (ac.op == "lte" and v <= t)):
                        hits += 1
                except (TypeError, ValueError):
                    pass
        f["constraint_fit"] = hits / float(len(plan.attributes))

    # ── shape ───────────────────────────────────────────────────────────────────────────────
    f["type_fit"] = endpoint_bonus(cand.node_type, plan.answer_type_hint if plan else "") / 0.15 \
        if plan and plan.answer_type_hint else 0.0
    f["type_fit"] = min(1.0, f["type_fit"])
    f["text_present"] = 1.0 if (cand.text or "").strip() else 0.0

    f["evidence_quality"] = evidence_quality(cand)
    f["informative"] = informativeness(cand, plan)

    return f


def _own_attribute_match(cand: Candidate, plan: QueryPlan) -> Optional[float]:
    """Does this candidate carry a field the QUESTION is asking about?

    Returns None when the node exposes no usable fields — NOT ASSESSED, never 0.0, so a node
    without attributes is not scored as "has the wrong ones".

    Matching is on the field NAME against the question's content words, stem-aware: a question
    asking about a "nickname" should be answered by a node carrying `nickname_used`, and one
    asking "how many dogs" by a node carrying `number_of_dogs`.
    """
    from opencortyx.core.substrate.document import is_indexable_attr
    from opencortyx.retrieval.common.text_norm import stem

    props = cand.properties or {}
    names = [k for k, v in props.items() if is_indexable_attr(k, v)]
    if not names:
        return None
    asked = content_terms(plan.normalized_query or plan.raw_query)
    hint = (plan.answer_type_hint or "").strip().lower()
    if hint:
        asked = asked | content_terms(hint)
    if not asked:
        return None
    asked_stems = {stem(a) for a in asked}
    best = 0.0
    for name in names:
        parts = {p for p in name.lower().replace("-", "_").split("_") if len(p) > 2}
        if not parts:
            continue
        hits = sum(1 for p in parts if stem(p) in asked_stems)
        if hits:
            best = max(best, hits / len(parts))
    return best


def informativeness(cand: Candidate, plan: Optional[QueryPlan]) -> float:
    """Does this candidate say anything the QUESTION did not already say?

    THE MEASURED FAILURE THIS EXISTS FOR. A bare entity node whose entire text is `"Joanna"`
    scores `semantic=1.00, lexical=1.00` on "What is Joanna allergic to?" — a perfect match on
    every similarity feature, carrying no information whatsoever. Across nine diverse probes it
    took rank 1 in seven of them, and for "Tell me about Joanna" the whole top-8 was
    `Joanna, Joanna, Joanna's story, Joanna's book, Joanna's work, ...`.

    Two costs, both real. The context builder gates these out at L4 (`carries_content`), so the
    ranking above it is spent ordering material that will be discarded. And the cross-encoder
    only scores the top N — so its entire budget went on name fragments, which is why a model
    that separates 0.995 from 0.000016 changed the benchmark by 0.00.

    THIS IS NOT A TYPE RULE. It never asks what a node IS. It asks what its text ADDS beyond the
    words already in the question: a node that merely echoes the query answers nothing, whatever
    it is called. A corpus of differently-named types behaves identically.
    """
    text = (cand.text or cand.label or "").strip()
    if not text:
        return 0.0
    own = content_terms(text)
    if not own:
        return 0.0
    if plan is None:
        return 1.0
    asked = content_terms(plan.normalized_query or plan.raw_query)
    if not asked:
        return 1.0
    novel = own - asked
    return round(len(novel) / len(own), 4)


def evidence_quality(cand: Candidate) -> float:
    """How reliable the best evidence for this candidate is — distinct from how much there is.

    `agreement` counts DISTINCT SURFACES: three surfaces proposing a node is three votes. It says
    nothing about whether those votes are exact identifier matches or trigram guesses, so a node
    found three weak ways scored like a node found three strong ways.

    Each piece of evidence is scored as `reliability(scale) x its own strength`, and the BEST is
    taken rather than the sum — three weak signals do not add up to one exact match, which is the
    same rule entity resolution applies when combining its methods.
    """
    best = 0.0
    for ev in cand.evidence:
        reliability = _SCALE_RELIABILITY.get(ev.scale, 0.40)
        # `raw_score` is on its own scale and is NOT comparable across surfaces, so it is used
        # only where it is already bounded in [0,1]. Elsewhere the rank carries the strength.
        if ev.scale in ("cosine", "ratio", "binding", "path"):
            strength = max(0.0, min(1.0, float(ev.raw_score)))
        else:
            strength = _rank_to_score(ev.rank, horizon=50.0)
        best = max(best, reliability * strength)
    return min(1.0, best)


def connectivity(graph) -> dict:
    """How well each candidate sits inside the subgraph that was actually retrieved.

    THE ONE FEATURE A FLAT LIST CANNOT PRODUCE. Every other feature is a property of a candidate
    on its own; this one is a property of its POSITION among the others, which is the stated
    reason `CandidateGraph` exists rather than a `list[Candidate]`.

    Log-scaled and normalised against the best-connected candidate. A linear count would let one
    hub saturate the feature and flatten everyone else to near-zero — the shape that made a
    PPR score, saturating at 1.0, poison the single relevance number it fed.

    Candidates in a graph with no edges all score 0.0, which is correct: nothing was learned
    about their position, and a feature that has learned nothing must contribute nothing rather
    than a default.
    """
    adj = graph.adjacency() if graph is not None else {}
    if not adj:
        return {}
    degrees = {nid: len(peers) for nid, peers in adj.items()}
    top = max(degrees.values(), default=0)
    if top <= 0:
        return {nid: 0.0 for nid in degrees}
    ceiling = math.log(1.0 + top)
    return {nid: min(1.0, math.log(1.0 + d) / ceiling) for nid, d in degrees.items()}


def annotate(candidates, plan: Optional[QueryPlan] = None, graph=None) -> None:
    """Attach features to every candidate, in place.

    `graph` is optional so the per-candidate features stay testable without one. Passing it adds
    `connectivity`; omitting it leaves that feature at 0.0 for everyone, which is a uniform
    offset and therefore changes no ordering.
    """
    conn = connectivity(graph) if graph is not None else {}
    for c in candidates:
        c.features.update(extract(c, plan))
        if conn:
            c.features["connectivity"] = conn.get(c.node_id, 0.0)
