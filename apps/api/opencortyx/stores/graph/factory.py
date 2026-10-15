"""The configured graph store, one per process."""
from __future__ import annotations

import threading

from opencortyx.core.config import settings
from opencortyx.stores.graph.port import GraphStore

_store: GraphStore | None = None
_lock = threading.Lock()


def create_graph_store(provider: str | None = None) -> GraphStore:
    provider = (provider or settings.GRAPH_STORE_PROVIDER).strip().lower()
    if provider == "nebulagraph":
        from opencortyx.stores.graph.adapters.nebulagraph import NebulaGraphStore
        return NebulaGraphStore(
            host=settings.NEBULA_HOST, port=settings.NEBULA_PORT, username=settings.NEBULA_USERNAME,
            password=settings.NEBULA_PASSWORD, space=settings.NEBULA_SPACE,
            timeout_s=settings.NEBULA_TIMEOUT_S, pool_size=settings.NEBULA_MAX_CONN_POOL_SIZE)
    if provider == "memory":
        from opencortyx.stores.graph.adapters.memory import MemoryGraphStore
        return MemoryGraphStore()
    raise ValueError(f"unknown GRAPH_STORE_PROVIDER {provider!r}: use nebulagraph or memory")


def get_graph_store() -> GraphStore:
    global _store
    if _store is None:
        with _lock:
            if _store is None:
                _store = create_graph_store()
    return _store


def set_graph_store(store: GraphStore | None) -> None:
    """Replace the process's store, for tests."""
    global _store
    _store = store
