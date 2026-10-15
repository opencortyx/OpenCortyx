"""Shared upsert logic for the built-in ontology TEMPLATES (Person, Organization).

Both templates register many node/edge types into `type_registry` as PLATFORM rows, shared by every
organization (an organization's edits go to its own copy). The rules:
  • Nodes: insert; on conflict BACKFILL attributes/domain only onto an EMPTY row — and never onto a
    base system type — so we fill stray empty rows but never clobber a customized or structural type.
  • The HUB node (Person / Organization) is the one exception: its base row may be a system structural
    node with an empty schema, so we fill its template attributes even when source == 'system'.
  • Edges: insert-only (do nothing on conflict) so base edge semantics (e.g. OWNS = tenancy) survive.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from opencortyx.core.db import V2Session
from opencortyx.domain import TypeRegistry
from opencortyx.domain.ontology import PLATFORM_ORG, TYPE_KEY


async def apply_template_rows(rows: list[dict], hub_name: str) -> int:
    empty_schema = sa.or_(TypeRegistry.properties_schema.is_(None),
                          sa.cast(TypeRegistry.properties_schema, sa.Text) == "{}")
    _GATE = ("allowed_source_types", "allowed_target_types")
    async with V2Session() as db:
        for r in rows:
            stmt = pg_insert(TypeRegistry).values(workspace_id=PLATFORM_ORG,
                                                  **{k: v for k, v in r.items() if k not in _GATE})
            if r["kind"] == "node":
                is_hub = r["name"] == hub_name
                # hub: fill even a system row; others: fill empty non-system rows only
                where = empty_schema if is_hub else sa.and_(empty_schema, TypeRegistry.source != "system")
                stmt = stmt.on_conflict_do_update(
                    index_elements=TYPE_KEY,
                    set_={"properties_schema": stmt.excluded.properties_schema,
                          "description": stmt.excluded.description,
                          "source": stmt.excluded.source, "status": stmt.excluded.status},
                    where=where)
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=TYPE_KEY)
            await db.execute(stmt)
        await db.commit()
    return len(rows)


def build_rows(hub: tuple, domains_nodes: dict, domains_edges: dict, source: str) -> list[dict]:
    """Flatten a template (hub + per-domain nodes/edges) into type_registry rows, deduping by name and
    unioning edge endpoints across domains that reuse a name."""
    nodes: dict[str, dict] = {}
    hub_name, hub_desc, hub_attrs = hub
    nodes[hub_name] = dict(name=hub_name, kind="node", description=hub_desc, source=source, status="active",
                           properties_schema={"domain": "Core", "hub": True,
                                              "fields": [{"name": n, "type": t} for n, t in hub_attrs]})
    for domain, items in domains_nodes.items():
        for name, desc, attrs in items:
            if name in nodes:
                continue
            nodes[name] = dict(name=name, kind="node", description=desc, source=source, status="active",
                               properties_schema={"domain": domain,
                                                  "fields": [{"name": n, "type": t} for n, t in attrs]})
    edges: dict[str, dict] = {}
    for domain, items in domains_edges.items():
        for name, desc, src, tgt in items:
            if name in edges:
                e = edges[name]
                e["allowed_source_types"] = sorted(set(e["allowed_source_types"]) | set(src))
                e["allowed_target_types"] = sorted(set(e["allowed_target_types"]) | set(tgt))
            else:
                edges[name] = dict(name=name, kind="edge", description=desc, source=source, status="active",
                                   allowed_source_types=list(src), allowed_target_types=list(tgt),
                                   properties_schema={"domain": domain})
    return list(nodes.values()) + list(edges.values())


_EDGES_PER_TYPE_CAP = 8   # keep the materialized template graph readable (cap fan-out per edge type)


async def materialize_template(workspace_id, rows: list[dict], template: str) -> dict:
    """Instantiate a template as graph nodes and edges so its hierarchy renders in the graph views.

    One concept node per type, carrying its description and attribute names, and an edge per
    relationship from source type to target type. Idempotent through deterministic ids. Concept
    nodes are structure, not temporal data, so they are not linked to Date nodes."""
    from opencortyx.core.substrate import NodeRepo
    node_rows = [r for r in rows if r["kind"] == "node"]
    edge_rows = [r for r in rows if r["kind"] == "edge"]
    repo = NodeRepo()
    name_to_id: dict[str, uuid.UUID] = {}
    for r in node_rows:
        attrs = (r.get("properties_schema") or {}).get("fields") or []
        nid = uuid.uuid5(uuid.NAMESPACE_URL, f"{workspace_id}:{template}:{r['name']}")
        await repo.upsert_node(
            node_id=nid, workspace_id=workspace_id, type=r["name"], label=r["name"], date_link=False,
            properties={"description": r["description"], "__template": template, "__concept": True,
                        "domain": (r.get("properties_schema") or {}).get("domain"),
                        "attributes": [a.get("name") for a in attrs]})
        name_to_id[r["name"]] = nid
    edges = []
    for r in edge_rows:
        pairs = [(s, t) for s in (r.get("allowed_source_types") or []) for t in (r.get("allowed_target_types") or [])
                 if name_to_id.get(s) and name_to_id.get(t) and s != t]
        for s, t in pairs[:_EDGES_PER_TYPE_CAP]:
            edges.append({"source_id": name_to_id[s], "target_id": name_to_id[t], "type": r["name"],
                          "properties": {"description": r["description"], "__template": template}})
    written = await repo.add_edges(workspace_id=workspace_id, edges=edges)
    return {"template": template, "nodes": len(node_rows), "edges_made": len(written.created)}
