"""Download the models search needs before anything asks for them.

Run by the embedding worker when it starts (core/worker/celery_app.py). Fetches the files only
(fastembed's `lazy_load`), into the shared model cache every service reads, so nothing is loaded
into memory here. Covers the keyword (BM25) model and every fastembed embedder a workspace has
registered, falling back to the default embedder on an install with no workspace yet.
"""
import asyncio

from opencortyx.core.config import BUILTIN_EMBED_MODEL
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

SPARSE_MODEL = "Qdrant/bm25"


async def _registered_embedders() -> set[str]:
    from sqlalchemy import select

    from opencortyx.core.db import V2Session
    from opencortyx.domain.control import Model

    async with V2Session() as db:
        rows = (await db.execute(select(Model.model_id).where(
            Model.role == "embedding", Model.provider == "fastembed"))).scalars().all()
    return set(rows)


def prefetch_models() -> dict:
    """Download the BM25 model and the fastembed embedders. Returns {model: "ready"|"failed: ..."}."""
    from fastembed import SparseTextEmbedding, TextEmbedding

    from opencortyx.core.inference.fastembed_models import cache_dir

    try:
        embedders = asyncio.run(_registered_embedders()) or {BUILTIN_EMBED_MODEL}
    except Exception:                                         # noqa: BLE001 — database not ready
        embedders = {BUILTIN_EMBED_MODEL}
    kwargs = {"cache_dir": cache_dir(), "lazy_load": True} if cache_dir() else {"lazy_load": True}
    out: dict = {}
    for name, cls in [(SPARSE_MODEL, SparseTextEmbedding)] + [(m, TextEmbedding) for m in sorted(embedders)]:
        try:
            cls(model_name=name, **kwargs)
            out[name] = "ready"
        except Exception as e:                                # noqa: BLE001 — offline: fetched on use
            out[name] = f"failed: {type(e).__name__}: {str(e)[:160]}"
    log.info("models.prefetch", **{k.replace("/", "_"): v for k, v in out.items()})
    return out
