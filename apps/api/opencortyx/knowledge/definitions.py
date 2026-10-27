"""Data definitions: Metrics, Segments and verified queries - agreed meanings the SQL planner uses.

    Metric         "revenue", "active customers": ONE agreed expression over a table, instead of a
                   formula the model guesses each time. DEFINED_ON the Attributes it reads.
    Segment        a reusable named filter ("active customer" = status = 'ACTIVE' AND expiry >= today).
                   A Metric can be scoped to one (USES_SEGMENT).
    VerifiedQuery  a question and the SQL someone checked. The planner is shown the closest ones as
                   worked examples; an accepted answer can be saved as one with a click.

Business terms are the glossary's Terms and Synonyms (knowledge/authoring.py): a synonym linked to an
Attribute or a Metric is how "permit" or "licence no." reaches the right column.

Every definition is checked against the data BEFORE it is saved: its SQL goes through the guard
and runs once, so a typo is an error on the form, not a wrong number in an answer weeks later.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Optional

from opencortyx.core.substrate import NodeRepo
from opencortyx.knowledge.authoring import create_node

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/data-definition")
KINDS = ("Metric", "Segment", "VerifiedQuery")


class InvalidDefinition(ValueError):
    """The definition does not run against the data; the message says why."""


def _id(workspace_id, kind: str, name: str) -> uuid.UUID:
    return uuid.uuid5(_NS, f"{workspace_id}:{kind}:{name.strip().lower()}")


async def _run(workspace_id, sql: str) -> tuple[list, list]:
    """Guard and run `sql` over the workspace's views; raise InvalidDefinition on any failure."""
    import anyio

    from opencortyx.retrieval.l2_data import _execute, guard
    from opencortyx.retrieval.l2_data.schema import load
    schema = await load(workspace_id, fresh=True)
    if schema.empty():
        raise InvalidDefinition("this workspace has no tables in the data store yet")
    try:
        checked = guard.check(sql, list(schema.tables))
    except guard.Rejected as e:
        raise InvalidDefinition(str(e)) from None
    try:
        return await anyio.to_thread.run_sync(lambda: _execute(workspace_id, schema, checked.sql, 30.0))
    except Exception as e:                                    # noqa: BLE001 - the database's reason
        raise InvalidDefinition(f"the query failed: {str(e)[:300]}") from None


async def _view_of(workspace_id, table: str) -> str:
    from opencortyx.retrieval.l2_data.schema import load
    schema = await load(workspace_id, fresh=True)
    t = schema.tables.get(table) or schema.table_of(table) or next(
        (x for x in schema.tables.values() if x.name.lower() == str(table).lower()), None)
    if t is None:
        raise InvalidDefinition(f"no table named {table!r}; one of: {', '.join(sorted(schema.tables))}")
    return t.view


async def _attributes(workspace_id, view: str, sql: str) -> list:
    """The Attribute nodes of `view` whose column a definition's SQL names."""
    from opencortyx.retrieval.l2_data.schema import load
    schema = await load(workspace_id)
    t = schema.tables.get(view)
    if t is None:
        return []
    text = sql.lower()
    names = [c.name for c in t.columns if c.name.lower() in text]
    if not names:
        return []
    repo = NodeRepo()
    out = []
    for a in await repo.find(workspace_id, types=["Attribute"], limit=5000):
        p = a.properties or {}
        if str(p.get("parent_table_id")) == t.asset_id and (p.get("physical_column") or a.label) in names:
            out.append(a.id)
    return out


async def _link(workspace_id, src, targets, edge: str) -> None:
    if targets:
        await NodeRepo().add_edges(workspace_id=workspace_id, edges=[
            {"source_id": src, "target_id": t, "type": edge} for t in targets])


