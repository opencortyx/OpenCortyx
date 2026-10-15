"""Keeping an ingestion in step with its live source: the sync run and its periodic tick."""
import uuid
from opencortyx.core.worker.celery_app import celery_app
from opencortyx.core.worker.utils import async_to_sync
from opencortyx.core.observability import get_logger
from opencortyx.bootstrap import init_v2_schema
from opencortyx.domain.control import Connector, Ingestion
from opencortyx.core.db import V2Session
from opencortyx.ingest.sources import datalake, sync
from opencortyx.ingest.pipelines.defaults import resolve_pipeline
from opencortyx.tasks._shared import _apply_anchors, _close_ingestion, _post_ingest, _reporter, _roll_up_anchors
from opencortyx.tasks.graph import _queue_embed_pass
from opencortyx.tasks.ingest import _queue_extract_pass, v2_ingest_sql

log = get_logger(__name__)


async def _resolve_storage_dirs(db, connector, directories):
    """Shared with v2_ingest_lake_files: from a connector row + optional folder list, return
    (storage_cfg, dirs). A lake STORAGE connector IS the storage; a 'files' connector references one
    via lake_connector_id; else fall back to the connector's single `directory`."""
    cfg = connector.config or {}
    ckind = (cfg.get("kind") or "").lower()
    if ckind in datalake.LAKE_STORAGE:
        storage_cfg = cfg
    else:
        storage_cfg = {"kind": "local"}
        if cfg.get("lake_connector_id"):
            lc = await db.get(Connector, uuid.UUID(cfg["lake_connector_id"]))
            storage_cfg = (lc.config or {}) if lc else storage_cfg
    dirs = [d for d in (directories or []) if d and str(d).strip()]
    if not dirs:
        # A connector with no folder of its own watches the mounted host folder — never "", which
        # walks whatever the container's working directory happens to be.
        fallback = cfg.get("directory") or cfg.get("root") or datalake.local_fs_root()
        dirs = [fallback] if fallback else []
    return storage_cfg, dirs


