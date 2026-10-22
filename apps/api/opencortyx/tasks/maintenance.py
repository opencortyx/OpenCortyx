"""Maintenance tasks: integrity audit, reaping stale runs, replaying quarantined edges."""
import uuid
from opencortyx.core.worker.celery_app import celery_app
from opencortyx.core.config import V2_SCHEMA, settings
from opencortyx.core.worker.utils import async_to_sync
from opencortyx.core.observability import get_logger
from opencortyx.core.db import V2Session

log = get_logger(__name__)


@celery_app.task(bind=True, queue="maintenance", name="opencortyx.audit_integrity")
def v2_audit_integrity(self, workspace_id: str = None):
    """Check the write-path invariants and SAY SO when one breaks. The alarm, not a dashboard.

    Everything else in this system records what happened; this is the part that notices. Three
    conditions, none of which occurs during healthy operation at any load or contention level, so any
    of them firing is a real defect rather than a threshold to tune:

      unaccounted    a call wrote nothing, captured nothing and raised nothing — data dropped silently
      balance_delta  the counters disagree with each other, so the numbers themselves are untrustworthy
      row_delta      the graph holds rows the funnel never accounted for, or lost rows it never saw
                     leave — i.e. something writes the graph bypassing NodeRepo

    Logged loudly rather than merely stored, because a number nobody reads is the same as no number.
    Returns the offending orgs so an operator (or an alerting hook) has something to act on."""
    async def _run():
        from sqlalchemy import text
        from opencortyx.core.substrate import accounting as _acct

        await _acct.flush()                     # audit what has actually been recorded
        if workspace_id:
            orgs = [uuid.UUID(workspace_id)]
        else:
            async with V2Session() as db:
                orgs = list((await db.execute(text(
                    f"SELECT workspace_id FROM {V2_SCHEMA}.write_accounting"))).scalars().all())
        bad = {}
        for o in orgs:
            r = await _acct.report(o)
            if r.get("available") and r.get("alarms"):
                bad[str(o)] = r["alarms"]
                for a in r["alarms"]:
                    log.error("integrity.alarm", workspace_id=str(o), alarm=str(a))
        return {"orgs_checked": len(orgs), "orgs_with_alarms": len(bad), "alarms": bad}

    return async_to_sync(_run)()


#: The queues ingest jobs wait on (compose.yaml).
_INGEST_QUEUES = ("graphrag", "extraction", "embedding", "resolution")


def _broker_idle() -> bool:
    """True when the broker holds no ingest job: none waiting and none handed to a worker. Only then
    can a run still `queued` have lost its job. Any doubt (broker unreachable) answers False."""
    try:
        import redis
        r = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=5)
        return not any(r.llen(q) for q in _INGEST_QUEUES) and not r.zcard("unacked_index")
    except Exception:                                   # noqa: BLE001
        return False


@celery_app.task(bind=True, queue="maintenance", name="opencortyx.sweep_uploads")
def v2_sweep_uploads(self):
    """Remove stored uploads (core/uploads.py) past their retention that no unfinished run needs."""
    async def _run():
        from sqlalchemy import text

        from opencortyx.core import uploads
        async with V2Session() as db:
            payloads = (await db.execute(text(
                f"SELECT retry_payload FROM {V2_SCHEMA}.ingest_run "
                f"WHERE status IN ('queued', 'running') AND retry_payload IS NOT NULL"))).scalars().all()
        keep: set[str] = set()
        for p in payloads:
            keep |= uploads.digests_in(p)
        out = uploads.sweep(keep)
        if out["removed"]:
            log.info("uploads.swept", **out)
        return out

    return async_to_sync(_run)()


