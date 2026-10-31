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

import json
import uuid

from opencortyx.core.observability import get_logger

log = get_logger(__name__)
#: Values read per field and run; past it the rest are left unread (a field of free text in a very
#: large table is better read by a pipeline built for it).
MAX_PER_FIELD = 20_000


async def _rows(workspace_id, t, fields: list, asset_names: set, extra: list = ()) -> list:
    """The type's records - key and the free-text fields - read from the given assets."""
    import anyio

    from opencortyx.knowledge.semantic.views import q
    from opencortyx.retrieval.l2_data.schema import load
    from opencortyx.stores.data import Views
    schema = await load(workspace_id, fresh=True)
    table = next((x for x in schema.objects() if x.name == t.name), None)
    if table is None:
        return []
    cols = list(dict.fromkeys([*t.keys, *fields, *extra]))
    filled = " OR ".join(f"{q(f)} IS NOT NULL" for f in fields)

    def read() -> list:
        with Views(workspace_id, lock=True, **schema.needs([table.view])) as v:
            # an entity's table names every file it is in (`_assets`); an event's row its one file (`_asset`)
            src = "_assets" if t.keys else "_asset AS _assets"
            rows = v.query(f"SELECT {', '.join(q(c) for c in cols)}, {src} FROM {q(table.view)} "
                           f"WHERE {filled} LIMIT {MAX_PER_FIELD}").to_pylist()
        return [r for r in rows if set(str(r.get("_assets") or "").split(", ")) & asset_names]
    return await anyio.to_thread.run_sync(read)


def _inputs(workspace_id, t, rows: list, fields: list, published: bool, asset_of: dict, unit_kind: str,
            up=None) -> list:
    """One text-reader input per non-empty free-text value. `up`: for a record that is not published
    itself, the link to the published record it belongs to - a turn's mentions are its session's."""
    from opencortyx.ingest.extraction.text_reader import Input
    from opencortyx.knowledge.semantic.publish import entity_id
    out = []
    for r in rows:
        # an event has no identifier: its row is named by its content, so evidence cites that row
        key = "|".join(str(r.get(k)) for k in t.keys) or str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
            {k: v for k, v in r.items() if k != "_assets"}, default=str, sort_keys=True)))
        asset = next((asset_of[n] for n in str(r.get("_assets") or "").split(", ") if n in asset_of), None)
        if asset is None:
            continue
        holder = (uuid.UUID(entity_id(workspace_id, t.name, key)) if published
                  else uuid.UUID(entity_id(workspace_id, up.target, str(r[up.source_prop])))
                  if up is not None and r.get(up.source_prop) not in (None, "")
                  else uuid.UUID(asset))
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
        published = t.kind == "entity" and bool(t.keys) and (t.materialize or everything)
        # not published itself: its mentions belong to the most specific published record it refers to
        ups = [] if published else [lk for lk in layer.links if lk.source == t.name and (tt := layer.type(lk.target))
                                    and tt.keys and (tt.materialize or everything)]
        up = max(ups, key=lambda lk: layer.type(lk.target).rows, default=None)
        rows = await _rows(workspace_id, t, prose, {b.asset_name for b in bound},
                           extra=[up.source_prop] if up else [])
        inputs = _inputs(workspace_id, t, rows, prose, published, {b.asset_name: b.asset_id for b in t.bindings},
                         "row" if bound[0].kind == "structured" else "record", up)
        out[t.name] = {"records": len(rows), **await read_inputs(workspace_id, inputs)}
    log.info("semantic_layer.text_fields", workspace_id=str(workspace_id), types=out)
    return out
