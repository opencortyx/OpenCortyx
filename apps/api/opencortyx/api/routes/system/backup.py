"""Routes: workspace export and import."""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from opencortyx.api.schemas.system.backup import ImportSpec
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.stores.graph.models import Node
from opencortyx.domain.ontology import TYPE_KEY, TypeRegistry
from opencortyx.core.substrate import NodeRepo
from opencortyx.domain.control import ExtractionTemplate, Pipeline

router = APIRouter()
_IMPORT_BATCH = 500


@router.get("/export")
async def export_graph(user: CurrentUser = Depends(require_auth)):
    """Portable backup of this organization: nodes, edges, ontology, templates and pipelines as one
    JSON document that `/import` restores into any OpenCortyx instance. Embeddings are rebuilt on
    import, and secrets (connector DSNs, model API keys) are never exported.

    Streamed one node type and one edge type at a time, so the document never exists in memory as a
    whole."""
    import datetime as _dt

    from fastapi.responses import StreamingResponse


    repo, org = NodeRepo(), user.workspace_id

    async def _gen():
        node_types = await repo.count_by_type(org)
        edge_types = await repo.count_edges_by_type(org)
        async with V2Session() as db:
            # this organization's own types only: built-ins are seeded on every install, and another
            # organization's types are never part of this one's backup
            types = (await db.execute(select(TypeRegistry).where(TypeRegistry.workspace_id == org))).scalars().all()
            templates = (await db.execute(select(ExtractionTemplate).where(
                ExtractionTemplate.workspace_id == org))).scalars().all()
            pipelines = (await db.execute(select(Pipeline).where(Pipeline.workspace_id == org))).scalars().all()

        head = {
            "opencortyx_export": "2",
            "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "workspace_id": str(org),
            "counts": {"nodes": sum(node_types.values()), "edges": sum(edge_types.values()),
                       "types": len(types), "templates": len(templates), "pipelines": len(pipelines)},
        }
        yield json.dumps(head)[:-1] + ', "nodes": ['

        first = True
        for node_type in node_types:
            for n in await repo.find(org, exact_types=[node_type]):
                row = {
                    "id": str(n.id), "collection_id": str(n.collection_id) if n.collection_id else None,
                    "type": n.type, "entity_type": n.entity_type, "label": n.label,
                    "properties": n.properties, "confidence": n.confidence, "level": n.level,
                    "source_ref": str(n.source_ref) if n.source_ref else None, "sensitivity": n.sensitivity,
                    "created_at": n.created_at.isoformat() if n.created_at else None,
                }
                yield ("" if first else ",") + json.dumps(row, default=str)
                first = False

        yield '], "edges": ['
        first = True
        for edge_type in edge_types:
            for e in await repo.edges(org, types=[edge_type]):
                row = {"id": str(e.id), "source_id": str(e.source_id), "target_id": str(e.target_id),
                       "type": e.type, "weight": e.weight, "properties": e.properties, "confidence": e.confidence}
                yield ("" if first else ",") + json.dumps(row, default=str)
                first = False

        tail = {
            "type_registry": [{
                "name": t.name, "kind": t.kind, "description": t.description,
                "properties_schema": t.properties_schema,
                "allowed_source_types": t.allowed_source_types,
                "allowed_target_types": t.allowed_target_types, "is_open": t.is_open,
                "source": t.source, "status": t.status,
            } for t in types],
            "templates": [{
                "name": t.name, "source": t.source, "mechanism": t.mechanism,
                "emit": t.emit, "status": t.status,
            } for t in templates],
            "pipelines": [{
                "name": p.name, "description": p.description, "kind": p.kind, "stages": p.stages,
            } for p in pipelines],
        }
        yield "], " + json.dumps(tail, default=str)[1:]

    fname = f"opencortyx-backup-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return StreamingResponse(_gen(), media_type="application/json",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


def _node_from(workspace_id, n: dict) -> Node | None:
    from datetime import datetime
    try:
        return Node(
            id=uuid.UUID(n["id"]), workspace_id=workspace_id,
            collection_id=uuid.UUID(n["collection_id"]) if n.get("collection_id") else None,
            type=n.get("type") or "Node", entity_type=n.get("entity_type"), label=n.get("label"),
            properties=n.get("properties") or {}, confidence=float(n.get("confidence", 1.0)),
            level=int(n.get("level") or 0),
            source_ref=uuid.UUID(n["source_ref"]) if n.get("source_ref") else None,
            sensitivity=n.get("sensitivity") or "normal",
            created_at=datetime.fromisoformat(n["created_at"]) if n.get("created_at") else None)
    except (KeyError, TypeError, ValueError):
        return None


