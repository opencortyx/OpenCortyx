"""Graph store contract, run against the in-memory store and, when reachable, NebulaGraph.

Set NEBULA_TEST_HOST (for example `nebulagraph` inside the compose network) to include NebulaGraph.
The test space is separate from the application's.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from opencortyx.stores.graph import Edge, EdgeQuery, Node, NodeQuery
from opencortyx.stores.graph.adapters.memory import MemoryGraphStore

_NEBULA_HOST = os.environ.get("NEBULA_TEST_HOST", "").strip()
_nebula_store = None


def _stores():
    params = [pytest.param("memory", id="memory")]
    if _NEBULA_HOST:
        params.append(pytest.param("nebulagraph", id="nebulagraph"))
    return params


@pytest.fixture(params=_stores())
async def store(request):
    global _nebula_store
    if request.param == "memory":
        yield MemoryGraphStore()
        return
    from opencortyx.stores.graph.adapters.nebulagraph import NebulaGraphStore
    if _nebula_store is None:
        _nebula_store = NebulaGraphStore(host=_NEBULA_HOST, port=9669, username="root", password="nebula",
                                         space="opencortyx_test", partitions=2)
    yield _nebula_store


@pytest.fixture
def org():
    return uuid.uuid4()


@pytest.fixture
async def clean(store, org):
    yield
    await store.clear_org(org)


def _node(org, type="Chunk", label="", **kw) -> Node:
    now = datetime.now(timezone.utc)
    return Node(id=kw.pop("id", uuid.uuid4()), workspace_id=org, type=type, label=label,
                created_at=kw.pop("created_at", now), updated_at=now, **kw)


async def test_nodes_round_trip_exactly(store, org, clean):
    text = 'a "quote" \\ back\\slash\nnew\ttab  中文 😀 $x {y}'
    source = _node(org, "Source", "report.pdf")
    node = _node(org, "Table", text, entity_type="dataset", collection_id=uuid.uuid4(),
                 source_ref=source.id, confidence=0.25, level=2, sensitivity="confidential",
                 properties={"text": text, "n": 3, "flag": True, "nested": {"a": [1, 2]}, "source_key": "k1"})
    await store.upsert_nodes([source, node])
    [got] = await store.get_nodes(org, [node.id])
    for field in ("id", "workspace_id", "type", "label", "entity_type", "properties", "collection_id",
                  "source_ref", "confidence", "level", "sensitivity"):
        assert getattr(got, field) == getattr(node, field), field
    assert abs((got.created_at - node.created_at).total_seconds()) < 0.001


async def test_nodes_are_isolated_by_organization(store, org, clean):
    node = _node(org, "Chunk", "private")
    await store.upsert_nodes([node])
    other = uuid.uuid4()
    assert await store.get_nodes(other, [node.id]) == []
    assert await store.find_nodes(other, NodeQuery(types=["Chunk"])) == []
    assert await store.delete_nodes(other, [node.id]) == 0
    assert len(await store.get_nodes(org, [node.id])) == 1


async def test_find_nodes_by_every_indexed_field(store, org, clean):
    t0 = datetime.now(timezone.utc) - timedelta(hours=1)
    coll = uuid.uuid4()
    table = _node(org, "Table", "Customers", properties={"source_key": "db1", "__base_type": "Table"},
                  collection_id=coll, created_at=t0)
    retyped = _node(org, "Aadhaar", "card.pdf", properties={"__base_type": "Source"})
    col = _node(org, "Attribute", "email", source_ref=table.id,
                properties={"source_key": "db1", "parent_table_id": str(table.id), "__status": "proposed"})
    other = _node(org, "entity", "Acme", entity_type="Organization")
    await store.upsert_nodes([table, retyped, col, other])

    def ids(nodes):
        return {n.id for n in nodes}

    assert ids(await store.find_nodes(org, NodeQuery(types=["Table"]))) == {table.id}
    assert ids(await store.find_nodes(org, NodeQuery(types=["Source"]))) == {retyped.id}
    assert ids(await store.find_nodes(org, NodeQuery(types=["Aadhaar"]))) == {retyped.id}
    assert ids(await store.find_nodes(org, NodeQuery(types=["Entity"]))) == {other.id}
    assert ids(await store.find_nodes(org, NodeQuery(exact_types=["Entity"]))) == set()
    assert ids(await store.find_nodes(org, NodeQuery(source_refs=[table.id]))) == {col.id}
    assert ids(await store.find_nodes(org, NodeQuery(source_key="db1"))) == {table.id, col.id}
    assert ids(await store.find_nodes(org, NodeQuery(collection_ids=[coll]))) == {table.id}
    assert ids(await store.find_nodes(org, NodeQuery(label="  customers "))) == {table.id}
    assert ids(await store.find_nodes(org, NodeQuery(label_contains="CUST"))) == {table.id}
    assert ids(await store.find_nodes(org, NodeQuery(status="proposed"))) == {col.id}
    assert ids(await store.find_nodes(org, NodeQuery(entity_type="Organization"))) == {other.id}
    assert ids(await store.find_nodes(org, NodeQuery(props={"parent_table_id": str(table.id)}))) == {col.id}
    assert ids(await store.find_nodes(org, NodeQuery(types=["Table", "Attribute"],
                                                     props_not={"__status": "proposed"}))) == {table.id}
    assert ids(await store.find_nodes(org, NodeQuery(created_after=t0 + timedelta(minutes=1)))) == \
        {retyped.id, col.id, other.id}
    newest = await store.find_nodes(org, NodeQuery(order_by="-created_at", limit=1))
    assert newest[0].id != table.id and len(newest) == 1
    assert await store.count_nodes(org, NodeQuery(types=["Table", "Attribute"])) == 2
    assert await store.count_nodes(org) == 4
    assert (await store.count_nodes_by_type(org))["Attribute"] == 1


async def test_edges_are_idempotent_and_keep_degree(store, org, clean):
    a, b, c = _node(org, "Table", "a"), _node(org, "Attribute", "b"), _node(org, "Attribute", "c")
    await store.upsert_nodes([a, b, c])
    first = await store.upsert_edges([Edge(workspace_id=org, source_id=a.id, target_id=b.id, type="HAS_ATTRIBUTE"),
                                      Edge(workspace_id=org, source_id=a.id, target_id=c.id, type="HAS_ATTRIBUTE")])
    assert len(first.created) == 2 and not first.updated and not first.refused
    again = await store.upsert_edges([Edge(workspace_id=org, source_id=a.id, target_id=b.id, type="HAS_ATTRIBUTE",
                                           weight=0.5)])
    assert len(again.updated) == 1 and not again.created
    await store.upsert_nodes([a])                         # rewriting a record keeps its degree
    degrees = {n.id: n.degree for n in await store.get_nodes(org, [a.id, b.id, c.id])}
    assert degrees == {a.id: 2, b.id: 1, c.id: 1}
    [edge] = await store.find_edges(org, EdgeQuery(source_ids=[a.id], target_ids=[b.id]))
    assert edge.weight == 0.5 and edge.type == "HAS_ATTRIBUTE"
    assert await store.count_edges(org) == 2
    assert (await store.count_edges_by_type(org)) == {"HAS_ATTRIBUTE": 2}


async def test_parallel_edges_differ_by_relation(store, org, clean):
    dog, max_ = _node(org, "Entity", "dog"), _node(org, "Entity", "Max")
    await store.upsert_nodes([dog, max_])
    written = await store.upsert_edges([
        Edge(workspace_id=org, source_id=dog.id, target_id=max_.id, type="RELATES_TO", properties={"relation": "has_name"}),
        Edge(workspace_id=org, source_id=dog.id, target_id=max_.id, type="RELATES_TO", properties={"relation": "is_a"}),
    ])
    assert len(written.created) == 2
    edges = await store.find_edges(org, EdgeQuery(source_ids=[dog.id]))
    assert sorted(e.properties["relation"] for e in edges) == ["has_name", "is_a"]
    assert await store.delete_edges(org, [e for e in edges if e.properties["relation"] == "is_a"]) == 1
    assert [e.properties["relation"] for e in await store.find_edges(org, EdgeQuery(source_ids=[dog.id]))] == ["has_name"]


async def test_edges_to_missing_or_foreign_nodes_are_refused(store, org, clean):
    a = _node(org, "Chunk", "a")
    foreign = _node(uuid.uuid4(), "Chunk", "foreign")
    await store.upsert_nodes([a, foreign])
    result = await store.upsert_edges([
        Edge(workspace_id=org, source_id=a.id, target_id=uuid.uuid4(), type="MENTIONS"),
        Edge(workspace_id=org, source_id=a.id, target_id=foreign.id, type="MENTIONS"),
    ])
    assert len(result.refused) == 2 and not result.created
    assert await store.find_edges(org, EdgeQuery(source_ids=[a.id])) == []
    await store.clear_org(foreign.workspace_id)


async def test_find_edges_and_neighbors(store, org, clean):
    a, b, c = _node(org, "Entity", "a"), _node(org, "Entity", "b"), _node(org, "Claim", "c")
    await store.upsert_nodes([a, b, c])
    await store.upsert_edges([
        Edge(workspace_id=org, source_id=a.id, target_id=b.id, type="WORKS_WITH", properties={"since": 2020}),
        Edge(workspace_id=org, source_id=c.id, target_id=a.id, type="ABOUT"),
    ])
    out = await store.neighbors(org, [a.id], direction="out")
    assert [(n.node.id, n.direction) for n in out] == [(b.id, "out")]
    inbound = await store.neighbors(org, [a.id], direction="in")
    assert [(n.node.id, n.edge.type) for n in inbound] == [(c.id, "ABOUT")]
    both = await store.neighbors(org, [a.id], direction="both")
    assert {n.node.id for n in both} == {b.id, c.id}
    assert {n.node.id for n in await store.neighbors(org, [a.id], direction="both", edge_types=["ABOUT"])} == {c.id}
    assert len(await store.find_edges(org, EdgeQuery(node_ids=[a.id]))) == 2
    assert len(await store.find_edges(org, EdgeQuery(target_ids=[a.id]))) == 1
    assert len(await store.find_edges(org, EdgeQuery(types=["WORKS_WITH"]))) == 1
    assert len(await store.find_edges(org, EdgeQuery(props={"since": 2020}))) == 1
    [edge] = await store.find_edges(org, EdgeQuery(types=["ABOUT"]))
    assert [e.id for e in await store.find_edges(org, EdgeQuery(ids=[edge.id]))] == [edge.id]
    [path] = await store.shortest_paths(org, b.id, c.id)
    assert path.node_ids == [b.id, a.id, c.id] and len(path.edges) == 2


async def test_neighbor_groups_count_and_pages_cover_each_group_exactly(store, org, clean):
    hub = _node(org, "Company", "hub")
    people = [_node(org, "Person", f"p{i}") for i in range(7)]
    docs = [_node(org, "Document", f"d{i}") for i in range(3)]
    day = _node(org, "Date", "2026-09-28")
    await store.upsert_nodes([hub, day, *people, *docs])
    await store.upsert_edges(
        [Edge(workspace_id=org, source_id=p.id, target_id=hub.id, type="WORKS_AT") for p in people]
        + [Edge(workspace_id=org, source_id=hub.id, target_id=d.id, type="MENTIONED_IN") for d in docs]
        + [Edge(workspace_id=org, source_id=hub.id, target_id=day.id, type="CREATED_ON"),
           Edge(workspace_id=org, source_id=hub.id, target_id=hub.id, type="SAME_AS")])
    groups = {(g.edge_type, g.direction, g.node_type): g.count
              for g in await store.neighbor_groups(org, hub.id, exclude_edge_types=["CREATED_ON"])}
    assert groups == {("WORKS_AT", "in", "Person"): 7, ("MENTIONED_IN", "out", "Document"): 3,
                      ("SAME_AS", "out", "Company"): 1}      # a self-loop counts once
    seen = []
    for offset in range(0, 9, 3):
        page = await store.neighbor_page(org, hub.id, edge_type="WORKS_AT", direction="in",
                                         node_type="Person", offset=offset, limit=3)
        assert all(h.direction == "in" and h.edge.type == "WORKS_AT" for h in page)
        seen += [str(h.node.id) for h in page]
    assert sorted(seen) == sorted(str(p.id) for p in people) and seen == sorted(seen)
    assert await store.neighbor_page(org, hub.id, edge_type="WORKS_AT", direction="out",
                                     node_type="Person") == []


async def test_deleting_nodes_removes_edges_and_fixes_degrees(store, org, clean):
    a, b, c = _node(org, "Entity", "a"), _node(org, "Entity", "b"), _node(org, "Entity", "c")
    await store.upsert_nodes([a, b, c])
    await store.upsert_edges([Edge(workspace_id=org, source_id=a.id, target_id=b.id, type="R"),
                              Edge(workspace_id=org, source_id=b.id, target_id=c.id, type="R")])
    assert await store.delete_nodes(org, [b.id]) == 1
    assert await store.find_edges(org, EdgeQuery(node_ids=[a.id, c.id])) == []
    assert {n.degree for n in await store.get_nodes(org, [a.id, c.id])} == {0}
    await store.upsert_edges([Edge(workspace_id=org, source_id=a.id, target_id=c.id, type="R")])
    [edge] = await store.find_edges(org, EdgeQuery(source_ids=[a.id]))
    assert await store.delete_edges(org, [edge]) == 1
    assert await store.delete_edges(org, [edge]) == 0
    assert {n.degree for n in await store.get_nodes(org, [a.id, c.id])} == {0}
    assert await store.clear_org(org) == 2
    assert await store.count_nodes(org) == 0
