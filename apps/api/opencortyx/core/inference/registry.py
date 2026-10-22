"""Model registry — the ONLY place a model comes from.

A spec is: {"provider","model_id","base_url","api_key","params"}, built from one row of a workspace's
registry (the Models page). Resolution order:
  1. explicit `ref` (a Model row id or name, scoped to the workspace + role)
  2. the workspace's default Model for that role (`is_default`); extraction and utility use the
     workspace's generation model when they have none of their own.

There is nothing behind that: no `.env` setting, no bundled model server, no other workspace's row.
A role the registry does not serve resolves to None, and the call that needed it fails with
`NoModelConfigured`, which says what to add.

The api_key is decrypted here (stored encrypted). Resolutions are TTL-cached so the hot extraction
path doesn't hit the DB per call.
"""
import uuid

from opencortyx.core.config import (
    BUILTIN_EMBED_MODEL, BUILTIN_EMBED_PROVIDER, BUILTIN_RERANK_MODEL, BUILTIN_RERANK_PROVIDER,
    LOCAL_LLM_PROVIDERS,
    OPENAI_COMPATIBLE_PROVIDERS,
)
from opencortyx.core.cache import TTLCache
from opencortyx.core.security.secrets import decrypt_secret
from opencortyx.core.db import V2Session
from opencortyx.domain import Model

_cache = TTLCache(maxsize=256, ttl=60)


class NoModelConfigured(RuntimeError):
    """A call needed a model the workspace's registry does not provide."""

    def __init__(self, role: str = "generation", detail: str = ""):
        self.role = role
        super().__init__(detail or (
            f"No {role} model is connected to this workspace. Add one on the Models page: a server you "
            "run (Ollama, LM Studio, vLLM, ... - any OpenAI-compatible address) or a hosted API."))


#: The roles something actually resolves a model for. A role outside this list would be a setting
#: nothing reads — registering a "vision" or "ocr" model changed nothing — so it is refused.
MODEL_ROLES = ("generation", "embedding", "rerank", "extraction", "utility")

#: Roles served by the workspace's generation model when they have no row of their own.
_SHARES_GENERATION = ("extraction", "utility")


def _row_to_spec(m) -> dict:
    return {"provider": (m.provider or "").lower(), "model_id": m.model_id,
            "base_url": m.base_url or None,
            "api_key": decrypt_secret(m.api_key) if m.api_key else None,
            "params": m.params or {}, "id": str(m.id), "name": m.name, "role": m.role}


#: Providers that run IN THIS PROCESS — no endpoint exists, and inventing one is a bug, because a
#: base_url on such a row makes the Models page claim a server that is not there.
IN_PROCESS_PROVIDERS = frozenset({
    "fastembed", "onnx", "sentence-transformers", "sentence_transformers", "local", "st",
}) | LOCAL_LLM_PROVIDERS
#: Every provider something here can call. A registered model with any other provider could never run.
KNOWN_PROVIDERS = OPENAI_COMPATIBLE_PROVIDERS | IN_PROCESS_PROVIDERS | {"anthropic", "huggingface", "hf"}


def _builtin_rows() -> list[dict]:
    """What a new workspace's registry starts with: the two models that run inside the API itself, as
    ordinary rows the Models page shows and can change or delete. No generation model — that is a
    server the workspace connects, by its address."""
    return [
        {"role": "embedding", "name": BUILTIN_EMBED_MODEL.split("/")[-1],
         "provider": BUILTIN_EMBED_PROVIDER, "model_id": BUILTIN_EMBED_MODEL,
         "base_url": None, "params": {}},
        {"role": "rerank", "name": BUILTIN_RERANK_MODEL.split("/")[-1],
         "provider": BUILTIN_RERANK_PROVIDER, "model_id": BUILTIN_RERANK_MODEL,
         "base_url": None, "params": {}},
    ]


async def ensure_default_models(workspace_id) -> int:
    """Register the built-in in-process models for a NEW workspace, for each role that has no row yet.
    Returns how many rows were created.

    Called when a workspace is created. A row the user later deletes stays deleted: the registry is
    the source of truth, and re-adding what someone removed would make the Models page lie."""
    from sqlalchemy import select
    created = 0
    try:
        async with V2Session() as db:
            rows = (await db.execute(select(Model).where(Model.workspace_id == workspace_id))).scalars().all()
            have = {r.role for r in rows}
            for row in _builtin_rows():
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


async def resolve_model(workspace_id, role: str = "generation", ref: str | None = None) -> dict | None:
    """The spec for (workspace, role, optional ref), or None when the workspace's registry has none."""
    ck = f"{workspace_id}:{role}:{ref or '*'}"
    hit = _cache.get(ck)
    if hit is not None:
        return hit or None                               # {} is cached as "nothing registered"
    # A database error propagates: reporting it as "no model connected" would send someone to the
    # Models page to fix a problem that is not there.
    from sqlalchemy import select
    spec: dict = {}
    async with V2Session() as db:
        row = None
        if ref:
            try:
                row = await db.get(Model, uuid.UUID(str(ref)))
            except ValueError:                                  # a name, not an id
                row = None
            if row is None or row.workspace_id != workspace_id:        # try by name within workspace+role
                row = (await db.execute(select(Model).where(
                    Model.workspace_id == workspace_id, Model.role == role, Model.name == ref))).scalar_one_or_none()
        if row is None:                                     # workspace default for this role
            row = (await db.execute(select(Model).where(
                Model.workspace_id == workspace_id, Model.role == role,
                Model.is_default == True))).scalar_one_or_none()   # noqa: E712
        if row is None and role in _SHARES_GENERATION:
            row = (await db.execute(select(Model).where(
                Model.workspace_id == workspace_id, Model.role == "generation",
                Model.is_default == True))).scalar_one_or_none()   # noqa: E712
        if row is not None and row.workspace_id == workspace_id:
            spec = _row_to_spec(row)
    _cache.set(ck, spec)
    return spec or None


def clear_cache() -> None:
    """Drop cached resolutions — call after a registry row changes."""
    _cache.clear()
