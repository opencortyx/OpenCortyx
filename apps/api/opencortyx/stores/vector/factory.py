"""The configured vector store, one per process."""
from __future__ import annotations

import threading

from opencortyx.core.config import VECTOR_DIM, settings
from opencortyx.stores.vector.port import VectorStore

_store: VectorStore | None = None
_lock = threading.Lock()


def create_vector_store(provider: str | None = None) -> VectorStore:
    provider = (provider or settings.VECTOR_STORE_PROVIDER).strip().lower()
    if provider == "qdrant":
        from opencortyx.stores.vector.adapters.qdrant import QdrantVectorStore
        return QdrantVectorStore(url=settings.QDRANT_URL, collection=settings.QDRANT_COLLECTION,
                                 dimension=VECTOR_DIM, api_key=settings.QDRANT_API_KEY,
                                 timeout_s=settings.QDRANT_TIMEOUT_S)
    if provider == "memory":
        from opencortyx.stores.vector.adapters.memory import MemoryVectorStore
        return MemoryVectorStore(dimension=VECTOR_DIM)
    raise ValueError(f"unknown VECTOR_STORE_PROVIDER {provider!r}: use qdrant or memory")


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = create_vector_store()
    return _store


def set_vector_store(store: VectorStore | None) -> None:
    """Replace the process's store, for tests."""
    global _store
    _store = store
