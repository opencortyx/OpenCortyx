"""Routes: the model registry for generation, embeddings and reranking."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select

from opencortyx.api.schemas.system.models import (
    FastembedDownload, HFDownload, ModelDiscover, ModelSpec, OllamaPull,
)
from opencortyx.api.services.system.models import (
    clear_default,
    model_cache_clear,
    model_view,
    provider_default_base,
)
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.control import Model
from opencortyx.core.inference.registry import MODEL_ROLES, resolve_model
from opencortyx.core.security.secrets import encrypt_secret

router = APIRouter()


@router.get("/models")
async def list_models(role: Optional[str] = None, user: CurrentUser = Depends(require_auth)):
    async with V2Session() as db:
        q = select(Model).where(Model.workspace_id == user.workspace_id)
        if role:
            q = q.where(Model.role == role)
        rows = (await db.execute(q.order_by(Model.role, Model.created_at.desc()))).scalars().all()
    return {"items": [model_view(m) for m in rows]}


@router.get("/models/active")
async def active_models(user: CurrentUser = Depends(require_auth)):
    """The model IN EFFECT per role right now — proof the registry drives the pipeline. Each role is
    served by its own default row, shares the generation model (extraction, utility), or is not
    connected; there is no other source."""
    out = []
    for role in MODEL_ROLES:
        spec = await resolve_model(user.workspace_id, role)
        if not spec:
            out.append({"role": role, "source": "none", "provider": None, "model_id": None,
                        "base_url": None, "name": None, "has_key": False,
                        "note": "Not connected - add a model for this role."})
            continue
        row = {"role": role, "source": "registry",
               "provider": spec.get("provider"), "model_id": spec.get("model_id"),
               "base_url": spec.get("base_url"), "name": spec.get("name"),
               "has_key": bool(spec.get("api_key"))}
        if spec.get("role") and spec.get("role") != role:
            row["shared_from"] = spec.get("role")
        if role == "rerank" and str(spec.get("provider") or "").lower() == "sentence-transformers":
            from opencortyx.core.inference.embedding import cross_encoder_installed
            if not cross_encoder_installed():
                # Registered is not the same as running: this image cannot load it, and every query
                # ranks without the cross-encoder. Said where the model is shown as "active".
                row["note"] = ("not installed in this image — searches rank without it. "
                               "Rebuild with INSTALL_RERANK=1 to use it.")
        out.append(row)
    return {"items": out}


@router.get("/models/runtime")
async def model_runtime(user: CurrentUser = Depends(require_auth)):
    """What each connected model server is ACTUALLY serving, and whether this workspace agrees.

    The question that matters is "is the server up, and is it serving the name my rows ask for?" — a
    server answers 404 to a model name it does not serve, and a healthy server serving a different
    name is indistinguishable, from the UI, from a broken model. Each row is reported as ok /
    mismatch / unreachable with the served names to choose from.
    """
    import httpx

    from opencortyx.core.config import OPENAI_COMPATIBLE_PROVIDERS

    async with V2Session() as db:
        rows = (await db.execute(select(Model).where(Model.workspace_id == user.workspace_id))).scalars().all()

    # Group the rows we must check by ENDPOINT, so one server is probed once however many roles
    # point at it — commonly one server answering every text role.
    by_url: dict = {}
    for m in rows:
        prov = (m.provider or "").lower()
        if prov not in OPENAI_COMPATIBLE_PROVIDERS:
            continue                     # in-process or hosted-SDK provider: nothing to probe here
        spec = await resolve_model(user.workspace_id, m.role, str(m.id)) or {}
        url = (spec.get("base_url") or "").rstrip("/")
        if url:
            by_url.setdefault(url, []).append((m, spec))

    gen = await resolve_model(user.workspace_id, "generation") or {}
    gen_url = (gen.get("base_url") or "").rstrip("/")

    servers = []
    for url, entries in by_url.items():
        served: list = []
        error = None
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(f"{url}/models")
                r.raise_for_status()
                served = sorted(d.get("id") for d in (r.json().get("data") or [])
                                if isinstance(d, dict) and d.get("id"))
        except Exception as e:
            error = str(e)[:200]

        models = []
        for m, spec in entries:
            if error:
                status, hint = "unreachable", "Start the model server, then refresh."
            elif spec.get("model_id") in served:
                status, hint = "ok", ""
            else:
                status = "mismatch"
                hint = (f"This server serves {served or 'nothing yet'}. Set this row's Model ID to "
                        f"one of those names.")
            models.append({"id": str(m.id), "name": m.name, "role": m.role,
                           "model_id": spec.get("model_id"),
                           "weights": (spec.get("params") or {}).get("weights"),
                           "status": status, "hint": hint})
        servers.append({"base_url": url, "reachable": error is None, "error": error,
                        "served_models": served, "models": models,
                        "is_generation": url == gen_url})

    return {"servers": servers,
            "note": ("A server lists the models it can serve. Set a role's Model ID to one of those "
                     "names; to add a model, pull or load it on the server itself.")}


def _check_role(role: str) -> None:
    if role not in MODEL_ROLES:
        raise HTTPException(status_code=422,
                            detail=f"'{role}' is not a model role — use one of {', '.join(MODEL_ROLES)}")


def _check_dimensions(spec: ModelSpec) -> None:
    """An embedder must make vectors the size the index holds, or nothing it embeds can be stored."""
    if spec.role != "embedding" or spec.provider.lower() != "fastembed":
        return
    from opencortyx.core.config import settings
    from opencortyx.core.inference.fastembed_models import describe
    d = describe("embedding", spec.model_id)
    dim = (d or {}).get("dim")
    if dim and dim != settings.EMBED_DIMENSIONS:
        raise HTTPException(status_code=400, detail=(
            f"{spec.model_id} makes {dim}-dimension vectors, and the index holds "
            f"{settings.EMBED_DIMENSIONS}. Pick a {settings.EMBED_DIMENSIONS}-dimension model, or set "
            f"EMBED_DIMENSIONS={dim} in .env, restart, and re-index."))


@router.post("/models")
async def create_model(spec: ModelSpec, user: CurrentUser = Depends(require_write)):
    if not spec.name.strip() or not spec.model_id.strip():
        raise HTTPException(status_code=400, detail="name and model_id are required")
    _check_role(spec.role)
    _check_dimensions(spec)
    async with V2Session() as db:
        if spec.is_default:
            await clear_default(db, user.workspace_id, spec.role)
        m = Model(workspace_id=user.workspace_id, name=spec.name.strip(), role=spec.role, provider=spec.provider,
                  model_id=spec.model_id.strip(), base_url=spec.base_url or None,
                  api_key=encrypt_secret(spec.api_key) if spec.api_key else None,
                  params=spec.params or {}, is_default=bool(spec.is_default))
        db.add(m)
        await db.commit()
        await db.refresh(m)
    model_cache_clear()
    return model_view(m)


@router.put("/models/{model_id}")
async def update_model(model_id: str, spec: ModelSpec, user: CurrentUser = Depends(require_write)):
    _check_role(spec.role)
    _check_dimensions(spec)
    async with V2Session() as db:
        m = await db.get(Model, uuid.UUID(model_id))
        if not m or m.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="model not found")
        m.name, m.role, m.provider = spec.name.strip() or m.name, spec.role, spec.provider
        m.model_id = spec.model_id.strip() or m.model_id
        m.base_url = spec.base_url or None
        m.params = spec.params or {}
        if spec.api_key:                                  # only overwrite the key when a new one is given
            m.api_key = encrypt_secret(spec.api_key)
        if spec.is_default and not m.is_default:
            await clear_default(db, user.workspace_id, m.role, keep_id=m.id)
        m.is_default = bool(spec.is_default)
        await db.commit()
        await db.refresh(m)
    model_cache_clear()
    return model_view(m)


@router.delete("/models/{model_id}")
async def delete_model(model_id: str, user: CurrentUser = Depends(require_write)):
    async with V2Session() as db:
        res = await db.execute(delete(Model).where(
            Model.id == uuid.UUID(model_id), Model.workspace_id == user.workspace_id))
        await db.commit()
    model_cache_clear()
    return {"status": "ok", "deleted": res.rowcount or 0}


@router.post("/models/{model_id}/test")
async def test_model(model_id: str, user: CurrentUser = Depends(require_auth)):
    """Probe a registered model (a tiny generation or embedding call) → clear ok/error for the UI."""
    from opencortyx.core.inference.registry import resolve_model
    spec = await resolve_model(user.workspace_id, "generation", model_id)   # role read from the row itself
    # resolve_model keys by role; fetch the row to honor its actual role
    async with V2Session() as db:
        m = await db.get(Model, uuid.UUID(model_id))
    if not m or m.workspace_id != user.workspace_id:
        raise HTTPException(status_code=404, detail="model not found")
    spec = await resolve_model(user.workspace_id, m.role, model_id)
    import anyio
    try:
        # One bound for the whole probe: an unreachable endpoint otherwise held the request open until
        # the client gave up, and the UI's "Test" button simply hung.
        with anyio.fail_after(MODEL_TEST_TIMEOUT_S):
            return await _probe(m, spec)
    except TimeoutError:
        raise HTTPException(status_code=400, detail=f"model test failed: no answer within {MODEL_TEST_TIMEOUT_S} s "
                                                    f"— check the endpoint is reachable")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"model test failed: {str(e)[:240]}")


MODEL_TEST_TIMEOUT_S = 30


async def _probe(m, spec) -> dict:
    """The smallest real call for the model's role."""
    if m.role == "embedding":
        from opencortyx.core.inference.embedding import embed_batch
        vecs = await embed_batch(["ping"], model_spec=spec)
        dims = len(vecs[0]) if vecs and vecs[0] else 0
        if not dims:                    # a model that returns no vector is not a working embedder
            return {"ok": False, "role": m.role, "dimensions": 0,
                    "error": "the model returned no embedding vector"}
        return {"ok": True, "role": m.role, "dimensions": dims}
    if m.role == "rerank":
        from opencortyx.retrieval.l3_rerank import cascade as _rr

        class _C:                       # the shape the rerankers score: .text and .score
            def __init__(self, t):
                self.text, self.score, self.meta, self.valid_from = t, 0.0, {}, None
        pair = [_C("The capital of France is Paris."), _C("Bananas are yellow.")]
        prov = str(spec.get("provider", "")).lower()
        if prov in _rr.HTTP_RERANK_PROVIDERS and spec.get("base_url"):
            got = await _rr.http_rerank("What is the capital of France?", pair, 2, spec)
            if got is None:
                return {"ok": False, "role": m.role,
                        "error": f"no usable response from {spec.get('base_url')} — check the "
                                 f"URL serves a /rerank endpoint and the key is valid"}
            return {"ok": True, "role": m.role, "sample": f"scored {len(got)} candidates"}
        import anyio
        from opencortyx.core.inference.embedding import get_cross_encoder
        model = None
        try:
            with anyio.fail_after(20):
                model = await anyio.to_thread.run_sync(
                    lambda: get_cross_encoder(spec.get("model_id"), prov))
        except TimeoutError:
            return {"ok": True, "role": m.role, "pending": True,
                    "sample": "downloading weights — this is the first load and continues in "
                              "the background. Test again shortly."}
        if model is None and prov == "fastembed":
            return {"ok": False, "role": m.role,
                    "error": f"{spec.get('model_id')} could not be loaded: it is not a fastembed "
                             "reranker, or its download failed. Pick one from the list and press "
                             "Download."}
        if model is None:
            return {"ok": False, "role": m.role,
                    "error": "sentence-transformers is not installed in this image. Rebuild "
                             "with INSTALL_RERANK=1 to use it. Until then, retrieval ranks "
                             "without the cross-encoder."}
        return {"ok": True, "role": m.role, "sample": "cross-encoder loaded"}
    from opencortyx.core.inference.llm import complete
    out = await complete("Reply with the single word: OK", max_tokens=8, temperature=0.0, model_spec=spec)
    return {"ok": True, "role": m.role, "sample": (out or "").strip()[:80]}