@celery_app.task(bind=True, queue="maintenance", name="opencortyx.reap_stale_runs")
def v2_reap_stale_runs(self):
    """Fail ingest runs whose worker died, so a half-finished asset cannot pass for a finished one.

    This is the recovery path for the one hole the write quarantine cannot cover: a process killed
    mid-ingest. The database is fine — the open transaction rolls back, so no partial graph is
    committed — but the IngestRun row is left saying `running` forever. Nothing ever contradicts it, so
    the asset looks like it is still being worked on, nobody re-runs it, and its nodes are simply
    absent. That is silent loss by omission rather than by a dropped write, and it is the more
    dangerous kind because every counter says zero.

    Liveness is read from `updated_at`, which the DAG executors bump at every step, so a live run —
    however slow — keeps touching its row. The threshold sits above the HARD task limit
    (TASK_TIME_LIMIT, 35 min), past which Celery has already killed the process: a run quiet for longer
    than that is not slow, it is gone. Marked `failed` with an explicit reason, which puts it in front
    of the user and makes it re-runnable; re-ingestion is idempotent (deterministic ids), so replaying
    it converges rather than duplicating.

    A `queued` run is different: it is waiting its turn, and behind a 1000-file drop that is hours.
    It is only lost when the broker holds no job at all (nothing waiting, nothing handed to a worker)
    and it still has not started; then it is sent again from the dispatch recorded on it."""
    async def _run():
        from sqlalchemy import text

        grace = int(getattr(settings, "TASK_TIME_LIMIT", 2100)) + 600     # hard limit + 10 min
        max_attempts = int(getattr(settings, "RUN_MAX_ATTEMPTS", 3) or 3)
        lost_queued = "OR status = 'queued'" if _broker_idle() else ""

        # Claim the dead runs first, in one statement, so two reapers cannot resume the same asset.
        async with V2Session() as db:
            rows = (await db.execute(text(f"""
                UPDATE {V2_SCHEMA}.ingest_run
                SET status = 'failed', finished_at = now(),
                    error = COALESCE(NULLIF(error, ''), '') ||
                            'Worker stopped before this run finished (no progress for over '
                            || CAST(:mins AS text) || ' minutes). '
                WHERE (status = 'running' {lost_queued})
                  AND updated_at < now() - make_interval(secs => CAST(:grace AS int))
                RETURNING id, asset, attempt, retry_payload"""),
                {"grace": int(grace), "mins": str(grace // 60)})).all()
            await db.commit()
        from opencortyx.ingest.runs.progress import reconcile_upload_ingestions
        await reconcile_upload_ingestions()
        if not rows:
            return {"reaped": 0, "resumed": 0, "needs_attention": 0}

        resumed, stuck = [], []
        for rid, asset, attempt, payload in rows:
            if payload and payload.get("task") and int(attempt or 0) < max_attempts:
                try:
                    celery_app.send_task(payload["task"],
                                         args=payload.get("args") or [],
                                         kwargs=payload.get("kwargs") or {})
                    resumed.append((rid, asset, int(attempt or 0) + 1))
                    continue
                except Exception as e:              # noqa: BLE001 — fall through to needs-attention
                    log.warning("run.resume_dispatch_failed", asset=str(asset), error=str(e))
            stuck.append((rid, asset, "no dispatch info" if not payload else "attempt limit reached"))

        async with V2Session() as db:
            for rid, _asset, att in resumed:
                await db.execute(text(f"""
                    UPDATE {V2_SCHEMA}.ingest_run
                    SET attempt = :a, error = COALESCE(error,'') ||
                        'Automatically resumed (attempt ' || CAST(:a_txt AS text) || ' of '
                        || CAST(:m AS text) || ').'
                    WHERE id = :i"""),
                    {"i": rid, "a": int(att), "a_txt": str(att), "m": str(max_attempts)})
            for rid, _asset, why in stuck:
                await db.execute(text(f"""
                    UPDATE {V2_SCHEMA}.ingest_run
                    SET error = COALESCE(error,'') || 'Not resumed automatically (' || :w
                                || ') — re-run this asset.' WHERE id = :i"""),
                    {"i": rid, "w": why})
            await db.commit()

        if resumed or stuck:
            log.info("run.reaped", resumed=len(resumed), needs_attention=len(stuck))
        return {"reaped": len(rows), "resumed": len(resumed), "needs_attention": len(stuck),
                "resumed_assets": [str(a) for _i, a, _n in resumed][:20],
                "stuck_assets": [str(a) for _i, a, _w in stuck][:20]}

    return async_to_sync(_run)()


@celery_app.task(bind=True, queue="maintenance", name="opencortyx.replay_quarantine")
def v2_replay_quarantine(self, workspace_id: str = None):
    """Write the graph edges that could not be written when they were first produced.

    Almost all of them are the same story: extraction emitted a relationship naming an entity that a
    LATER chunk creates, so the edge was correct and merely early. Minutes later the endpoint exists
    and the identical write succeeds. Until this ran, those edges were simply gone — and gone without
    a record, which made the loss impossible to measure, let alone fix.

    Anything still unwritable stays queued and visible rather than being deleted; `capture` stops
    retrying after MAX_ATTEMPTS but never forgets. That is what makes "zero loss" checkable instead of
    aspirational: the graph either has the edge, or this table says why it does not."""
    async def _run():
        from sqlalchemy import text
        from opencortyx.core.substrate.quarantine import replay

        # Fold this process's write counters into the shared totals first. Every worker does it on its
        # own tick, so the measured `unaccounted` reflects all of them and not just whoever is asked.
        from opencortyx.core.substrate import accounting as _acct
        flushed = await _acct.flush()

        if workspace_id:
            orgs = [uuid.UUID(workspace_id)]
        else:
            async with V2Session() as db:
                orgs = list((await db.execute(text(
                    f"SELECT DISTINCT workspace_id FROM {V2_SCHEMA}.write_quarantine "
                    f"WHERE resolved_at IS NULL AND retryable"))).scalars().all())
        out = {}
        for o in orgs:
            r = await replay(o)
            if r["tried"]:
                out[str(o)] = r
        return {"orgs": len(orgs), "result": out, "accounting_flushed": flushed}

    return async_to_sync(_run)()
