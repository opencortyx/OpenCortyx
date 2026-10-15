"""Model registry — resolve a model REFERENCE (a registered Model id/name, or nothing) to a concrete
spec the provider dispatchers in `llm.py` / `embedding.py` can use.

A spec is: {"provider","model_id","base_url","api_key","params"}. Resolution order:
  1. explicit `ref` (a Model row id or name, scoped to org + role)
  2. the org's default Model for that role (`is_default`)
  3. env fallback (settings) — so behaviour is unchanged until a model is registered.

The api_key is decrypted here (stored encrypted). Resolutions are TTL-cached so the hot extraction
path doesn't hit the DB per call.
"""
import uuid

from opencortyx.core.config import settings, OPENAI_COMPATIBLE_PROVIDERS
from opencortyx.core.cache import TTLCache
from opencortyx.core.security.secrets import decrypt_secret
from opencortyx.core.db import V2Session
from opencortyx.domain import Model

_cache = TTLCache(maxsize=256, ttl=60)


def _env_spec(role: str) -> dict:
    """Fallback to the env-configured provider (current behaviour) when nothing is registered."""
    if role == "embedding":
        prov: str = settings.EMBED_PROVIDER or "fastembed"
        return {"provider": prov, "model_id": settings.EMBED_MODEL,
                "base_url": _base_url_for(prov, embedding=True),
                "api_key": settings.LLM_API_KEY, "params": {}}
    # generation / rerank
    prov = settings.LLM_PROVIDER or "vllm"
    return {"provider": prov, "model_id": settings.LLM_MODEL,
            "base_url": _base_url_for(prov),
            "api_key": settings.LLM_API_KEY, "params": _weights_params(settings.LLM_MODEL_WEIGHTS)}


#: The roles something actually resolves a model for. A role outside this list would be a setting
#: nothing reads — registering a "vision" or "ocr" model changed nothing — so it is refused.
MODEL_ROLES = ("generation", "embedding", "rerank", "extraction", "utility")

_RETIRED_PROVIDERS = {"ollama"}


#: Ollama tag -> the repo id the in-process embedder resolves. Same weights, same 768 dimensions,
#: different naming convention — so a migrated row does not name a model its new provider cannot load.
_RETIRED_MODEL_IDS = {
    "nomic-embed-text": "nomic-ai/nomic-embed-text-v1.5",
    "nomic-embed-text:latest": "nomic-ai/nomic-embed-text-v1.5",
}


def _coerce_provider(prov: str, role: str) -> str:
    if (prov or "").lower() not in _RETIRED_PROVIDERS:
        return prov
    return "fastembed" if role == "embedding" else (settings.LLM_PROVIDER or "vllm")


def coerce_legacy_row(provider: str, model_id: str, base_url: str | None, role: str) -> tuple:
    """Normalise one persisted registry row to the current provider vocabulary.

    ONE implementation, called by both readers of the `model` table (`_row_to_spec` here and
    `embedding.active_embedding_spec`). They previously each carried their own copy of the provider
    list and had already drifted once — with the result that a row generated text against one server
    and embedded against another. Returns (provider, model_id, base_url).
    """
    retired = (provider or "").lower() in _RETIRED_PROVIDERS
    prov = _coerce_provider(provider or settings.LLM_PROVIDER or "vllm", role)
    mid = model_id
    if prov in ("fastembed", "onnx"):
        mid = _RETIRED_MODEL_IDS.get(mid, mid)
    # A stale endpoint is as wrong as a stale provider: a coerced row that kept Ollama's port would
    # send vLLM-bound traffic to a port nothing listens on. None means "resolve from the provider".
    # Only a RETIRED row is coerced — an openai_compatible row registered against Ollama's
    # OpenAI-compatible API names its endpoint on purpose, and that URL is the only place it lives.
    base = base_url or None
    if retired and base and ":11434" in base:
        base = None
    return prov, mid, base


def _row_to_spec(m) -> dict:
    prov, mid, base = coerce_legacy_row(m.provider, m.model_id, m.base_url, m.role)
    return {"provider": prov, "model_id": mid,
            "base_url": (base or _base_url_for(prov, embedding=(m.role == "embedding"))),
            "api_key": decrypt_secret(m.api_key) if m.api_key else settings.LLM_API_KEY,
            "params": m.params or {}, "id": str(m.id), "name": m.name, "role": m.role}