@router.post("/models/discover")
async def discover_models(body: ModelDiscover, user: CurrentUser = Depends(require_auth)):
    """List the models a provider serves RIGHT NOW, from `GET /v1/models` — a server you run (Ollama,
    LM Studio, vLLM, ...) or a hosted endpoint's catalogue (OpenAI, Groq, Together, Azure).

    This is what powers the Add-model picker: a server's model names are its own, and typing one by
    hand is the most common way to end up with a row that 404s on every call. Never raises — returns
    {items, source, note} with a plain-English note when the provider can't be reached."""
    import httpx

    from opencortyx.core.security.secrets import decrypt_secret

    provider = (body.provider or "openai_compatible").lower()
    base = (body.base_url or "").rstrip("/")
    api_key = body.api_key or None
    # editing an existing model → reuse its stored base/key when the form didn't re-supply them
    if body.model_id:
        try:
            async with V2Session() as db:
                m = await db.get(Model, uuid.UUID(body.model_id))
            if m and m.workspace_id == user.workspace_id:
                base = base or (m.base_url or "").rstrip("/")
                if not api_key and m.api_key:
                    api_key = decrypt_secret(m.api_key)
        except Exception:
            pass

    if provider == "fastembed":                         # every model fastembed can run, by role
        import anyio

        from opencortyx.core.inference import fastembed_models
        role = "rerank" if body.role == "rerank" else "embedding"
        items = await anyio.to_thread.run_sync(fastembed_models.catalogue, role)
        return {"items": items, "source": "fastembed", "provider": provider, "note": None}

    if provider == "transformers":                      # what is downloaded into the model cache
        import anyio

        from opencortyx.core.inference import hf_hub
        items = await anyio.to_thread.run_sync(hf_hub.downloaded)
        return {"items": items, "source": "model cache", "provider": provider,
                "note": None if items else ("Nothing downloaded yet. Type a Hugging Face model as "
                                            "the Model ID and press Download.")}

    try:
        if provider in ("openai", "openai_compatible", "groq", "together", "azure", "huggingface"):
            b = (base or provider_default_base(provider)).rstrip("/")
            if not b:
                return {"items": [], "source": b, "provider": provider,
                        "note": "Enter the server's address (e.g. http://host.docker.internal:11434/v1 "
                                "for Ollama on this computer), then Refresh to list its models."}
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            async with httpx.AsyncClient(timeout=12.0) as c:
                r = await c.get(f"{b}/models", headers=headers)
                r.raise_for_status()
                payload = r.json()
            data = payload.get("data") or payload.get("models") or []
            items = [{"id": (d.get("id") or d.get("name")), "detail": d.get("owned_by")}
                     for d in data if isinstance(d, dict) and (d.get("id") or d.get("name"))]
            items.sort(key=lambda x: x["id"] or "")
            note = None
            if not items:
                note = ("Endpoint reachable but advertised no models — the server may still be "
                        "loading one, or none is pulled yet.")
            return {"items": items, "source": b, "provider": provider, "note": note}
    except Exception as e:
        return {"items": [], "source": base, "provider": provider,
                "note": f"Couldn't reach {provider}: {str(e)[:160]}"}

    # anthropic / huggingface / fastembed / sentence-transformers / rerank servers:
    # no universal listing endpoint.
    return {"items": [], "source": base, "provider": provider,
            "note": "This provider has no model-list endpoint — enter the model id manually."}


