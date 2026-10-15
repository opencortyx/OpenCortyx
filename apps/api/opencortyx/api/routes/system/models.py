"""Routes: the model registry for generation, embeddings and reranking."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select

from opencortyx.api.schemas.system.models import ModelDiscover, ModelSpec
from opencortyx.api.services.system.models import (
    clear_default,
    model_cache_clear,
    model_view,
    provider_default_base,
)
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.control import Model
from opencortyx.core.inference.registry import MODEL_ROLES, ensure_default_models, resolve_model
from opencortyx.core.security.secrets import encrypt_secret

router = APIRouter()


@router.get("/models")
async def list_models(role: Optional[str] = None, user: CurrentUser = Depends(require_auth)):
    from opencortyx.core.inference.registry import ensure_default_models
    await ensure_default_models(user.workspace_id)        # self-heal: existing orgs get the built-in defaults
    async with V2Session() as db:
        q = select(Model).where(Model.workspace_id == user.workspace_id)
        if role:
            q = q.where(Model.role == role)
        rows = (await db.execute(q.order_by(Model.role, Model.created_at.desc()))).scalars().all()
    return {"items": [model_view(m) for m in rows]}


@router.get("/models/active")
async def active_models(user: CurrentUser = Depends(require_auth)):
    """The model ACTUALLY IN EFFECT per wired role right now — proof the registry is connected to the
    pipeline. Resolution is: a registered model marked default for the role → else the env/built-in
    default. The UI shows this so you can see what's running and that assigning a default takes over."""
    from opencortyx.core.config import CROSS_ENCODER_MODEL

    await ensure_default_models(user.workspace_id)        # self-heal: existing orgs get the built-in defaults
    out = []
    for role in MODEL_ROLES:
        spec = await resolve_model(user.workspace_id, role)
        registered = bool(spec.get("id"))
        if role == "rerank" and not registered:
            # the built-in default reranker is a LOCAL cross-encoder, not the LLM env fallback
            out.append({"role": role, "source": "default", "provider": "sentence-transformers (local)",
                        "model_id": CROSS_ENCODER_MODEL, "base_url": None, "name": None, "has_key": False,
                        "note": "Local cross-encoder; LLM rerank used as fallback if unavailable."})
            continue
        row = {"role": role, "source": "registry" if registered else "env",
               "provider": spec.get("provider"), "model_id": spec.get("model_id"),
               "base_url": spec.get("base_url"), "name": spec.get("name"),
               "has_key": bool(spec.get("api_key"))}
        if role == "rerank" and str(spec.get("provider") or "").lower() == "sentence-transformers":
            from opencortyx.core.inference.embedding import cross_encoder_installed
            if not cross_encoder_installed():
                # Registered is not the same as running: this image cannot load it, and every query
                # is reranked by the LLM instead. Said where the model is shown as "active".
                row["note"] = ("not installed in this image — searches use the LLM reranker. "
                               "Rebuild with INSTALL_RERANK=1 or point this role at an HTTP reranker.")
        out.append(row)
    return {"items": out}


@router.get("/models/runtime")
async def model_runtime(user: CurrentUser = Depends(require_auth)):
    """What the inference server is ACTUALLY running, and whether this workspace agrees with it.

    This replaces the old Ollama "pull a model" endpoint, which has no vLLM equivalent and could not
    be made to have one: vLLM loads exactly one model, chosen by the flags it was STARTED with, and
    holds it pinned for the process lifetime. There is no runtime download to trigger, so a Download
    button here could only ever fail.

    What is genuinely needed instead is the answer to "is the server up, and is it serving the name
    my rows ask for?" — because vLLM answers 404 to every name that is not its `--served-model-name`,
    and a healthy server serving the wrong alias is indistinguishable, from the UI, from a broken
    model. Each row is reported as ok / mismatch / unreachable with the served names to choose from.
    """
    import httpx

    from opencortyx.core.config import OPENAI_COMPATIBLE_PROVIDERS

    await ensure_default_models(user.workspace_id)
    async with V2Session() as db:
        rows = (await db.execute(select(Model).where(Model.workspace_id == user.workspace_id))).scalars().all()

    # Group the rows we must check by ENDPOINT, so one server is probed once however many roles
    # point at it — the single-GPU default, where every text role shares one vLLM.
    by_url: dict = {}
    for m in rows:
        prov = (m.provider or "").lower()
        if prov not in OPENAI_COMPATIBLE_PROVIDERS:
            continue                     # in-process or hosted-SDK provider: nothing to probe here
        spec = await resolve_model(user.workspace_id, m.role, str(m.id))
        url = (spec.get("base_url") or "").rstrip("/")
        if url:
            by_url.setdefault(url, []).append((m, spec))

    # Always include the generation model's server, even with no rows pointing at it — an empty
    # registry must not read as "nothing to report" when the whole stack depends on that endpoint.
    gen = await resolve_model(user.workspace_id, "generation")
    gen_provider = str(gen.get("provider") or "").lower()
    env_url = (gen.get("base_url") or "").rstrip("/")
    if env_url and env_url not in by_url and gen_provider in OPENAI_COMPATIBLE_PROVIDERS:
        by_url[env_url] = []

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
                        f"one of those names"
                        + (f", or restart the server with --served-model-name {spec.get('model_id')}."
                           if str(spec.get("provider") or "").lower() == "vllm" else "."))
            models.append({"id": str(m.id), "name": m.name, "role": m.role,
                           "model_id": spec.get("model_id"),
                           "weights": (spec.get("params") or {}).get("weights"),
                           "status": status, "hint": hint})
        servers.append({"base_url": url, "reachable": error is None, "error": error,
                        "served_models": served, "models": models,
                        "is_env_default": url == env_url})

    return {"servers": servers, "note": _server_note(gen_provider)}


