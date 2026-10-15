"""L1 — Embedding retrieval: one ANN surface per attribute.

Each indexable attribute of a node is embedded on its own (`NodeRepo.index_embeddings`), so a node
is found by the one field that matches a question instead of an average of all its fields, and the
evidence names which field fired: `attr_embedding(description)` is a different claim from
`attr_embedding(speaker)`.
"""
from __future__ import annotations

import uuid
from typing import Optional

from opencortyx.core.substrate import document as _node_document
from opencortyx.core.substrate import NodeRepo
from opencortyx.retrieval.contracts import Candidate, Evidence
from opencortyx.stores.vector import DOCUMENT_ATTR, VectorFilter


_SPECIFICITY_TTL_S = 600
_specificity_cache: dict[str, tuple[float, dict]] = {}


async def attribute_specificity(workspace_id: uuid.UUID) -> dict[str, float]:
    """How much a match on each attribute narrows the corpus: distinct values over rows, in (0, 1].

    An attribute holding two values across thousands of nodes (`speaker`) matches everything a
    name matches, so its cosine is scaled down rather than out-voting attributes that discriminate.
    Cached per organization for ten minutes: it reads every attribute point.
    """
    import time
    cached = _specificity_cache.get(str(workspace_id))
    if cached and time.monotonic() - cached[0] < _SPECIFICITY_TTL_S:
        return cached[1]
    out: dict[str, float] = {}
    for attr, (n, distinct) in (await NodeRepo().vectors.attribute_values(workspace_id)).items():
        if n > 0:
            out[attr] = max(0.05, min(1.0, distinct / float(n)))
    _specificity_cache[str(workspace_id)] = (time.monotonic(), out)
    return out


class EmbeddingRetrieval:
    surface_attr = "attr_embedding"

    def __init__(self, embed, specificity: Optional[dict] = None, repo: Optional[NodeRepo] = None):
        self._embed = embed
        self._specificity = specificity or {}
        self._repo = repo or NodeRepo()

    async def load_specificity(self, workspace_id: uuid.UUID) -> dict:
        if not self._specificity:
            self._specificity = await attribute_specificity(workspace_id)
        return self._specificity

    async def attr_ann(self, workspace_id: uuid.UUID, qvec: list, k: int, attrs: Optional[list[str]] = None,
                       collection_ids: Optional[list] = None, rep: str = "") -> list[Candidate]:
        """ANN over attribute surfaces; `attrs` restricts to named fields. Ranks are per attribute."""
        flt = VectorFilter(workspace_id=workspace_id, attrs=attrs, exclude_attrs=[DOCUMENT_ATTR], exclude_superseded=True,
                           collection_ids=collection_ids or None, include_unscoped=True)
        hits = await self._repo.vectors.search_dense(list(qvec), flt, k)
        nodes = {str(n.id): n for n in await self._repo.get_many(workspace_id, [h.node_id for h in hits])}
        spec = self._specificity or {}
        per_attr_rank: dict[str, int] = {}
        out = []
        for hit in hits:
            node = nodes.get(str(hit.node_id))
            if node is None:
                continue
            rank = per_attr_rank.get(hit.attr, 0)
            per_attr_rank[hit.attr] = rank + 1
            c = Candidate(node_id=node.id, node_type=node.type or "", label=node.label or "",
                          text=_node_document.from_row(node.properties, node.label, node.type),
                          properties=node.properties or {})
            c.add(Evidence(surface=self.surface_attr, rank=rank, raw_score=float(hit.score) * spec.get(hit.attr, 1.0),
                           scale="cosine", detail=f"{hit.attr}|{rep}" if rep else hit.attr,
                           matched_text=hit.text or ""))
            out.append(c)
        return out

    async def retrieve(self, workspace_id: uuid.UUID, qvecs: dict[str, list], k: int = 100,
                       attrs: Optional[list[str]] = None, collection_ids: Optional[list] = None) -> list[Candidate]:
        """Every query representation ("full", "concept", "entity") through the attribute surfaces."""
        results: list[Candidate] = []
        for rep, qv in (qvecs or {}).items():
            if qv:
                results += await self.attr_ann(workspace_id, qv, k, attrs, collection_ids, rep=rep)
        return results
