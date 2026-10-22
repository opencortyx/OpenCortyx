"""Routes: live checks of the API, database, worker and models."""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from opencortyx.core.security.auth import CurrentUser, require_auth
from opencortyx.core.db import V2Session
from opencortyx.domain import TypeRegistry

router = APIRouter()


def _model_server_name(provider: str | None) -> str:
    """The check names the connection type: a server you run (Ollama, LM Studio, vLLM, ...) or a
    hosted API, all of which may speak the OpenAI protocol."""
    prov = (provider or "").lower()
    return f"Model server ({prov or 'LLM'})"


@router.get("/health")
async def system_health(user: CurrentUser = Depends(require_auth)):
    """Probe API, Postgres, the Celery worker, the workspace's generation model server (reachable +
    serving the model ids this workspace actually requests) and run a live test embed. Each check returns status
    (ok|warn|down), a human detail line, and a fix hint."""
    import time

    import anyio
    import httpx
    from sqlalchemy import text as _sql_text

    from opencortyx.core.config import settings

    checks: list = []

    def add(name, status, detail, fix=""):
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})

    # 1 — API (we are responding, so it's up)
    from opencortyx import __version__
    add("API", "ok", f"FastAPI is serving requests - OpenCortyx {__version__}.")

    from opencortyx.core.inference import warmup as _warm
    _w = _warm.status()
    if not _w["models"]:
        add("Models", "warn", "Warmup has not started.",
            "Restart the API — model warmup runs at startup.")
    elif _w["ready"]:
        _failed = [k for k, v in _w["models"].items() if v["state"] == "failed"]
        if _failed:  # genuine failures only; "n/a" means not part of this deployment
            add("Models", "warn",
                "Loaded, except: " + ", ".join(
                    f"{k} ({_w['models'][k]['detail'][:60]})" for k in _failed),
                "A failed model degrades one feature; the rest of the stack is unaffected.")
        else:
            # "n/a" = not part of this deployment (no local reranker installed): say so, rather than
            # list it as resident "in 0.0s".
            loaded = [f"{k} in {v['seconds']}s" for k, v in _w["models"].items() if v["state"] != "n/a"]
            absent = [k for k, v in _w["models"].items() if v["state"] == "n/a"]
            add("Models", "ok", "All models resident: " + ", ".join(loaded)
                + (f" (not used here: {', '.join(absent)})" if absent else ""))
    else:
        add("Models", "warn",
            "Preparing models (first install downloads ~4.2 GB): " + ", ".join(
                f"{k}={v['state']}" for k, v in _w["models"].items()),
            "Nothing to do — the stack is usable now and gets faster as these land.")

    # 2 — Postgres. Counts the ONTOLOGY, which is what Postgres actually holds. This used to count
    # `Node`, which has not been a Postgres table since the move to NebulaGraph (ADR-004) — it is a
    # dataclass — so every install reported "Postgres: down" with a SQLAlchemy error while the
    # database was perfectly healthy. A health page that cries wolf about the database is worse than
    # no health page: the one time it is right, nobody believes it.
    t0 = time.time()
    try:
        async with V2Session() as db:
            await db.execute(_sql_text("SELECT 1"))
            n_types = (await db.execute(select(func.count()).select_from(TypeRegistry))).scalar() or 0
            server = (await db.execute(_sql_text("SHOW server_version"))).scalar() or "?"
        add("Postgres", "ok",
            f"PostgreSQL {server}, connected in {int((time.time() - t0) * 1000)} ms - "
            f"{n_types} ontology types registered.")
    except Exception as e:
        add("Postgres", "down", f"Cannot query the database: {e}",
            "Make sure the 'postgres' container is running ( docker compose ps ) and the database credentials are correct.")

    # 2b — the two stores that actually hold the data. Both were missing from this page entirely,
    # which is a strange omission for a system whose write path spans three stores without a
    # transaction (ADR-004): losing either is exactly the failure an operator needs to see here.
    t0 = time.time()
    try:
        from opencortyx.stores.graph import get_graph_store
        graph = get_graph_store()
        if not await graph.health_check():
            raise RuntimeError("health check returned false")
        n_nodes = await graph.count_nodes(user.workspace_id)
        info = await graph.describe()
        ver = f" {info['version']}" if info.get("version") else ""
        add("Graph", "ok",
            f"{info.get('provider', graph.provider_name)}{ver} in "
            f"{int((time.time() - t0) * 1000)} ms - {n_nodes} nodes; {info.get('detail', '')}".rstrip("; "))
    except Exception as e:
        add("Graph", "down", f"Cannot reach the graph store: {e}",
            "Check the 'nebulagraph' and 'nebula-storaged' containers ( docker compose ps ).")

    t0 = time.time()
    try:
        from opencortyx.stores.vector import get_vector_store
        vectors = get_vector_store()
        if not await vectors.health_check():
            raise RuntimeError("health check returned false")
        n_vec = await vectors.count_nodes(user.workspace_id)
        info = await vectors.describe()
        ver = f" {info['version']}" if info.get("version") else ""
        add("Vectors", "ok",
            f"{info.get('provider', vectors.provider_name)}{ver} in "
            f"{int((time.time() - t0) * 1000)} ms - {n_vec} nodes indexed; "
            f"{info.get('detail', '')}".rstrip("; "))
    except Exception as e:
        add("Vectors", "down", f"Cannot reach the vector store: {e}",
            "Check the 'qdrant' container ( docker compose ps ).")

    # 2c — Redis. The Celery ping below proves a worker is listening, which is not the same as the
    # broker being healthy: a reachable Redis with no worker and an unreachable Redis look identical
    # from that check alone.
    t0 = time.time()
    try:
        import redis.asyncio as _redis
        client = _redis.from_url(settings.REDIS_URL)
        try:
            info = await client.info("server")
        finally:
            await client.aclose()
        add("Redis", "ok",
            f"Redis {info.get('redis_version', '?')} answered in {int((time.time() - t0) * 1000)} ms.")
    except Exception as e:                                    # noqa: BLE001
        add("Redis", "down", f"Cannot reach the broker/cache: {e}",
            "Check the 'redis' container ( docker compose ps ).")

    # 2d — the MCP server. It is how agents reach this workspace, so it being down is invisible from
    # the dashboard's own traffic: nothing the web app does touches it.
    internal = settings.MCP_INTERNAL_URL.rstrip("/")
    public = settings.MCP_PUBLIC_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{internal}/healthz")
        if r.status_code == 200:
            add("MCP server", "ok", f"Answering - agents connect at {public}.")
        else:
            add("MCP server", "warn", f"{internal}/healthz returned {r.status_code}.",
                "Check the 'mcp' container ( docker compose logs mcp ).")
    except Exception as e:                                    # noqa: BLE001
        add("MCP server", "warn", f"Not reachable at {internal}: {e}",
            "Only agent integrations use it; the dashboard works without it.")

    # 3 — Celery worker (broker ping)
    try:
        from opencortyx.core.worker.celery_app import celery_app
        pong = await anyio.to_thread.run_sync(lambda: celery_app.control.ping(timeout=2.0))
        if pong:
            names = ", ".join(sorted(k for entry in pong for k in entry.keys()))
            add("Worker (Celery)", "ok", f"{len(pong)} worker(s) online: {names}.")
        else:
            add("Worker (Celery)", "down", "No workers answered the ping.",
                "Ingestion runs in the worker — start it with  docker compose restart worker  (then re-check).")
    except Exception as e:
        add("Worker (Celery)", "down", f"Cannot reach the broker: {e}",
            "Check that Redis (the broker) and the worker container are both running.")

    # The model server checked is the one this workspace's Model Registry selected for generation;
    # nothing else decides where requests go.
    from opencortyx.core.config import OPENAI_COMPATIBLE_PROVIDERS, SELF_HOSTED_PROVIDERS
    from opencortyx.core.inference.registry import resolve_model
    gen = await resolve_model(user.workspace_id, "generation") or {}
    provider = str(gen.get("provider") or "").lower()
    base = (gen.get("base_url") or "").rstrip("/")
    _model_server_name_ = _model_server_name(provider)
    if not gen:
        add("Model server", "warn", "No generation model is connected to this workspace, so the AI "
            "Assistant cannot answer yet. Uploads and search work without one.",
            "Add one on the Models page: a server you run (Ollama, LM Studio, vLLM, ... by its "
            "address, e.g.  http://host.docker.internal:11434/v1  for Ollama on this computer) or a "
            "hosted API with its key.")
    elif provider in OPENAI_COMPATIBLE_PROVIDERS:
        if not base and provider in SELF_HOSTED_PROVIDERS:
            add(_model_server_name_, "down",
                f"The generation model uses '{provider}' but has no Base URL, so requests would go "
                f"to the public OpenAI API and fail on auth.",
                "Set the generation model's Base URL on the Models page (keep the /v1 suffix).")
        elif base:
            want = set()
            for role in ("generation", "extraction", "utility"):
                spec = gen if role == "generation" else (await resolve_model(user.workspace_id, role) or {})
                if (spec.get("base_url") or "").rstrip("/") == base and spec.get("model_id"):
                    want.add(spec["model_id"])
            try:
                async with httpx.AsyncClient(timeout=8.0) as c:
                    r = await c.get(f"{base}/models")
                    r.raise_for_status()
                    served = {d.get("id") for d in (r.json().get("data") or [])
                              if isinstance(d, dict) and d.get("id")}
                served_str = ", ".join(sorted(served)) or "none"
                missing = sorted(want - served)
                if not served:
                    add(_model_server_name_, "warn",
                        f"Reachable at {base} but advertising no models.",
                        "It is probably still loading weights - a cold start downloads them. Re-check shortly.")
                elif missing:
                    add(_model_server_name_, "warn",
                        f"Reachable at {base} and serving: {served_str}. But this workspace requests "
                        f"{', '.join(missing)}, which it does NOT serve - those calls return 404.",
                        f"Set the Model ID on the Models page to one it serves, e.g.  {sorted(served)[0]} .")
                else:
                    add(_model_server_name_, "ok",
                        f"Reachable at {base} - serving: {served_str}.")
            except Exception as e:
                add(_model_server_name_, "down", f"Not reachable at {base}: {e}",
                    "Start your model server and check the address on the Models page. From inside "
                    "Docker, a server on this computer is  host.docker.internal , not  localhost .")

    # 5 — Live test embed (exercises the real embedding path end-to-end)
    try:
        from opencortyx.core.inference.embedding import embed_batch
        t1 = time.time()
        vecs = await embed_batch(["opencortyx system health probe"])
        dim = len(vecs[0]) if vecs and vecs[0] else 0
        if dim > 0:
            add("Embedding", "ok", f"Generated a {dim}-dim vector in {int((time.time() - t1) * 1000)} ms.")
        else:
            add("Embedding", "down", "The embedding call returned an empty vector.",
                "Check the embedding model on the Models page and that there is free memory.")
    except Exception as e:
        msg = str(e)
        oom = "memory" in msg.lower() or "oom" in msg.lower()
        add("Embedding", "down", f"Embedding failed: {msg}",
            ("Looks like an out-of-memory error - close other apps or give Docker more memory."
             if oom else
             "Check the embedding model on the Models page; with an HTTP embedder, check its Base "
             "URL is reachable."))

    overall = "ok"
    if any(c["status"] == "down" for c in checks):
        overall = "down"
    elif any(c["status"] == "warn" for c in checks):
        overall = "warn"
    return {"overall": overall, "checks": checks, "ts": __import__("datetime").datetime.utcnow().isoformat() + "Z"}
