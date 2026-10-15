"""Ingestion progress tracking.

One `IngestRun` row per asset (a file or a SQL source). The DAG executors thread a
`ProgressReporter` through and call `.start()/.step()/.finish()` as they go, so the Progress tab
sees live status, percentage, and per-stage detail. Cancellation is cooperative: deleting a
queued/running asset flips its status to `canceled`; the executor checks `.is_canceled()` between
stages and stops, then the asset's nodes (incl. ghost/derived nodes) are purged.

Every write here is BEST-EFFORT — progress bookkeeping must never raise into the ingestion path.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from opencortyx.core.db import V2Session
from opencortyx.domain import IngestRun, Ingestion

log = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


async def reconcile_orphans(stale_minutes: int = 30) -> int:
    """Called once on API startup. A run still `running`/`queued` with no progress for a while is an
    orphan from a prior crash/restart (its Celery task is gone) — mark it `failed` so the Progress tab
    reflects reality instead of showing perpetual ghosts. Healthy runs bump `updated_at` every step,
    so a stale timestamp means the task is truly dead. Best-effort; never raises."""
    try:
        cutoff = _now() - timedelta(minutes=stale_minutes)
        async with V2Session() as db:
            res = await db.execute(
                update(IngestRun)
                .where(IngestRun.status.in_(["running", "queued"]), IngestRun.updated_at < cutoff)
                .values(status="failed", error="interrupted (service restart)", finished_at=_now()))
            await db.commit()
            return res.rowcount or 0
    except Exception:
        return 0


def _self_dispatch() -> dict | None:
    """How to run THIS work again, read from the Celery task currently executing.

    Taken from the running task rather than passed in by each caller on purpose: every ingestion path
    — unstructured, semi-structured, structured, every extractor, anything added later — already runs
    inside a task that knows its own name and arguments. Reading it here means recovery works for all
    of them without a single call site having to remember to opt in, which is the only version of this
    that stays true as the codebase grows.

    None when there is no task (a run created directly from the API before dispatch); such a run simply
    cannot be auto-resumed and is reported instead."""
    try:
        from celery import current_task
        t = current_task
        if not t or not getattr(t, "request", None) or not t.request.id:
            return None
        return {"task": t.name,
                "args": list(t.request.args or []),
                "kwargs": dict(t.request.kwargs or {})}
    except Exception:                              # noqa: BLE001 — never block run creation
        return None


async def create_run(workspace_id, kind, asset, task_id=None, source_ref=None):
    """Create a `queued` run row; returns its id (uuid) or None on failure (never raises)."""
    try:
        async with V2Session() as db:
            r = IngestRun(workspace_id=workspace_id, kind=kind, asset=(asset or "")[:500],
                          task_id=task_id, source_ref=(str(source_ref)[:300] if source_ref else None),
                          status="queued", steps=[], result={},
                          retry_payload=_self_dispatch())
            db.add(r)
            await db.commit()
            await db.refresh(r)
            return r.id
    except Exception:
        return None


class ProgressReporter:
    """Threaded through the executors. The run id may be None (then everything is a no-op), which
    keeps non-UI / raw-API ingestion paths working unchanged."""

    def __init__(self, run_id=None):
        self.run_id = run_id
        self._steps = []          # [{i, name, status, count}]
        self._total = 1

    async def start(self, total_steps):
        self._total = max(1, int(total_steps or 1))
        await self._patch(status="running", total_steps=self._total, current_step=0,
                          percent=2, started_at=_now())

    async def resume(self):
        """Continue a run another task started: pick up its steps and step total, so the steps this
        task reports are added beside them instead of replacing them."""
        if not self.run_id:
            return
        try:
            async with V2Session() as db:
                r = await db.get(IngestRun, uuid.UUID(str(self.run_id)))
                if r is not None:
                    self._steps = list(r.steps or [])
                    self._total = max(1, int(r.total_steps or 1))
        except Exception:                           # noqa: BLE001
            pass

    async def set_source(self, source_ref):
        """Record the asset's stable id once known (source_id for docs / source_key for structured)."""
        if source_ref:
            await self._patch(source_ref=str(source_ref)[:300])

    async def step(self, index, name, status="running", count=None):
        """Upsert a step by index; recompute percent from completed steps / total."""
        self._steps = [s for s in self._steps if s.get("i") != index]
        self._steps.append({"i": index, "name": str(name)[:120], "status": status,
                            "count": (int(count) if count is not None else None)})
        ordered = sorted(self._steps, key=lambda x: x.get("i", 0))
        done = sum(1 for s in ordered if s.get("status") in ("completed", "failed", "skipped"))
        pct = min(98, max(2, int(done / max(1, self._total) * 100)))
        await self._patch(steps=ordered, current_step=done, percent=pct)

    async def finish(self, status="completed", result=None, error=None):
        res = result if isinstance(result, dict) else {}
        if status == "completed" and (res.get("duplicate") is True
                                      or res.get("status") in ("unchanged", "duplicate")):
            # A path that found identical content already ingested says so in its own result
            # ("unchanged" for documents); the run says it in one word for every path, so the outcome
            # offers "open what is already there" instead of "tell it what to read".
            status = "duplicate"
        if status == "duplicate":
            await self.set_source(res.get("source_id") or res.get("source_key"))
            if await self._written_by_this_run(res.get("source_id")):
                # The "existing" asset is this run's own: an earlier attempt of the same run wrote it
                # and was killed (an OOM'd worker child) before it could report, and the broker
                # re-delivered the task. A completed ingest, not a duplicate of something already there.
                status = "completed"
        fields = {"status": status, "result": (result or {}), "finished_at": _now()}
        if status in ("completed", "duplicate"):
            fields["percent"] = 100
        if error is not None:
            fields["error"] = str(error)[:2000]
        await self._patch(**fields)
        await settle_upload_ingestion(self.run_id)
        if status == "completed":
            await self._index_new_attributes()

    async def _written_by_this_run(self, source_id) -> bool:
        """Whether the asset `source_id` names was first written after this run was created."""
        if not (self.run_id and source_id):
            return False
        try:
            from opencortyx.core.substrate import NodeRepo
            async with V2Session() as db:
                run = await db.get(IngestRun, self.run_id)
            if run is None or run.created_at is None:
                return False
            node = await NodeRepo().get(run.workspace_id, uuid.UUID(str(source_id)))
            return bool(node is not None and node.created_at and node.created_at >= run.created_at)
        except Exception:                           # noqa: BLE001 — undecidable → keep what the path said
            return False

    async def _index_new_attributes(self):
        """Give the nodes this run created their per-attribute embedding surfaces.

        Fired from `finish` because EVERY ingest path reports through this reporter - document,
        JSON, structured, semi-structured, connector, lake. Wiring it into one ingest task would
        have left the other five silently unindexed, which is the shape of the bug this fixes:
        `node_attr_embedding` had exactly one writer, a manual CLI, so anything ingested through
        the UI had the `attr_embedding` surface empty and simply retrieved worse.

        SCOPED TO THIS RUN by `started_at`, not to the whole org. Org-wide it would have queued
        20,000 nodes of embedding work on EVERY completion - measured 398,549 unindexed nodes on
        this deployment - onto the same queue the ingest itself depends on, so a one-file upload
        paid for the entire backlog. Whole-graph indexing stays available as an explicit
        `backfill=True` call.

        Dispatched as a task rather than awaited, so embedding cost never extends the ingest.
        Best-effort by design: a failure here must not turn a COMPLETED ingest into a failed one -
        the nodes are written and correct, they are only missing an index that the same idempotent
        task will fill on the next run.
        """
        if not self.run_id:
            return
        try:
            async with V2Session() as db:
                run = await db.get(IngestRun, self.run_id)
            workspace_id = getattr(run, "workspace_id", None) if run else None
            if not workspace_id:
                return
            since = getattr(run, "started_at", None) or getattr(run, "created_at", None)
            if not since:
                log.warning("attribute indexing skipped: run %s has no start time", self.run_id)
                return
            from opencortyx.tasks.graph import v2_index_node_attrs
            v2_index_node_attrs.delay(str(workspace_id), since_iso=since.isoformat())
        except Exception as exc:                      # never fail a completed ingest
            log.warning("attribute indexing not dispatched: %s: %s", type(exc).__name__, exc)

    async def is_canceled(self) -> bool:
        if not self.run_id:
            return False
        try:
            async with V2Session() as db:
                r = await db.get(IngestRun, self.run_id)
                return bool(r and r.status == "canceled")
        except Exception:
            return False

    async def _patch(self, **fields):
        if not self.run_id:
            return
        try:
            async with V2Session() as db:
                # Locked: the deferred half of the same run writes `result` from another task
                # (`mark_deferred_extraction`). Unlocked, each read-modify-write could overwrite the
                # other's — a run lost the reader it was read with, or showed "still extracting"
                # after extraction had finished.
                r = await db.get(IngestRun, self.run_id, with_for_update=True)
                if not r:
                    return
                if r.status == "canceled":
                    return          # a cancel always wins — never resurrect a canceled run
                for k, v in fields.items():
                    if k == "result":
                        v = _keep_deferred_progress(r.result, v)
                    setattr(r, k, v)
                await db.commit()
        except Exception:
            pass