def _check_role(role: str) -> None:
    if role not in MODEL_ROLES:
        raise HTTPException(status_code=422,
                            detail=f"'{role}' is not a model role — use one of {', '.join(MODEL_ROLES)}")


def _server_note(provider: str) -> str:
    """How models get onto THIS deployment's server — vLLM and a multi-model server differ."""
    if provider == "vllm":
        return ("vLLM serves one model per process, loaded at startup. To change which model runs, "
                "change the server's --model / --served-model-name and restart it — there is "
                "nothing to download from here.")
    return ("This server lists every model it can serve. Set a role's Model ID to one of the names "
            "above; to add a model, pull it on the server itself.")


@router.post("/models")
async def create_model(spec: ModelSpec, user: CurrentUser = Depends(require_write)):
    if not spec.name.strip() or not spec.model_id.strip():
        raise HTTPException(status_code=400, detail="name and model_id are required")
    _check_role(spec.role)
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
                    lambda: get_cross_encoder(spec.get("model_id")))
        except TimeoutError:
            return {"ok": True, "role": m.role, "pending": True,
                    "sample": "downloading weights — this is the first load and continues in "
                              "the background. Test again shortly."}
        if model is None:
            return {"ok": False, "role": m.role,
                    "error": "sentence-transformers is not installed in this image. Rebuild "
                             "with INSTALL_RERANK=1, or point this role at an HTTP reranker "
                             "(TEI / Infinity / Cohere / Jina). Retrieval currently falls back "
                             "to the LLM reranker."}
        return {"ok": True, "role": m.role, "sample": "cross-encoder loaded"}
    from opencortyx.core.inference.llm import complete
    out = await complete("Reply with the single word: OK", max_tokens=8, temperature=0.0, model_spec=spec)
    return {"ok": True, "role": m.role, "sample": (out or "").strip()[:80]}


@router.post("/models/discover")
async def discover_models(body: ModelDiscover, user: CurrentUser = Depends(require_auth)):
    """List the models a provider serves RIGHT NOW, from `GET /v1/models` — the vLLM server's own
    `--served-model-name`(s), or a hosted endpoint's catalogue (OpenAI, Groq, Together, Azure).

    This is what powers the Add-model picker, and for vLLM it is more than a convenience: the served
    name is an alias chosen at server start, so it is genuinely unguessable from the outside, and
    typing it by hand is the single most common way to end up with a row that 404s on every call.
    Picking from this list makes that class of mistake unreachable. Never raises — returns
    {items, source, note} with a plain-English note when the provider can't be reached."""
    import httpx

    from opencortyx.core.security.secrets import decrypt_secret

    provider = (body.provider or "vllm").lower()
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

    try:
        if provider in ("vllm", "openai", "openai_compatible", "groq", "together", "azure"):
            b = (base or provider_default_base(provider)).rstrip("/")
            if not b:
                return {"items": [], "source": b, "provider": provider,
                        "note": "Enter this provider's Base URL, then Refresh to list its models."}
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
                note = ("Endpoint reachable but advertised no models — for vLLM this usually means "
                        "it is still loading weights; its /health goes ready only after the load.")
            return {"items": items, "source": b, "provider": provider, "note": note}
    except Exception as e:
        return {"items": [], "source": base, "provider": provider,
                "note": f"Couldn't reach {provider}: {str(e)[:160]}"}

    # anthropic / huggingface / fastembed / sentence-transformers / rerank servers:
    # no universal listing endpoint.
    return {"items": [], "source": base, "provider": provider,
            "note": "This provider has no model-list endpoint — enter the model id manually."}
