"""Qdrant adapter.

One collection holds every organization; `workspace_id` is a tenant index, so each search reads only its
organization's segment of the HNSW graph. Points have a named dense vector and a named BM25 sparse
vector with Qdrant's IDF modifier. The synchronous client runs in worker threads, so the adapter
works from Celery tasks that each run on a fresh event loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from typing import Any, Sequence

from opencortyx.stores.vector.exceptions import VectorStoreOperationError
from opencortyx.stores.vector.models import (
    DOCUMENT_ATTR, SparseVector, VectorFilter, VectorHit, VectorPoint, type_variants,
)
from opencortyx.stores.vector.port import VectorStore

logger = logging.getLogger("opencortyx.stores.vector.qdrant")

DENSE = "dense"
SPARSE = "bm25"
#: The tenant field's name before it was renamed `workspace_id`; a collection an older release
#: created still has it until `_rename_tenant_field` runs at start-up.
_LEGACY_TENANT = "org_id"
_KEYWORD_FIELDS = ("node_id", "attr", "types", "collection_id", "source_ref", "sensitivity", "source", "names")
_BOOL_FIELDS = ("superseded", "structural", "internal", "hidden", "memory", "embedded")
_TEXT_FIELDS = ("search", "names_text")
_BATCH = 256


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]



def _quantization(m):
    """int8 scalar quantization kept in memory: a quarter of the dense vectors' size."""
    return m.ScalarQuantization(scalar=m.ScalarQuantizationConfig(type=m.ScalarType.INT8, quantile=0.99,
                                                                   always_ram=True))

