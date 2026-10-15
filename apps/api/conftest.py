"""Fixtures shared by every test directory."""
import re
import uuid
import zlib

import pytest


def _sparse(text: str):
    from opencortyx.stores.vector import SparseVector
    terms = sorted({zlib.crc32(t.encode()) for t in re.findall(r"\w+", (text or "").lower())})
    return SparseVector(indices=terms, values=[1.0] * len(terms))


@pytest.fixture
def memory_repo(monkeypatch):
    """A NodeRepo over in-memory graph and vector stores, with a word-hash keyword encoder in place of
    the BM25 model, so repository-level tests run offline."""
    from opencortyx.core.inference import embedding
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.stores.graph.adapters.memory import MemoryGraphStore
    from opencortyx.stores.vector.adapters.memory import MemoryVectorStore

    async def sparse_embed(texts):
        return [_sparse(t) for t in texts]

    async def sparse_query(text):
        return _sparse(text)

    from opencortyx.retrieval.l1_candidates import hop2, lexical
    monkeypatch.setattr(embedding, "sparse_embed", sparse_embed)
    for module in (embedding, hop2, lexical):
        monkeypatch.setattr(module, "sparse_query", sparse_query)
    return NodeRepo(MemoryGraphStore(), MemoryVectorStore())


@pytest.fixture
async def live_org():
    """A throwaway organization on the configured graph and vector stores, cleared afterwards. Skips
    when either store is unreachable."""
    from opencortyx.core.substrate import NodeRepo
    repo = NodeRepo()
    try:
        await repo.graph.initialize()
        await repo.vectors.initialize()
    except Exception as e:                                 # noqa: BLE001
        pytest.skip(f"graph or vector store unreachable ({e})")
    org = uuid.uuid4()
    yield org
    await repo.clear_org(org)
