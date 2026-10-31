"""Maintenance tasks: integrity audit, reaping stale runs, replaying quarantined edges."""
import os
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


@celery_app.task(bind=True, queue="maintenance", name="opencortyx.compact_data")
def v2_compact_data(self):
    """Compact the managed Delta tables and vacuum what no version from the last week needs
    (stores/data). Small files from many one-document commits otherwise slow every read."""
    from opencortyx.stores.data import compact
    out = compact()
    if out["tables"] or out["failed"]:
        log.info("data.compacted", **out)
    return out


@celery_app.task(bind=True, queue="maintenance", name="opencortyx.check_external_sources")
def v2_check_external_sources(self):
    """Re-read an external asset whose data changed: a customer's Delta table at a new version, or a
    live table whose declared watermark column (`metadata.watermark_column`) moved. The data is
    never copied, so this is what keeps the graph's knowledge of it current. One re-ingest per
    connector, however many of its tables changed."""
    async def _run():
        import anyio
        from sqlalchemy import select

        from opencortyx.core.dataplane.connectors import attach_dsn
        from opencortyx.domain.data import DataAsset
        from opencortyx.stores.data import AssetRef, Views, delta
        async with V2Session() as db:
            rows = (await db.execute(select(DataAsset).where(
                DataAsset.storage == "external", DataAsset.status == "active"))).scalars().all()
        due: dict = {}
        for a in rows:
            meta = dict(a.metadata_ or {})
            changed, new_ref = False, a.version_ref
            try:
                if a.format == "delta" and os.path.isdir(a.location or ""):
                    v = delta.version(a.location)
                    new_ref = None if v is None else str(v)
                    changed = new_ref != a.version_ref
                elif meta.get("watermark_column") and a.connector_id and a.format in ("postgres", "mysql", "sqlite"):
                    col = str(meta["watermark_column"]).replace('"', '""')
                    ref = AssetRef.from_row(a)
                    ref.dsn = await attach_dsn(a.workspace_id, a.connector_id, a.format)
                    ref.options = {**ref.options, "table": a.location}

                    def _max():
                        with Views(a.workspace_id, [ref]) as v:
                            view = next(iter(v.names))
                            return v.query(f'SELECT CAST(max("{col}") AS VARCHAR) AS m FROM "{view}"').column("m")[0].as_py()
                    mark = await anyio.to_thread.run_sync(_max)
                    changed = bool(mark) and mark != meta.get("watermark")
                    meta["watermark"] = mark
            except Exception as e:                            # noqa: BLE001 - unreachable: next tick
                log.info("external.check_skipped", asset_id=str(a.id), error=str(e)[:200])
                continue
            if changed and a.connector_id:
                due.setdefault((str(a.workspace_id), str(a.connector_id)), []).append((a.id, new_ref, meta))
        if not due:
            return {"changed": 0}
        from opencortyx.tasks.ingest import v2_ingest_sql
        async with V2Session() as db:
            for items in due.values():
                for aid, new_ref, meta in items:
                    row = await db.get(DataAsset, aid)
                    if row is not None:
                        row.version_ref, row.metadata_ = new_ref, meta
            await db.commit()
        for (ws, conn), items in due.items():
            v2_ingest_sql.delay(ws, None, None, None, False, None, None, conn, False)
            log.info("external.changed", workspace_id=ws, connector_id=conn, tables=len(items))
        return {"changed": sum(len(v) for v in due.values()), "connectors": len(due)}

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
                  AND NOT (COALESCE(result, '{{}}'::jsonb) ? 'finalizing')
                RETURNING id, asset, attempt, retry_payload"""),
                {"grace": int(grace), "mins": str(grace // 60)})).all()
            await db.commit()
        # An ingested run waits on its embeddings and resolution, not on a worker: closed there,
        # or here when that raced or never came - never reaped and re-run.
        from opencortyx.ingest.runs.progress import reconcile_finalizing, reconcile_upload_ingestions
        await reconcile_finalizing()
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


#: Seconds of quiet after an ingestion before the workspace's data is read for its meaning: a
#: thousand-file upload books one pass, not a thousand.
DETECT_DEBOUNCE_S = 120


def schedule_detection(workspace_id) -> bool:
    """Book one detection pass for the workspace (knowledge/semantic/detector.py), debounced the way
    the browse-tree index is. Best-effort: no Redis means no automatic pass, never a failed ingest."""
    from opencortyx.core.config import runtime as _rc
    if not _rc.knowledge_detect():
        return False
    try:
        import redis as _redis
        r = _redis.Redis.from_url(settings.CELERY_BROKER_URL)
        if not r.set(f"v2:detect:{workspace_id}", b"1", nx=True, ex=3600):
            return False
        v2_detect_knowledge.apply_async(args=[str(workspace_id)], countdown=DETECT_DEBOUNCE_S)
        return True
    except Exception as e:                                    # noqa: BLE001
        log.warning("semantic_layer.schedule_skipped", error=str(e))
        return False


# On the extraction queue: naming a type and breaking a tie ask the generation model, which runs
# in-process (~1.2 GiB); the maintenance worker is sized without a model (docs/memory-budget.md) and was
# OOM-killed mid-detection.
@celery_app.task(bind=True, queue="extraction", name="opencortyx.detect_knowledge")
def v2_detect_knowledge(self, workspace_id: str):
    """Propose the workspace's entity types, keys and links from its data; then publish what is
    chosen to the graph - the types a mapping marks `materialize`, and with `knowledge_publish`
    every accepted entity type."""
    from opencortyx.core.config import runtime as _rc
    from opencortyx.knowledge.semantic import detector, publish, store
    try:
        import redis as _redis
        _redis.Redis.from_url(settings.CELERY_BROKER_URL).delete(f"v2:detect:{workspace_id}")
    except Exception:                                         # noqa: BLE001
        pass

    async def run():
        layer = await detector.detect(workspace_id, apply=True)
        asked = _after_detection(workspace_id)
        chosen = await store.choose(workspace_id, asset_ids=asked, materialize=True) if asked else []
        published = await publish.publish(workspace_id, everything=_rc.knowledge_publish())
        _done_after_detection(workspace_id, asked)        # its records are in: no longer pending
        await publish.link_schema(workspace_id, layer)    # the detected links between columns / keys
        return {"types": len(layer.types), "links": len(layer.links), "published": published,
                "chosen_for_graph": chosen}
    return async_to_sync(run)()


#: Assets whose upload asked for knowledge (Add data's "extract knowledge"): they
#: wait for the detection pass that binds them to their types.
_AFTER_DETECTION = "v2:detect:knowledge:{}"


#: An upload that asked for knowledge (Add data), until its run completes.
_PUBLISH_RUN = "v2:knowledge-run:{}"


def want_in_graph(run_id) -> None:
    """Remember that this upload asked for knowledge: its records in the graph, its free text read."""
    try:
        import redis as _redis
        _redis.Redis.from_url(settings.CELERY_BROKER_URL).set(_PUBLISH_RUN.format(run_id), b"1", ex=7 * 24 * 3600)
    except Exception as e:                                    # noqa: BLE001
        log.warning("semantic_layer.publish_choice_skipped", error=str(e))


def book_after_detection(workspace_id, asset_id, run_id=None) -> None:
    """An ingested table or JSON asset whose upload asked for knowledge: its records go into the graph
    and its free text is read once the next detection pass has bound it to its types. Best-effort."""
    try:
        import redis as _redis
        r = _redis.Redis.from_url(settings.CELERY_BROKER_URL)
        if run_id and r.delete(_PUBLISH_RUN.format(run_id)):
            key = _AFTER_DETECTION.format(workspace_id)
            r.sadd(key, str(asset_id))
            r.expire(key, 7 * 24 * 3600)
    except Exception as e:                                    # noqa: BLE001
        log.warning("semantic_layer.after_detection_skipped", error=str(e))


def _after_detection(workspace_id) -> set:
    try:
        import redis as _redis
        members = _redis.Redis.from_url(settings.CELERY_BROKER_URL).smembers(_AFTER_DETECTION.format(workspace_id))
        return {m.decode() if isinstance(m, bytes) else str(m) for m in members}
    except Exception:                                         # noqa: BLE001
        return set()


def _done_after_detection(workspace_id, asset_ids) -> None:
    """Published: the assets leave the booking (others booked meanwhile wait for the next pass)."""
    if not asset_ids:
        return
    try:
        import redis as _redis
        _redis.Redis.from_url(settings.CELERY_BROKER_URL).srem(_AFTER_DETECTION.format(workspace_id), *asset_ids)
    except Exception:                                         # noqa: BLE001
        pass


def knowledge_pending(workspace_id, run_id, asset_id) -> bool:
    """Whether an upload that asked for knowledge is still waiting for detection to put its records
    in the graph: its run is not finished, or its asset waits for the next detection pass."""
    try:
        import redis as _redis
        r = _redis.Redis.from_url(settings.CELERY_BROKER_URL)
        return bool(r.exists(_PUBLISH_RUN.format(run_id))) or (
            bool(asset_id) and bool(r.sismember(_AFTER_DETECTION.format(workspace_id), str(asset_id))))
    except Exception:                                         # noqa: BLE001
        return False


@celery_app.task(bind=True, queue="extraction", name="opencortyx.apply_knowledge_choices", acks_late=True,
                 max_retries=2)
def v2_apply_knowledge_choices(self, workspace_id: str, asset_ids: list | None = None,
                               publish_first: bool = False):
    """Act on what people chose: publish the types marked for the graph (`publish_first`, when that
    choice changed in the Mapper). `asset_ids` is accepted for jobs queued before it was retired."""
    from opencortyx.core.config import runtime as _rc
    from opencortyx.knowledge.semantic import publish

    async def run():
        out = {}
        if publish_first:
            out["published"] = await publish.publish(workspace_id, everything=_rc.knowledge_publish())
        return out
    try:
        return async_to_sync(run)()
    except Exception as exc:
        from opencortyx.tasks._shared import retry_policy
        _max, _delay = retry_policy(self, exc, 2)
        raise self.retry(exc=exc, countdown=_delay, max_retries=_max)