class QdrantVectorStore(VectorStore):
    provider_name = "qdrant"

    def __init__(self, url: str, collection: str, dimension: int, api_key: str | None = None,
                 timeout_s: float = 30.0):
        self.url = url
        self.collection = collection
        self.dimension = dimension
        self.api_key = api_key or None
        self.timeout = int(timeout_s)
        self._client = None
        self._ready = False
        self._lock = threading.Lock()

    def _connect(self):
        if self._ready:
            return self._client
        with self._lock:
            if not self._ready:
                from qdrant_client import QdrantClient
                self._client = QdrantClient(url=self.url, api_key=self.api_key, timeout=self.timeout)
                self._bootstrap(self._client)
                self._ready = True
        return self._client

    def _bootstrap(self, client) -> None:
        from qdrant_client import models as m
        if client.collection_exists(self.collection):
            info = client.get_collection(self.collection)
            dense = (info.config.params.vectors or {}).get(DENSE) if isinstance(info.config.params.vectors, dict) else None
            if dense is None or dense.size != self.dimension:
                raise VectorStoreOperationError(
                    f"Qdrant collection '{self.collection}' does not hold {self.dimension}-dimension "
                    f"'{DENSE}' vectors; set QDRANT_COLLECTION to a new name or re-index after changing "
                    "the embedding model", provider=self.provider_name)
            if not (dense.on_disk and info.config.quantization_config):
                # A collection from before: its originals move to disk, an int8 copy stays in memory.
                client.update_collection(
                    self.collection, vectors_config={DENSE: m.VectorParamsDiff(on_disk=True)},
                    sparse_vectors_config={SPARSE: m.SparseVectorParams(index=m.SparseIndexParams(on_disk=True))},
                    hnsw_config=m.HnswConfigDiff(on_disk=True), quantization_config=_quantization(m))
        else:
            # Originals on disk, a 4x smaller int8 copy of the dense vectors in memory, which a search
            # reads first and then rescores against the originals. All in memory, 545,000 points of a
            # thousand-file workspace (384-dimension vectors, BM25, payload indexes) passed Qdrant's
            # 1 GB and it was killed 18 times in one upload; the vectors alone were ~550 MB.
            client.create_collection(
                self.collection,
                vectors_config={DENSE: m.VectorParams(size=self.dimension, distance=m.Distance.COSINE,
                                                      on_disk=True)},
                sparse_vectors_config={SPARSE: m.SparseVectorParams(
                    modifier=m.Modifier.IDF, index=m.SparseIndexParams(on_disk=True))},
                hnsw_config=m.HnswConfigDiff(payload_m=16, m=0, on_disk=True),
                quantization_config=_quantization(m),
                on_disk_payload=True,
            )
        schema = client.get_collection(self.collection).payload_schema or {}
        if _LEGACY_TENANT in schema:
            self._rename_tenant_field(client)
            schema = client.get_collection(self.collection).payload_schema or {}
        if "workspace_id" not in schema:
            client.create_payload_index(self.collection, "workspace_id",
                                        m.KeywordIndexParams(type=m.KeywordIndexType.KEYWORD, is_tenant=True))
        for name in _KEYWORD_FIELDS:
            if name not in schema:
                client.create_payload_index(self.collection, name,
                                            m.KeywordIndexParams(type=m.KeywordIndexType.KEYWORD, on_disk=True))
        for name in _BOOL_FIELDS:
            if name not in schema:
                client.create_payload_index(self.collection, name, m.PayloadSchemaType.BOOL)
        if "confidence" not in schema:
            client.create_payload_index(self.collection, "confidence", m.PayloadSchemaType.FLOAT)
        for name in _TEXT_FIELDS:
            if name not in schema:
                client.create_payload_index(self.collection, name, m.TextIndexParams(
                    type=m.TextIndexType.TEXT, tokenizer=m.TokenizerType.WORD, lowercase=True,
                    phrase_matching=True, min_token_len=1, on_disk=True))

    def _rename_tenant_field(self, client) -> None:
        """Rename an older collection's `org_id` payload field to `workspace_id`.

        Done per workspace, by filter, so no point is rewritten one by one: copy the value into
        `workspace_id`, then drop `org_id` from those points. The old index goes last, once no point
        carries the field; until then a start-up that was interrupted finds it and resumes."""
        from qdrant_client import models as m
        legacy = _LEGACY_TENANT
        logger.info("Qdrant: renaming payload %s to workspace_id in %s", legacy, self.collection)
        while True:
            points, _ = client.scroll(self.collection, limit=1, with_payload=[legacy], with_vectors=False,
                                      scroll_filter=m.Filter(must_not=[m.IsEmptyCondition(
                                          is_empty=m.PayloadField(key=legacy))]))
            if not points:
                break
            value = (points[0].payload or {}).get(legacy)
            same = m.Filter(must=[m.FieldCondition(key=legacy, match=m.MatchValue(value=value))])
            client.set_payload(self.collection, payload={"workspace_id": value}, points=same, wait=True)
            client.delete_payload(self.collection, keys=[legacy], points=same, wait=True)
        client.delete_payload_index(self.collection, legacy, wait=True)

    async def _call(self, fn, *args, **kwargs):
        def run():
            return fn(self._connect(), *args, **kwargs)
        try:
            return await asyncio.to_thread(run)
        except VectorStoreOperationError:
            raise
        except Exception as e:                          # noqa: BLE001 - client errors vary by transport
            raise VectorStoreOperationError(f"Qdrant request failed: {e}", provider=self.provider_name,
                                            original_error=e) from e

    @staticmethod
    def _filter(flt: VectorFilter):
        from qdrant_client import models as m

        def any_of(key, values):
            return m.FieldCondition(key=key, match=m.MatchAny(any=[str(v) for v in values]))

        def equals(key, value):
            return m.FieldCondition(key=key, match=m.MatchValue(value=value))

        must = [equals("workspace_id", str(flt.workspace_id))]
        must_not = []
        if flt.attrs is not None:
            must.append(any_of("attr", flt.attrs))
        if flt.exclude_attrs:
            must_not.append(any_of("attr", flt.exclude_attrs))
        if flt.node_ids is not None:
            must.append(any_of("node_id", flt.node_ids))
        if flt.types:
            must.append(any_of("types", type_variants(*flt.types)))
        if flt.collection_ids is not None:
            if flt.include_unscoped:
                must.append(m.Filter(should=[any_of("collection_id", flt.collection_ids),
                                             m.IsEmptyCondition(is_empty=m.PayloadField(key="collection_id"))]))
            else:
                must.append(any_of("collection_id", flt.collection_ids))
        if flt.source_ref is not None:
            must.append(equals("source_ref", str(flt.source_ref)))
        if flt.sensitivities is not None:
            must.append(any_of("sensitivity", flt.sensitivities))
        if flt.min_confidence is not None:
            must.append(m.FieldCondition(key="confidence", range=m.Range(gte=flt.min_confidence)))
        if flt.evidence or flt.exclude_superseded:
            must_not.append(equals("superseded", True))
        if flt.evidence:
            must_not += [equals("structural", True), equals("internal", True), equals("hidden", True)]
        if flt.scope == "conversations":
            must.append(equals("memory", True))
        elif flt.scope == "knowledge":
            must_not.append(equals("memory", True))
        elif flt.scope and flt.scope.startswith("source:"):
            must.append(equals("source", flt.scope[len("source:"):]))
        if flt.text_all:
            must.append(m.FieldCondition(key="search", match=m.MatchText(text=flt.text_all)))
        if flt.phrase:
            must.append(m.FieldCondition(key="search", match=m.MatchPhrase(phrase=flt.phrase)))
        if flt.names is not None:
            must.append(any_of("names", [n.lower() for n in flt.names]))
        if flt.names_phrase:
            must.append(m.FieldCondition(key="names_text", match=m.MatchPhrase(phrase=flt.names_phrase)))
        if flt.has_props:
            must.append(m.Filter(should=[m.Filter(must_not=[m.IsEmptyCondition(
                is_empty=m.PayloadField(key=f"props.{k}"))]) for k in flt.has_props]))
        if flt.embedded is not None:
            must.append(equals("embedded", flt.embedded))
        return m.Filter(must=must, must_not=must_not or None)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._connect)

    async def health_check(self) -> bool:
        try:
            await self._call(lambda c: c.get_collection(self.collection))
            return True
        except Exception:                               # noqa: BLE001
            return False

    async def describe(self) -> dict:
        version, detail = "", ""
        try:
            info = await self._call(lambda c: c.info())
            version = getattr(info, "version", "") or ""
        except Exception:                               # noqa: BLE001 — version is nice to have
            pass
        try:
            coll = await self._call(lambda c: c.get_collection(self.collection))
            detail = f"collection '{self.collection}', {coll.points_count or 0} points"
        except Exception as e:                          # noqa: BLE001
            detail = f"collection '{self.collection}' unavailable: {e}"
        return {"provider": self.provider_name, "version": version, "detail": detail}

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client, self._ready = None, False

    async def upsert(self, points: Sequence[VectorPoint]) -> None:
        from qdrant_client import models as m
        structs = []
        for p in points:
            vectors: dict[str, Any] = {}
            if p.dense is not None:
                if len(p.dense) != self.dimension:
                    raise VectorStoreOperationError(
                        f"vector for {p.node_id}:{p.attr} has {len(p.dense)} dimensions, expected {self.dimension}",
                        provider=self.provider_name)
                vectors[DENSE] = list(p.dense)
            if p.sparse is not None and p.sparse.indices:
                vectors[SPARSE] = m.SparseVector(indices=list(p.sparse.indices), values=list(p.sparse.values))
            payload = {**p.fields, "workspace_id": str(p.workspace_id), "node_id": str(p.node_id), "attr": p.attr, "text": p.text}
            structs.append(m.PointStruct(id=p.id, vector=vectors, payload=payload))
        for batch in _chunks(structs, _BATCH):
            await self._call(lambda c, b=batch: c.upsert(self.collection, points=b, wait=True))

    async def _query(self, query, using: str, flt: VectorFilter, k: int) -> list[VectorHit]:
        import uuid
        response = await self._call(lambda c: c.query_points(
            self.collection, query=query, using=using, query_filter=self._filter(flt), limit=k,
            with_payload=["node_id", "attr", "text"]))
        return [VectorHit(node_id=uuid.UUID(p.payload["node_id"]), attr=p.payload.get("attr", ""),
                          text=p.payload.get("text", ""), score=float(p.score)) for p in response.points]

    async def search_dense(self, vector: list[float], flt: VectorFilter, k: int) -> list[VectorHit]:
        return await self._query(list(vector), DENSE, flt, k)

    async def search_sparse(self, vector: SparseVector, flt: VectorFilter, k: int) -> list[VectorHit]:
        from qdrant_client import models as m
        if not vector.indices:
            return []
        return await self._query(m.SparseVector(indices=list(vector.indices), values=list(vector.values)),
                                 SPARSE, flt, k)

    async def update_fields(self, workspace_id, node_id, fields: dict) -> None:
        await self._call(lambda c: c.set_payload(
            self.collection, payload=dict(fields),
            points=self._filter(VectorFilter(workspace_id=workspace_id, node_ids=[node_id])), wait=True))

    async def _scroll(self, flt: VectorFilter, fields: list[str]):
        offset = None
        while True:
            records, offset = await self._call(lambda c, o=offset: c.scroll(
                self.collection, scroll_filter=self._filter(flt), limit=1000, offset=o,
                with_payload=fields, with_vectors=False))
            for r in records:
                yield r.payload or {}
            if offset is None:
                return

    async def scroll(self, flt: VectorFilter, *, limit: int, fields: Sequence[str]) -> list[dict]:
        out = []
        async for payload in self._scroll(flt, list(fields)):
            out.append(payload)
            if len(out) >= limit:
                break
        return out

    async def embedded_nodes(self, workspace_id, node_ids: Sequence) -> set[str]:
        found: set[str] = set()
        for batch in _chunks([str(n) for n in node_ids], 1000):
            flt = VectorFilter(workspace_id=workspace_id, node_ids=batch, attrs=[DOCUMENT_ATTR], embedded=True)
            async for payload in self._scroll(flt, ["node_id"]):
                found.add(payload["node_id"])
        return found

    async def count(self, flt: VectorFilter) -> int:
        result = await self._call(lambda c: c.count(self.collection, count_filter=self._filter(flt), exact=True))
        return int(result.count)

    async def count_nodes(self, workspace_id) -> int:
        return await self.count(VectorFilter(workspace_id=workspace_id, attrs=[DOCUMENT_ATTR], embedded=True))

    async def attribute_values(self, workspace_id) -> dict[str, tuple[int, int]]:
        counts: dict[str, int] = {}
        distinct: dict[str, set] = {}
        flt = VectorFilter(workspace_id=workspace_id, exclude_attrs=[DOCUMENT_ATTR])
        async for payload in self._scroll(flt, ["attr", "text"]):
            attr = payload.get("attr", "")
            counts[attr] = counts.get(attr, 0) + 1
            distinct.setdefault(attr, set()).add(hashlib.blake2b((payload.get("text") or "").encode(),
                                                                 digest_size=8).digest())
        return {a: (n, len(distinct[a])) for a, n in counts.items()}

    async def delete(self, flt: VectorFilter) -> None:
        from qdrant_client import models as m
        ids = [str(n) for n in flt.node_ids] if flt.node_ids is not None else None
        batches = list(_chunks(ids, 1000)) if ids is not None else [None]
        for batch in batches:
            part = VectorFilter(**{**flt.__dict__, "node_ids": batch}) if batch is not None else flt
            await self._call(lambda c, f=self._filter(part): c.delete(
                self.collection, points_selector=m.FilterSelector(filter=f), wait=True))

    async def clear_org(self, workspace_id) -> None:
        from qdrant_client import models as m
        flt = self._filter(VectorFilter(workspace_id=workspace_id))
        await self._call(lambda c: c.delete(self.collection, points_selector=m.FilterSelector(filter=flt), wait=True))
