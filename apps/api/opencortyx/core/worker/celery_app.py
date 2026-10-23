"""v2 Celery app — self-contained (no v1). Task modules: the `opencortyx.tasks` package."""
import random
from typing import Any

from celery import Celery, Task
from celery.exceptions import Ignore, Retry
from celery.signals import task_postrun, worker_process_init, worker_process_shutdown

from opencortyx.core.config import settings


@task_postrun.connect
def _flush_write_accounting(**kwargs):
    """Fold this process's write counters into the shared totals when a task ends.

    The counters live in the process that did the writing, and the workers that actually write the
    graph (ingest, extraction) never run the maintenance tasks — so without this their numbers would
    sit in memory and the measured `unaccounted` would only ever reflect whichever process happened to
    be asked. Once per task is nothing next to the work a task does, and the guard makes an idle task
    free."""
    try:
        from opencortyx.core.substrate import accounting
        if not any(any(v.values()) for v in accounting._counts.values()):
            return
        from opencortyx.core.worker.utils import async_to_sync
        async_to_sync(accounting.flush)()
    except Exception:                       # noqa: BLE001 — bookkeeping must never fail a task
        pass


@worker_process_shutdown.connect
def _release_graph_sessions(**_kw):
    """Sign this pool child out of the graph before it exits. A child recycled after
    --max-tasks-per-child / --max-memory-per-child otherwise leaves its whole session pool open on
    graphd, and enough recycles exhaust the per-client session limit for the entire container."""
    try:
        from opencortyx.stores.graph import factory
        store = factory._store
        reset = getattr(store, "_reset", None)
        if callable(reset):
            reset()
    except Exception:                       # noqa: BLE001 — shutting down must never raise
        pass


@worker_process_init.connect
def _setup_worker(sender=None, conf=None, **kwargs):
    try:
        import nest_asyncio
        nest_asyncio.apply()
    except Exception:
        pass


_TRANSIENT = ("deadlock", "serialization failure", "could not serialize",
              "lock timeout", "lock_timeout", "connection was closed",
              "server closed the connection", "terminating connection")


def _is_transient(exc: BaseException) -> bool:
    return any(s in str(exc).lower() for s in _TRANSIENT)


class ResilientTask(Task):
    """Base task that retries TRANSIENT database contention instead of losing the work.

    Every task in this app writes to the same graph, and users can define their own extractors,
    curations and ingestions on top of it — so we can never enumerate in advance which pairs of rows
    two concurrent writers will touch, nor in which order. Lock cycles are therefore a permanent
    property of the system, not a bug to be designed away in one place, and the only thing that
    generalises is making them survivable everywhere by default.

    Contention is genuinely transient: Postgres kills exactly one participant so the others commit and
    release, which means the victim's work usually succeeds unchanged on a second attempt. Retrying it
    is correct, not a papering-over. Backoff is jittered because victims that retry in lockstep simply
    re-collide.

    Only transient errors are retried — a bug or bad data still fails immediately and loudly, and the
    three tasks that hand-rolled their own retry keep it (their `Retry` passes straight through)."""
    max_db_retries = 3

    def __call__(self, *args, **kwargs):
        lost = _attempt_started(self)
        if lost >= MAX_LOST_ATTEMPTS:
            _give_up(self, args, kwargs, lost)
            raise Ignore()                          # acknowledged: the broker stops re-delivering it
        try:
            return super().__call__(*args, **kwargs)
        except (Retry, Ignore):
            raise                                   # a task that already decided its own fate
        except Exception as exc:
            done = (self.request.retries or 0) if self.request else 0
            if _is_transient(exc) and done < self.max_db_retries:
                delay = round(2 ** done + random.random() * 2, 2)
                from opencortyx.core.observability import get_logger
                get_logger(__name__).warning(
                    "task.transient_contention_retry", task=self.name,
                    attempt=done + 1, max_attempts=self.max_db_retries, retry_in_s=delay,
                    error=str(exc).splitlines()[0][:120])
                raise self.retry(exc=exc, countdown=delay, max_retries=self.max_db_retries)
            raise
        finally:
            _attempt_finished(self)


