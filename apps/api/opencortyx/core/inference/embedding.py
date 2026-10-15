"""Self-contained embeddings for v2 (no v1 import).

`fastembed` (in-process ONNX, CPU) by default; vLLM / OpenAI / any OpenAI-compatible embedding
server over HTTP when one is configured. Keeps the existing `embed` / `embed_batch` API so no v2
caller changes.

Embeddings are deliberately NOT forced onto the GPU alongside generation. An embed is a tiny forward
pass — ~10-20 ms in-process with no network hop — whereas vLLM pins one model per process for the
lifetime of that process. On a single card, spending VRAM to serve an embedder is spending it
against the generation model that actually needs it. Point EMBED_PROVIDER at `vllm` when a second
server exists; the wire format is identical either way.
"""

from opencortyx.core import http
from opencortyx.core.config import settings, OPENAI_COMPATIBLE_PROVIDERS
from opencortyx.core.cache import TTLCache
from opencortyx.core.observability import get_logger
from opencortyx.stores.vector import SparseVector

log = get_logger(__name__)

# query-embedding cache — repeat queries skip the model round-trip entirely (TTL long: text→vec is stable)
_embed_cache = TTLCache(maxsize=settings.EMBED_CACHE_SIZE, ttl=86400)
_active_embed_cache = TTLCache(maxsize=2, ttl=60)


async def active_embedding_spec() -> dict | None:
    """The embedding model actually selected in the Model Registry (the is_default embedding row), so a
    choice in the Models UI drives EVERY embedding call — not just the env default. Returns None when
    nothing is registered (→ env fallback). Normally this equals the env default (the built-in row mirrors
    it), so behaviour only changes when you deliberately pick a different embedding model — which needs a
    re-index, as the Models UI warns."""
    hit = _active_embed_cache.get("spec")
    if hit is not None:
        return hit or None                               # {} is cached as "no override" → return None
    spec: dict = {}
    try:
        from sqlalchemy import select
        from opencortyx.domain import Model
        from opencortyx.core.db import V2Session
        from opencortyx.core.security.secrets import decrypt_secret
        async with V2Session() as db:
            row = (await db.execute(select(Model).where(
                Model.role == "embedding", Model.is_default == True))).scalars().first()   # noqa: E712
        if row is not None and row.model_id:
            from opencortyx.core.inference.registry import _base_url_for, coerce_legacy_row
            prov, model_id, base = coerce_legacy_row(
                row.provider or settings.EMBED_PROVIDER or "fastembed",
                row.model_id, row.base_url, "embedding")
            # A blank base_url must resolve to THIS provider's endpoint.
            spec = {"provider": prov, "model_id": model_id,
                    "base_url": base or _base_url_for(prov, embedding=True),
                    "api_key": decrypt_secret(row.api_key) if row.api_key else None}
    except Exception:
        spec = {}
    _active_embed_cache.set("spec", spec)
    return spec or None


def clear_active_embedding():
    """Drop the cached embedding selection AND the text→vector cache — call after changing the embedding
    model so the new model takes effect and stale vectors aren't served."""
    _active_embed_cache.clear()
    _embed_cache.clear()


_FASTEMBED_MODELS: dict = {}
_ST_MODELS: dict = {}


def _get_fastembed_model(model_name: str | None = None):
    import os
    name = model_name or "nomic-ai/nomic-embed-text-v1.5"
    if name not in _FASTEMBED_MODELS:
        from fastembed import TextEmbedding
        threads = settings.FASTEMBED_THREADS or None      # 0 means "let onnxruntime choose"
        cache_dir = settings.FASTEMBED_CACHE_DIR or ("/cache" if os.path.exists("/cache") else None)
        kwargs: dict = {}
        if threads:
            kwargs["threads"] = threads
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        _FASTEMBED_MODELS[name] = TextEmbedding(model_name=name, **kwargs)
    return _FASTEMBED_MODELS[name]


_SPARSE_MODEL = "Qdrant/bm25"
_sparse_models: dict = {}


def _get_sparse_model():
    import os
    if _SPARSE_MODEL not in _sparse_models:
        from fastembed import SparseTextEmbedding
        cache_dir = settings.FASTEMBED_CACHE_DIR or ("/cache" if os.path.exists("/cache") else None)
        _sparse_models[_SPARSE_MODEL] = SparseTextEmbedding(
            _SPARSE_MODEL, **({"cache_dir": cache_dir} if cache_dir else {}))
    return _sparse_models[_SPARSE_MODEL]


