"""An extractor's input read from the data store, not from the graph.

A template selects graph nodes by type (`source.select`), as always. With `source.data` it reads its
input where it lives instead - nothing is copied into the graph first:

    {"asset": <id or name>}                          every chunk of a document, every row of a
                                                     table, every record of a JSON / XML file
    {"asset": ..., "column": "notes"}                one column of a table (or a top-level field of a
                                                     record): each non-empty value
    {"asset": ..., "path": "$.contact.email"}        one JSON path inside each record
    {"type": "Customer"}                             each record of a semantic-layer entity type,
                                                     across every asset it is bound to
    {"type": "Turn", "column": "content"}            one field of each record of a type
    {"asset": ..., "columns": ["notes", "summary"]}  several columns (a table) or top-level keys (JSON)
    {"asset": ..., "sections": ["Pricing"]}          a document's passages under these headings only

Each input is a node-shaped record that is never written (core/dataplane/passages.VIRTUAL): its
text is the value, its parent the asset, its evidence unit the row it came from - so what an
extractor emits is attached to the asset and cites the exact row, the way chunk inputs already are.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

from opencortyx.core.dataplane.passages import VIRTUAL, record as passage
from opencortyx.stores.graph.models import Node

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/extractor-input")
#: The base type data inputs carry (`source.select` cannot name them; `source.data` does).
DATA_INPUT = "DataInput"


def _text(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return "" if v is None else str(v)


def _node(workspace_id, asset_id: str, unit: str, text: str, field: str, collection_id=None) -> Node:
    return Node(id=uuid.uuid5(_NS, f"{asset_id}:{unit}:{field}"), workspace_id=uuid.UUID(str(workspace_id)),
                type=DATA_INPUT, label=" ".join(text.split())[:80] or unit,
                properties={"text": text, "field": field, VIRTUAL: True, "__base_type": DATA_INPUT,
                            "__asset_id": asset_id, "__unit_key": unit, "__reading_unit": True},
                collection_id=collection_id, source_ref=uuid.UUID(asset_id))


async def _asset(workspace_id, ref: str):
    from opencortyx.core.dataplane import assets
    for a in await assets.refs(workspace_id):
        if a.asset_id == str(ref) or a.name == str(ref):
            return a
    return None


async def select(workspace_id, spec: dict, *, limit: int = 1000) -> list[Node]:
    """The inputs `spec` (a template's `source.data`) names, at most `limit`."""
    import anyio

    from opencortyx.stores.data import Views, layout
    spec = spec or {}
    if spec.get("type"):
        return await _of_type(workspace_id, str(spec["type"]), limit, spec.get("column"))
    a = await _asset(workspace_id, spec.get("asset") or "")
    if a is None:
        return []
    path = spec.get("path")
    columns = [str(c) for c in (spec.get("columns") or ([spec["column"]] if spec.get("column") else [])) if str(c).strip()]
    sections = [str(s).strip().lower() for s in (spec.get("sections") or []) if str(s).strip()]

    def _lit(text: str) -> str:
        return "'" + text.replace("'", "''") + "'"

    def read() -> list[Node]:
        with Views(workspace_id, [a], lock=True) as v:
            view = '"' + next(iter(v.names)).replace('"', '""') + '"'
            if a.kind == layout.UNSTRUCTURED:
                rows = v.query(f"SELECT * FROM {view} ORDER BY ordinal").to_pylist()
                if sections:                    # the passages under the chosen headings only
                    rows = [r for r in rows if any(s in str(r.get("section_path") or "").lower() for s in sections)]
                return [passage(workspace_id, r) for r in rows[:int(limit)]]
            unit = "_row_key" if a.kind == layout.STRUCTURED else "record_id"
            if path:
                reads = [(f"json_extract_string(raw, {_lit(str(path))})", str(path))]
            elif columns:
                # a JSON record's top-level key is read from the record; a table's column is a column
                reads = [((f"json_extract_string(raw, {_lit('$.' + chr(34) + c + chr(34))})"
                           if a.kind == layout.SEMISTRUCTURED else '"' + c.replace('"', '""') + '"'), c)
                         for c in columns]
            else:
                reads = [("raw" if a.kind == layout.SEMISTRUCTURED else "to_json(t)", "record")]
            out = []
            for expr, field in reads:
                rows = v.query(f"SELECT CAST({unit} AS VARCHAR) AS u, CAST({expr} AS VARCHAR) AS x FROM {view} AS t "
                               f"WHERE {expr} IS NOT NULL LIMIT {int(limit)}").to_pylist()
                out += [_node(workspace_id, a.asset_id, r["u"], r["x"], field) for r in rows if r["x"]]
            return out[:int(limit)]
    return await anyio.to_thread.run_sync(read)


async def _of_type(workspace_id, type_name: str, limit: int, column: Optional[str] = None) -> list[Node]:
    """Each record of a semantic-layer entity type, as one input: its properties as text - or, with
    `column`, that one field. A record of a type with no identifier (an event) is named by its
    content."""
    import anyio

    from opencortyx.knowledge.semantic import store
    from opencortyx.retrieval.l2_data.schema import load
    from opencortyx.stores.data import Views
    layer = await store.load(workspace_id)
    t = layer.type(type_name)
    schema = await load(workspace_id, fresh=True)
    table = next((x for x in schema.objects() if x.name == type_name), None)
    if t is None or table is None:
        return []
    first: Optional[str] = t.bindings[0].asset_id if t.bindings else None

    def read() -> list[Node]:
        with Views(workspace_id, lock=True, **schema.needs([table.view])) as v:
            rows = v.query(f'SELECT * FROM "{table.view}" LIMIT {int(limit)}').to_pylist()
        out = []
        for r in rows:
            key = "|".join(str(r.get(k)) for k in t.keys) or str(uuid.uuid5(_NS, json.dumps(r, default=str, sort_keys=True)))
            text = (_text(r.get(column)) if column else
                    "; ".join(f"{k}: {_text(v)}" for k, v in r.items() if v is not None and not k.startswith("_")))
            if text.strip():
                out.append(_node(workspace_id, first or str(uuid.uuid5(_NS, type_name)), key, text,
                                 f"{type_name}.{column}" if column else type_name))
        return out
    return await anyio.to_thread.run_sync(read) if first else []