#: Attempts of one task that may die mid-run (the worker process SIGKILLed — in practice by the
#: kernel for memory) before it is given up. With acks_late + reject_on_worker_lost such a task is
#: re-queued at once, forever: one PDF too large for the worker was re-run 1,100 times in two hours
#: and every upload behind it sat "waiting for a worker".
MAX_LOST_ATTEMPTS = 2
_ATTEMPT_KEY = "opencortyx:task-attempts:{}"


def _attempts_store():
    import redis
    from opencortyx.core.config import settings
    return redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=2)


def _attempt_started(task) -> int:
    """How many earlier attempts of THIS task died mid-run. An attempt marks itself on start and
    clears the mark when it ends however it ends — success, failure, or a scheduled retry — so a
    mark already present means the attempt before never reached its end."""
    task_id = getattr(getattr(task, "request", None), "id", None)
    if not task_id:
        return 0
    try:
        r = _attempts_store()
        key = _ATTEMPT_KEY.format(task_id)
        n = r.incr(key)
        r.expire(key, 86400)
        return max(0, int(n) - 1)
    except Exception:                               # noqa: BLE001 — never block a task on this
        return 0


def _attempt_finished(task) -> None:
    task_id = getattr(getattr(task, "request", None), "id", None)
    if task_id:
        try:
            _attempts_store().delete(_ATTEMPT_KEY.format(task_id))
        except Exception:                           # noqa: BLE001
            pass


#: Called with (run_id, reason) when a task is given up on. The layer that owns ingestion runs
#: registers here (`opencortyx.tasks`), so core never imports it.
GIVE_UP_HOOKS: list = []


def _give_up(task, args, kwargs, lost: int) -> None:
    """Fail the run this task was doing, with the reason, instead of starting it once more."""
    import inspect
    reason = (f"stopped after {lost} attempts: the worker process was killed each time, which "
              f"almost always means the file needs more memory than the worker has. Raise the "
              f"worker's memory (compose.yaml mem_limit) or split the file.")
    try:
        from opencortyx.core import observability as _obs
        _obs.get_logger().error("task.given_up_after_lost_attempts", task=task.name,
                                task_id=task.request.id, attempts=lost)
    except Exception:                               # noqa: BLE001
        pass
    try:
        run_id = inspect.signature(task.run).bind_partial(*args, **kwargs).arguments.get("run_id")
    except (TypeError, ValueError):
        run_id = kwargs.get("run_id")
    if run_id:
        for hook in GIVE_UP_HOOKS:
            try:
                hook(run_id, reason)
            except Exception:                       # noqa: BLE001
                pass
    _attempt_finished(task)


celery_app = Celery(
    "opencortyx_v2",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["opencortyx.tasks.enrich", "opencortyx.tasks.graph", "opencortyx.tasks.graph_tree", "opencortyx.tasks.ingest", "opencortyx.tasks.maintenance", "opencortyx.tasks.resolution", "opencortyx.tasks.sync"],
    task_cls=ResilientTask,          # applies to EVERY task, including ones added later
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_routes={
        # Embedding gets its own queue too: it is fast per item but high-volume, and mixing it into
        # `extraction` would let a slow LLM backlog delay vectors that retrieval is waiting on.
        "opencortyx.embed_source": {"queue": "embedding"},
        # an asset's resolution has its own queue: a long one never holds up embeddings, another asset's
        # resolution or the next ingest (the task itself waits for the asset's embeddings)
        "opencortyx.resolve_asset": {"queue": "resolution"},
        "opencortyx.link_existing": {"queue": "resolution"},
        "opencortyx.extract_document": {"queue": "extraction"},
        "opencortyx.run_document_pipeline": {"queue": "extraction"},   # a pipeline's deferred stages
        "opencortyx.*": {"queue": "graphrag"},
    },
    # Every tick expires after its own interval: one that could not start in time is dropped, and
    # the next tick does the same work. Without it, ticks queued behind a long ingest piled up (49
    # sync ticks behind one large PDF) and then ran back to back for nothing.
    beat_schedule={name: {"task": task, "schedule": every, "options": {"expires": every}}
                   for name, task, every in (
        ("ingestion-sync-tick", "opencortyx.sync_tick",
         float(settings.SYNC_TICK_SECONDS) if getattr(settings, "SYNC_TICK_SECONDS", None) else 30.0),
        ("quarantine-replay-tick", "opencortyx.replay_quarantine",
         float(getattr(settings, "QUARANTINE_REPLAY_SECONDS", 60.0) or 60.0)),
        ("stale-run-reap-tick", "opencortyx.reap_stale_runs", 300.0),
        ("integrity-audit-tick", "opencortyx.audit_integrity",
         float(getattr(settings, "INTEGRITY_AUDIT_SECONDS", 600.0) or 600.0)),
        # Embeds what a lost job left without vectors (tasks/graph.py, v2_sweep_unembedded).
        ("embedding-sweep-tick", "opencortyx.sweep_unembedded", 600.0),
        # Removes stored uploads past their retention (core/uploads.py).
        ("upload-sweep-tick", "opencortyx.sweep_uploads", 3600.0),
    )},
    task_soft_time_limit=settings.TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.TASK_TIME_LIMIT,
    task_track_started=True,

    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Stopping a worker (an update, a restart) must not strand the job it is running. Docker stops
    # the workers with SIGQUIT (compose.yaml), which starts Celery's soft shutdown: running jobs get
    # this many seconds to finish, then are cancelled and their messages put straight back on the
    # queue for the next worker. Before, the worker was killed after Docker's grace period with the
    # job unacknowledged, and Redis only re-delivered it after `visibility_timeout` (40 minutes): an
    # upload's embeddings, interrupted by an update, simply did not exist for that long.
    # Needs the prefork pool: soft shutdown ends by cancelling the running jobs, which a thread cannot
    # be ("does not implement kill_job"), so every worker runs processes.
    worker_soft_shutdown_timeout=20.0,
    worker_enable_soft_shutdown_on_idle=True,

    worker_prefetch_multiplier=1,

    result_expires=3600,

    broker_transport_options={"visibility_timeout": settings.TASK_TIME_LIMIT + 300},
)

