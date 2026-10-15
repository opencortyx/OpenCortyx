"""Routes: reading, authoring and searching nodes."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from opencortyx.api.deps import audit
from opencortyx.api.schemas.graph.nodes import NodePatch, NodeSpec
from opencortyx.api.services.graph.nodes import node_subgraph
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.knowledge import authoring

router = APIRouter()


@router.get("/nodes/{node_id}")
async def get_node_details(node_id: uuid.UUID, hops: int = 1, focus: bool = False,
                           user: CurrentUser = Depends(require_auth)):
    """Fetch a specific node and its N-hop subgraph.

    `focus=true` returns only what this node actually produced: the walk skips time-bookkeeping edges,
    which otherwise bridge every node in the workspace through the shared Date nodes. Use it for
    "what came out of THIS source" views (e.g. verifying a schema mapping).

    `asset` is the catalogue's own view of the node (kind, format, counts) — the same projection the
    asset list uses, so the detail page never re-derives what kind of asset it is from a type name."""
    from opencortyx.api.services.ingestion.structured import asset_view
    from opencortyx.core.substrate import NodeRepo
    res = await node_subgraph(user.workspace_id, node_id, hops=hops, focus=focus)
    node = await NodeRepo().get(user.workspace_id, node_id)
    if node is not None and isinstance(res, dict):
        res["asset"] = asset_view(node)
    return res


@router.get("/nodes/{node_id}/sources")
async def node_sources(node_id: uuid.UUID, q: str = "", offset: int = 0, limit: int = 25,
                       user: CurrentUser = Depends(require_auth)):
    """Every asset a node came from, ranked for `q`, a page at a time — what a citation's "+ N more"
    opens. A shared entity can live in thousands of assets, so this pages rather than returning all."""
    from opencortyx.core.substrate.provenance import sources_page
    return await sources_page(user.workspace_id, node_id, query=q, offset=offset, limit=limit)


@router.post("/nodes")
async def create_node_ep(spec: NodeSpec, user: CurrentUser = Depends(require_write)):
    from opencortyx.core.substrate.validation import validate_node
    async with V2Session() as db:               # write-time ontology validation (registered types)
        ok, reason = await validate_node(db, user.workspace_id, spec.type, spec.properties)
    if not ok:
        raise HTTPException(status_code=422, detail=reason)
    nid = await authoring.create_node(
        user.workspace_id, type=spec.type, label=spec.label, properties=spec.properties,
        collection_id=(uuid.UUID(spec.collection_id) if spec.collection_id else None),
        author=user.user_id)
    await audit(user.workspace_id, user.user_id, "curate/create_node", node_id=nid,
                meta={"type": spec.type, "label": spec.label})
    return {"id": str(nid)}


@router.patch("/nodes/{node_id}")
async def update_node_ep(node_id: uuid.UUID, spec: NodePatch, user: CurrentUser = Depends(require_write)):
    ok = await authoring.update_node(user.workspace_id, node_id, label=spec.label or None,
                                     properties=spec.properties, collection_id=spec.collection_id,
                                     author=user.user_id)
    if ok:
        await audit(user.workspace_id, user.user_id, "curate/update_node", node_id=node_id,
                    meta={"label": spec.label or None, "fields": list((spec.properties or {}).keys())})
    if not ok:
        raise HTTPException(status_code=404, detail="node not found")
    return {"status": "ok"}


@router.delete("/nodes/{node_id}")
async def delete_node_ep(node_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    ok = await authoring.delete_node(user.workspace_id, node_id)
    if ok:
        await audit(user.workspace_id, user.user_id, "curate/delete_node", node_id=node_id)
    return {"status": "ok" if ok else "not_found"}


@router.get("/search")
async def search_ep(q: str = "", types: Optional[str] = None, limit: int = 20,
                    user: CurrentUser = Depends(require_auth)):
    """Autocomplete for linking — find nodes by label (optionally filtered by comma-sep types)."""
    tlist = [t.strip() for t in types.split(",")] if types else None
    return {"results": await authoring.search_nodes(user.workspace_id, q=q, types=tlist, limit=limit)}
