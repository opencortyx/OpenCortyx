"""The free text inside records, read: when an upload asks for knowledge (Add data's "extract entities
and relationships"), each free-text field the Detector found in its tables or JSON (`role == "prose"`)
is read by the text reader (`ingest/extraction/text_reader.py`, no LLM) once detection has bound the
asset to its types.

Each non-empty value is one input, citing its row or record:

    a published record   the record's own entity node MENTIONS what its text names
                         (Customer "Alice Smith" -MENTIONS-> Product "X200")
    otherwise            the asset MENTIONS it

What is read is then resolved by name onto the published records it names (`resolution/mentions.py`),
so a person named in a ticket meets the customer row of the same name.
"""
from __future__ import annotations

import uuid

from opencortyx.core.observability import get_logger

log = get_logger(__name__)
#: Values read per field and run; past it the rest are left unread (a field of free text in a very
#: large table is better read by a pipeline built for it).
MAX_PER_FIELD = 20_000


async def _rows(workspace_id, t, fields: list, asset_names: set) -> list:
    """The type's records - key and the free-text fields - read from the given assets."""
    import anyio

    from opencortyx.knowledge.semantic.views import q
    from opencortyx.retrieval.l2_data.schema import load
    from opencortyx.stores.data import Views
    schema = await load(workspace_id, fresh=True)
    table = next((x for x in schema.objects() if x.name == t.name), None)
    if table is None:
        return []
    cols = list(dict.fromkeys([*t.keys, *fields]))
    filled = " OR ".join(f"{q(f)} IS NOT NULL" for f in fields)

    def read() -> list:
        with Views(workspace_id, lock=True, **schema.needs([table.view])) as v:
            rows = v.query(f"SELECT {', '.join(q(c) for c in cols)}, _assets FROM {q(table.view)} "
                           f"WHERE {filled} LIMIT {MAX_PER_FIELD}").to_pylist()
        return [r for r in rows if set(str(r.get("_assets") or "").split(", ")) & asset_names]
    return await anyio.to_thread.run_sync(read)


def _inputs(workspace_id, t, rows: list, fields: list, published: bool, asset_of: dict, unit_kind: str) -> list:
    """One text-reader input per non-empty free-text value."""
    from opencortyx.ingest.extraction.text_reader import Input
    from opencortyx.knowledge.semantic.publish import entity_id
    out = []
    for r in rows:
        key = "|".join(str(r.get(k)) for k in t.keys)
        asset = next((asset_of[n] for n in str(r.get("_assets") or "").split(", ") if n in asset_of), None)
        if asset is None:
            continue
        holder = uuid.UUID(entity_id(workspace_id, t.name, key)) if published else uuid.UUID(asset)
        for f in fields:
            if r.get(f) not in (None, ""):
                out.append(Input(text=str(r[f]), asset_id=asset, unit_kind=unit_kind, unit_key=key, holder=holder))
    return out


async def read_assets(workspace_id, asset_ids) -> dict:
    """Read the free-text fields of the records `asset_ids` hold. Returns per-type counts."""
    from opencortyx.core.config import runtime as rc
    from opencortyx.ingest.extraction.text_reader import read_inputs
    from opencortyx.knowledge.semantic import store

    wanted = {str(a) for a in asset_ids or ()}
    if not wanted:
        return {}
    layer = (await store.load(workspace_id)).accepted()
    everything = rc.knowledge_publish()
    out: dict = {}
    for t in layer.types:
        bound = [b for b in t.bindings if b.asset_id in wanted]
        prose = [p.name for p in t.properties if p.role == "prose"]
        if not bound or not prose:
            continue
        rows = await _rows(workspace_id, t, prose, {b.asset_name for b in bound})
        published = t.kind == "entity" and bool(t.keys) and (t.materialize or everything)
        inputs = _inputs(workspace_id, t, rows, prose, published, {b.asset_name: b.asset_id for b in t.bindings},
                         "row" if bound[0].kind == "structured" else "record")
        out[t.name] = {"records": len(rows), **await read_inputs(workspace_id, inputs)}
    log.info("semantic_layer.text_fields", workspace_id=str(workspace_id), types=out)
    return out
