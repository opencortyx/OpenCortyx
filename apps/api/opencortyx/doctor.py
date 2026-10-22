"""In-container health check used by `bin/opencortyx doctor`.

    python -m opencortyx.doctor
"""
import asyncio
import os
import sys

import httpx
from opencortyx.core.config import settings

OK, WARN, FAIL = "ok", "warn", "fail"


def _line(status: str, name: str, detail: str = "") -> None:
    symbols = (
        {"ok": "[OK]", "warn": "[!]", "fail": "[FAIL]"}
        if sys.platform == "win32"
        else {"ok": "✓", "warn": "!", "fail": "✗"}
    )
    mark = symbols[status]
    print(f"  {mark} {name}" + (f": {detail}" if detail else ""))


async def check_database() -> str:
    import asyncpg
    from opencortyx.core.config import settings, V2_SCHEMA
    url = os.environ.get("DATABASE_URL", str(settings.DATABASE_URL)).replace("postgresql+asyncpg://", "postgresql://")
    try:
        conn = await asyncpg.connect(url, timeout=10)
        try:
            version = await conn.fetchval("SHOW server_version")
            tables = await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = $1", V2_SCHEMA)
            rev = await conn.fetchval(f"SELECT version_num FROM {V2_SCHEMA}.alembic_version LIMIT 1")
        finally:
            await conn.close()
    except Exception as e:  # noqa: BLE001
        _line(FAIL, "database", str(e)[:200])
        return FAIL
    if not tables or not rev:
        _line(FAIL, "database", f"tables={tables} migration={rev} (schema not bootstrapped)")
        return FAIL
    _line(OK, "database", f"PostgreSQL {version}, {tables} control-plane tables, migration {rev}")
    return OK


def check_redis() -> str:
    import redis
    try:
        r_url = os.environ.get("REDIS_URL", str(settings.REDIS_URL))
        redis.Redis.from_url(r_url, socket_timeout=5).ping()
    except Exception as e:  # noqa: BLE001
        _line(FAIL, "redis", str(e)[:200])
        return FAIL
    _line(OK, "redis")
    return OK


async def _probe_generation(label: str, gen: dict) -> str:
    base = (gen.get("base_url") or "").rstrip("/")
    provider, model, key = gen.get("provider"), gen.get("model_id"), gen.get("api_key")
    if not base:
        _line(OK if key else FAIL, "llm", f"{label}: {provider} (hosted, key {'set' if key else 'MISSING'})")
        return OK if key else FAIL
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{base}/models", headers=headers)
            r.raise_for_status()
            served = [m.get("id") for m in (r.json().get("data") or [])]
    except Exception as e:  # noqa: BLE001
        _line(FAIL, "llm", f"{label}: {provider} at {base} unreachable: {str(e)[:160]}")
        return FAIL
    if model not in served:
        _line(FAIL, "llm", f"{label}: model '{model}' not served by {base} (serves: {served})")
        return FAIL
    _line(OK, "llm", f"{label}: {provider} serving '{model}'")
    return OK


async def check_llm() -> str:
    """Each workspace's generation model, from its Model Registry. There is no deployment-wide one."""
    from sqlalchemy import text
    from opencortyx.core.config import V2_SCHEMA
    from opencortyx.core.db import V2Session
    from opencortyx.core.inference.registry import resolve_model
    async with V2Session() as db:
        workspaces = (await db.execute(text(
            f"SELECT workspace_id, name FROM {V2_SCHEMA}.workspace ORDER BY created_at"))).all()
    if not workspaces:
        _line(WARN, "llm", "no workspace yet: create the admin at the dashboard, then connect a model "
                           "on the Models page")
        return WARN
    results = []
    for workspace_id, name in workspaces:
        label = f"workspace {name or str(workspace_id)[:8]}"
        gen = await resolve_model(workspace_id, "generation")
        if not gen:
            _line(WARN, "llm", f"{label}: no generation model - connect one on the Models page")
            results.append(WARN)
            continue
        results.append(await _probe_generation(label, gen))
    return FAIL if FAIL in results else (WARN if WARN in results else OK)


async def check_embedding() -> str:
    from opencortyx.core.inference.embedding import embed_batch
    try:
        vectors = await embed_batch(["opencortyx doctor probe"])
        dim = len(vectors[0]) if vectors and vectors[0] else 0
    except Exception as e:  # noqa: BLE001
        _line(FAIL, "embedding", str(e)[:200])
        return FAIL
    if dim != settings.EMBED_DIMENSIONS:
        _line(FAIL, "embedding", f"model returns {dim} dims, EMBED_DIMENSIONS={settings.EMBED_DIMENSIONS}")
        return FAIL
    from opencortyx.core.inference.embedding import active_embedding_spec
    _line(OK, "embedding", f"{(await active_embedding_spec() or {}).get('model_id')} ({dim} dims)")
    return OK