async def add_segment(workspace_id, *, name: str, table: str, condition: str, description: str = "",
                      owner: Optional[str] = None, author=None) -> dict:
    view = await _view_of(workspace_id, table)
    _, rows = await _run(workspace_id, f'SELECT count(*) AS n FROM "{view}" WHERE {condition}')
    nid = await create_node(workspace_id, type="Segment", label=name, node_id=_id(workspace_id, "Segment", name),
                            properties={"name": name, "table": view, "condition": condition,
                                        "description": description, "owner": owner,
                                        "rows_matched": rows[0][0] if rows else 0}, author=author)
    await _link(workspace_id, nid, await _attributes(workspace_id, view, condition), "DEFINED_ON")
    _invalidate(workspace_id)
    return {"id": str(nid), "rows_matched": rows[0][0] if rows else 0}


async def add_metric(workspace_id, *, name: str, table: str, expression: str, description: str = "",
                     filters: str = "", segment: Optional[str] = None, grain: str = "", unit: str = "",
                     synonyms: Optional[list] = None, owner: Optional[str] = None, author=None) -> dict:
    view = await _view_of(workspace_id, table)
    seg_cond, seg_id = "", None
    if segment:
        seg = next(iter(await NodeRepo().find(workspace_id, types=["Segment"], label=segment)), None)
        if seg is None:
            raise InvalidDefinition(f"no segment named {segment!r}")
        seg_cond, seg_id = str((seg.properties or {}).get("condition") or ""), seg.id
    where = " AND ".join(f"({c})" for c in (seg_cond, filters) if c)
    _, rows = await _run(workspace_id, f'SELECT {expression} AS value FROM "{view}"'
                                       + (f" WHERE {where}" if where else ""))
    nid = await create_node(workspace_id, type="Metric", label=name, node_id=_id(workspace_id, "Metric", name),
                            properties={"name": name, "table": view, "expression": expression,
                                        "filters": where, "segment": segment, "description": description,
                                        "grain": grain, "unit": unit, "owner": owner,
                                        "synonyms": list(synonyms or []),
                                        "last_value": rows[0][0] if rows else None}, author=author)
    await _link(workspace_id, nid, await _attributes(workspace_id, view, f"{expression} {where}"), "DEFINED_ON")
    if seg_id:
        await _link(workspace_id, nid, [seg_id], "USES_SEGMENT")
    _invalidate(workspace_id)
    return {"id": str(nid), "value": rows[0][0] if rows else None}


async def add_verified_query(workspace_id, *, question: str, sql: str, verified_by: Optional[str] = None,
                             author=None) -> dict:
    cols, rows = await _run(workspace_id, sql)
    checksum = hashlib.sha256(json.dumps([cols, rows], default=str).encode()).hexdigest()[:16]
    from datetime import datetime, timezone
    from opencortyx.retrieval.l2_data.schema import load
    schema = await load(workspace_id)
    used = [v for v in schema.tables if f'"{v}"' in sql or f" {v} " in f" {sql} "]
    nid = await create_node(workspace_id, type="VerifiedQuery", label=question[:200],
                            node_id=_id(workspace_id, "VerifiedQuery", question),
                            properties={"question": question, "sql": sql, "verified_by": verified_by,
                                        "verified_at": datetime.now(timezone.utc).isoformat(),
                                        "result_checksum": checksum, "assets": used}, author=author)
    attrs = []
    for v in used:
        attrs += await _attributes(workspace_id, v, sql)
    await _link(workspace_id, nid, attrs, "USES")
    return {"id": str(nid), "rows": len(rows), "result_checksum": checksum}


async def list_definitions(workspace_id) -> dict:
    repo = NodeRepo()
    out = {}
    for kind in KINDS:
        out[kind] = [{"id": str(n.id), "name": n.label, **{k: v for k, v in (n.properties or {}).items()
                                                           if not str(k).startswith("_") and k != "text"}}
                     for n in await repo.find(workspace_id, types=[kind], limit=1000)]
    return out


async def delete_definition(workspace_id, node_id) -> bool:
    repo = NodeRepo()
    node = await repo.get(workspace_id, node_id)
    if node is None or node.type not in KINDS:
        return False
    await repo.delete_nodes(workspace_id, [node.id])
    _invalidate(workspace_id)
    return True


def _invalidate(workspace_id) -> None:
    from opencortyx.retrieval.l2_data.schema import invalidate
    invalidate(workspace_id)
