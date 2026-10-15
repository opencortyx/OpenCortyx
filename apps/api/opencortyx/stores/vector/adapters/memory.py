"""In-process vector store for tests. Same filtering as the Qdrant adapter."""
from __future__ import annotations
from opencortyx.stores.vector.exceptions import VectorStoreValidationError

import copy
import math
from typing import Sequence

from opencortyx.stores.vector.models import (
    DOCUMENT_ATTR, SparseVector, VectorFilter, VectorHit, VectorPoint,
)
from opencortyx.stores.vector.port import VectorStore


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class MemoryVectorStore(VectorStore):
    provider_name = "memory"

    def __init__(self, dimension: int = 768):
        self.dimension = dimension
        self._points: dict[str, VectorPoint] = {}

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def describe(self) -> dict:
        return {"provider": self.provider_name, "version": "", "detail": "in-process, not durable"}

    async def close(self) -> None:
        return None

    @staticmethod
    def _payload(p: VectorPoint) -> dict:
        return {**p.fields, "workspace_id": str(p.workspace_id), "node_id": str(p.node_id), "attr": p.attr, "text": p.text}

    def _matching(self, flt: VectorFilter):
        return [p for p in self._points.values() if flt.matches(self._payload(p))]

    async def upsert(self, points: Sequence[VectorPoint]) -> None:
        for p in points:
            if p.dense is not None and len(p.dense) != self.dimension:
                raise VectorStoreValidationError(
                    f"expected {self.dimension} dimensions, got {len(p.dense)}", provider="memory")
            self._points[p.id] = copy.deepcopy(p)

    @staticmethod
    def _hits(scored, k):
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [VectorHit(node_id=p.node_id, attr=p.attr, text=p.text, score=s) for s, p in scored[:k]]

    async def search_dense(self, vector: list[float], flt: VectorFilter, k: int) -> list[VectorHit]:
        return self._hits([(_cosine(vector, p.dense), p) for p in self._matching(flt) if p.dense is not None], k)

    async def search_sparse(self, vector: SparseVector, flt: VectorFilter, k: int) -> list[VectorHit]:
        query = dict(zip(vector.indices, vector.values))
        scored = []
        for p in self._matching(flt):
            if p.sparse is None:
                continue
            score = sum(v * query.get(i, 0.0) for i, v in zip(p.sparse.indices, p.sparse.values))
            if score > 0:
                scored.append((score, p))
        return self._hits(scored, k)

    async def update_fields(self, workspace_id, node_id, fields: dict) -> None:
        for p in self._matching(VectorFilter(workspace_id=workspace_id, node_ids=[node_id])):
            p.fields = {**p.fields, **fields}

    async def scroll(self, flt: VectorFilter, *, limit: int, fields: Sequence[str]) -> list[dict]:
        return [{k: v for k, v in self._payload(p).items() if k in set(fields)}
                for p in self._matching(flt)[:limit]]

    async def embedded_nodes(self, workspace_id, node_ids: Sequence) -> set[str]:
        return {str(p.node_id) for p in self._matching(VectorFilter(
            workspace_id=workspace_id, node_ids=list(node_ids), attrs=[DOCUMENT_ATTR], embedded=True))}

    async def count(self, flt: VectorFilter) -> int:
        return len(self._matching(flt))

    async def count_nodes(self, workspace_id) -> int:
        return await self.count(VectorFilter(workspace_id=workspace_id, attrs=[DOCUMENT_ATTR], embedded=True))

    async def attribute_values(self, workspace_id) -> dict[str, tuple[int, int]]:
        texts: dict[str, list[str]] = {}
        for p in self._matching(VectorFilter(workspace_id=workspace_id, exclude_attrs=[DOCUMENT_ATTR])):
            texts.setdefault(p.attr, []).append(p.text)
        return {a: (len(v), len(set(v))) for a, v in texts.items()}

    async def delete(self, flt: VectorFilter) -> None:
        for p in self._matching(flt):
            self._points.pop(p.id, None)

    async def clear_org(self, workspace_id) -> None:
        for p in self._matching(VectorFilter(workspace_id=workspace_id)):
            self._points.pop(p.id, None)