#: The step a deferred extraction reports on the run that queued it.
EXTRACTION_STEP = "Entity & claim extraction"


def _keep_deferred_progress(old, new):
    """The run's final ingest result, keeping what its deferred half already reported.

    The ingest half queues the deferred half and THEN records its result. A small document's
    deferred half can finish in between; replacing the whole result would then put back
    `extraction: deferred` and drop the counts it wrote."""
    old, new = dict(old or {}), dict(new or {})
    if new.get("extraction") != "deferred" or old.get("extraction") not in ("running", "done", "failed"):
        return new
    return {**new, **{k: v for k, v in old.items() if k not in new}, "extraction": old["extraction"]}


async def mark_deferred_extraction(run_id, state: str, *, counts: dict | None = None,
                                   error: str | None = None,
                                   step: str | None = EXTRACTION_STEP) -> None:
    """Record the deferred half of a document ingest on the run that queued it.

    `state` is "running" | "done" | "failed". The run itself stays `completed` — the document is
    searchable the moment its chunks land, which is the point of deferring — but `result.extraction`
    and a step say what is still happening, so the outcome can say "still extracting" instead of
    telling the user their file produced nothing. Best-effort, like every write here."""
    if not run_id:
        return
    try:
        async with V2Session() as db:
            r = await db.get(IngestRun, uuid.UUID(str(run_id)), with_for_update=True)  # see _patch
            if r is None or r.status == "canceled":
                return
            result = dict(r.result or {})
            result["extraction"] = {"running": "deferred"}.get(state, state)
            if counts:
                result.update({k: v for k, v in counts.items() if isinstance(v, (int, str, bool))})
            if error:
                result["extraction_error"] = str(error)[:500]
            steps = list(r.steps or [])
            if step:        # a pipeline's deferred stages report their own steps; they pass None
                steps = [s for s in steps if s.get("name") != step]
                step_status = {"running": "running", "done": "completed", "failed": "failed"}[state]
                total = int((counts or {}).get("graph") or 0) or None
                steps.append({"i": len(steps) + 1, "name": step, "status": step_status,
                              "count": total if state == "done" else None})
            r.result, r.steps = result, steps
            r.total_steps = max(int(r.total_steps or 0), len(steps))
            r.current_step = sum(1 for st in steps if st.get("status") in ("completed", "failed", "skipped"))
            await db.commit()
    except Exception as e:                          # noqa: BLE001
        log.warning("progress.mark_extraction_failed run=%s error=%s", run_id, e)