# ── Downloading models ────────────────────────────────────────────────────────────────────────────
# Two ways to run a Hugging Face model on this machine: in OpenCortyx itself (CPU, `transformers`,
# downloaded into the shared model cache), or in an Ollama server the user runs (fast, GPU), which
# pulls GGUF repositories straight from the Hub. OpenCortyx still ships no model server.

def _hf_error(e: Exception) -> HTTPException:
    if isinstance(e, ValueError):
        return HTTPException(status_code=400, detail=str(e))
    return HTTPException(status_code=502, detail=f"Couldn't reach Hugging Face: {str(e)[:200]}")


@router.post("/models/hf/info")
async def hf_model_info(body: HFDownload, user: CurrentUser = Depends(require_write)):
    """Size, type and whether `repo` can run in OpenCortyx, before anything is downloaded."""
    import anyio

    from opencortyx.core.inference import hf_hub
    try:
        return await anyio.to_thread.run_sync(hf_hub.info, body.repo.strip(), body.token)
    except Exception as e:
        raise _hf_error(e)


@router.post("/models/hf/download")
async def hf_download(body: HFDownload, user: CurrentUser = Depends(require_write)):
    """Start downloading a model into the model cache; poll GET /models/hf/download for progress."""
    import anyio

    from opencortyx.core.inference import hf_hub
    try:
        return await anyio.to_thread.run_sync(hf_hub.start, body.repo.strip(), body.token)
    except Exception as e:
        raise _hf_error(e)


