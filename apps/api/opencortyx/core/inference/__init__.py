"""Model access: the providers, the registry that resolves them, and keeping them resident.

    llm.py        complete() / stream_complete() — OpenAI-compatible servers, Anthropic, HuggingFace
    embedding.py  embed() / embed_batch() / sparse_embed(), and the in-process model caches
                  (fastembed ONNX, SentenceTransformers, the rerank cross-encoder)
    registry.py   resolve a model REFERENCE (a registered Model row, or nothing) to a concrete
                  provider + endpoint, so "which model ran" has one answer
    warmup.py     load the models in the background, so the first request does not pay for it

These belong together because they share the thing that makes them awkward: a model is expensive to
load and must be loaded exactly once per process. Splitting the caches from the callers is how a
process ends up holding two copies of the same weights.
"""
from opencortyx.core.inference.embedding import (
    embed, embed_batch, get_cross_encoder, sparse_embed, sparse_query,
)
from opencortyx.core.inference.llm import complete, stream_complete
from opencortyx.core.inference.registry import ensure_default_models, resolve_model

__all__ = ["complete", "stream_complete", "embed", "embed_batch", "sparse_embed", "sparse_query",
           "get_cross_encoder", "resolve_model", "ensure_default_models"]
