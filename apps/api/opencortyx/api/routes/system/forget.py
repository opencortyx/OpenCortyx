"""Routes: forgetting - a subject (a person, a company, any record) or an asset, from every store.

The work is knowledge/forget.py. Each request is a deletion job with a step per store and a proof
that nothing is left; an incomplete job can be retried. Forgetting is admin-only: it is permanent,
reaches every store, and leaves a tombstone so nothing reads it back.
"""
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from opencortyx.api.deps import audit
from opencortyx.core.db import V2Session
from opencortyx.core.security.auth import CurrentUser, require_admin
from opencortyx.domain.governance import DeletionJob, Tombstone
from opencortyx.knowledge import forget

router = APIRouter()


class ForgetSubjectIn(BaseModel):
    node_id: str
    #: documents that mention the subject, chosen to be forgotten whole with it
    also_assets: List[str] = Field(default_factory=list, max_length=500)
    confirm: str = Field(..., min_length=1, max_length=200)   # the subject's name, typed again


def _uid(v: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(v))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{what} is not an id")


@router.get("/forget/subject/{node_id}/preview")
async def preview_subject(node_id: uuid.UUID, admin: CurrentUser = Depends(require_admin)):
    """What forgetting this subject would remove, and the documents that also mention it (to choose
    whether to forget them whole)."""
    out = await forget.preview_subject(admin.workspace_id, node_id)
    if out is None:
        raise HTTPException(status_code=404, detail="record not found")
    return out


@router.post("/forget/subject")
async def forget_subject(body: ForgetSubjectIn, admin: CurrentUser = Depends(require_admin)):
    """Forget the subject everywhere. `confirm` must be the subject's name (its label), as typed
    in the confirmation dialog: forgetting cannot be undone."""
    from opencortyx.core.substrate import NodeRepo
    nid = _uid(body.node_id, "node_id")
    node = await NodeRepo().get(admin.workspace_id, nid)
    if node is None:
        raise HTTPException(status_code=404, detail="record not found")
    if (body.confirm or "").strip().lower() != (node.label or "").strip().lower():
        raise HTTPException(status_code=422, detail="type the record's name exactly to confirm")
    assets = [_uid(a, "also_assets") for a in body.also_assets]
    res = await forget.forget_subject(admin.workspace_id, nid, also_assets=assets, by=str(admin.user_id))
    if res is None:
        raise HTTPException(status_code=404, detail="record not found")
    await audit(admin.workspace_id, admin.user_id, "subject/forget", node_id=None,
                meta={"job_id": res["id"], "status": res["status"]})
    return res


@router.get("/forget/jobs")
async def list_jobs(limit: int = Query(50, ge=1, le=500), admin: CurrentUser = Depends(require_admin)):
    async with V2Session() as db:
        rows = (await db.execute(select(DeletionJob).where(DeletionJob.workspace_id == admin.workspace_id)
                                 .order_by(DeletionJob.created_at.desc()).limit(limit))).scalars().all()
    return {"items": [forget.job_view(r) for r in rows]}


@router.get("/forget/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, admin: CurrentUser = Depends(require_admin)):
    async with V2Session() as db:
        row = await db.get(DeletionJob, job_id)
    if row is None or row.workspace_id != admin.workspace_id:
        raise HTTPException(status_code=404, detail="job not found")
    return forget.job_view(row)


@router.post("/forget/jobs/{job_id}/retry")
async def retry_job(job_id: uuid.UUID, admin: CurrentUser = Depends(require_admin)):
    """Run an incomplete job's failed steps again, then prove again."""
    res = await forget.retry(admin.workspace_id, job_id)
    if res is None:
        raise HTTPException(status_code=404, detail="job not found")
    return res


@router.get("/forget/tombstones")
async def list_tombstones(kind: Optional[str] = None, admin: CurrentUser = Depends(require_admin)):
    """What was forgotten, as hashes only (the tombstone does not remember what it forgot), with the
    job that made each one."""
    q = select(Tombstone).where(Tombstone.workspace_id == admin.workspace_id)
    if kind:
        q = q.where(Tombstone.kind == kind)
    async with V2Session() as db:
        rows = (await db.execute(q.order_by(Tombstone.created_at.desc()).limit(1000))).scalars().all()
    return {"items": [{"id": str(t.id), "kind": t.kind, "hash": t.key_hash[:12],
                       "job_id": str(t.job_id) if t.job_id else None, "created_by": t.created_by,
                       "created_at": t.created_at.isoformat() if t.created_at else None} for t in rows]}


@router.delete("/forget/tombstones/{tombstone_id}")
async def lift_tombstone(tombstone_id: uuid.UUID, admin: CurrentUser = Depends(require_admin)):
    """Allow what was forgotten to be read again (it is not restored: it is only no longer refused)."""
    if not await forget.lift_tombstone(admin.workspace_id, tombstone_id):
        raise HTTPException(status_code=404, detail="tombstone not found")
    await audit(admin.workspace_id, admin.user_id, "tombstone/lift", node_id=None, meta={"id": str(tombstone_id)})
    return {"status": "lifted"}
