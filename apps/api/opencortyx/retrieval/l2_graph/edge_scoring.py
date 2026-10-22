"""L2 — Edge relevance, COMPUTED against the query. No weight table anywhere.

    question ─embed─┐
                    ├─► cosine(relation name, question) ─┐
    relation name ──┘                                    ├─► edge relevance
    corpus frequency ─► rarity (IDF) ────────────────────┘

WHY THIS CANNOT BE A TABLE

The previous engine decided expansion with two literals: `_PROVENANCE_EDGES` (a set — membership
granted the right to inherit relevance at a flat 0.9) and everything else at a flat 0.05. Two
consequences, both measured:

  - An edge type absent from the set could never carry relevance, whatever the question asked.
  - The flat 0.9 meant a question about *when* something happened and a question about *who owns*
    something walked the identical edges with identical priority.

A table also cannot transfer between corpora. `TRANSACTED_WITH` is meaningful in a payments graph
and absent from a conversation graph; a hardcoded weight for it is dead code in one and a missing
entry in the other.

THE APPROACH, AND WHERE IT COMES FROM

Current KG-QA systems (PropRAG, FiDeLiS, Search-on-Graph, S-Path-RAG) converge on scoring
candidate RELATIONS by semantic similarity to the question at each hop, then pruning. Relation
names are short natural-language phrases once de-snaked — "EXTRACTED_FROM" reads as "extracted
from" — so the same encoder that embeds the question embeds them.

Rarity is blended in for the same reason IDF exists in lexical search: an edge type joining
almost everything to almost everything discriminates nothing, however well its name matches.

The two blend coefficients below are the only tuned numbers in this module. They are HYPER-
PARAMETERS of a formula, not per-type constants: changing a corpus changes the scores because the
inputs change, not because a table needs editing.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Optional


from opencortyx.core.substrate import NodeRepo
from opencortyx.retrieval.common.text_norm import (  # noqa: F401
    humanize, singularize,
)

W_SEMANTIC = 0.75
W_RARITY = 0.25

#: Relations below this are not walked. A floor rather than a filter list: an edge type is
#: excluded because the QUESTION does not implicate it, never because of what it is called.
MIN_EDGE_RELEVANCE = 0.12


@dataclass
class RelationProfile:
    """What the graph knows about one edge type, independent of any question."""
    relation: str
    count: int
    rarity: float = 0.0                 # 0..1, higher = more discriminating
    text: str = ""


@dataclass
class EdgeRelevance:
    """A per-question scoring of every relation in the graph."""
    scores: dict[str, float] = field(default_factory=dict)
    profiles: dict[str, RelationProfile] = field(default_factory=dict)
    explain: dict[str, str] = field(default_factory=dict)

    def score(self, relation: str) -> float:
        return self.scores.get(relation, 0.0)

    def walkable(self, threshold: float = MIN_EDGE_RELEVANCE) -> list[str]:
        return [r for r, s in sorted(self.scores.items(), key=lambda kv: -kv[1])
                if s >= threshold]

    def top(self, n: int = 8) -> list[tuple[str, float]]:
        return sorted(self.scores.items(), key=lambda kv: -kv[1])[:n]


def _profiles(counts: dict[str, int]) -> dict[str, RelationProfile]:
    total = sum(counts.values()) or 1
    out: dict[str, RelationProfile] = {}
    for rel, c in counts.items():
        # IDF-shaped: a relation covering the whole graph says nothing about which part answers a question.
        rarity = math.log(1.0 + total / max(c, 1)) / math.log(1.0 + total)
        out[rel] = RelationProfile(relation=rel, count=c, rarity=max(0.0, min(1.0, rarity)), text=humanize(rel))
    return out


async def profile_relations_for(workspace_id: uuid.UUID, node_ids: list[str]) -> dict[str, RelationProfile]:
    """Relations incident to these seeds, with counts local to them.

    Profiling the whole organization scores relations that cannot be walked from the seeds; the
    question "which relations are worth walking" has to be asked of the seeds.
    """
    if not node_ids:
        return {}
    return _profiles(await NodeRepo().incident_edge_counts(workspace_id, [str(n) for n in node_ids]))


async def profile_relations(workspace_id: uuid.UUID) -> dict[str, RelationProfile]:
    """Every relation present and how common it is, discovered rather than declared."""
    return _profiles(await NodeRepo().count_edges_by_type(workspace_id))


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sa = sb = 0.0
    for x, y in zip(a, b):
        x = float(x); y = float(y)
        dot += x * y; sa += x * x; sb += y * y
    if sa <= 0 or sb <= 0:
        return 0.0
    return dot / (math.sqrt(sa) * math.sqrt(sb))


class EdgeScorer:
    """Scores every relation against one question. Relation embeddings are cached per process —
    an edge vocabulary is small and static relative to query volume."""

    def __init__(self, embed_batch):
        self._embed_batch = embed_batch
        self._vec_cache: dict[str, list] = {}

    async def _relation_vectors(self, profiles: dict[str, RelationProfile]) -> dict[str, list]:
        missing = [r for r in profiles if r not in self._vec_cache]
        if missing:
            texts = [profiles[r].text or humanize(r) for r in missing]
            vecs = await self._embed_batch(texts)
            for r, v in zip(missing, vecs):
                if v:
                    self._vec_cache[r] = v
        return {r: self._vec_cache[r] for r in profiles if r in self._vec_cache}

    async def score_relations(self, workspace_id: uuid.UUID, query_vec: list,
                              profiles: Optional[dict[str, RelationProfile]] = None,
                              ) -> EdgeRelevance:
        profiles = profiles if profiles is not None else await profile_relations(workspace_id)
        if not profiles:
            return EdgeRelevance()
        vecs = await self._relation_vectors(profiles)

        raw: dict[str, float] = {}
        for rel, prof in profiles.items():
            sem = _cosine(query_vec, vecs.get(rel)) if query_vec else 0.0
            raw[rel] = max(0.0, sem)

        vals = list(raw.values())
        spread = (max(vals) - min(vals)) if vals else 0.0
        ordered = sorted(raw.items(), key=lambda kv: -kv[1])
        n = max(len(ordered) - 1, 1)
        if spread >= 0.05:
            sem_norm = {rel: (1.0 - i / n) for i, (rel, _) in enumerate(ordered)}
        else:
            sem_norm = dict(raw)

        er = EdgeRelevance(profiles=profiles)
        for rel, prof in profiles.items():
            s = W_SEMANTIC * sem_norm.get(rel, 0.0) + W_RARITY * prof.rarity
            er.scores[rel] = round(max(0.0, min(1.0, s)), 4)
            er.explain[rel] = (f"sem={raw[rel]:.3f}->{sem_norm.get(rel, 0):.2f} "
                               f"rarity={prof.rarity:.2f} n={prof.count}")
        return er


def endpoint_bonus(node_type: str, answer_type_hint: str) -> float:
    """Small boost when a candidate's type resembles what the question asks FOR.

    Compared as TEXT, never looked up: "which companies" boosts a node whose type string looks
    like "company". An unknown or absent hint contributes nothing rather than penalising, so a
    corpus whose type names do not resemble English is unaffected instead of being ranked wrongly.
    """
    if not node_type or not answer_type_hint:
        return 0.0
    a, b = humanize(node_type), humanize(answer_type_hint)
    if not a or not b:
        return 0.0
    if a == b or singularize(a) == singularize(b):
        return 0.15
    if a in b or b in a:
        return 0.08
    return 0.0