def _weights_params(weights: str | None, provider: str | None = None) -> dict:
    """Record the WEIGHTS behind a served-model alias, so the registry is not blind to what the
    server actually loaded.

    vLLM is called with `--served-model-name`, which is an alias chosen by whoever wrote the compose
    file — "opencortyx-llm" says nothing about which repo was downloaded. Without this the Models page can
    display a row and still be unable to answer "which model is this, really". Empty for providers
    whose model_id already IS the weights (a HuggingFace repo, an OpenAI model name).
    """
    w = (weights or "").strip()
    if (provider or settings.LLM_PROVIDER or "vllm").lower() != "vllm":
        return {}          # the model id IS the weights; a vLLM repo label here named the wrong model
    return {"weights": w} if w else {}


def _fast_params() -> dict:
    """Weights for the extraction/utility rows.

    Falls back to the MAIN model's weights when no separate fast model is configured, because in
    that case the fast roles are not a different model — they resolve to the same alias on the same
    server. Reporting weights for `generation` and nothing for `extraction` would suggest two
    models where there is one.
    """
    if not (settings.FAST_MODEL or "").strip() and not (settings.FAST_MODEL_WEIGHTS or "").strip():
        return _weights_params(settings.LLM_MODEL_WEIGHTS)
    return _weights_params(settings.FAST_MODEL_WEIGHTS,
                           settings.FAST_MODEL_PROVIDER or settings.LLM_PROVIDER)


#: Providers that run IN THIS PROCESS — no endpoint exists, and inventing one is a bug, because a
#: base_url on such a row makes the Models page claim a server that is not there.
IN_PROCESS_PROVIDERS = frozenset({
    "fastembed", "onnx", "sentence-transformers", "sentence_transformers", "local", "st",
})
#: Every provider something here can call. A registered model with any other provider could never run.
KNOWN_PROVIDERS = OPENAI_COMPATIBLE_PROVIDERS | IN_PROCESS_PROVIDERS | {"anthropic", "huggingface", "hf", "tei"}


def _base_url_for(provider: str, *, embedding: bool = False) -> str | None:
    """The endpoint a provider should be registered with.

    Resolved from the provider FAMILY, never from one global default: an OpenAI-compatible server
    (vLLM et al.) uses its own configured base URL, in-process embedders have none, and hosted APIs
    use their SDK default (None). Getting this wrong is silent — the row displays the provider you
    chose while its requests go somewhere else.
    """
    p = (provider or settings.LLM_PROVIDER or "vllm").lower()
    if p in IN_PROCESS_PROVIDERS:
        return None
    if p in OPENAI_COMPATIBLE_PROVIDERS:
        explicit = settings.EMBED_BASE_URL if embedding else settings.LLM_BASE_URL
        # Fall back to the other role's URL: a single vLLM deployment commonly fronts both.
        return explicit or settings.LLM_BASE_URL or settings.EMBED_BASE_URL or None
    return None


def _default_model_rows() -> list[dict]:
    """The built-in on-prem defaults registered for every workspace, so the Model Registry shows what's
    actually running and users can swap any of them per use case. Mirrors the env/config defaults."""
    from opencortyx.core.config import CROSS_ENCODER_MODEL, GENERATION_MODEL
    llm_provider = settings.LLM_PROVIDER or "vllm"
    embed_provider = settings.EMBED_PROVIDER or "fastembed"
    embed_model = settings.EMBED_MODEL or "nomic-ai/nomic-embed-text-v1.5"
    llm_url = _base_url_for(llm_provider)
    embed_url = _base_url_for(embed_provider, embedding=True)
    # The fast-model roles may point at their own server (see config.FAST_MODEL_*). Each field falls
    # back independently, so setting only the provider still resolves a sensible model and URL.
    fast_provider = settings.FAST_MODEL_PROVIDER or llm_provider
    fast_model = settings.FAST_MODEL or settings.LLM_MODEL
    fast_url = settings.FAST_MODEL_BASE_URL or (
        llm_url if fast_provider == llm_provider else _base_url_for(fast_provider))
    gen_weights = _weights_params(settings.LLM_MODEL_WEIGHTS)
    gen_label = gen_weights.get("weights") or GENERATION_MODEL
    return [
        {"role": "generation", "name": f"{gen_label} (built-in)",
         "provider": llm_provider, "model_id": GENERATION_MODEL,
         "base_url": llm_url, "params": gen_weights},
        {"role": "embedding", "name": f"{embed_model} (built-in {embed_provider})",
         "provider": embed_provider, "model_id": embed_model,
         "base_url": embed_url, "params": {}},
        {"role": "rerank", "name": f"{CROSS_ENCODER_MODEL.split('/')[-1]} (built-in)",
         "provider": "sentence-transformers", "model_id": CROSS_ENCODER_MODEL,
         "base_url": None, "params": {}},
        {"role": "extraction", "name": f"{fast_model} (built-in)",
         "provider": fast_provider, "model_id": fast_model,
         "base_url": fast_url, "params": _fast_params()},
        {"role": "utility", "name": f"{fast_model} (built-in)",
         "provider": fast_provider, "model_id": fast_model,
         "base_url": fast_url, "params": _fast_params()},
    ]


