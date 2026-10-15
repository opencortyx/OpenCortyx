"""Routes: ingestion run progress."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select

from opencortyx.api.services.ingestion.ingestions import run_view
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.control import IngestRun

router = APIRouter()


@router.get("/progress")
async def list_progress(limit: int = 100, user: CurrentUser = Depends(require_auth)):
    """List ingestion runs (newest first) + a status summary for the header cards."""
    async with V2Session() as db:
        rows = (await db.execute(
            select(IngestRun).where(IngestRun.workspace_id == user.workspace_id)
            .order_by(IngestRun.created_at.desc()).limit(limit))).scalars().all()
        counts = dict((await db.execute(
            select(IngestRun.status, func.count()).where(IngestRun.workspace_id == user.workspace_id)
            .group_by(IngestRun.status))).all())
    summary = {"total": sum(counts.values()),
               "queued": counts.get("queued", 0), "running": counts.get("running", 0),
               "completed": counts.get("completed", 0), "failed": counts.get("failed", 0),
               "canceled": counts.get("canceled", 0)}
    return {"summary": summary, "items": [run_view(r) for r in rows]}


@router.get("/progress/{run_id}")
async def get_progress(run_id: str, user: CurrentUser = Depends(require_auth)):
    """One run with full per-stage detail + final result."""
    async with V2Session() as db:
        r = await db.get(IngestRun, uuid.UUID(run_id))
    if not r or r.workspace_id != user.workspace_id:
        raise HTTPException(status_code=404, detail="run not found")
    return run_view(r, detail=True)


@router.get("/progress/{run_id}/outcome")
async def get_outcome(run_id: str, user: CurrentUser = Depends(require_auth)):
    """What this run actually produced, and what is worth doing about it.

    Distinct from `/progress/{run_id}`, which reports how the WORKER went — steps, percent, the
    scalars that path happened to return. This reports what is in the graph because of it, which
    is the question the person who uploaded the file was asking. Safe to poll while a run is
    still going: it answers with what exists so far.
    """
    from opencortyx.ingest.runs.outcome import ingest_outcome

    try:
        outcome = await ingest_outcome(user.workspace_id, run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{run_id!r} is not a run id") from exc
    if outcome is None:
        raise HTTPException(status_code=404, detail="run not found")
    return outcome.to_dict()


@router.delete("/progress/{run_id}")
async def delete_progress(run_id: str, purge: bool = True, user: CurrentUser = Depends(require_write)):
    """Delete a progress record. If it's still queued/running this cancels it cooperatively (the
    executor stops at the next stage boundary) + revokes the Celery task, and — when `purge` —
    deletes what that half-finished run wrote so nothing dangles.

    A FINISHED run's record is only a record: deleting it never touches the asset. Every run of the
    same file shares one asset, so purging on a failed or canceled run's delete erased the content a
    later, successful run had produced. An asset is removed with its own delete."""
    from opencortyx.ingest.runs.progress import purge_asset
    rid = uuid.UUID(run_id)
    async with V2Session() as db:
        r = await db.get(IngestRun, rid)
        if not r or r.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="run not found")
        was_active = r.status in ("queued", "running")
        kind, source_ref, task_id = r.kind, r.source_ref, r.task_id
        if was_active:
            r.status = "canceled"            # cooperative stop — executor checks between stages
            await db.commit()
    if was_active and task_id:               # stop a queued task from starting / a running one sooner
        try:
            from opencortyx.core.worker.celery_app import celery_app
            celery_app.control.revoke(task_id)
        except Exception:
            pass
    purged = 0
    if purge and was_active and source_ref:
        purged = await purge_asset(user.workspace_id, kind, source_ref)
    async with V2Session() as db:
        await db.execute(delete(IngestRun).where(
            IngestRun.id == rid, IngestRun.workspace_id == user.workspace_id))
        await db.commit()
    return {"status": "ok", "canceled": was_active, "purged_nodes": purged}


@router.delete("/progress")
async def clear_progress(status: Optional[str] = None, user: CurrentUser = Depends(require_write)):
    """Clear progress records (audit-log style). `status=completed` clears only those; default clears
    every TERMINAL record (completed/failed/canceled) and never touches active runs."""
    async with V2Session() as db:
        q = delete(IngestRun).where(IngestRun.workspace_id == user.workspace_id)
        q = q.where(IngestRun.status == status) if status else \
            q.where(IngestRun.status.in_(["completed", "failed", "canceled"]))
        res = await db.execute(q)
        await db.commit()
    return {"status": "ok", "cleared": res.rowcount or 0}
