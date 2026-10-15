"""Routes: worker, queue and workspace resets."""
from fastapi import APIRouter, Depends
from sqlalchemy import delete, select

from opencortyx.domain.control import AuditLog, IngestRun
from opencortyx.domain.ontology import TypeRegistry
from opencortyx.core.security.auth import CurrentUser, require_admin
from opencortyx.core.db import V2Session
from opencortyx.core.substrate import NodeRepo
from opencortyx.core.worker.celery_app import purge_task_queue, revoke_tasks

router = APIRouter()


@router.post("/worker/reset")
async def worker_reset(hard: bool = False, user: CurrentUser = Depends(require_admin)):
    """Un-wedge the worker. Data is never touched.
      • soft: kill only the tasks it is CURRENTLY executing — queued work survives and resumes.
      • hard: the total task nuke — drop every PENDING task from the broker (Redis), kill
        running/reserved/scheduled ones, and restart the worker process (Docker revives it with
        fresh state and an empty queue).
    Killed runs are marked failed, dropped-pending runs canceled, so the Progress tab reflects
    reality — re-uploading those documents re-processes them (a doc only counts as 'unchanged'
    once ingestion actually COMPLETED)."""
    import asyncio

    from sqlalchemy import update as sa_update

    from opencortyx.core.worker.celery_app import reset_worker
    res = await asyncio.to_thread(reset_worker, hard)
    async with V2Session() as db:
        failed = (await db.execute(sa_update(IngestRun).where(
            IngestRun.workspace_id == user.workspace_id, IngestRun.status == "running")
            .values(status="failed", error="killed by worker reset"))).rowcount or 0
        canceled = 0
        if hard:                                   # their broker messages are gone — reflect it
            canceled = (await db.execute(sa_update(IngestRun).where(
                IngestRun.workspace_id == user.workspace_id, IngestRun.status == "queued")
                .values(status="canceled", error="dropped by hard worker reset"))).rowcount or 0
        db.add(AuditLog(workspace_id=user.workspace_id, actor=str(user.user_id), action="worker/reset",
                        meta={**res, "hard": hard, "runs_failed": failed, "runs_canceled": canceled}))
        await db.commit()
    return {"status": "ok", **res, "runs_failed": failed, "runs_canceled": canceled}


@router.post("/queue/purge")
async def purge_queue(user: CurrentUser = Depends(require_admin)):
    """Clear the task QUEUE: drop every queued message at the broker (works even with no worker
    alive) and revoke+terminate everything workers already picked up or scheduled. Also cancels
    this org's queued/running Progress rows so the UI reflects reality. The missing lifecycle
    piece: DB resets never touched the broker, so stale tasks kept their place in line forever."""
    import asyncio

    from sqlalchemy import update as sa_update

    res = await asyncio.to_thread(purge_task_queue)
    async with V2Session() as db:
        marked = (await db.execute(sa_update(IngestRun).where(
            IngestRun.workspace_id == user.workspace_id, IngestRun.status.in_(["queued", "running"]))
            .values(status="canceled"))).rowcount or 0
        db.add(AuditLog(workspace_id=user.workspace_id, actor=str(user.user_id), action="queue/purge",
                        meta={**res, "runs_canceled": marked}))
        await db.commit()
    return {"status": "ok", **res, "runs_canceled": marked}


@router.delete("/reset")
async def reset_database(user: CurrentUser = Depends(require_admin)):
    """DANGER: Deletes the organization's whole graph and its search index, and stops this
    organization's queued and running tasks so none of them keeps writing after the wipe.

    Only THIS organization's tasks: purging the broker (`purge_task_queue`, admin `/queue/purge`) would
    cancel every other tenant's work too. Admin-only, because it deletes the whole graph."""
    import asyncio

    async with V2Session() as db:
        task_ids = (await db.execute(select(IngestRun.task_id).where(
            IngestRun.workspace_id == user.workspace_id, IngestRun.status.in_(["queued", "running"])))).scalars().all()
    queue_res = await asyncio.to_thread(revoke_tasks, task_ids)
    await NodeRepo().clear_org(user.workspace_id)
    async with V2Session() as db:
        # This organization's auto-discovered ontology candidates (Review Inbox → Ontology) described
        # the data being wiped. Built-in and user-registered types stay, and so does every other
        # organization's ontology.
        await db.execute(delete(TypeRegistry).where(
            TypeRegistry.workspace_id == user.workspace_id, TypeRegistry.status == "candidate",
            TypeRegistry.source == "discovered"))
        # The resolution index and ledger name nodes that no longer exist; kept, the next ingest of
        # the same records would be "resolved" as SAME_AS nodes the reset deleted.
        from opencortyx.domain.resolution import ResolutionDecision, ResolutionKey
        await db.execute(delete(ResolutionKey).where(ResolutionKey.workspace_id == user.workspace_id))
        await db.execute(delete(ResolutionDecision).where(ResolutionDecision.workspace_id == user.workspace_id))
        # Also clear the Ingestion Progress runs (Progress tab) — they live in ingest_run, not node/edge.
        run_ids = (await db.execute(select(IngestRun.id).where(
            IngestRun.workspace_id == user.workspace_id))).scalars().all()
        await db.execute(delete(IngestRun).where(IngestRun.workspace_id == user.workspace_id))

        # Log the action
        db.add(AuditLog(
            workspace_id=user.workspace_id,
            actor=str(user.user_id),
            action="database/reset",
            meta={"note": "All graph nodes and edges deleted", "queue": queue_res}
        ))

        await db.commit()
    # Everything else that described the deleted graph: device uploads whose runs are gone (saved
    # connector jobs are configuration and stay), writes held for a graph that no longer exists, and
    # the write counters, re-based on the now-empty graph so the loss alarm starts from zero.
    from opencortyx.core.substrate import accounting, quarantine
    from opencortyx.ingest.runs.progress import forget_upload_runs
    await forget_upload_runs(user.workspace_id, run_ids)
    held = await quarantine.clear(user.workspace_id)
    await accounting.rebaseline(user.workspace_id, reason="database reset")
    return {"status": "ok", "message": "All data deleted.", "queue": queue_res,
            "quarantine_cleared": held}