ONE_TIME = "one_time"          # ingestion mode for a device upload — ran once, cannot be re-run


async def record_device_upload(workspace_id, kind, filename, run_id=None, batch_id=None,
                               pipeline_id=None) -> None:
    """Register a ONE-TIME device upload as a saved Ingestion.

    A connector-based ingestion is a re-runnable job, so it gets an `Ingestion` row the moment it is
    saved. An upload from the user's device had no such record at all, which is why the Ingestion page
    could say "No ingestions yet" right after a successful upload. Now every upload lands here too —
    it just can't be re-run (the file lives on the user's machine, not on the server).

    Files uploaded together share a `batch_id` and collapse into ONE row that accumulates their names
    and run ids, so a 40-file drop is one ingestion with 40 runs, not 40 list entries. Without a
    batch_id the run id is used, i.e. one row per file. Best-effort — never raises into the upload."""
    from opencortyx.ingest.sources import sync
    key = str(batch_id or run_id or uuid.uuid4())
    fname = (filename or "upload")[:300]
    try:
        async with V2Session() as db:
            row = (await db.execute(select(Ingestion).where(
                Ingestion.workspace_id == workspace_id,
                Ingestion.config["batch_id"].astext == key))).scalars().first()
            now = _now()
            if row is None:
                cfg = {"mode": ONE_TIME, "source": "device_upload", "batch_id": key,
                       "files": [fname], "runs": ([str(run_id)] if run_id else [])}
                db.add(Ingestion(
                    workspace_id=workspace_id, name=fname, kind=sync.normalize_kind(kind),
                    connector_id=None,
                    pipeline_id=(uuid.UUID(str(pipeline_id)) if pipeline_id else None),
                    config=cfg, status="running",
                    last_run_id=(str(run_id) if run_id else None), last_run_at=now))
            else:
                cfg = dict(row.config or {})
                files = list(cfg.get("files") or [])
                runs = list(cfg.get("runs") or [])
                if fname not in files:
                    files.append(fname)
                if run_id and str(run_id) not in runs:
                    runs.append(str(run_id))
                cfg.update({"files": files, "runs": runs})
                row.config = cfg               # reassign — JSONB needs a new object to be flushed
                row.status = "running"
                if run_id:
                    row.last_run_id = str(run_id)
                row.last_run_at = now
            await db.commit()
    except Exception:
        pass