_QUEUES = ("graphrag", "extraction", "embedding")
_QUEUE = _QUEUES[0]


def reset_worker(hard: bool = False) -> dict:
    """Un-wedge the worker. Never touches DATA — only task state.

      • soft (default): SIGKILL every task a worker is currently executing — the prefork pool
        replaces the killed children and immediately continues with the QUEUED tasks. This is
        the fix for 'a task hung and nothing moves': queued work survives, only the hung
        executions die.
      • hard: the TOTAL task nuke — first drain every PENDING message from the broker (Redis)
        via purge_task_queue() so nothing can be picked up, then SIGKILL running/reserved/
        scheduled tasks, then broadcast shutdown so the container exits and Docker's
        `restart: unless-stopped` revives it with FRESH code/state and an EMPTY queue.

    Blocking control-channel calls — run via asyncio.to_thread. If the worker is so far gone
    that even its control thread is dead, fall back to `docker compose restart worker`."""
    out: dict[str, Any] = {"terminated": 0, "purged_queued": 0, "shutdown_sent": False, "errors": []}
    if hard:
        # 1) drop ALL pending messages at the broker FIRST — respawned pool children must find
        #    an empty queue, not grab the next task between our kill and the shutdown.
        q = purge_task_queue()
        out["purged_queued"] = q.get("purged_queued", 0)
        out["terminated"] += q.get("revoked", 0)              # reserved/scheduled revoked there
        out["errors"].extend(q.get("errors") or [])
    try:
        insp = celery_app.control.inspect(timeout=2.0)
        act = insp.active() or {}
        ids = {t.get("id") for tasks in act.values() for t in (tasks or []) if t.get("id")}
        for tid in ids:
            celery_app.control.revoke(tid, terminate=True, signal="SIGKILL")
        out["terminated"] += len(ids)
    except Exception as e:                                    # noqa: BLE001
        out["errors"].append(f"terminate: {e}")
    if hard:
        try:
            celery_app.control.shutdown()
            out["shutdown_sent"] = True
        except Exception as e:                                # noqa: BLE001
            out["errors"].append(f"shutdown: {e}")
    return out


def revoke_tasks(task_ids) -> dict:
    """Stop exactly these tasks, queued or running (revoke + terminate). The tenant-scoped counterpart
    to `purge_task_queue`: one tenant's reset must never cancel another tenant's work."""
    ids = [str(t) for t in task_ids if t]
    out: dict[str, Any] = {"revoked": 0, "errors": []}
    if ids:
        try:
            celery_app.control.revoke(ids, terminate=True)
            out["revoked"] = len(ids)
        except Exception as e:                                # noqa: BLE001 — best-effort, like the purge
            out["errors"].append(str(e))
    return out