async def check_reranker() -> str:
    """The rerank models the workspaces chose: a fastembed one must be downloaded; a
    sentence-transformers one needs the library, which only an INSTALL_RERANK=1 image has."""
    from opencortyx.core.inference.embedding import cross_encoder_installed
    from opencortyx.core.inference.fastembed_models import describe, is_downloaded
    rows = await _default_rows("rerank")
    if not rows:
        _line(OK, "reranker", "none chosen; retrieval ranks without one")
        return OK
    worst = OK
    for label, provider, model_id in rows:
        if provider == "fastembed":
            d = describe("rerank", model_id)
            if d and is_downloaded(d):
                _line(OK, "reranker", f"{label}: {model_id} (fastembed, downloaded)")
            else:
                _line(WARN, "reranker", f"{label}: {model_id} not downloaded yet - open Models, edit it "
                                        "and press Download (or it downloads on first search)")
                worst = WARN
        elif provider in ("sentence-transformers", "sentence_transformers", "st"):
            if cross_encoder_installed():
                _line(OK, "reranker", f"{label}: {model_id} (sentence-transformers)")
            else:
                _line(WARN, "reranker", f"{label}: {model_id} needs an INSTALL_RERANK=1 image; "
                                        "retrieval ranks without it (or pick a fastembed reranker)")
                worst = WARN
        else:
            _line(OK, "reranker", f"{label}: {model_id} ({provider})")
    return worst


async def _default_rows(role: str) -> list:
    """(workspace label, provider, model_id) of each workspace's default model for `role`."""
    from sqlalchemy import text

    from opencortyx.core.db import V2Session
    from opencortyx.domain.base import V2_SCHEMA
    async with V2Session() as db:
        rows = (await db.execute(text(
            f"SELECT coalesce(w.name, ''), m.provider, m.model_id FROM {V2_SCHEMA}.model m "
            f"LEFT JOIN {V2_SCHEMA}.workspace w ON w.workspace_id = m.workspace_id "
            f"WHERE m.role = :role AND m.is_default"), {"role": role})).all()
    return [(name or "workspace", (prov or "").lower(), mid) for name, prov, mid in rows]


async def check_models() -> str:
    """Every model the workspaces use and OpenCortyx runs itself is downloaded. The embedding worker
    The defaults ship in the image; a model chosen later is downloaded from the Models page."""
    from opencortyx.core.inference import hf_hub
    from opencortyx.core.inference.fastembed_models import describe, is_downloaded
    worst, missing, ready = OK, [], []
    for label, provider, model_id in await _default_rows("embedding"):
        d = describe("embedding", model_id) if provider == "fastembed" else None
        if d is not None:
            (ready if is_downloaded(d) else missing).append(model_id)
    for role in ("generation", "extraction", "utility"):
        for label, provider, model_id in await _default_rows(role):
            if provider == "transformers":
                (ready if hf_hub.is_downloaded(model_id) else missing).append(model_id)
    if missing:
        _line(WARN, "models", f"not downloaded yet: {', '.join(sorted(set(missing)))} - open Models, "
                              "edit the model and press Download (or it downloads on first use)")
        worst = WARN
    else:
        _line(OK, "models", f"downloaded: {', '.join(sorted(set(ready))) or 'none needed'}")
    return worst


#: The queues every installation runs, and the worker that consumes each (compose.yaml).
_QUEUES = {"graphrag": "worker", "extraction": "extraction-worker", "embedding": "embedding-worker",
           "resolution": "embedding-worker", "maintenance": "maintenance-worker"}


def check_workers() -> str:
    """Each queue has a live worker listening on it."""
    from opencortyx.core.worker.celery_app import celery_app
    try:
        replies = celery_app.control.inspect(timeout=5).active_queues() or {}
    except Exception as e:                                              # noqa: BLE001
        _line(FAIL, "workers", f"could not ask the workers: {str(e)[:160]}")
        return FAIL
    served = {q["name"] for qs in replies.values() for q in qs}
    missing = [f"{q} ({svc})" for q, svc in _QUEUES.items() if q not in served]
    if missing:
        _line(FAIL, "workers", f"no worker listening on: {', '.join(missing)} - see: opencortyx logs <service>")
        return FAIL
    _line(OK, "workers", f"{len(replies)} worker(s) answering on {', '.join(sorted(served & set(_QUEUES)))}")
    return OK


