"""v2 DB access — self-contained engine + session + schema bootstrap (no v1 import).

Phase 1 of the cutover: v2 owns its own async engine (loop/pid-cached like the old v1 factory)
instead of borrowing `opencortyx.db.session`. One Postgres, isolated `opencortyx_v2` schema.
"""
import os
import asyncio
from collections.abc import AsyncGenerator
from typing import Dict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from opencortyx.core.config import settings, V2_SCHEMA
from opencortyx.domain.base import V2Base
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

# Cache engines/sessionmakers by (pid, loop) for process + event-loop safety.
_engine_cache: Dict[tuple, object] = {}
_session_cache: Dict[tuple, async_sessionmaker] = {}
# separate caches for the optional read replica (scalability: offload read-heavy retrieval)
_read_engine_cache: Dict[tuple, object] = {}
_read_session_cache: Dict[tuple, async_sessionmaker] = {}

_BOOTSTRAP_LOCK_KEY = 0x0C27_0002          # arbitrary but stable app-wide key for pg_advisory_lock


def _new_engine(url: str | None = None):
    return create_async_engine(
        url or settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        echo=settings.LOG_LEVEL == "DEBUG",
        pool_pre_ping=True,
        pool_recycle=settings.DB_POOL_RECYCLE,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.DB_STATEMENT_TIMEOUT_MS),
                "idle_in_transaction_session_timeout": str(settings.DB_IDLE_TX_TIMEOUT_MS),
            },
            "command_timeout": settings.DB_COMMAND_TIMEOUT_S,
        },
    )


def get_engine():
    """Get/create the async engine for the current process + running loop."""
    pid = os.getpid()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _new_engine()
    key = (pid, loop)
    if key not in _engine_cache:
        for k in list(_engine_cache.keys()):       # purge stale (other-process) engines
            if k[0] != pid:
                _engine_cache.pop(k, None)
                _session_cache.pop(k, None)
        _engine_cache[key] = _new_engine()
    return _engine_cache[key]


def V2Session():
    """A session bound to the v2 engine, scoped per (pid, loop)."""
    pid = os.getpid()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return async_sessionmaker(get_engine(), expire_on_commit=False)()
    key = (pid, loop)
    if key not in _session_cache:
        _session_cache[key] = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_cache[key]()


async def guarded_step(db, label, make_coro, *, retries: int = 3, workspace_id=None, asset=None):
    """Run one optional write step in a SAVEPOINT, retrying transient deadlocks. None if it gave up.

    Use this ANY time a failure in one item of a batch must not cost the rest — the shape is
    "for item in batch: try: write(db, item) except: log and carry on". That shape is a trap: once a
    statement raises, Postgres has aborted the transaction, so the handler's `carry on` runs against a
    dead session and EVERY later item fails with "This Session's transaction has been rolled back due
    to a previous exception". The batch is then lost in silence, which is strictly worse than the
    single failure the handler was written to tolerate. Measured on this codebase: two nodes written,
    one bad step swallowed, zero survivors.

    A SAVEPOINT bounds the damage to the step that failed — ROLLBACK TO SAVEPOINT leaves the outer
    transaction usable — so the intent the caller wrote actually holds.

    It also retries deadlocks, which are transient by construction: the winner commits and releases,
    so the victim usually succeeds on a second attempt. The backoff is jittered because two victims
    retrying in lockstep simply collide again.

    `make_coro` must be a CALLABLE returning a fresh awaitable, not a coroutine object: a retry has to
    re-run the step, and a coroutine can only be awaited once. Passing one directly is an easy slip
    that would only surface on the retry path — i.e. during a deadlock, when it is least welcome — so
    it is detected here and degraded to a single guarded attempt rather than blowing up."""
    import inspect
    import random

    if inspect.iscoroutine(make_coro):                # tolerated: isolate it, but it cannot be retried
        awaitable, retries = make_coro, 1
        make_coro = lambda: awaitable                 # noqa: E731
    for attempt in range(retries):
        try:
            async with db.begin_nested():
                return await make_coro()
        except Exception as e:                        # noqa: BLE001 — never abort the caller's batch
            msg = str(e).lower()
            if (("deadlock" in msg or "serialization" in msg or "could not serialize" in msg
                 or "lock timeout" in msg) and attempt < retries - 1):
                await asyncio.sleep(0.05 * (2 ** attempt) + random.random() * 0.05)
                continue
            log.warning("db.guarded_step_failed", step=str(label), error=str(e))
            if workspace_id is not None:
                try:
                    from opencortyx.core.substrate.quarantine import capture
                    await capture(workspace_id, "step", {"step": str(label)}, reason="step_failed",
                                  retryable=False, asset=asset, error=str(e))
                except Exception:                     # noqa: BLE001 — recording must never escalate
                    pass
            return None
    return None


def get_read_engine():
    """Engine for read-only queries → the read replica if DATABASE_READ_URL is set, else primary."""
    if not settings.DATABASE_READ_URL:
        return get_engine()
    pid = os.getpid()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _new_engine(settings.DATABASE_READ_URL)
    key = (pid, loop)
    if key not in _read_engine_cache:
        for k in list(_read_engine_cache.keys()):
            if k[0] != pid:
                _read_engine_cache.pop(k, None)
                _read_session_cache.pop(k, None)
        _read_engine_cache[key] = _new_engine(settings.DATABASE_READ_URL)
    return _read_engine_cache[key]


def V2ReadSession():
    """A session for READ-ONLY retrieval. Routes to the read replica when configured; otherwise
    identical to V2Session. Lets retrieval scale horizontally across replicas."""
    if not settings.DATABASE_READ_URL:
        return V2Session()
    pid = os.getpid()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return async_sessionmaker(get_read_engine(), expire_on_commit=False)()
    key = (pid, loop)
    if key not in _read_session_cache:
        _read_session_cache[key] = async_sessionmaker(get_read_engine(), expire_on_commit=False)
    return _read_session_cache[key]()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a v2 session (commit on success, rollback on error)."""
    async with V2Session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def close_db():
    """Dispose engines owned by this process + loop (called after each Celery task)."""
    pid = os.getpid()
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    for (key_pid, loop), engine in list(_engine_cache.items()):
        if key_pid == pid and (loop == current_loop or not loop.is_running()):
            try:
                await engine.dispose()
            except Exception:
                pass
            finally:
                _engine_cache.pop((key_pid, loop), None)
                _session_cache.pop((key_pid, loop), None)


async def schema_exists() -> bool:
    """True when the control-plane tables already exist, so a new process can skip the DDL."""
    try:
        async with get_engine().connect() as conn:
            return bool((await conn.execute(text(
                "SELECT to_regclass(:t) IS NOT NULL"), {"t": f"{V2_SCHEMA}.app_user"})).scalar())
    except Exception:                                   # noqa: BLE001 - unknown means run it
        return False


async def create_schema_and_tables() -> None:
    """Create the `opencortyx_v2` schema and every declared table. Idempotent.

    Pure DDL: it knows nothing about which ontology or which first user a fresh install should
    carry. That composition lives in `opencortyx.bootstrap`, which is what callers invoke.
    """
    import opencortyx.domain  # noqa: F401 - registers every table on V2Base
    async with get_engine().begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOOTSTRAP_LOCK_KEY})
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {V2_SCHEMA}"))
        await conn.run_sync(V2Base.metadata.create_all)
