"""Services: the model registry for generation, embeddings and reranking."""
from sqlalchemy import select


def model_view(m) -> dict:
    return {"id": str(m.id), "name": m.name, "role": m.role, "provider": m.provider,
            "model_id": m.model_id, "base_url": m.base_url, "params": m.params or {},
            "is_default": m.is_default, "has_key": bool(m.api_key),
            "created_at": m.created_at.isoformat() if m.created_at else None}


async def clear_default(db, workspace_id, role, keep_id=None):
    from opencortyx.domain.control import Model
    rows = (await db.execute(select(Model).where(
        Model.workspace_id == workspace_id, Model.role == role, Model.is_default == True))).scalars().all()  # noqa: E712
    for r in rows:
        if keep_id is None or r.id != keep_id:
            r.is_default = False


def provider_default_base(provider: str) -> str:
    """Base URL to discover against when the form left it blank.

    Hosted providers have a well-known public endpoint. SELF-HOSTED ones (vLLM and any other
    OpenAI-compatible server) do not — there is nothing to hardcode, because the endpoint is
    wherever this deployment happens to run it. Their default is therefore the CONFIGURED one
    (`LLM_BASE_URL`, falling back to `EMBED_BASE_URL` since one vLLM deployment commonly fronts both).

    Without this, picking "vLLM" in the Add-model dialog and pressing Discover returned nothing
    unless the user retyped a URL the deployment already knows — the one case where discovery
    should be most automatic, since the server is ours.
    """
    from opencortyx.core.config import SELF_HOSTED_PROVIDERS, settings
    p = (provider or "").lower()
    hosted = {
        "openai": "https://api.openai.com/v1",
        "groq": "https://api.groq.com/openai/v1",
    }
    if p in hosted:
        return hosted[p]
    if p in SELF_HOSTED_PROVIDERS:
        return (settings.LLM_BASE_URL or settings.EMBED_BASE_URL or "").rstrip("/")
    return ""


def model_cache_clear():
    try:
        from opencortyx.core.inference import registry as mr
        mr._cache.clear()
    except Exception:
        pass
    try:                                                  # a changed embedding selection must take effect
        from opencortyx.core.inference.embedding import clear_active_embedding
        clear_active_embedding()
    except Exception:
        pass