def purge_task_queue() -> dict:
    """Hard-clear ALL pending + in-flight work — the queue-lifecycle counterpart to a DB reset.

    Two layers, both best-effort:
      • queue_purge on the broker channel — drops every QUEUED message directly at the broker,
        so it works even when NO worker is running (celery_app.control.purge() would not: it
        only broadcasts to live workers).
      • inspect + revoke(terminate=True) — kills tasks a worker already picked up (active),
        prefetched (reserved), or scheduled for later, on every live worker.

    Blocking network calls — run via asyncio.to_thread from async endpoints. Broker-wide (not
    per-org): fine for single-workspace deploys; a shared multi-org broker should gate this
    behind the instance admin."""
    out: dict[str, Any] = {"purged_queued": 0, "revoked": 0, "errors": []}
    try:
        with celery_app.connection_for_write() as conn:
            for q in _QUEUES:
                try:
                    out["purged_queued"] += int(conn.default_channel.queue_purge(q) or 0)
                except Exception as e:                        # noqa: BLE001 — one queue may not
                    out["errors"].append(f"queue purge {q}: {e}")   # exist yet; keep going
    except Exception as e:                                    # noqa: BLE001 — best-effort per layer
        out["errors"].append(f"queue purge: {e}")
    try:
        insp = celery_app.control.inspect(timeout=2.0)
        ids: set = set()
        for getter in ("active", "reserved", "scheduled"):
            try:
                data = getattr(insp, getter)() or {}
            except Exception:
                data = {}
            for tasks in data.values():
                for t in (tasks or []):
                    tid = t.get("id") or (t.get("request") or {}).get("id")
                    if tid:
                        ids.add(tid)
        for tid in ids:
            celery_app.control.revoke(tid, terminate=True, signal="SIGKILL")
        out["revoked"] = len(ids)
    except Exception as e:                                    # noqa: BLE001
        out["errors"].append(f"revoke: {e}")
    return out


from celery import signals as _signals  # noqa: E402

_TASK_T0: dict = {}          # task_id -> start time. Bounded: entries are popped on completion,
_TASK_CTX: dict = {}         # and both dicts are swept below if a task ever leaves one behind.
_TASK_STATE_MAX = 10_000


