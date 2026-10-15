"""OpenCortyx API."""
from contextlib import asynccontextmanager

from opencortyx import __version__
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from opencortyx.core.config import settings
from opencortyx.bootstrap import init_v2_schema
from opencortyx.core.db import close_db
from opencortyx.api.router import router as v2_router
import opencortyx.tasks.graph_tree  # noqa: F401,E402 — registers the graph-change hook (tree index)

log = structlog.get_logger()


_DB_WAIT_SECONDS = 120
_DB_WAIT_STEP = 2.0


async def _await_database():
    """Block until the database accepts us, or give up loudly after _DB_WAIT_SECONDS."""
    import asyncio
    import time
    deadline = time.monotonic() + _DB_WAIT_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            await init_v2_schema()   # idempotent: schema + tables + base/structured ontology
            if attempt > 1:
                log.info("opencortyx.database_ready", attempts=attempt)
            return
        except Exception as e:       # noqa: BLE001 — any connect-time failure is worth waiting out
            if time.monotonic() >= deadline:
                log.error("opencortyx.database_unavailable",
                          waited_seconds=_DB_WAIT_SECONDS, attempts=attempt, error=str(e))
                raise
            if attempt == 1:
                log.warning("opencortyx.database_not_ready", error=str(e)[:160],
                            waiting_up_to=_DB_WAIT_SECONDS)
            await asyncio.sleep(_DB_WAIT_STEP)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("opencortyx.startup", schema="opencortyx_v2")
    await _await_database()
    from opencortyx.stores.graph import get_graph_store
    from opencortyx.stores.vector import get_vector_store
    for store in (get_graph_store(), get_vector_store()):
        try:
            await store.initialize()
            log.info("opencortyx.store_ready", provider=store.provider_name)
        except Exception as e:  # noqa: BLE001
            log.warning("opencortyx.store_init_failed", provider=store.provider_name, error=str(e))
    from opencortyx.ingest.runs.progress import reconcile_orphans
    n = await reconcile_orphans()    # mark crashed/orphaned ingestion runs as failed (no ghosts)
    if n:
        log.info("opencortyx.reconcile_orphans", failed=n)
    import asyncio as _asyncio
    from opencortyx.core.inference import warmup
    warmup.start()

    async def _preflight_after_warmup():
        for _ in range(180):                    # up to ~15 min for a first-install download
            if warmup.status()["ready"]:
                break
            await _asyncio.sleep(5)
        try:                         # report degradation LOUDLY, just not before we can serve
            from opencortyx.core.preflight import run_preflight
            await run_preflight()
        except Exception as e:       # noqa: BLE001
            log.warning("opencortyx.preflight_skipped", error=str(e))

    _asyncio.get_running_loop().create_task(_preflight_after_warmup())
    yield
    await close_db()
    log.info("opencortyx.shutdown")


from opencortyx.core.observability import configure_logging as _configure_logging
_configure_logging(service="api")

app = FastAPI(
    title="OpenCortyx",
    description="Grounded, cited, token-efficient context for any AI app — one store, open ontology.",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting + request-size guard on the LLM/embedding/upload endpoints (cost + DoS protection).
from opencortyx.core.security.ratelimit import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware)


@app.middleware("http")
async def _observability(request, call_next):
    """One record and one timing per request, labelled by ROUTE TEMPLATE.

    The template ("/api/v2/nodes/{node_id}"), never the raw path: labelling by raw path makes the
    label set unbounded — one time series per node id — which turns the metrics endpoint into the
    memory leak it exists to detect.
    """
    import time as _t
    from opencortyx.core import observability as _obs
    t0 = _t.perf_counter()
    _obs.HTTP_IN_FLIGHT.inc()
    status = "500"
    try:
        resp = await call_next(request)
        status = str(resp.status_code)
        return resp
    finally:
        _obs.HTTP_IN_FLIGHT.dec()
        # `route` is resolved AFTER routing, so read it here rather than up front.
        r = request.scope.get("route")
        route = getattr(r, "path", None) or "unmatched"
        dur = _t.perf_counter() - t0
        try:
            _obs.HTTP_REQUESTS.labels(method=request.method, route=route,
                                      status=status[0] + "xx").inc()
            _obs.HTTP_LATENCY.labels(method=request.method, route=route).observe(dur)
            if route != "/metrics":
                _obs.get_logger().info(
                    "http.request", method=request.method, route=route, status=int(status),
                    duration_ms=round(dur * 1000, 2),
                    workspace_id=str(getattr(getattr(request.state, "user", None), "workspace_id", "") or "") or None,
                    client=(request.client.host if request.client else None))
        except Exception:                                  # noqa: BLE001 — never fail a response
            pass