@router.post("/import")
async def import_graph(spec: ImportSpec, user: CurrentUser = Depends(require_write)):
    """Restore a backup into this organization. `merge` upserts by id (idempotent); `replace` clears the
    organization's graph first. Node ids are preserved so edges reconnect exactly; embeddings are rebuilt
    in the background."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from opencortyx.core.substrate.quarantine import capture
    d = spec.data or {}
    if not isinstance(d, dict) or (not d.get("nodes") and not d.get("edges")):
        raise HTTPException(status_code=400, detail="Not an OpenCortyx export (no nodes/edges found).")
    res = {"nodes": 0, "edges": 0, "types": 0, "templates": 0, "pipelines": 0}
    repo, org = NodeRepo(), user.workspace_id

    if spec.mode == "replace":
        await repo.clear_org(org)

    nodes = [n for n in (_node_from(org, raw) for raw in d.get("nodes") or []) if n is not None]
    for start in range(0, len(nodes), _IMPORT_BATCH):
        res["nodes"] += len(await repo.save_many(org, nodes[start:start + _IMPORT_BATCH]))

    edges = []
    for e in d.get("edges") or []:
        try:
            edges.append({"source_id": uuid.UUID(e["source_id"]), "target_id": uuid.UUID(e["target_id"]),
                          "type": e.get("type") or "REL", "weight": e.get("weight", 1.0),
                          "properties": e.get("properties") or {}, "confidence": e.get("confidence", 1.0)})
        except (KeyError, TypeError, ValueError) as ex:
            # an unparseable endpoint never becomes valid, but it was in the user's file: record it
            await capture(org, "edge", {"source_id": e.get("source_id"), "target_id": e.get("target_id"),
                                        "type": e.get("type")},
                          reason="import_bad_endpoint_id", retryable=False, asset="graph-import", error=str(ex))
    for start in range(0, len(edges), _IMPORT_BATCH):
        written = await repo.add_edges(workspace_id=org, edges=edges[start:start + _IMPORT_BATCH], asset="graph-import")
        res["edges"] += len(written.created) + len(written.updated)

    async with V2Session() as db:
        for t in (d.get("type_registry") or []):
            if not t.get("name") or not t.get("kind"):
                continue
            await db.execute(pg_insert(TypeRegistry).values(
                workspace_id=org, name=t["name"], kind=t["kind"], description=t.get("description") or "",
                properties_schema=t.get("properties_schema") or {},
                allowed_source_types=t.get("allowed_source_types"),
                allowed_target_types=t.get("allowed_target_types"),
                is_open=t.get("is_open", True), source=t.get("source") or "system",
                status=t.get("status") or "active",
            ).on_conflict_do_nothing(index_elements=TYPE_KEY))
            res["types"] += 1

        for t in (d.get("templates") or []):
            if not t.get("name"):
                continue
            ex = (await db.execute(select(ExtractionTemplate).where(
                ExtractionTemplate.workspace_id == user.workspace_id,
                ExtractionTemplate.name == t["name"]))).scalar_one_or_none()
            if ex:
                ex.source, ex.mechanism, ex.emit, ex.status = (
                    t.get("source") or {}, t.get("mechanism") or {}, t.get("emit") or {},
                    t.get("status") or "active")
            else:
                db.add(ExtractionTemplate(workspace_id=user.workspace_id, name=t["name"],
                       source=t.get("source") or {}, mechanism=t.get("mechanism") or {},
                       emit=t.get("emit") or {}, status=t.get("status") or "active"))
            res["templates"] += 1

        for p in (d.get("pipelines") or []):
            if not p.get("name"):
                continue
            ex = (await db.execute(select(Pipeline).where(
                Pipeline.workspace_id == user.workspace_id, Pipeline.name == p["name"]))).scalar_one_or_none()
            if ex:
                ex.description, ex.kind, ex.stages = (
                    p.get("description") or "", p.get("kind") or "document", p.get("stages") or [])
            else:
                db.add(Pipeline(workspace_id=user.workspace_id, name=p["name"],
                       description=p.get("description") or "", kind=p.get("kind") or "document",
                       stages=p.get("stages") or []))
            res["pipelines"] += 1

        await db.commit()

    if nodes:
        from opencortyx.tasks.graph import v2_index_node_attrs
        v2_index_node_attrs.delay(str(org), backfill=True, limit=max(20000, len(nodes)))
    return {"status": "ok", "mode": spec.mode, "imported": res}