def _sweep(d: dict) -> None:
    """A signal pair that never fires its second half (worker SIGKILLed mid-task) would otherwise
    leak one entry per task, forever, in a long-lived worker. Bound it."""
    if len(d) > _TASK_STATE_MAX:
        for k in list(d)[: len(d) - _TASK_STATE_MAX // 2]:
            d.pop(k, None)


@_signals.worker_init.connect
def _obs_worker_parent_init(**_kw):
    """Parent process: clear stale mmap files, then serve the aggregate on METRICS_PORT."""
    try:
        from opencortyx.core.observability import (
            configure_logging, reset_multiproc_dir, start_worker_metrics_server,
        )
        configure_logging(service="worker")
        reset_multiproc_dir()
        start_worker_metrics_server()
    except Exception as e:                                    # noqa: BLE001
        # Deliberately loud: observability failing SILENTLY is how a 60%-built index passed for a
        # completed run. If the thing that reports problems cannot start, that is itself the report.
        import traceback
        print(f"OBSERVABILITY worker_init FAILED: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()


@_signals.setup_logging.connect
def _obs_setup_logging(loglevel=None, **_kw):
    """The app owns logging in EVERY worker process — the parent and each pool child — not Celery.

    Celery sends this from `app.log.setup`, which runs in the parent and again in every prefork child
    (`process_initializer`, including children replaced under --max-tasks-per-child), so this is the
    one place a pool child's logging is configured; its metrics land in the shared mmap dir on their
    own. Having a receiver at all is also Celery's documented switch for "hands off" — and that is the
    fix, not a detail. Without one, Celery replaced the root handlers with its own text formatter and
    swapped `sys.stdout` for a LoggingProxy that re-logs anything printed; pool children inherit the
    proxy. `configure_logging` then ran in the child and pointed the root handler, and structlog's
    printer, at that proxy — which logs into the root handler, which writes to the proxy, whose
    recursion guard drops the line. Every line a prefork child wrote vanished: `task.end`, Celery's own
    "Task … succeeded", any warning from task code. The graphrag and extraction workers logged
    "received" (the parent) and never a completion (the child), so a finished task looked exactly
    like a lost one. The embedding worker was spared only because a threads pool has no children.
    """
    try:
        from opencortyx.core.observability import configure_logging
        configure_logging(service="worker", level=loglevel or None)
    except Exception as e:                                    # noqa: BLE001
        import traceback
        print(f"OBSERVABILITY setup_logging FAILED: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()


@_signals.task_prerun.connect
def _obs_prerun(task_id=None, task=None, args=None, kwargs=None, **_kw):
    try:
        import time as _t
        from opencortyx.core import observability as _obs
        name = getattr(task, "name", "unknown")
        _TASK_T0[task_id] = _t.perf_counter()
        _sweep(_TASK_T0)
        _obs.TASK_IN_FLIGHT.labels(task=name).inc()
        # workspace_id is the first positional argument of every ingestion/maintenance task in this app.
        org = args[0] if args and isinstance(args[0], str) and len(args[0]) == 36 else None
        # run_id is a keyword on the ingestion tasks. Binding it here rather than at each call site
        # is what lets every downstream log line — including the ones inside ingest/ and the
        # capabilities — be filtered to a single run without those modules knowing a run exists.
        run = (kwargs or {}).get("run_id")
        _TASK_CTX[task_id] = _obs.bind_context(task=name, task_id=task_id, workspace_id=org,
                                               run_id=str(run) if run else None)
        _sweep(_TASK_CTX)
        _obs.get_logger().info("task.start", task=name, task_id=task_id)
    except Exception:                                         # noqa: BLE001
        pass


@_signals.task_postrun.connect
def _obs_postrun(task_id=None, task=None, state=None, **_kw):
    try:
        import time as _t
        from opencortyx.core import observability as _obs
        name = getattr(task, "name", "unknown")
        t0 = _TASK_T0.pop(task_id, None)
        dur = (_t.perf_counter() - t0) if t0 else None
        _obs.TASK_IN_FLIGHT.labels(task=name).dec()
        if dur is not None:
            _obs.TASK_DURATION.labels(task=name).observe(dur)
        _obs.TASKS.labels(task=name, state=str(state or "unknown").lower()).inc()
        _obs._refresh_process_gauges()
        _obs.get_logger().info("task.end", task=name, task_id=task_id,
                               status=str(state or "unknown").lower(),
                               duration_ms=round(dur * 1000, 1) if dur is not None else None)
        tok = _TASK_CTX.pop(task_id, None)
        if tok is not None:
            _obs.reset_context(tok)
    except Exception:                                         # noqa: BLE001
        pass


@_signals.task_retry.connect
def _obs_retry(request=None, reason=None, **_kw):
    try:
        from opencortyx.core import observability as _obs
        name = getattr(request, "task", "unknown")
        _obs.TASKS.labels(task=name, state="retry").inc()
        _obs.get_logger().warning("task.retry", task=name,
                                  task_id=getattr(request, "id", None),
                                  retry_count=getattr(request, "retries", None),
                                  error_type=type(reason).__name__ if reason else None,
                                  error_message=str(reason)[:300] if reason else None)
    except Exception:                                         # noqa: BLE001
        pass


@_signals.task_failure.connect
def _obs_failure(task_id=None, exception=None, sender=None, **_kw):
    try:
        from opencortyx.core import observability as _obs
        _obs.get_logger().error("task.failed", task=getattr(sender, "name", "unknown"),
                                task_id=task_id,
                                error_type=type(exception).__name__ if exception else None,
                                error_message=str(exception)[:500] if exception else None)
    except Exception:                                         # noqa: BLE001
        pass


@_signals.worker_shutting_down.connect
def _obs_worker_shutdown(**_kw):
    """A worker that is going away should SAY so. Otherwise a SIGKILLed pool child and a clean
    scale-down look identical in the logs — which is exactly the ambiguity that let an OOM kill
    pass for a completed run."""
    try:
        from opencortyx.core import observability as _obs
        _obs.get_logger().warning("worker.shutting_down", in_flight=len(_TASK_T0))
    except Exception:                                         # noqa: BLE001
        pass