async def sparse_embed(texts: list):
    """BM25 term vectors for documents, for keyword search. Computed locally; no model weights."""
    import asyncio
    if not texts:
        return []

    def run():
        return [SparseVector(indices=v.indices.tolist(), values=v.values.tolist())
                for v in _get_sparse_model().embed(texts)]
    return await asyncio.to_thread(run)


async def sparse_query(text: str):
    """BM25 term vector for a query: the query's terms, weighted by the index's IDF at search time."""
    import asyncio

    def run():
        vectors = list(_get_sparse_model().query_embed(text or ""))
        v = vectors[0] if vectors else None
        return SparseVector(indices=v.indices.tolist() if v is not None else [],
                            values=v.values.tolist() if v is not None else [])
    return await asyncio.to_thread(run)


def _get_st_model(model_name: str | None = None):
    name = model_name or "nomic-ai/nomic-embed-text-v1.5"
    if name not in _ST_MODELS:
        from sentence_transformers import SentenceTransformer
        _ST_MODELS[name] = SentenceTransformer(name, trust_remote_code=True)
    return _ST_MODELS[name]


_ce_models: dict = {}


def cross_encoder_installed() -> bool:
    """Whether this image can run an in-process cross-encoder at all. Cheap: no import, no load."""
    import importlib.util
    return importlib.util.find_spec("sentence_transformers") is not None


def get_cross_encoder(model_id: str | None = None):
    """The cached in-process cross-encoder, or None when there is no usable one.

    Lives here with the other model caches rather than in the reranker that uses it, because warm-up
    has to load the same instance before the first query and must not import retrieval to do it.

    Returns None — never raises — when `sentence-transformers` is absent or this particular model
    fails to load, because both are recoverable: the caller falls back to the LLM reranker.
    """
    from opencortyx.core.config import CROSS_ENCODER_MODEL
    mid = model_id or CROSS_ENCODER_MODEL
    if mid in _ce_models:
        return _ce_models[mid]
    try:
        from sentence_transformers import CrossEncoder
    except Exception:                       # noqa: BLE001 — library not installed; fall back to LLM
        _ce_models[mid] = None
        return None
    try:
        _ce_models[mid] = CrossEncoder(mid)
    except Exception:                       # noqa: BLE001 — this model can't load; fall back to LLM
        _ce_models[mid] = None
    return _ce_models[mid]


def _char_budget_groups(texts: list, budget: int) -> list:
    """Split texts, in order, into groups whose padded size (count x longest text) fits `budget`.

    ONNX pads a batch to its longest text and keeps the largest activation it has allocated, so the
    embedder's memory follows the biggest batch it ever ran. With fastembed's default batch of 256,
    sixteen 8,000-character texts pushed an embedding worker past 4 GiB. A text longer than the
    budget is embedded on its own.
    """
    groups: list = []
    current: list = []
    longest = 0
    for text in texts:
        size = max(1, len(text))
        if current and (len(current) + 1) * max(longest, size) > budget:
            groups.append(current)
            current, longest = [], 0
        current.append(text)
        longest = max(longest, size)
    if current:
        groups.append(current)
    return groups


async def _fastembed_embed_batch(texts: list, model=None) -> list:
    """In-process FastEmbed ONNX CPU embeddings (~10-20ms per query, zero network hop).

    THE LOAD IS OFFLOADED TOO, not just the embed. `_get_fastembed_model` was called here on the
    event loop, and on a cold process that call downloads 523 MB and builds an ONNX session — 120
    seconds, measured, during which the API answered NOTHING. Every endpoint, not just retrieval:
    one uncached embed froze the whole server, so the first query after any restart looked like a
    hang and a first install looked broken.

    The `.embed()` call was already in an executor, which made the freeze easy to miss — the
    expensive part was the line above it.
    """
    if not texts:
        return []
    import asyncio
    loop = asyncio.get_running_loop()

    groups = _char_budget_groups(texts, max(1, settings.FASTEMBED_MAX_BATCH_CHARS))

    def _run():
        m = _get_fastembed_model(model)          # load AND embed, both off the loop
        return [v.tolist() if hasattr(v, "tolist") else list(v)
                for group in groups for v in m.embed(group, batch_size=len(group))]
    return await loop.run_in_executor(None, _run)


async def _st_embed_batch(texts: list, model=None) -> list:
    """In-process SentenceTransformers / Hugging Face model (~10-50ms per query, zero network hop)."""
    if not texts:
        return []
    import asyncio
    loop = asyncio.get_running_loop()
    m = await loop.run_in_executor(None, lambda: _get_st_model(model))   # same reason as above
    def _run():
        embs = m.encode(texts, normalize_embeddings=True)
        return embs.tolist() if hasattr(embs, "tolist") else [list(e) for e in embs]
    return await loop.run_in_executor(None, _run)


