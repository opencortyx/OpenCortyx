"""Routes: comments on assets."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from opencortyx.api.schemas.graph.comments import CommentSpec
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.substrate import NodeRepo

router = APIRouter()
_repo = NodeRepo()


@router.get("/assets/{node_id}/comments")
async def list_comments_ep(node_id: uuid.UUID, user: CurrentUser = Depends(require_auth)):
    rows = await _repo.find(user.workspace_id, exact_types=["Comment"], source_refs=[node_id], order_by="created_at")
    return {"items": [{"id": str(c.id), "text": (c.properties or {}).get("text", ""),
                       "author": (c.properties or {}).get("author"),
                       "ts": (c.properties or {}).get("ts") or (c.created_at.isoformat() if c.created_at else None)}
                      for c in rows]}


@router.post("/assets/{node_id}/comments")
async def add_comment_ep(node_id: uuid.UUID, spec: CommentSpec, user: CurrentUser = Depends(require_write)):
    text = (spec.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty comment")
    if await _repo.get(user.workspace_id, node_id) is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    ts = datetime.now(timezone.utc).isoformat()
    comment = await _repo.upsert_node(workspace_id=user.workspace_id, type="Comment", label=text[:60], source_ref=node_id,
                                      properties={"text": text, "author": str(user.user_id), "ts": ts})
    await _repo.add_edge(workspace_id=user.workspace_id, source_id=node_id, target_id=comment.id, type="HAS_COMMENT")
    return {"id": str(comment.id), "text": text, "author": str(user.user_id), "ts": ts}


@router.delete("/comments/{comment_id}")
async def delete_comment_ep(comment_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    comment = await _repo.get(user.workspace_id, comment_id)
    if comment is None or comment.type != "Comment":
        raise HTTPException(status_code=404, detail="Comment not found")
    await _repo.delete_nodes(user.workspace_id, [comment_id])
    return {"deleted": True}
