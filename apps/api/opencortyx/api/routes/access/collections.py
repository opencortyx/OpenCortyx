"""Routes: collections."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from opencortyx.api.schemas.access.collections import CollectionSpec
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.core.substrate import NodeRepo
from opencortyx.domain.control import Collection

router = APIRouter()
_repo = NodeRepo()


@router.get("/collections")
async def list_collections(user: CurrentUser = Depends(require_auth)):
    async with V2Session() as db:
        rows = (await db.execute(select(Collection).where(Collection.workspace_id == user.workspace_id))).scalars().all()
    return {"collections": [{"id": str(c.id), "name": c.name, "description": c.description} for c in rows]}


@router.post("/collections")
async def create_collection(spec: CollectionSpec, user: CurrentUser = Depends(require_write)):
    name = " ".join((spec.name or "").split())[:255]
    if not name:
        raise HTTPException(status_code=422, detail="A collection needs a name.")
    async with V2Session() as db:
        dup = (await db.execute(select(Collection).where(Collection.workspace_id == user.workspace_id,
                                                         func.lower(Collection.name) == name.lower()))).scalars().first()
        if dup is not None:
            raise HTTPException(status_code=409, detail=f'A collection called "{name}" already exists.')
        c = Collection(workspace_id=user.workspace_id, name=name, description=spec.description or "")
        db.add(c)
        await db.commit()
        await db.refresh(c)
    return {"id": str(c.id), "name": c.name}


async def _collection(db, workspace_id, collection_id: str):
    c = (await db.execute(select(Collection).where(
        Collection.id == uuid.UUID(collection_id), Collection.workspace_id == workspace_id))).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=404, detail="collection not found")
    return c


@router.get("/collections/{collection_id}/delete-preview")
async def preview_collection_delete(collection_id: str,
                                    user: CurrentUser = Depends(require_write)):
    """What deleting this collection would remove: nodes by type, plus the edges touching them. A
    collection delete removes a whole corpus, so the confirm dialog shows its full reach."""
    async with V2Session() as db:
        c = await _collection(db, user.workspace_id, collection_id)
    nodes = await _repo.find(user.workspace_id, collection_ids=[c.id])
    by_type: dict[str, int] = {}
    for n in nodes:
        by_type[n.type] = by_type.get(n.type, 0) + 1
    edges = await _repo.edges(user.workspace_id, node_ids=[n.id for n in nodes]) if nodes else []
    return {"collection_id": str(c.id), "name": c.name, "nodes": len(nodes), "edges": len(edges),
            "by_type": [{"type": t, "count": n} for t, n in sorted(by_type.items(), key=lambda kv: -kv[1])]}


@router.delete("/collections/{collection_id}")
async def delete_collection(collection_id: str, user: CurrentUser = Depends(require_write)):
    """Delete a collection and everything in it from every store: each asset through the full
    deletion (knowledge/forget.py - data, evidence, cache, uploads, chats...), then the records left
    in the collection, then the policies that named it."""
    from sqlalchemy import delete as sa_delete

    from opencortyx.core.security import scope as _scope
    from opencortyx.domain.control import Policy
    from opencortyx.domain.ontology import is_asset_node
    from opencortyx.knowledge.forget import forget_asset
    async with V2Session() as db:
        c = await _collection(db, user.workspace_id, collection_id)
        cid = c.id
    with _scope.unscoped():
        nodes = await _repo.find(user.workspace_id, collection_ids=[cid])
    jobs = []
    for n in [n for n in nodes if is_asset_node(n)]:
        res = await forget_asset(user.workspace_id, n.id, by=str(user.user_id), tombstone=False)
        if res:
            jobs.append({"id": res["id"], "status": res["status"]})
    with _scope.unscoped():
        left = await _repo.find(user.workspace_id, collection_ids=[cid])
    if left:
        await _repo.delete_nodes(user.workspace_id, [n.id for n in left])
    async with V2Session() as db:
        await db.execute(sa_delete(Policy).where(Policy.workspace_id == user.workspace_id, Policy.collection_id == cid))
        c = await _collection(db, user.workspace_id, collection_id)
        await db.delete(c)
        await db.commit()
    _scope.forget(workspace_id=user.workspace_id)
    return {"status": "ok", "deleted": True, "assets": len(jobs),
            "incomplete": [j for j in jobs if j["status"] != "done"]}