async def _ingest_one_file(oid, kind, data: bytes, name: str, pipeline, pipeline_id):
    """Ingest ONE file with the default walker for its KIND and return the created source_id."""
    from opencortyx.ingest.runner import ingest_file
    res = await ingest_file(oid, data, name, kind=kind, pipeline=pipeline, pipeline_id=pipeline_id)
    _queue_embed_pass(str(oid), res)
    if kind == sync.KIND_UNSTRUCTURED:
        await _post_ingest(oid, res, "document")
    else:
        _queue_extract_pass(str(oid), res)
        await _post_ingest(oid, res, kind)
    return res.get("source_id")


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.sync_ingestion")
def v2_sync_ingestion(self, workspace_id: str, ingestion_id: str, run_id: str = None):
    """CHANGE-DETECTION sync for a saved FILE ingestion (unstructured OR semi-structured), across ALL
    of its connector→pipeline mappings. Instead of re-ingesting everything, stat each source cheaply,
    diff against the stored manifest, and touch ONLY the delta:
      • added / changed → ingest the file with that mapping's pipeline (changed = soft-remove old first)
      • removed         → soft-remove the source's graph nodes (kept, excluded from answers)
      • unchanged       → skipped entirely (no read, no LLM)
    Self-healing: on a fatal error the ingestion HIBERNATES (status=paused, failure recorded) and the
    manifest keeps every COMPLETED file, so the next run/tick resumes from where it left off — never
    from the beginning. Manifest keys are namespaced by connector so multiple connectors never collide."""
    async def _run():
        import anyio
        from datetime import datetime, timezone
        from opencortyx.ingest.sources import datalake, sync
        from opencortyx.domain.control import Ingestion, Connector
        from opencortyx.core.config import runtime as rc
        await init_v2_schema()
        oid = uuid.UUID(workspace_id)

        async with V2Session() as db:
            ing = await db.get(Ingestion, uuid.UUID(ingestion_id))
            if not ing or ing.workspace_id != oid:
                raise RuntimeError("ingestion not found")
            ing_kind = sync.normalize_kind(ing.kind)
            cfg = dict(ing.config or {})
            manifest = dict(cfg.get(sync.MANIFEST_KEY) or {})

            meta, current, walk_errors = {}, [], []
            unreadable, listed_any = [], False          # folders that failed to list / any that did

            for mp in sync.resolve_mappings(ing):
                cid, pid = mp.get("connector_id"), mp.get("pipeline_id")
                conn = None
                if cid:
                    try:
                        conn = await db.get(Connector, uuid.UUID(str(cid)))
                    except (ValueError, TypeError):
                        conn = await db.get(Connector, uuid.uuid5(ing.workspace_id, str(cid)))
                if not conn:
                    walk_errors.append(f"connector {cid}: not found")
                    continue

                auto = sync.mapping_kind(mp.get("kind")) == sync.KIND_AUTO
                # saved row OR system default (virtual, under a sentinel id) — one resolver for both
                pl = await resolve_pipeline(db, ing.workspace_id, pid) if pid and not auto else None

                pipeline = list(pl.stages) if (pl and getattr(pl, "stages", None)) else None
                if pid and not auto and not pipeline:
                    walk_errors.append(
                        f"pipeline {pid}: not found or has no stages — skipped (refusing to run the "
                        f"default extraction plan for a mapping that specified a pipeline)")
                    continue
                # Auto-detect: each file's kind comes from its format, and each kind has its own pipeline.
                by_kind = {}
                for k, kpid in ((mp.get("pipelines") or {}).items() if auto else ()):
                    kpl = await resolve_pipeline(db, ing.workspace_id, kpid)
                    by_kind[k] = (kpid, list(kpl.stages) if (kpl and getattr(kpl, "stages", None)) else None)
                    if not by_kind[k][1]:
                        walk_errors.append(f"pipeline {kpid} for {k} files: not found or has no stages "
                                           f"— those files are skipped")
                # per-mapping kind: explicit → the mapping's pipeline kind → the ingestion's kind
                m_kind = None if auto else sync.normalize_kind(
                    mp.get("kind") or (pl.kind if pl else None) or ing_kind)
                exts = ([e for e in (mp.get("formats") or []) if e]
                        or (sync.auto_exts() if auto else sync.default_exts(m_kind)))
                storage_cfg, dirs = await _resolve_storage_dirs(db, conn, mp.get("directories") or [])
                if (storage_cfg.get("kind") or "local").lower() == "local":
                    try:
                        await rc.load()
                    except Exception:
                        pass
                    if not rc.allow_local_files():
                        raise RuntimeError("Local file access is disabled (Settings → Local files).")
                    dirs = [datalake.translate_local_path(d) for d in dirs]
                for d in (dirs or [""]):
                    try:
                        listed = await anyio.to_thread.run_sync(
                            lambda dd=d, sc=storage_cfg, ex=exts: datalake.stat_files(sc, dd, ex))
                    except Exception as e:
                        walk_errors.append(f"{d or '(connector dir)'}: {e}")
                        unreadable.append(f"{cid}|{pid}|{(d or '').rstrip('/')}")
                        continue
                    listed_any = True
                    for name, path, sig in listed:
                        key = f"{cid}|{pid}|{path}"
                        if key in meta:
                            continue
                        f_kind, f_pid, f_pipeline = m_kind, pid, pipeline
                        if auto:
                            f_kind = sync.file_kind(name)
                            f_pid, f_pipeline = by_kind.get(f_kind, (None, None))
                            if f_pid and not f_pipeline:
                                continue
                        meta[key] = {"name": name, "path": path, "storage_cfg": storage_cfg,
                                     "pipeline": f_pipeline, "pipeline_id": f_pid, "kind": f_kind,
                                     # the folder the user asked to watch — the directory mirror is
                                     # drawn relative to THIS, not to the container's mount point
                                     "root": d,
                                     "dir_nodes": bool(mp.get("directory_nodes")
                                                       if mp.get("directory_nodes") is not None
                                                       else cfg.get("directory_nodes"))}
                        current.append((name, key, sig))

        if unreadable and not listed_any:
            # Nothing could be read at all: a failed run with the reason (which hibernates the ingestion
            # and retries it after the backoff), never a "completed" pass over zero files.
            raise FileNotFoundError("; ".join(walk_errors))
        # diff current listing against the stored manifest (keys namespaced by connector)
        plan = sync.compute_plan(manifest, current)
        spared = sync.spare_unreadable(plan, unreadable)
        todo = plan["added"] + plan["changed"]
        rep = _reporter(run_id)
        if rep:
            await rep.start(max(1, len(todo) + len(plan["removed"])))
        done = {"added": 0, "changed": 0, "removed": 0, "unchanged": len(plan["unchanged"]),
                "errors": list(walk_errors)}
        new_manifest = {}
        for it in plan["unchanged"]:                       # carry unchanged entries forward untouched
            new_manifest[it["path"]] = manifest.get(it["path"]) or {"sig": it["sig"]}
        for it in spared:                                  # under an unreadable folder: kept, not removed
            new_manifest[it["path"]] = manifest[it["path"]]

        step = 0
        for it in todo:                                    # ingest added + changed
            step += 1
            key = it["path"]                               # compute_plan's "path" is our namespaced key
            m = meta.get(key) or {}
            if rep and await rep.is_canceled():
                break
            if rep:
                await rep.step(step, m.get("name") or key, "running")
            try:
                if it.get("source_id"):                    # changed → retire the old version first
                    await sync.soft_remove_source(oid, it["source_id"])
                data = await anyio.to_thread.run_sync(
                    lambda p=m["path"], sc=m["storage_cfg"]: datalake.read_file(sc, p))
                sid = await _ingest_one_file(oid, m["kind"], data, m["name"],
                                             m.get("pipeline"), m.get("pipeline_id"))
                if cfg.get("anchors"):
                    _ar = {"source_id": sid}
                    await _apply_anchors(str(oid), _ar, cfg["anchors"], m.get("path") or key)
                    _roll_up_anchors(_ar, done)
                if m.get("dir_nodes") and sid:
                    try:
                        from opencortyx.ingest.sources import directory as _dir
                        _raw = str(m.get("path") or key or "").replace("\\", "/")
                        _rel = _raw
                        for _base in ((m.get("root") or ""),):
                            _b = str(_base or "").replace("\\", "/").rstrip("/")
                            if _b and _raw.startswith(_b + "/"):
                                _rel = _raw[len(_b) + 1:]
                                break
                        _dres = await _dir.link_asset_path(oid, source_id=sid, path=_rel)
                        done["directories"] = done.get("directories", 0) + _dres["directories"]
                    except Exception as _de:
                        done["errors"].append(f"directory tree for {m.get('name') or key}: {_de}")
                new_manifest[key] = {"sig": it["sig"], "source_id": sid,
                                     "ingested_at": datetime.now(timezone.utc).isoformat()}
                done["changed" if it.get("source_id") else "added"] += 1
                if rep:
                    await rep.step(step, m.get("name") or key, "completed")
            except Exception as e:
                if key in manifest:                        # keep prior entry → retried next run, not lost
                    new_manifest[key] = manifest[key]
                done["errors"].append(f"{m.get('name') or key}: {e}")
                if rep:
                    await rep.step(step, m.get("name") or key, "failed")

        for it in plan["removed"]:                         # soft-remove vanished files
            step += 1
            if rep:
                await rep.step(step, it["name"], "running")
            try:
                if it.get("source_id"):
                    await sync.soft_remove_source(oid, it["source_id"])
                done["removed"] += 1
                if rep:
                    await rep.step(step, it["name"], "completed")
            except Exception as e:
                new_manifest[it["path"]] = manifest.get(it["path"])
                done["errors"].append(f"remove {it['name']}: {e}")
                if rep:
                    await rep.step(step, it["name"], "failed")

        # persist the new manifest + clear any hibernation (we completed a pass)
        async with V2Session() as db:
            ing = await db.get(Ingestion, uuid.UUID(ingestion_id))
            if ing:
                nc = dict(ing.config or {})
                nc[sync.MANIFEST_KEY] = new_manifest
                nc.pop("hibernated", None)
                nc.pop("hibernated_at", None)
                ing.config = nc
                ing.status = "completed"
                ing.last_run_at = datetime.now(timezone.utc)
                if run_id:
                    ing.last_run_id = str(run_id)
                await db.commit()
        if rep:
            await rep.finish("completed", result=done)
        return done

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        _hibernate_ingestion(workspace_id, ingestion_id, exc, run_id)
        raise self.retry(exc=exc, countdown=30, max_retries=1)