def check_queues() -> str:
    """Jobs waiting and running. A running job older than the broker's visibility timeout was lost
    (its worker stopped); Redis hands it out again, but it should not happen."""
    import time

    import redis

    from opencortyx.core.config import settings
    try:
        r = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=5)
        waiting = {q: r.llen(q) for q in _QUEUES}
        running = r.zrange("unacked_index", 0, -1, withscores=True)
    except Exception as e:                                              # noqa: BLE001
        _line(FAIL, "queues", f"broker unreachable: {str(e)[:160]}")
        return FAIL
    now = time.time()
    oldest = max((now - ts for _, ts in running), default=0)
    lost_after = settings.TASK_TIME_LIMIT + 300
    detail = (f"{sum(waiting.values())} waiting ({', '.join(f'{q} {n}' for q, n in waiting.items() if n) or 'none'}), "
              f"{len(running)} held by workers" + (f", oldest {int(oldest // 60)} min" if running else ""))
    if oldest > lost_after:
        _line(WARN, "queues", detail + f" - a job has been held over {lost_after // 60} min: its worker "
                                       "was probably stopped mid-job; Redis will hand it out again")
        return WARN
    _line(OK, "queues", detail)
    return OK


def check_uploads() -> str:
    """The store uploaded files wait in for their worker (core/uploads.py): how much it holds, and
    whether its disk has room for more. A full disk refuses uploads."""
    import shutil
    from pathlib import Path

    from opencortyx.core.config import settings
    root = Path(settings.UPLOAD_STORE)
    if not root.exists():
        _line(WARN, "uploads", f"{root} does not exist: is the `uploads` volume mounted?")
        return WARN
    files = [f for f in root.glob("*/*") if f.is_file() and not f.name.startswith(".")]
    held = sum(f.stat().st_size for f in files)
    free = shutil.disk_usage(root).free
    detail = (f"{len(files)} file(s), {held / 2**20:.0f} MB held; {free / 2**30:.1f} GB free "
              f"(kept {settings.UPLOAD_RETENTION_HOURS} h after upload)")
    if free < 2 * 2**30:
        _line(WARN, "uploads", detail + " - under 2 GB free: free disk space or lower UPLOAD_RETENTION_HOURS")
        return WARN
    _line(OK, "uploads", detail)
    return OK


async def check_search_index() -> str:
    """Recent nodes that still have no vectors: findable by keywords only. The embedding sweep runs
    every 10 minutes and fills them; a count that stays is a stuck embedding worker."""
    from datetime import datetime, timedelta, timezone

    from opencortyx.core.substrate import NodeRepo
    from opencortyx.tasks.graph import SWEEP_SETTLE_MINUTES, SWEEP_WINDOW_HOURS
    now = datetime.now(timezone.utc)
    settled = now - timedelta(minutes=SWEEP_SETTLE_MINUTES)
    repo, total = NodeRepo(), 0
    from sqlalchemy import text

    from opencortyx.core.db import V2Session
    from opencortyx.domain.base import V2_SCHEMA
    async with V2Session() as db:
        workspaces = [w for (w,) in (await db.execute(text(f"SELECT workspace_id FROM {V2_SCHEMA}.workspace"))).all()]
    for oid in workspaces:
        total += len(await repo.unembedded_ids(oid, created_after=now - timedelta(hours=SWEEP_WINDOW_HOURS),
                                               created_before=settled))
    if total:
        _line(WARN, "search index", f"{total} recent node(s) without vectors (keyword search only); the "
                                    "embedding sweep fills them within 10 minutes")
        return WARN
    _line(OK, "search index", "every recent node has vectors")
    return OK


async def check_graph_store() -> str:
    from opencortyx.stores.graph import get_graph_store
    return await _check_store("graph_store", get_graph_store())


async def check_vector_store() -> str:
    from opencortyx.stores.vector import get_vector_store
    return await _check_store("vector_store", get_vector_store())


async def _check_store(name: str, store) -> str:
    """A store counts as healthy only when it answers a query — reachable, not merely configured."""
    try:
        await store.initialize()
        if await store.health_check():
            _line(OK, name, f"{store.provider_name} reachable")
            return OK
        _line(FAIL, name, f"{store.provider_name} did not answer a health check")
        return FAIL
    except Exception as e:  # noqa: BLE001
        _line(FAIL, name, f"{store.provider_name} failed: {str(e)[:160]}")
        return FAIL


async def main() -> int:
    results = [await check_database(), check_redis(), check_workers(), check_queues(),
               await check_models(), await check_llm(), await check_embedding(),
               await check_reranker(), await check_graph_store(), await check_vector_store(),
               check_uploads(), await check_search_index()]
    failed = results.count(FAIL)
    print(f"\n  {results.count(OK)} ok, {results.count(WARN)} warnings, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
