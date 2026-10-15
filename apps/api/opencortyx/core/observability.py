"""Structured logging + metrics — the two things that turn "it broke" into "here is what broke".

WHY THIS MODULE EXISTS. Before it, this system answered none of the questions an operator has to
ask at 3am. `print()` was the logging strategy in every module but one, so a line said what happened
but never for WHICH tenant, WHICH run, or HOW LONG it took — and correlating an ingest to its
follow-on tasks meant grepping timestamps by eye. There were no metrics at all, which is how an
OOM-killed indexer left a retrieval surface 60% built while the run reported "completed, 100%":
nothing was counting, so nothing noticed.

Two deliberate choices:

  * **Logs carry CONTEXT, not just text.** `bind_context()` puts `workspace_id` / `run_id` / `task_id`
    into a contextvar, so every log line emitted downstream carries them without the call site
    having to remember. Contextvars (not thread-locals) because the whole codebase is asyncio.

  * **Metrics are LOW cardinality.** Labels are bounded sets — route templates, task names, queue
    names, statuses. Never a node id, org id, filename or query string: an unbounded label set is
    how a metrics endpoint becomes the memory leak it was meant to detect.

Nothing here may ever fail a request. Every public function is safe to call before configuration,
and the metric helpers no-op when the client library is unavailable.

That is also why this module reads `LOG_LEVEL`, `OCX_SERVICE`, `METRICS_PORT` and
`PROMETHEUS_MULTIPROC_DIR` straight from the environment instead of from `V2Settings`, which
every other module in the codebase is required to use. Importing settings here would make
logging depend on configuration having loaded — so a configuration error, precisely the thing
you need the log for, would be the one failure that produces no log. These four are the
documented exception, not a pattern to copy.
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import sys
import time
from typing import Any

try:
    from prometheus_client import (  # type: ignore
        CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest,
    )
    _HAVE_PROM = True
except Exception:                                          # noqa: BLE001
    _HAVE_PROM = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

_MULTIPROC = bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))
if _MULTIPROC:
    try:
        os.makedirs(os.environ["PROMETHEUS_MULTIPROC_DIR"], exist_ok=True)
    except Exception:                                      # noqa: BLE001 — fall back to in-process
        _MULTIPROC = False

#: Typed loosely on purpose: every metric helper in this module already no-ops when
#: prometheus_client is absent, and threading `CollectorRegistry | None` through them would add a
#: narrowing branch to each one to restate a guard that is already there.
REGISTRY: Any = CollectorRegistry() if _HAVE_PROM else None

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
#: Task durations run minutes, not milliseconds.
_TASK_BUCKETS = (0.1, 0.5, 1, 5, 15, 60, 300, 900, 1800, 3600)


def _counter(name, doc, labels=()):
    return Counter(name, doc, labels, registry=REGISTRY) if _HAVE_PROM else _NullMetric()


def _gauge(name, doc, labels=()):
    if not _HAVE_PROM:
        return _NullMetric()
    if _MULTIPROC:
        return Gauge(name, doc, labels, registry=REGISTRY, multiprocess_mode="livesum")
    return Gauge(name, doc, labels, registry=REGISTRY)


def _hist(name, doc, labels=(), buckets=_LATENCY_BUCKETS):
    return Histogram(name, doc, labels, buckets=buckets, registry=REGISTRY) if _HAVE_PROM else _NullMetric()


class _NullMetric:
    """No-op stand-in so call sites never branch on whether metrics are available."""

    def labels(self, *a, **k):
        return self

    def inc(self, *a, **k):
        return None

    def dec(self, *a, **k):
        return None

    def set(self, *a, **k):
        return None

    def observe(self, *a, **k):
        return None


# ── HTTP ────────────────────────────────────────────────────────────────────────────────────────
HTTP_REQUESTS = _counter("opencortyx_http_requests_total",
                         "HTTP requests by route template, method and status class.",
                         ("method", "route", "status"))
HTTP_LATENCY = _hist("opencortyx_http_request_duration_seconds",
                     "HTTP request latency.", ("method", "route"))
HTTP_IN_FLIGHT = _gauge("opencortyx_http_in_flight",
                        "Requests currently being served — the saturation signal.")

# ── Celery ──────────────────────────────────────────────────────────────────────────────────────
TASKS = _counter("opencortyx_tasks_total",
                 "Celery tasks by name and terminal state (success/failure/retry/revoked).",
                 ("task", "state"))
TASK_DURATION = _hist("opencortyx_task_duration_seconds",
                      "Celery task wall-clock duration.", ("task",), buckets=_TASK_BUCKETS)
TASK_IN_FLIGHT = _gauge("opencortyx_tasks_in_flight", "Tasks currently executing.", ("task",))
QUEUE_DEPTH = _gauge("opencortyx_queue_depth",
                     "Messages waiting in each broker queue — the backpressure signal.", ("queue",))

# ── ingestion / data quality ────────────────────────────────────────────────────────────────────
INGEST_RECORDS = _counter("opencortyx_ingest_records_total",
                          "Records seen by ingestion, by paradigm and outcome.",
                          ("paradigm", "outcome"))
INGEST_LAG = _gauge("opencortyx_ingest_lag_seconds",
                    "Age of the oldest run still not finished — ingestion falling behind.")
DQ_FAILURES = _counter("opencortyx_data_quality_failures_total",
                       "Records rejected or parked, by reason. Bounded reason set.", ("reason",))

PROMPT_ELISIONS = _counter("opencortyx_prompt_elisions_total",
                           "Prompts trimmed to fit the model's context window.", ("model",))
PROMPT_CHARS_DROPPED = _counter("opencortyx_prompt_chars_dropped_total",
                                "Characters removed from prompts to fit the window.", ("model",))

# ── resources ───────────────────────────────────────────────────────────────────────────────────
PROC_MEMORY = _gauge("opencortyx_process_memory_bytes", "Resident memory of this process.")
PROC_CPU = _gauge("opencortyx_process_cpu_percent", "CPU percent of this process.")
DB_POOL = _gauge("opencortyx_db_pool_connections", "SQLAlchemy pool connections by state.", ("state",))


# ── structured logging ──────────────────────────────────────────────────────────────────────────
_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("ocx_log_ctx", default={})

#: Anything whose KEY looks like a secret is redacted before rendering. Keys, not values, because a
#: value-based heuristic both misses novel formats and mangles legitimate content.
_SECRET_KEY = re.compile(
    r"(?i)(pass(word|wd)?|secret|token|api[_-]?key|authorization|cookie|credential|private[_-]?key)")
_REDACTED = "***redacted***"


def _redact(_logger, _name, event: dict) -> dict:
    for k in list(event):
        if _SECRET_KEY.search(k):
            event[k] = _REDACTED
    return event


_SERVICE = "opencortyx"


def _add_service(_logger, _name, event: dict) -> dict:
    event.setdefault("service", _SERVICE)
    return event


def _inject_context(_logger, _name, event: dict) -> dict:
    """Merge the ambient run/tenant context. Explicit keys at the call site always win."""
    for k, v in (_ctx.get() or {}).items():
        event.setdefault(k, v)
    return event


def configure_logging(service: str | None = None, level: int | None = None) -> None:
    """JSON logs on stdout — one object per line, whoever emitted it — with a service tag and the
    ambient context merged in.

    `level` is for a caller that has already resolved one (a Celery worker passes its `--loglevel`);
    otherwise LOG_LEVEL decides. Idempotent: safe to call from the API, every worker, and the test suite.
    """
    global _SERVICE
    import structlog

    svc = service or os.environ.get("OCX_SERVICE") or "opencortyx"
    _SERVICE = svc
    if level is None:
        level = getattr(logging, str(os.environ.get("LOG_LEVEL", "INFO")).upper(), logging.INFO)

    # What every line gets, whether it was written through structlog or the standard library.
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_context,
        _add_service,
        _redact,
    ]
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[structlog.stdlib.add_logger_name, *shared],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    ))
    logging.basicConfig(handlers=[handler], level=level, force=True)
    # Uvicorn installs its own handlers; without this its access log stays unstructured and the
    # two formats interleave, which defeats any log shipper parsing one of them.
    for noisy in ("uvicorn", "uvicorn.error"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True
    _acc = logging.getLogger("uvicorn.access")
    _acc.handlers.clear()
    _acc.propagate = False
    _acc.disabled = True

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    import structlog
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_context(**kw: Any) -> contextvars.Token:
    """Attach workspace_id / run_id / task_id (etc.) to every log line emitted from here on.

    Returns a token; pass it to `reset_context` to pop the scope. Values that are None are dropped
    so an unset field never appears as a null in the log stream.
    """
    merged = {**(_ctx.get() or {}), **{k: v for k, v in kw.items() if v is not None}}
    return _ctx.set(merged)


def reset_context(token: contextvars.Token) -> None:
    try:
        _ctx.reset(token)
    except (ValueError, LookupError):                      # token from another context — ignore
        pass


# ── helpers used by the wiring ──────────────────────────────────────────────────────────────────
def render_metrics() -> tuple[bytes, str]:
    """The exposition payload for /metrics, with process gauges refreshed at scrape time."""
    _refresh_process_gauges()
    if not _HAVE_PROM:
        return (b"# prometheus_client is not installed in this image\n", CONTENT_TYPE_LATEST)
    if _MULTIPROC:
        from prometheus_client import multiprocess
        reg = CollectorRegistry()
        multiprocess.MultiProcessCollector(reg)     # aggregate every pool child's mmap files
        return (generate_latest(reg), CONTENT_TYPE_LATEST)
    return (generate_latest(REGISTRY), CONTENT_TYPE_LATEST)


#: cgroup v2 first, then v1. Reading the CONTAINER's accounting rather than this process's RSS is
#: the point: a Celery parent plus its pool children share one ceiling, and the number that gets a
#: container OOM-killed is their total, not any one process's.
_CGROUP_FILES = (
    ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
    ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes"),
)


def container_memory() -> dict:
    """This container's memory ceiling and current use, in bytes. Empty when not containerised.

    `memory.max` reads "max" when no limit is set, which is not a number and not an error — an
    unlimited container is a real configuration, so it is reported as a null ceiling rather than
    swallowed or guessed at.
    """
    for limit_file, usage_file in _CGROUP_FILES:
        try:
            with open(limit_file) as fh:
                raw = fh.read().strip()
            with open(usage_file) as fh:
                used = int(fh.read().strip())
        except (OSError, ValueError):
            continue
        limit = None if raw == "max" else int(raw)
        # cgroup v1 reports a sentinel near 2**63 instead of "max".
        if limit is not None and limit > (1 << 62):
            limit = None
        return {"limit_bytes": limit, "used_bytes": used}
    return {}


def _refresh_process_gauges() -> None:
    try:
        import psutil
        p = psutil.Process()
        PROC_MEMORY.set(p.memory_info().rss)
        PROC_CPU.set(p.cpu_percent(interval=None))
    except Exception:                                      # noqa: BLE001 — never fail a scrape
        pass


def observe_queue_depths(queues: tuple[str, ...]) -> None:
    """Sample broker backlog. Called from the scrape path so depth is read on demand rather than
    polled on a timer nobody tunes."""
    try:
        import redis
        from opencortyx.core.config import settings
        r = redis.Redis.from_url(settings.REDIS_URL, socket_timeout=2)
        for q in queues:
            QUEUE_DEPTH.labels(queue=q).set(r.llen(q))
        r.close()
    except Exception:                                      # noqa: BLE001
        pass


class timed:
    """`with timed(HTTP_LATENCY.labels(...)):` — records the duration even when the block raises."""

    __slots__ = ("_m", "_t0")

    def __init__(self, metric):
        self._m = metric

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        try:
            self._m.observe(time.perf_counter() - self._t0)
        except Exception:                                  # noqa: BLE001
            pass
        return False


def start_worker_metrics_server(port: int | None = None) -> None:
    """Expose /metrics from a Celery worker container, so Prometheus scrapes it as its own target.

    Started in the worker PARENT (once per container, on `worker_init`) rather than per pool child —
    children would fight over the port. The parent serves the aggregate of every child's mmap files,
    which is precisely why multiprocess mode exists.

    Best-effort: a worker that cannot bind its metrics port must still process work.
    """
    if not _HAVE_PROM:
        return
    p = int(port or os.environ.get("METRICS_PORT") or 9100)
    try:
        from prometheus_client import multiprocess, start_http_server
        if _MULTIPROC:
            reg = CollectorRegistry()
            multiprocess.MultiProcessCollector(reg)
        else:
            reg = REGISTRY
        start_http_server(p, registry=reg)
        get_logger().info("metrics.server_started", port=p)
    except Exception as e:                                 # noqa: BLE001
        try:
            get_logger().warning("metrics.server_failed", port=p,
                                 error_type=type(e).__name__, error_message=str(e)[:200])
        except Exception:                                  # noqa: BLE001
            pass


def reset_multiproc_dir() -> None:
    """Clear stale mmap files at worker start.

    prometheus_client writes one file per (metric, pid). A restarted container reuses pids, so files
    from the PREVIOUS life would otherwise be aggregated into this one's numbers — counters that only
    go up, describing work this process never did.
    """
    d = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            if f.endswith(".db"):
                os.unlink(os.path.join(d, f))
    except Exception:                                      # noqa: BLE001
        pass


async def observe_db_and_lag() -> None:
    """Sample the two health signals that only exist in the database, at scrape time.

      * DB POOL — a pool at its ceiling is the difference between "the API is slow" and "the API is
        out of connections", and they need opposite responses.
      * INGEST LAG — the age of the OLDEST unfinished run. Throughput alone cannot tell you that
        ingestion is falling behind; a queue that drains at exactly the rate it fills looks perfect
        right up to the moment it doesn't. Lag is the leading indicator.

    Both are best-effort: a scrape must never fail, and must never be the thing that exhausts the
    pool it is measuring.
    """
    try:
        from opencortyx.core.db import get_engine
        pool = get_engine().pool
        DB_POOL.labels(state="in_use").set(pool.checkedout())
        DB_POOL.labels(state="idle").set(pool.checkedin())
        DB_POOL.labels(state="overflow").set(max(0, pool.overflow()))
    except Exception:                                      # noqa: BLE001
        pass
    try:
        from sqlalchemy import text as _t
        from opencortyx.core.db import V2Session
        async with V2Session() as db:
            lag = (await db.execute(_t(
                "select coalesce(extract(epoch from (now() - min(created_at))), 0) "
                "from opencortyx_v2.ingest_run where status in ('queued','running')"))).scalar()
        INGEST_LAG.set(float(lag or 0.0))
    except Exception:                                      # noqa: BLE001
        pass