def _hibernate_ingestion(workspace_id: str, ingestion_id: str, exc, run_id=None):
    """Record a failure, close the run and put the ingestion to sleep (status=paused,
    config.hibernated=True).

    The run is closed here because this is the only place that sees the failure: a task that raises
    after its run row was created would otherwise leave that row saying `queued` until the stale-run
    reaper notices, and the Progress tab would show work that is not happening. Runs in its own
    short-lived loop so it works from a task's except-block, and never raises."""
    async def _do():
        from datetime import datetime, timezone
        async with V2Session() as db:
            ing = await db.get(Ingestion, uuid.UUID(ingestion_id))
            if not ing:
                return
            nc = sync.record_failure(dict(ing.config or {}), exc, run_id)
            nc["hibernated"] = True
            nc["hibernated_at"] = datetime.now(timezone.utc).isoformat()
            ing.config = nc
            ing.status = "paused"
            await db.commit()
        if run_id:
            await _reporter(str(run_id)).finish("failed", error=exc)
    try:
        async_to_sync(_do)()
    except Exception as e:                                 # noqa: BLE001 — recovery must not mask the cause
        log.warning("ingestion.hibernate_bookkeeping_failed", ingestion_id=str(ingestion_id), error=str(e))


@celery_app.task(queue="graphrag", name="opencortyx.sync_tick")
def v2_sync_tick():
    """Celery-beat heartbeat (all three kinds). Each tick finds ingestions that are DUE and dispatches
    the right job. Due =
      • realtime → every tick
      • scheduled → its cron matches this minute
      • HIBERNATED (auto-paused by a failure) + not manually paused → retry after a backoff (self-heal).
    Manually paused ingestions (config.paused) and on-demand ones are never auto-run. Already-running
    ingestions are skipped so ticks don't pile up. File kinds → change-detection sync; structured →
    an additive re-run (idempotent upsert) of its table selections."""
    async def _run():
        from datetime import datetime, timezone
        from sqlalchemy import select
        from opencortyx.core.config import settings
        from opencortyx.ingest.runs.progress import create_run
        await init_v2_schema()
        now = datetime.now(timezone.utc)
        backoff = float(getattr(settings, "SYNC_HIBERNATE_BACKOFF_SECONDS", 120) or 120)
        dispatched = []
        async with V2Session() as db:
            rows = (await db.execute(select(Ingestion))).scalars().all()
            for ing in rows:
                cfg = ing.config or {}
                if cfg.get("paused"):                      # user paused → never auto-run
                    continue
                if (ing.status or "") == "running":
                    continue
                mode = sync.normalize_mode(cfg.get("mode"))
                if mode == sync.MODE_ON_DEMAND:
                    continue
                cron_hit = mode == sync.MODE_SCHEDULED and sync.cron_due(ing.schedule, now)
                if cron_hit and ing.last_run_at is not None:
                    last = ing.last_run_at
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if last.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0):
                        cron_hit = False
                due = (mode == sync.MODE_REALTIME) or cron_hit
                if cfg.get("hibernated"):                  # self-heal: retried only after the backoff,
                    due = sync.hibernation_allows(cfg, now, backoff)   # whatever the schedule says
                if not due:
                    continue
                kind = sync.normalize_kind(ing.kind)
                rid = await create_run(ing.workspace_id, kind, f"{ing.name} · sync")
                ing.status = "running"
                ing.last_run_id = str(rid) if rid else None
                ing.last_run_at = now          # stamps the slot this fired in (per-minute cron guard)
                await db.commit()
                # File-scan (incl. structured watch-folders) → change-detection sync; a structured DB
                # connector (no folders) → additive re-introspect.
                if sync.is_file_sync(ing):
                    v2_sync_ingestion.delay(str(ing.workspace_id), str(ing.id), str(rid) if rid else None)
                else:
                    await _dispatch_structured_rerun(db, ing, rid)
                dispatched.append(str(ing.id))
        return {"dispatched": dispatched, "at": now.isoformat()}
    return async_to_sync(_run)()


