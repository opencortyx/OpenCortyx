"""After a run, embedding is scoped to the sources the run wrote, never to "every node since it began".

A thousand files dropped at once each asked for every node created since their own start: each task
loaded nearly the whole upload, the graph store ran out of memory, and embedding stalled for hours.
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from opencortyx.ingest.runs import progress
from opencortyx.tasks import graph as graph_tasks
from opencortyx.tasks.graph import result_source_ids

WS = uuid.uuid4()


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, run_id):
        return SimpleNamespace(workspace_id=WS, started_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
                               created_at=None)


@pytest.fixture
def dispatched(monkeypatch):
    sent = []
    monkeypatch.setattr(progress, "V2Session", _Session)
    monkeypatch.setattr(graph_tasks.v2_embed_source, "delay", lambda ws, sid: sent.append(("source", sid)))
    monkeypatch.setattr(graph_tasks.v2_index_node_attrs, "delay",
                        lambda ws, since_iso=None: sent.append(("window", since_iso)))
    return sent


def test_result_source_ids_reads_own_and_nested_results():
    assert result_source_ids({"source_id": "a"}) == ["a"]
    assert result_source_ids({"source_ids": ["a", "b"], "source_id": "c"}) == ["a", "b"]
    assert result_source_ids({"one.pdf": {"source_id": "a"}, "two.md": {"source_ids": ["b", "a"]}}) == ["a", "b"]
    assert result_source_ids({"rows": 3}) == []
    assert result_source_ids(None) == []


async def test_deferred_embedding_queues_nothing_more(dispatched):
    await progress.ProgressReporter(uuid.uuid4())._index_new_attributes(
        {"source_id": "a", "embedding": "deferred"})
    assert dispatched == []


async def test_a_run_embeds_its_own_sources(dispatched):
    await progress.ProgressReporter(uuid.uuid4())._index_new_attributes({"source_ids": ["a", "b"]})
    assert dispatched == [("source", "a"), ("source", "b")]


async def test_a_result_without_sources_falls_back_to_the_run_window(dispatched):
    await progress.ProgressReporter(uuid.uuid4())._index_new_attributes({"rows": 3})
    assert dispatched == [("window", "2026-10-22T00:00:00+00:00")]


async def test_what_embedding_owes_is_read_from_the_vector_store():
    """The sweep's work list: nodes whose keyword point is written but not marked embedded, found
    without reading the graph."""
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.stores.graph import Node
    from opencortyx.stores.vector.adapters.memory import MemoryVectorStore
    from opencortyx.stores.vector.models import document_point
    ws, vectors = uuid.uuid4(), MemoryVectorStore()
    owed, done = (Node(id=uuid.uuid4(), workspace_id=ws, type="Text", label=x) for x in ("owed", "done"))
    other = Node(id=uuid.uuid4(), workspace_id=uuid.uuid4(), type="Text", label="elsewhere")
    await vectors.upsert([document_point(owed, None), document_point(done, None, embedded=True),
                          document_point(other, None)])
    assert await NodeRepo(vectors=vectors).keyword_only_ids(ws, limit=100) == [str(owed.id)]