async def _openai_embed_batch(texts: list, model=None, base_url=None, api_key=None) -> list:
    """vLLM / OpenAI / any OpenAI-compatible embedding endpoint.

    Splits into EMBED_BATCH_SIZE-sized requests and issues them CONCURRENTLY. Both halves matter and
    for opposite reasons: one request per text wastes a round trip on a latency-bound call, while a
    single request carrying an entire ingest batch runs into the server's own max-input cap and
    fails the lot. Sending the sub-batches in parallel is what lets a batching server (vLLM) fill its
    scheduler instead of processing our chunks one after another.

    Uses the POOLED AsyncOpenAI client from llm.py — this built a fresh client, and therefore a fresh
    TCP connection, on every batch.
    """
    if not texts:
        return []
    import asyncio
    from opencortyx.core.inference.llm import _openai_client
    from opencortyx.core.config import runtime as rc

    client = _openai_client(api_key, base_url or None)
    mid = model or settings.EMBED_MODEL
    size = max(1, int(settings.EMBED_BATCH_SIZE))
    groups = [texts[i:i + size] for i in range(0, len(texts), size)]
    if len(groups) == 1:
        resp = await client.embeddings.create(input=groups[0], model=mid)
        return [d.embedding for d in resp.data]

    sem = asyncio.Semaphore(max(1, rc.embed_concurrency()))

    async def _one(group):
        async with sem:
            resp = await client.embeddings.create(input=group, model=mid)
            return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]

    out: list = []
    for part in await asyncio.gather(*(_one(g) for g in groups)):
        out.extend(part)
    return out


async def _hf_embed_batch(texts: list, model=None, base_url=None, api_key=None) -> list:
    """HuggingFace feature-extraction Inference API."""
    if not texts:
        return []
    url = base_url or f"https://api-inference.huggingface.co/models/{model}"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = await http.get_client().post(url, headers=headers, json={"inputs": texts}, timeout=120.0)
    r.raise_for_status()
    return r.json() or []


async def embed(text: str) -> list:
    text = (text or "").strip()
    if not text:
        return []
    from opencortyx.core.config import runtime as rc
    key = text[:8000]
    use_cache = rc.embed_cache_enabled()
    if use_cache:
        cached = _embed_cache.get(key)
        if cached is not None:
            return cached
    out = await embed_batch([key])
    vec = out[0] if out else []
    if use_cache and vec:
        _embed_cache.set(key, vec)
    return vec


async def embed_batch(texts: list, model_spec: dict | None = None) -> list:
    texts = [(t or "")[:8000] for t in (texts or [])]
    if not texts:
        return []
    try:
        if model_spec is None:                               # no explicit spec → use the registry selection
            model_spec = await active_embedding_spec()
        if model_spec:                                       # registry-resolved embedding model
            prov = (model_spec.get("provider") or settings.EMBED_PROVIDER or "fastembed").lower()
            mid, base, key = model_spec.get("model_id"), model_spec.get("base_url"), model_spec.get("api_key")
        else:                                                # env-level fallback
            prov = (settings.EMBED_PROVIDER or "fastembed").lower()
            mid, base, key = settings.EMBED_MODEL, (settings.EMBED_BASE_URL or None), None

        if prov in OPENAI_COMPATIBLE_PROVIDERS:
            # A self-hosted embedding server has no public endpoint; fall back to the generation
            # server's URL only because one vLLM deployment commonly fronts both roles.
            return await _openai_embed_batch(
                texts, model=mid, base_url=(base or settings.EMBED_BASE_URL or settings.LLM_BASE_URL or None),
                api_key=key)
        if prov in ("fastembed", "onnx"):
            return await _fastembed_embed_batch(texts, model=mid)
        if prov in ("sentence-transformers", "sentence_transformers", "local", "st"):
            return await _st_embed_batch(texts, model=mid)
        if prov in ("huggingface", "hf"):
            return await _hf_embed_batch(texts, model=mid, base_url=base, api_key=key)
        raise ValueError(
            f"unsupported embedding provider {prov!r}. Use fastembed (in-process), vllm / any "
            f"OpenAI-compatible embedding server, sentence-transformers, or huggingface.")
    except Exception as e:
        log.warning("embedding.service_unavailable", error=str(e),
                    action="falling back to empty embeddings")
        return [[] for _ in texts]


