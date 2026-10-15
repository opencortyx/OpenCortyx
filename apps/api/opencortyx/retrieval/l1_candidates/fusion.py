"""L1 — Candidate fusion: rank-based, evidence-preserving.

    surface A ranking ─┐
    surface B ranking ─┼─► RRF ─► fused score  (evidence kept intact per candidate)
    surface C ranking ─┘

WHY RANK AND NOT SCORE

BM25 returns ~0–30. Cosine returns 0–1. Trigram similarity returns 0–1 but is not distributed like
cosine. PPR mass depends on graph size. A weighted sum of those is arithmetic on incomparable
units, and whichever surface has the widest range quietly decides the ranking.

Rank is comparable by construction. RRF gives each surface's rank-r candidate `w / (k + r)`, so a
surface contributes according to where it PUT something, not how it scored it.

TWO FAILURES THIS IS SHAPED AROUND, BOTH MEASURED ON THIS SYSTEM

1. Truncating on one ordering while ranking on another. A previous build cut the pool by RRF and
   then ordered everything downstream by cosine, discarding exactly the candidates the two
   disagreed about — and they disagree by construction, since RRF rewards agreement ACROSS
   surfaces while cosine rewards depth WITHIN one. `fuse` therefore returns a complete ordering
   and never truncates; cutting is a separate, explicit decision.

2. A single `relevance` number of unknown provenance. When a graph-walk score overwrote a cosine,
   a node whose true similarity was 0.8401 entered ranking at 0.0961 and fell from rank 4 to 1328.
   Here the fused score is a NEW field; per-surface evidence is never overwritten, and every
   downstream stage can still ask what each surface actually said.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from opencortyx.retrieval.contracts import Candidate, CandidateGraph

#: RRF smoothing. 60 is the standard value: large enough that rank 1 and rank 2 are not wildly
#: separated, small enough that rank 200 still contributes something.
RRF_K = 60

UNPLANNED_SURFACE_WEIGHT = 0.15

REL_RANK_SCALE = 120


def merge(candidate_lists: Iterable[Iterable[Candidate]]) -> CandidateGraph:
    """Union candidates, accumulating evidence. Never overwrites, never dedups evidence away."""
    graph = CandidateGraph()
    for lst in candidate_lists:
        for cand in (lst or []):
            graph.upsert(cand)
    return graph


def rrf(graph: CandidateGraph, weights: Optional[dict[str, float]] = None,
        k: int = RRF_K) -> CandidateGraph:
    """Score every candidate by weighted reciprocal rank fusion, in place.

    `weights` comes from the L0 plan. A surface's weight scales its VOTE; it never removes the
    vote, because a plan is a hypothesis and a wrong hypothesis must cost precision rather than
    make a question unanswerable.

    A candidate's best rank per surface is used, so a node appearing three times in one surface's
    output does not out-vote a node appearing once in three surfaces — that would reward
    duplication rather than agreement.
    """
    w = weights or {}
    from opencortyx.retrieval import experiments as _exp
    _relative = _exp.on("rrf_relative_rank")
    depth: dict[str, int] = defaultdict(int)
    if _relative:
        for cand in graph.candidates.values():
            for ev in cand.evidence:
                depth[ev.surface] += 1
    for cand in graph.candidates.values():
        best_per_surface: dict[str, int] = {}
        for ev in cand.evidence:
            cur = best_per_surface.get(ev.surface)
            if cur is None or ev.rank < cur:
                best_per_surface[ev.surface] = ev.rank
        score = 0.0
        for surface, rank in best_per_surface.items():
            if _relative:
                eff = (rank / max(depth.get(surface, 1), 1)) * REL_RANK_SCALE
            else:
                eff = rank
            score += w.get(surface, UNPLANNED_SURFACE_WEIGHT) / (k + eff + 1)
        cand.fusion_score = score
        cand.features["n_surfaces"] = float(len(best_per_surface))
        cand.features["best_rank"] = float(min(best_per_surface.values())) \
            if best_per_surface else 0.0
    return graph


def surface_report(graph: CandidateGraph) -> dict[str, dict]:
    """Per-surface contribution: how many candidates it proposed, and how many ONLY it proposed.

    This is the measurement that decides whether a surface earns its latency. A surface that
    contributes many candidates but no unique ones is paying for duplication.
    """
    by_surface: dict[str, set] = defaultdict(set)
    for nid, cand in graph.candidates.items():
        for s in cand.surfaces:
            by_surface[s].add(nid)
    report: dict[str, dict] = {}
    for s, ids in by_surface.items():
        others: set = set()
        for other, oids in by_surface.items():
            if other != s:
                others |= oids
        report[s] = {"n": len(ids), "unique": len(ids - others),
                     "overlap": len(ids & others)}
    return report


def cut(graph: CandidateGraph, limit: int) -> list[Candidate]:
    """Take the top `limit` by fused score.

    Separate from `rrf` on purpose. Fusion produces a complete ordering; truncation is a policy
    decision with a measured cost curve, and conflating the two is what let a cost constant
    (`RERANK_POOL_N`, sized for a cross-encoder's budget) silently decide recall.
    """
    ranked = graph.ranked()
    return ranked[:limit] if limit and limit > 0 else ranked
