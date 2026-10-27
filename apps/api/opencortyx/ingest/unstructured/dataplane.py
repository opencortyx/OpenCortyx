"""Documents on the data plane: chunks in Delta, passages in the search index, nothing in the graph.

With the workspace's data in Delta (`data_store = delta`), a document's layout elements become rows
of the workspace's ONE `unstructured` table (stores/data) instead of graph nodes:

    asset_id, version, chunk_id, ordinal, element_type, level, parent_chunk_id, section_path,
    page, bbox, text, table_grid, ingested_at

`chunk_id` is the id the element's node had, so everything keyed by it - extractor ids, evidence,
search points - keeps the same value whichever store a workspace uses.

The same rows, shaped like nodes but never written to the graph (`record`), are what extractors
read and what the search index holds: one point per chunk, exactly as a chunk node had, typed
`Passage` besides its element type. Retrieval fills in their text from Delta (`load_records`).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

from opencortyx.core.dataplane.passages import PASSAGE, VIRTUAL, record  # noqa: F401 - re-exported
from opencortyx.stores.graph.models import Node


def _schema():
    import pyarrow as pa
    return pa.schema([
        ("asset_id", pa.string()), ("version", pa.int64()), ("chunk_id", pa.string()),
        ("ordinal", pa.int64()), ("element_type", pa.string()), ("level", pa.int64()),
        ("parent_chunk_id", pa.string()), ("section_path", pa.string()), ("page", pa.int64()),
        ("bbox", pa.string()), ("text", pa.string()), ("table_grid", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
    ])


def chunk_row(asset_id, version: int, chunk_id, ordinal: int, element_type: str, text: str, *,
              level=None, parent_chunk_id=None, section_path: str = "", page=None, bbox=None,
              grid=None) -> dict:
    return {"asset_id": str(asset_id), "version": int(version), "chunk_id": str(chunk_id),
            "ordinal": int(ordinal), "element_type": element_type or "Text",
            "level": None if level is None else int(level),
            "parent_chunk_id": str(parent_chunk_id) if parent_chunk_id else None,
            "section_path": section_path or "", "page": None if page in (None, "") else int(page),
            "bbox": json.dumps(bbox) if bbox else None, "text": text or "",
            "table_grid": json.dumps(grid) if grid else None,
            "ingested_at": datetime.now(timezone.utc)}


async def store_chunks(workspace_id, asset_id, rows: list[dict]) -> Optional[int]:
    """Replace a document's chunks in the shared table; returns the table version written. Its
    earlier version stays readable (time travel) for evidence pinned to it, until vacuumed."""
    import anyio
    import pyarrow as pa

    from opencortyx.stores.data import UNSTRUCTURED, DataStore

    def go():
        store = DataStore(workspace_id)
        store.delete_asset(UNSTRUCTURED, asset_id)
        if not rows:
            return store.version(UNSTRUCTURED)
        return store.append(UNSTRUCTURED, asset_id, pa.Table.from_pylist(rows, schema=_schema()))
    return await anyio.to_thread.run_sync(go)


async def load_records(workspace_id, asset_id, *, collection_id=None, chunk_ids: Optional[Iterable] = None) -> dict:
    """{chunk id: record} for a document's chunks (or the named ones), read from Delta."""
    import anyio

    from opencortyx.stores.data import UNSTRUCTURED, DataStore
    store = DataStore(workspace_id)

    def go():
        if chunk_ids is not None:
            return list(store.lookup(UNSTRUCTURED, asset_id, chunk_ids).values())
        t = store.read(UNSTRUCTURED, asset_id)
        return t.to_pylist() if t is not None else []
    rows = await anyio.to_thread.run_sync(go)
    version = store.version(UNSTRUCTURED)
    out = {}
    for r in sorted(rows, key=lambda r: r.get("ordinal") or 0):
        n = record(workspace_id, r, collection_id=collection_id, version_ref=version)
        out[n.id] = n
    return out


async def has_chunks(workspace_id, asset_id) -> bool:
    import anyio

    from opencortyx.stores.data import UNSTRUCTURED, DataStore

    def go():
        t = DataStore(workspace_id).read(UNSTRUCTURED, asset_id, columns=["chunk_id"], limit=1)
        return bool(t is not None and t.num_rows)
    try:
        return await anyio.to_thread.run_sync(go)
    except Exception:                                         # noqa: BLE001 - no table yet
        return False


async def index_passages(workspace_id, records: Iterable[Node]) -> int:
    """Keyword points for the chunks now (their vectors come with the asset's embedding pass), the
    same points chunk nodes had."""
    from opencortyx.core.inference.embedding import sparse_embed
    from opencortyx.stores.vector import get_vector_store
    from opencortyx.stores.vector.models import document_point, document_text
    nodes = list(records)
    if not nodes:
        return 0
    sparse = await sparse_embed([document_text(n) or n.type for n in nodes])
    await get_vector_store().upsert([document_point(n, vec) for n, vec in zip(nodes, sparse)])
    return len(nodes)


async def drop_passages(workspace_id, asset_id) -> None:
    """A document's search points (its chunks'), when it is replaced or removed."""
    from opencortyx.stores.vector import get_vector_store
    from opencortyx.stores.vector.models import VectorFilter
    await get_vector_store().delete(VectorFilter(workspace_id=uuid.UUID(str(workspace_id)),
                                                 source_ref=uuid.UUID(str(asset_id)), types=[PASSAGE]))


async def mark_stale(workspace_id, asset_id, version_ref) -> int:
    """After a document changed: drop its evidence from older versions; a node left with none is
    `stale` - kept, flagged, and shown as such, never silently deleted."""
    from opencortyx.core.dataplane import evidence
    from opencortyx.core.substrate import NodeRepo
    lost = await evidence.drop_for_asset(workspace_id, asset_id, keep_version=version_ref)
    orphans = await evidence.without_evidence(workspace_id, lost)
    if orphans:
        await NodeRepo().patch_properties(workspace_id, orphans, {"__stale": True})
    return len(orphans)