@app.middleware("http")
async def _transient_db_contention(request, call_next):
    """Turn database lock contention into an honest, retryable 503 instead of an opaque 500.

    Curation writes the same rows extraction does — a user renaming an entity while an ingest is
    merging into it is the ordinary case, not a rare one — so an API request can lose a lock race
    exactly as a background task can. Background tasks retry themselves (see celery_app.ResilientTask).
    A request cannot be retried for the caller: the handler may already have sent mail, called a model,
    or spent money, and replaying that silently is worse than failing. What it CAN do is tell the truth,
    so the client knows this is a momentary collision and safe to repeat rather than a bug in its
    payload. 503 + Retry-After is exactly that contract, and it is what makes an automatic client retry
    correct instead of a guess."""
    from starlette.responses import JSONResponse

    from opencortyx.core.worker.celery_app import _is_transient
    try:
        return await call_next(request)
    except Exception as exc:                        # noqa: BLE001 — re-raised unless it is contention
        if not _is_transient(exc):
            raise
        log.warning("db.contention path=%s %s", request.url.path, str(exc).splitlines()[0][:200])
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "1"},
            content={"detail": "The database was briefly locked by another write. "
                               "Nothing was saved — please retry.", "retryable": True},
        )

@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus exposition. Unversioned and unauthenticated by design — it is scraped by the
    platform, exposes no tenant data (every label is a bounded, non-identifying set), and an auth
    dependency here is a classic way to end up with no monitoring during an auth outage.
    Restrict it at the network layer, not in the app."""
    from fastapi import Response
    from opencortyx.core import observability as _obs
    from opencortyx.core.worker.celery_app import _QUEUES
    _obs.observe_queue_depths(_QUEUES)
    await _obs.observe_db_and_lag()
    body, ctype = _obs.render_metrics()
    return Response(content=body, media_type=ctype)


from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402


def _field_error(e: dict) -> str:
    """One validation error as a person reads it: `field: reason`, without pydantic's prefixes."""
    where = ".".join(str(p) for p in (e.get("loc") or ()) if p not in ("body", "query", "path"))
    msg = str(e.get("msg") or "invalid").removeprefix("Value error, ")
    return f"{where}: {msg}" if where else msg


@app.exception_handler(RequestValidationError)
async def _readable_validation_error(_request, exc: RequestValidationError):
    """422s carry a sentence in `detail` — every screen shows `detail` as text, and a list of error
    objects rendered as "[object Object]" — and keep the structured list under `errors`."""
    errors = exc.errors()
    return JSONResponse(status_code=422, content={
        "detail": "; ".join(_field_error(e) for e in errors) or "invalid request",
        "errors": [{k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in errors],
    })


from sqlalchemy.exc import DBAPIError  # noqa: E402


def _sqlstate(exc: DBAPIError) -> str:
    for err in (exc.orig, getattr(exc.orig, "__cause__", None)):
        code = getattr(err, "sqlstate", None) or getattr(err, "pgcode", None)
        if code:
            return str(code)
    return ""


@app.exception_handler(DBAPIError)
async def _data_error_is_the_callers(_request, exc: DBAPIError):
    """PostgreSQL class 22 ("data exception": a value too long for its column, a character the
    encoding cannot hold, a number out of range) is bad INPUT, so it is a 422 that says so — not the 500
    it surfaced as. Every other database error is still a real 500."""
    if _sqlstate(exc).startswith("22"):
        reason = str(getattr(exc.orig, "args", [""])[0] or exc.orig).split("\n")[0][:200]
        return JSONResponse(status_code=422, content={"detail": f"A value in the request cannot be stored: {reason}"})
    raise exc


app.include_router(v2_router, prefix="/api")


@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "version": __version__}


@app.get("/", tags=["System"])
async def root():
    return {"name": "OpenCortyx", "version": __version__, "docs": "/docs", "api": "/api/v2"}