async def _dispatch_structured_rerun(db, ing, run_id):
    """Scheduled/realtime STRUCTURED sync = an additive re-run of the ingestion's table selections
    (introspect + upsert; prune=False). Structured upserts are idempotent, so re-running keeps the
    catalogue current without a file manifest. Fans out over every connector→pipeline mapping."""
    import copy
    cfg = ing.config or {}
    enrich = bool(cfg.get("enrich", True))
    sels = cfg.get("selections") or [{"schema": None, "tables": [], "views": []}]
    jobs = []
    for mp in sync.resolve_mappings(ing):
        cid, pid = mp.get("connector_id"), mp.get("pipeline_id")
        if not cid:
            continue
        pl = await resolve_pipeline(db, ing.workspace_id, pid) if pid else None
        for sel in sels:
            targets = list(sel.get("tables") or []) + list(sel.get("views") or [])
            stages = copy.deepcopy(pl.stages) if (pl and pl.stages) else [
                {"id": "stage_0", "action": "base_schema", "config": {}}]
            for s in stages:
                if isinstance(s, dict) and s.get("action") == "base_schema" and targets:
                    s.setdefault("config", {})["target_tables"] = targets
            jobs.append((sel.get("schema"), stages, str(cid)))
    # Every job carries the ingestion id so whichever finishes last releases the "running" flag — without
    # it the ingestion stays running forever and the schedule fires exactly once (see _close_ingestion).
    for schema, stages, cid in jobs:
        v2_ingest_sql.delay(str(ing.workspace_id), None, schema, None, enrich, stages,
                            str(run_id) if run_id else None, cid, False, str(ing.id))
    if not jobs:
        # Nothing dispatchable (no usable connector) — release it now, otherwise this ingestion would
        # sit in "running" and never be considered again.
        await _close_ingestion(str(ing.id), "failed", run_id)