async def purge_asset(workspace_id, kind, source_ref) -> int:
    """Delete the nodes produced for one asset (used when a queued/running asset is deleted).

    Documents → the Source, its chunks and everything derived from them.
    Semi-structured → the Source and its whole walked tree, at any depth.
    Structured → the source's tables and attributes and the business nodes derived from them.
    Returns how many nodes were deleted. Best-effort.
    """
    if not source_ref:
        return 0
    from opencortyx.core.substrate import NodeRepo
    repo = NodeRepo()
    try:
        if kind == "semistructured":
            sid = uuid.UUID(str(source_ref))
            tree = await repo.descendants(workspace_id, [sid], max_depth=64)
            return await repo.delete_nodes(workspace_id, [sid] + [n.id for n in tree])
        if kind == "document":
            sid = uuid.UUID(str(source_ref))
            from opencortyx.ingest.unstructured.ingest import _purge_document
            await _purge_document(workspace_id, sid)
            return await repo.delete_nodes(workspace_id, [sid])
        base = {str(n.id) for n in await repo.find(workspace_id, source_key=str(source_ref))}
        if not base:
            return 0
        derived = [n.id for n in await repo.find(workspace_id)
                   if any(str((n.properties or {}).get(k) or "") in base
                          for k in ("parent_table_id", "affected_table_id", "target_table_id", "table_id"))]
        return await repo.delete_nodes(workspace_id, list(base) + derived)
    except Exception:
        return 0


_OPEN_RUN = ("queued", "running")


async def settle_upload_ingestion(run_id) -> None:
    """Close the ONE-TIME upload ingestion this run belongs to, once none of its runs is still going.

    An upload's ingestion was created "running" and nothing ever wrote it back: `_close_ingestion`
    only serves scheduled jobs. The Ingestion page hid that by preferring the run's own status —
    until the run was gone (its asset deleted), and the ingestion then read RUNNING for good.
    Best-effort: bookkeeping never fails the run it describes."""
    if not run_id:
        return
    try:
        async with V2Session() as db:
            ing = (await db.execute(select(Ingestion).where(
                Ingestion.config["source"].astext == "device_upload",
                Ingestion.config["runs"].contains([str(run_id)])))).scalars().first()
            if ing is None:
                return
            ids = [uuid.UUID(r) for r in (ing.config or {}).get("runs") or []]
            states = [s for (s,) in (await db.execute(
                select(IngestRun.status).where(IngestRun.id.in_(ids)))).all()]
            if any(s in _OPEN_RUN for s in states):
                return
            ing.status = "failed" if "failed" in states else "completed"
            await db.commit()
    except Exception as e:                          # noqa: BLE001
        log.warning("progress.settle_upload_failed run=%s error=%s", run_id, e)


async def forget_upload_runs(workspace_id, run_ids) -> None:
    """Drop deleted runs from the upload ingestions that list them; an upload with no runs left is
    removed — its file's asset is gone, so there is nothing left for the row to describe."""
    gone = {str(r) for r in run_ids or []}
    if not gone:
        return
    try:
        async with V2Session() as db:
            rows = (await db.execute(select(Ingestion).where(
                Ingestion.workspace_id == workspace_id,
                Ingestion.config["source"].astext == "device_upload"))).scalars().all()
            for ing in rows:
                cfg = dict(ing.config or {})
                runs = [r for r in cfg.get("runs") or [] if r not in gone]
                if len(runs) == len(cfg.get("runs") or []):
                    continue
                if not runs:
                    await db.delete(ing)
                    continue
                cfg["runs"] = runs
                ing.config = cfg                     # reassign — JSONB needs a new object to be flushed
                if str(ing.last_run_id or "") in gone:
                    ing.last_run_id = runs[-1]
            await db.commit()
    except Exception as e:                          # noqa: BLE001
        log.warning("progress.forget_upload_runs_failed error=%s", e)


async def reconcile_upload_ingestions() -> int:
    """Repair upload ingestions left RUNNING: settle the ones whose runs all finished, remove the ones
    whose runs no longer exist. The same two rules as `settle_upload_ingestion` / `forget_upload_runs`,
    applied to rows written before those existed. Returns how many rows changed."""
    changed = 0
    try:
        async with V2Session() as db:
            rows = (await db.execute(select(Ingestion).where(
                Ingestion.status == "running",
                Ingestion.config["source"].astext == "device_upload"))).scalars().all()
            for ing in rows:
                ids = [uuid.UUID(r) for r in (ing.config or {}).get("runs") or []]
                states = [st for (st,) in (await db.execute(
                    select(IngestRun.status).where(IngestRun.id.in_(ids)))).all()] if ids else []
                if not states:
                    await db.delete(ing)
                elif not any(st in _OPEN_RUN for st in states):
                    ing.status = "failed" if "failed" in states else "completed"
                else:
                    continue
                changed += 1
            await db.commit()
    except Exception as e:                          # noqa: BLE001
        log.warning("progress.reconcile_uploads_failed error=%s", e)
    return changed
