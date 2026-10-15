"""Routes: authoring edges and relationships."""
import uuid

from fastapi import APIRouter, Depends, HTTPException

from opencortyx.api.deps import audit
from opencortyx.api.schemas.graph.edges import EdgeSpec, RelationshipSpec
from opencortyx.core.security.auth import CurrentUser, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.ontology import TypeRegistry, find_type, own_type
from opencortyx.core.substrate import NodeRepo
from opencortyx.knowledge import authoring

router = APIRouter()


@router.post("/edges")
async def create_edge_ep(spec: EdgeSpec, user: CurrentUser = Depends(require_write)):
    """Link two nodes. The edge type is registered in the ontology, or widened to these endpoint
    types, so the Studio shows what users actually connect."""

    repo = NodeRepo()
    s = await repo.get(user.workspace_id, uuid.UUID(spec.source_id))
    t = await repo.get(user.workspace_id, uuid.UUID(spec.target_id))
    if s is None or t is None:
        raise HTTPException(status_code=404, detail="Source or target node not found.")

    async with V2Session() as db:
        reg = await find_type(db, user.workspace_id, spec.type, "edge")
        if reg is None:
            db.add(TypeRegistry(workspace_id=user.workspace_id, name=spec.type, kind="edge", description="",
                                allowed_source_types=[s.type], allowed_target_types=[t.type],
                                properties_schema={}, source="user", status="active"))
        elif ((reg.allowed_source_types and s.type not in reg.allowed_source_types)
              or (reg.allowed_target_types and t.type not in reg.allowed_target_types)):
            # widened in this organization's own copy: a built-in edge type is never changed for others
            reg = await own_type(db, user.workspace_id, spec.type, "edge")
            if reg.allowed_source_types and s.type not in reg.allowed_source_types:
                reg.allowed_source_types = [*reg.allowed_source_types, s.type]
            if reg.allowed_target_types and t.type not in reg.allowed_target_types:
                reg.allowed_target_types = [*reg.allowed_target_types, t.type]
        await db.commit()

    res = await authoring.create_edge(
        user.workspace_id, source_id=s.id, target_id=t.id, type=spec.type, properties=spec.properties,
        author=user.user_id)
    if isinstance(res, dict) and res.get("status") == "ok":
        await audit(user.workspace_id, user.user_id, "curate/create_edge",
                    meta={"type": spec.type, "source": spec.source_id, "target": spec.target_id})
    return res


@router.delete("/edges/{edge_id}")
async def delete_edge_ep(edge_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    ok = await authoring.delete_edge(user.workspace_id, edge_id)
    if ok:
        await audit(user.workspace_id, user.user_id, "curate/delete_edge", meta={"edge_id": str(edge_id)})
    return {"status": "ok" if ok else "not_found"}


@router.post("/relationships")
async def add_relationship_ep(spec: RelationshipSpec, user: CurrentUser = Depends(require_write)):
    res = await authoring.add_relationship(user.workspace_id, source_id=spec.source_id,
                                           target_id=spec.target_id, relation=spec.relation,
                                           author=user.user_id)
    if isinstance(res, dict) and res.get("status") == "endpoint_not_found":
        raise HTTPException(status_code=404, detail="Source or target node not found.")
    if isinstance(res, dict) and res.get("status") == "ok":
        await audit(user.workspace_id, user.user_id, "curate/relationship",
                    meta={"source": spec.source_id, "target": spec.target_id, "relation": spec.relation})
    return res
