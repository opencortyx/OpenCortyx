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

    Hosted providers have a well-known public endpoint. A server you run yourself (Ollama, LM Studio,
    vLLM, ... — `openai_compatible`) has none: its address is whatever you type on the Models page,
    so blank stays blank and discovery says it needs one.
    """
    hosted = {
        "openai": "https://api.openai.com/v1",
        "groq": "https://api.groq.com/openai/v1",
        "huggingface": "https://router.huggingface.co/v1",
    }
    return hosted.get((provider or "").lower(), "")


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
