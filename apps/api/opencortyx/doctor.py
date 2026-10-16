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


def check_reranker() -> str:
    try:
        import sentence_transformers  # noqa: F401
    except Exception:  # noqa: BLE001
        _line(WARN, "reranker", "not installed (INSTALL_RERANK=0); retrieval ranks without it")
        return WARN
    _line(OK, "reranker", "sentence-transformers available")
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
    results = [await check_database(), check_redis(), await check_llm(), await check_embedding(),
               check_reranker(), await check_graph_store(), await check_vector_store()]
    failed = results.count(FAIL)
    print(f"\n  {results.count(OK)} ok, {results.count(WARN)} warnings, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
