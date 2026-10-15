"""Routes: the browse tree the dashboard graph draws (see `knowledge/graph_tree.py`), and asset tags.

Every read is one parent's children, one page; the size of a response never depends on the size of
the graph. Items name what they open into with an opaque `key`, which the page sends back as is.
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from opencortyx.api.deps import audit
from opencortyx.api.schemas.graph.assets import TagsEdit
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.substrate import NodeRepo
from opencortyx.knowledge import graph_tree

router = APIRouter()


def _queue_index(org, *, now: bool = True) -> bool:
    from opencortyx.tasks.graph_tree import schedule_tree_index
    return schedule_tree_index(str(org), now=now)


@router.get("/graph/tree")
async def graph_tree_top(user: CurrentUser = Depends(require_auth)):
    """The first screen: the workspace root, and the largest parts of the graph no asset reaches."""
    out = await graph_tree.top(user.workspace_id)
    if out["run"] is None and await NodeRepo().count(user.workspace_id):
        # First view of a graph indexed before this feature existed: index it now.
        _queue_index(user.workspace_id)
        out["run"] = {"status": "queued"}
    return out


@router.get("/graph/tree/children")
async def graph_tree_children(key: str, offset: int = 0, limit: Optional[int] = None,
                              user: CurrentUser = Depends(require_auth)):
    """What one item opens into, a page at a time (at most 100): a source's tags, a tag's assets,
    an asset's parts, a node's relationships — or, past 1000 of them, their groups."""
    try:
        return await graph_tree.children(user.workspace_id, key, offset, limit)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/graph/tree/locate/{node_id}")
async def graph_tree_locate(node_id: uuid.UUID, user: CurrentUser = Depends(require_auth)):
    """The items to open, top down, to bring a node on screen."""
    found = await graph_tree.locate(user.workspace_id, node_id)
    if found is None:
        raise HTTPException(404, "no indexed asset reaches this node")
    return found


@router.get("/graph/tree/search")
async def graph_tree_search(q: str, limit: int = 20, user: CurrentUser = Depends(require_auth)):
    return await graph_tree.search(user.workspace_id, q, max(1, min(int(limit), 50)))


@router.post("/graph/tree/rebuild")
async def graph_tree_rebuild(user: CurrentUser = Depends(require_write)):
    """Re-index now instead of waiting for the next change."""
    if not _queue_index(user.workspace_id):
        raise HTTPException(503, "the task queue is not reachable")
    return {"status": "queued"}


@router.get("/assets/{asset_id}/tags")
async def get_asset_tags(asset_id: uuid.UUID, user: CurrentUser = Depends(require_auth)):
    node = await NodeRepo().get(user.workspace_id, asset_id)
    if node is None:
        raise HTTPException(404, "asset not found")
    tags, auto = graph_tree.asset_tags(node)
    return {"tags": [] if auto else tags, "default": graph_tree.default_tag(node)}


@router.put("/assets/{asset_id}/tags")
async def put_asset_tags(asset_id: uuid.UUID, body: TagsEdit, user: CurrentUser = Depends(require_write)):
    """Replace an asset's tags. An empty list gives it back its default tag (its file extension)."""
    try:
        tags = await graph_tree.set_tags(user.workspace_id, asset_id, body.tags)
    except LookupError:
        raise HTTPException(404, "asset not found")
    await audit(user.workspace_id, user.user_id, "asset/tags", node_id=asset_id, meta={"tags": tags})
    node = await NodeRepo().get(user.workspace_id, asset_id)
    return {"tags": tags, "default": graph_tree.default_tag(node) if node else ""}
