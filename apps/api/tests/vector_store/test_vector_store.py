"""Vector store contract, run against the in-memory store and, when reachable, Qdrant.

Set QDRANT_TEST_URL (for example `http://qdrant:6333` inside the compose network) to include Qdrant.
The test collection is separate from the application's.
"""
import os
import uuid

import pytest

from opencortyx.stores.graph import Node
from opencortyx.stores.vector import (
    DOCUMENT_ATTR, SparseVector, VectorFilter, VectorPoint, search_fields,
)
from opencortyx.stores.vector.models import document_point
from opencortyx.stores.vector.adapters.memory import MemoryVectorStore

_QDRANT_URL = os.environ.get("QDRANT_TEST_URL", "").strip()
_DIM = 4


def _stores():
    params = [pytest.param("memory", id="memory")]
    if _QDRANT_URL:
        params.append(pytest.param("qdrant", id="qdrant"))
    return params


@pytest.fixture(params=_stores())
async def store(request):
    if request.param == "memory":
        yield MemoryVectorStore(dimension=_DIM)
        return
    from opencortyx.stores.vector.adapters.qdrant import QdrantVectorStore
    s = QdrantVectorStore(url=_QDRANT_URL, collection="opencortyx_test", dimension=_DIM)
    yield s
    await s.close()


@pytest.fixture
async def org(store):
    workspace_id = uuid.uuid4()
    yield workspace_id
    await store.clear_org(workspace_id)


def _point(org, node, attr, text, dense=None, sparse=None):
    return VectorPoint(workspace_id=org, node_id=node.id, attr=attr, text=text, fields=search_fields(node),
                       dense=dense, sparse=sparse)


def _node(org, type="Chunk", **props):
    return Node(id=uuid.uuid4(), workspace_id=org, type=type, properties=props)


async def test_dense_search_ranks_and_filters(store, org):
    chunk, table = _node(org, "Chunk"), _node(org, "Table", __base_type="Table")
    await store.upsert([
        _point(org, chunk, "text", "near", dense=[1, 0, 0, 0]),
        _point(org, table, "label", "far", dense=[0, 1, 0, 0]),
    ])
    hits = await store.search_dense([1, 0.1, 0, 0], VectorFilter(workspace_id=org), k=5)
    assert [h.node_id for h in hits] == [chunk.id, table.id]
    assert hits[0].attr == "text" and hits[0].text == "near" and hits[0].score > hits[1].score
    only_tables = await store.search_dense([1, 0, 0, 0], VectorFilter(workspace_id=org, types=["table"]), k=5)
    assert [h.node_id for h in only_tables] == [table.id]
    assert await store.search_dense([1, 0, 0, 0], VectorFilter(workspace_id=uuid.uuid4()), k=5) == []


async def test_evidence_filter_and_field_updates(store, org):
    fact = _node(org, "Fact")
    internal = _node(org, "EvalCase", __internal="true")
    memory = _node(org, "Turn", __memory="true")
    await store.upsert([_point(org, n, "text", "t", dense=[1, 0, 0, 0]) for n in (fact, internal, memory)])
    evidence = VectorFilter(workspace_id=org, evidence=True)
    assert {h.node_id for h in await store.search_dense([1, 0, 0, 0], evidence, 5)} == {fact.id, memory.id}
    knowledge = VectorFilter(workspace_id=org, evidence=True, scope="knowledge")
    assert {h.node_id for h in await store.search_dense([1, 0, 0, 0], knowledge, 5)} == {fact.id}
    fact.properties["superseded"] = True
    await store.update_fields(org, fact.id, search_fields(fact))
    assert {h.node_id for h in await store.search_dense([1, 0, 0, 0], knowledge, 5)} == set()


async def test_sparse_search_and_bookkeeping(store, org):
    a = _node(org, "Chunk", text="Invoice REC-42 for Door Dash", source_key="k1", date_time="23 January, 2022")
    a.label = "Acme Corp"
    b = _node(org, "Chunk", text="receipt")
    await store.upsert([
        document_point(a, SparseVector([7, 9], [1.0, 1.0]), embedded=True),
        document_point(b, SparseVector([3], [1.0])),
        _point(org, a, "text", "invoice 42", dense=[0, 0, 1, 0]),
        _point(org, b, "text", "invoice 42", dense=[0, 0, 0, 1]),
    ])
    hits = await store.search_sparse(SparseVector([9], [1.0]), VectorFilter(workspace_id=org, attrs=[DOCUMENT_ATTR]), 5)
    assert [h.node_id for h in hits] == [a.id]
    assert await store.embedded_nodes(org, [a.id, b.id, uuid.uuid4()]) == {str(a.id)}
    assert await store.count_nodes(org) == 1
    assert await store.attribute_values(org) == {"text": (2, 1)}

    def ids(rows):
        return {r["node_id"] for r in rows}
    doc = VectorFilter(workspace_id=org, attrs=[DOCUMENT_ATTR])
    assert ids(await store.scroll(VectorFilter(**{**doc.__dict__, "phrase": "door dash"}), limit=5, fields=["node_id"])) == {str(a.id)}
    assert ids(await store.scroll(VectorFilter(**{**doc.__dict__, "phrase": "dash door"}), limit=5, fields=["node_id"])) == set()
    assert ids(await store.scroll(VectorFilter(**{**doc.__dict__, "phrase": "rec-42"}), limit=5, fields=["node_id"])) == {str(a.id)}
    assert ids(await store.scroll(VectorFilter(**{**doc.__dict__, "names": ["ACME CORP"]}), limit=5, fields=["node_id"])) == {str(a.id)}
    assert ids(await store.scroll(VectorFilter(**{**doc.__dict__, "names_phrase": "acme"}), limit=5, fields=["node_id"])) == {str(a.id)}
    rows = await store.scroll(VectorFilter(**{**doc.__dict__, "has_props": ["date_time"]}), limit=5, fields=["node_id", "props"])
    assert ids(rows) == {str(a.id)} and rows[0]["props"]["source_key"] == "k1"
    await store.delete(VectorFilter(workspace_id=org, node_ids=[a.id]))
    assert await store.count(VectorFilter(workspace_id=org)) == 2
    assert await store.embedded_nodes(org, [a.id]) == set()