async def ensure_default_models(workspace_id) -> int:
    """Idempotently register the built-in on-prem defaults (generation/embedding/rerank) for an org if
    that role has no model yet. Self-healing: existing workspaces get them on next registry read. Returns
    how many rows were created.

    The env/config defaults only SEED a role that has no row. Once a row exists, the registry is the
    source of truth and is never rewritten from env — the Models page is where a model is chosen, and
    a row that silently reverted to whatever the environment said would undo that choice."""
    from sqlalchemy import select
    created = 0
    try:
        async with V2Session() as db:
            rows = (await db.execute(select(Model).where(Model.workspace_id == workspace_id))).scalars().all()
            have = {r.role for r in rows}
            for row in _default_model_rows():
                if row["role"] in have:
                    continue
                db.add(Model(workspace_id=workspace_id, name=row["name"], role=row["role"], provider=row["provider"],
                             model_id=row["model_id"], base_url=row["base_url"], params=row["params"],
                             is_default=True))
                created += 1
            if created:
                await db.commit()
    except Exception:
        pass
    if created:
        _cache.clear()
    return created


async def resolve_model(workspace_id, role: str = "generation", ref: str | None = None) -> dict:
    """Resolve a model spec for (org, role, optional ref). Never raises — falls back to env."""
    ck = f"{workspace_id}:{role}:{ref or '*'}"
    hit = _cache.get(ck)
    if hit is not None:
        return hit
    spec = _env_spec(role)
    try:
        from sqlalchemy import select
        async with V2Session() as db:
            row = None
            if ref:
                try:
                    row = await db.get(Model, uuid.UUID(str(ref)))
                except Exception:
                    row = None
                if row is None or row.workspace_id != workspace_id:        # try by name within org+role
                    row = (await db.execute(select(Model).where(
                        Model.workspace_id == workspace_id, Model.role == role, Model.name == ref))).scalar_one_or_none()
            if row is None:                                     # org default for this role
                row = (await db.execute(select(Model).where(
                    Model.workspace_id == workspace_id, Model.role == role,
                    Model.is_default == True))).scalar_one_or_none()   # noqa: E712
            if row is None and role == "extraction":
                row = (await db.execute(select(Model).where(
                    Model.workspace_id == workspace_id, Model.role == "generation",
                    Model.is_default == True))).scalar_one_or_none()   # noqa: E712
            if row is not None and (row.workspace_id == workspace_id):
                spec = _row_to_spec(row)
    except Exception:
        pass
    _cache.set(ck, spec)
    return spec


async def active_generation_spec() -> dict | None:
    """The generation model selected in the Model Registry (the is_default generation row), for calls
    that carry no org — the counterpart of `embedding.active_embedding_spec`. Without it those calls
    went to the env provider while everything else followed the Models page. None when nothing is
    registered (→ env fallback)."""
    hit = _cache.get("__active__:generation")
    if hit is not None:
        return hit or None
    spec: dict = {}
    try:
        from sqlalchemy import select
        async with V2Session() as db:
            row = (await db.execute(select(Model).where(
                Model.role == "generation", Model.is_default == True)   # noqa: E712
                .order_by(Model.created_at))).scalars().first()
        if row is not None and row.model_id:
            spec = _row_to_spec(row)
    except Exception:
        spec = {}
    _cache.set("__active__:generation", spec)
    return spec or None
