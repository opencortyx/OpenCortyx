"""Semi-structured assets on the data plane: every record in Delta, its fields as Attribute nodes.

With the workspace's data in Delta (`data_store = delta`), a JSON / XML / YAML payload or an API
sync keeps its walker graph exactly as before (KeyNodes, HAS_<KEY> edges, references, categorical
values) and gains:

  - its records in DATA_ROOT/<ws>/semistructured/<asset_id>: one row per top-level record, the
    whole record as JSON (`raw`, nested arrays and long text included - what the walker skipped is
    still queryable) and its top-level scalar fields flattened into typed columns;
  - its fields, profiled, as data-layer `Attribute` nodes (path, type, null share, distinct count,
    value_set for a category) under the asset;
  - a declared join rule ("order.customer_id joins Customer.id") as a data-layer `JOINS_TO` between
    the two fields' Attribute nodes, so the SQL planner joins the way the mapping does.

A record's id is the walk's own record prefix (`<file>:<n>`), which is also its evidence unit: a
node from it opens the record.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

from opencortyx.core.observability import get_logger
from opencortyx.stores.data.layout import SEMISTRUCTURED

log = get_logger(__name__)
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/semistructured-attribute")
_SYSTEM = ("record_id", "parent_record_id", "json_path", "sync_run", "fetched_at", "raw")


def record_row(record_id: str, index: int, value, *, sync_run: Optional[str] = None,
               parent_record_id: Optional[str] = None) -> dict:
    row = {"record_id": str(record_id), "parent_record_id": parent_record_id, "json_path": f"$[{index}]",
           "sync_run": sync_run, "fetched_at": datetime.now(timezone.utc),
           "raw": json.dumps(value, ensure_ascii=False, default=str)}
    if isinstance(value, dict):
        for k, v in value.items():
            name = str(k)
            if name in _SYSTEM or name.startswith("_"):
                name = f"f_{name.lstrip('_')}"
            if isinstance(v, (str, int, float, bool)) or v is None:
                row.setdefault(name, v)
    return row


def _column_type(values: list):
    import pyarrow as pa
    seen = [v for v in values if v is not None]
    if seen and all(isinstance(v, bool) for v in seen):
        return pa.bool_()
    if seen and all(isinstance(v, int) and not isinstance(v, bool) for v in seen):
        return pa.int64()
    if seen and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seen):
        return pa.float64()
    return pa.string()


def to_table(rows: list[dict]):
    """Rows with differing fields and mixed types as one typed Arrow table: a field whose values are
    all numbers (or all booleans) keeps that type, anything mixed is text."""
    import pyarrow as pa
    names: list = []
    for r in rows:
        for k in r:
            if k not in names:
                names.append(k)
    cols, fields = [], []
    for n in names:
        vals = [r.get(n) for r in rows]
        if n == "fetched_at":
            t = pa.timestamp("us", tz="UTC")
        elif n in _SYSTEM:
            t = pa.string()
        else:
            t = _column_type(vals)
        if t == pa.string():
            vals = [None if v is None else (v if isinstance(v, str) else json.dumps(v) if isinstance(v, bool) else str(v))
                    for v in vals]
        elif t == pa.float64():
            vals = [None if v is None else float(v) for v in vals]
        cols.append(pa.array(vals, type=t))
        fields.append(pa.field(n, t))
    return pa.Table.from_arrays(cols, schema=pa.schema(fields))


def attribute_id(workspace_id, asset_id, path: str) -> uuid.UUID:
    return uuid.uuid5(_NS, f"{workspace_id}:{asset_id}:{path}")


async def store_records(workspace_id, asset_id, rows: list[dict], *, name: str, fmt: str = "json",
                        source_key: str = "", record_types: Iterable[str] = (),
                        collection_id=None, sync_run: Optional[str] = None,
                        external: Optional[dict] = None) -> dict:
    """Replace the asset's records in Delta, register the asset, and write its fields as profiled
    Attribute nodes under the asset node. `external` ({format, location, connector_id}) is a source
    read in place - a MongoDB collection: registered and profiled from what was read, not copied."""
    import anyio

    from opencortyx.core.dataplane import assets
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.ingest.structured.dataplane import attribute_profile_props, profile
    from opencortyx.stores.data import AssetRef, DataStore, delta
    if not rows:
        return {"rows": 0}
    store = DataStore(workspace_id)
    table = to_table(rows)

    def go():
        fields = [{"name": f.name, "type": str(f.type)} for f in table.schema if f.name not in _SYSTEM]
        if external:
            from opencortyx.ingest.structured.dataplane import profile_arrow
            return datetime.now(timezone.utc).isoformat(), fields, profile_arrow(table, fields)
        version = delta.overwrite(store.path(SEMISTRUCTURED, asset_id), table)
        ref = AssetRef(asset_id=str(asset_id), name=name, kind=SEMISTRUCTURED)
        return version, fields, profile(workspace_id, ref, fields)
    version, fields, prof = await anyio.to_thread.run_sync(go)
    types = sorted({t for t in record_types if t})
    meta = {"source_key": source_key, "records": len(rows), "record_types": types, "sync_run": sync_run,
            "collection_id": str(collection_id) if collection_id else None}
    if external:
        await assets.register(workspace_id, asset_id, kind=SEMISTRUCTURED, name=name, storage="external",
                              format=external["format"], location=external["location"],
                              connector_id=external.get("connector_id"), version_ref=version,
                              metadata={**meta, "table": external["location"], "live": True})
    else:
        await assets.register(workspace_id, asset_id, kind=SEMISTRUCTURED, name=name, storage="managed",
                              format=fmt, location=f"{SEMISTRUCTURED}/{asset_id}", version_ref=version,
                              metadata=meta)
    repo = NodeRepo()
    specs, edges = [], []
    for f in fields:
        path = f"$.{f['name']}"
        aid = attribute_id(workspace_id, asset_id, path)
        specs.append({"node_id": aid, "type": "Attribute", "label": f["name"],
                      "properties": {"path": path, "physical_column": f["name"], "data_type": f["type"],
                                     "parent_table_id": str(asset_id), "record_types": types,
                                     "source_key": source_key, "native": True,
                                     **attribute_profile_props(prof.get(f["name"]))},
                      "source_ref": asset_id, "collection_id": collection_id})
        edges.append({"source_id": asset_id, "target_id": aid, "type": "HAS_ATTRIBUTE"})
    if specs:
        await repo.upsert_many(workspace_id, specs, date_link=False)
        await repo.add_edges(workspace_id=workspace_id, edges=edges)
    await repo.patch_properties(workspace_id, [asset_id], {
        "asset_kind": SEMISTRUCTURED, "storage": "external" if external else "managed",
        "records": len(rows), "data_version": version})
    return {"rows": len(rows), "version": version, "attributes": len(specs)}


async def link_join_rules(workspace_id, asset_id, edge_rules: list, record_types: Iterable[str]) -> int:
    """A declared join rule as a data-layer JOINS_TO between the two fields' Attribute nodes - the
    schema view of the reference edges the walker draws between records. The target field is looked
    up among the workspace's Attribute nodes whose asset holds records of the target type."""
    from opencortyx.core.substrate import NodeRepo
    repo = NodeRepo()
    made = 0
    mine = set(record_types or ())
    for r in edge_rules or []:
        for local_side, other in (("from", "to"), ("to", "from")):
            j = r.get(other) or {}
            if (j.get("kind") or "self") != "join" or not j.get("type"):
                continue
            local_field = j.get("field") or "id"
            target_field = j.get("on") or local_field
            anchor_type = (r.get(local_side) or {}).get("type")
            if anchor_type and mine and anchor_type not in mine:
                continue
            src = attribute_id(workspace_id, asset_id, f"$.{local_field}")
            if await repo.get(workspace_id, src) is None:
                continue
            for t in await repo.find(workspace_id, types=["Attribute"], label=target_field):
                p = t.properties or {}
                if j["type"] in (p.get("record_types") or []) or j["type"].lower() == str(p.get("physical_table") or "").lower():
                    ok = await repo.add_edge(workspace_id=workspace_id, source_id=src, target_id=t.id,
                                             type="JOINS_TO", properties={
                                                 "rule": r.get("type"), "from_key": local_field,
                                                 "to_key": target_field, "cardinality": "MANY_TO_ONE",
                                                 "relationship_type": "DECLARED_REFERENCE"})
                    made += int(bool(ok))
    return made
