"""Chunks of documents kept in the data store, shaped like the nodes they used to be.

A chunk row (stores/data `unstructured`) becomes a node-shaped record - never written to the graph -
for the extractors, for the search index (one point per chunk, typed `Passage`), and for retrieval,
which reads chunks it found back from Delta (`hydrate`). Here, in core, because the graph's reader
(NodeRepo.get_many) and citation provenance both need it; ingest/unstructured/dataplane.py writes the
rows.
"""
from __future__ import annotations

import uuid

from opencortyx.stores.graph.models import Node

#: The type every chunk's search point carries, whatever its element type.
PASSAGE = "Passage"
#: Marks a node-shaped chunk record that is not in the graph.
VIRTUAL = "__virtual"


def record(workspace_id, row: dict, *, collection_id=None, version_ref=None) -> Node:
    """A chunk row shaped like the node it used to be."""
    text = row.get("text") or ""
    props = {"text": text, "ordinal": row.get("ordinal"), "page_number": row.get("page"),
             "label": row.get("element_type"), "section_path": row.get("section_path") or None,
             "__source": "document", "__reading_unit": True, VIRTUAL: True, "__base_type": PASSAGE,
             "__asset_id": row["asset_id"], "__unit_key": row["chunk_id"],
             "__version_ref": None if version_ref is None else str(version_ref)}
    if row.get("level") is not None:
        props["level"] = row["level"]
    return Node(id=uuid.UUID(row["chunk_id"]), workspace_id=uuid.UUID(str(workspace_id)),
                type=row.get("element_type") or "Text",
                label=(" ".join(text.split())[:80] or f"{row.get('element_type')} #{row.get('ordinal')}"),
                properties={k: v for k, v in props.items() if v is not None},
                collection_id=uuid.UUID(str(collection_id)) if collection_id else None,
                source_ref=uuid.UUID(row["asset_id"]))


async def hydrate(workspace_id, chunk_ids) -> list[Node]:
    """Chunk records for ids the graph does not have - one batched read of the workspace's chunks
    table, filtered by id. Empty for a workspace whose data is in the graph."""
    from opencortyx.core.dataplane.mode import uses_delta
    ids = [str(i) for i in chunk_ids if i]
    if not ids or not await uses_delta(workspace_id):
        return []
    import anyio

    from opencortyx.stores.data import UNSTRUCTURED, DataStore
    store = DataStore(workspace_id)
    try:
        rows = await anyio.to_thread.run_sync(lambda: store.chunks(ids))
    except Exception:                                         # noqa: BLE001 - no table yet
        return []
    version = store.version(UNSTRUCTURED)
    return [record(workspace_id, r, version_ref=version) for r in rows]