@router.get("/models/hf/download")
async def hf_download_status(repo: str, user: CurrentUser = Depends(require_auth)):
    import anyio

    from opencortyx.core.inference import hf_hub
    return await anyio.to_thread.run_sync(hf_hub.status, repo.strip())


@router.get("/models/hf/local")
async def hf_local_models(user: CurrentUser = Depends(require_auth)):
    """The text-generation models downloaded into the model cache."""
    import anyio

    from opencortyx.core.inference import hf_hub
    return {"items": await anyio.to_thread.run_sync(hf_hub.downloaded)}


@router.delete("/models/hf/local")
async def hf_delete_local(repo: str, user: CurrentUser = Depends(require_write)):
    """Free the disk a downloaded model uses. A registry row that names it stops working."""
    import anyio

    from opencortyx.core.inference import hf_hub
    if not await anyio.to_thread.run_sync(hf_hub.delete, repo.strip()):
        raise HTTPException(status_code=404, detail=f"'{repo}' is not downloaded")
    return {"status": "ok", "deleted": repo}


@router.post("/models/fastembed/download")
async def fastembed_download(body: FastembedDownload, user: CurrentUser = Depends(require_write)):
    """Download a fastembed embedder or reranker now, rather than on its first use."""
    import anyio

    from opencortyx.core.inference import fastembed_models
    try:
        return await anyio.to_thread.run_sync(fastembed_models.start, body.role, body.model.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/models/fastembed/download")
async def fastembed_download_status(model: str, role: str = "embedding",
                                    user: CurrentUser = Depends(require_auth)):
    import anyio

    from opencortyx.core.inference import fastembed_models
    return await anyio.to_thread.run_sync(fastembed_models.status, role, model.strip())


def _ollama_name(model: str) -> str:
    """Ollama's name for a model: a Hugging Face repository is `hf.co/<repo>[:quant]`."""
    name = model.strip()
    for prefix in ("https://huggingface.co/", "huggingface.co/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    if "/" in name and not name.startswith("hf.co/"):
        name = "hf.co/" + name
    return name


@router.post("/models/ollama/pull")
async def ollama_pull(body: OllamaPull, user: CurrentUser = Depends(require_write)):
    """Have an Ollama server download a model, streaming its progress as NDJSON lines.

    Ollama pulls GGUF repositories from Hugging Face itself (`hf.co/<repo>`), so the weights go
    straight to the server that runs them. If the page closes, Ollama stops; pulling again resumes.
    """
    import json

    import httpx
    from fastapi.responses import StreamingResponse

    root = body.base_url.strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    name = _ollama_name(body.model)
    if not name:
        raise HTTPException(status_code=400, detail="enter a model to download")
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            (await c.get(f"{root}/api/version")).raise_for_status()
    except Exception:
        raise HTTPException(status_code=400, detail=(
            f"No Ollama server answers at {root}. Downloading works with Ollama; LM Studio and vLLM "
            "load models themselves. Ollama on this computer: http://host.docker.internal:11434/v1"))

    async def progress():
        yield json.dumps({"model": name, "status": "starting"}) + "\n"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as c:
                async with c.stream("POST", f"{root}/api/pull",
                                    json={"model": name, "stream": True}) as r:
                    if r.status_code >= 400:
                        text = (await r.aread()).decode(errors="replace")
                        yield json.dumps({"error": text[:300] or f"HTTP {r.status_code}"}) + "\n"
                        return
                    async for line in r.aiter_lines():
                        if line.strip():
                            yield line + "\n"
        except Exception as e:                          # noqa: BLE001 — reported in the stream
            yield json.dumps({"error": f"The download stopped: {str(e)[:200]}"}) + "\n"

    return StreamingResponse(progress(), media_type="application/x-ndjson")
