"""The dashboard's browse tree — against the in-memory graph store, no database.

What the page relies on, and what these tests pin: an untagged asset carries its file extension; a
part of the graph is a root of its own exactly when no asset reaches it (by an edge or a provenance
pointer); an asset opens into what it produced and what that links to; and past GROUP_OVER children
a parent opens into groups, each paging exactly once over its members.
"""
import uuid
from datetime import datetime, timezone

import pytest

from opencortyx.knowledge import graph_tree as gt
from opencortyx.stores.graph.adapters.memory import MemoryGraphStore
from opencortyx.stores.graph.factory import set_graph_store
from opencortyx.stores.graph.models import Edge, Node


@pytest.fixture
def org():
    return uuid.uuid4()


@pytest.fixture
def store():
    s = MemoryGraphStore()
    set_graph_store(s)
    yield s
    set_graph_store(None)


def _n(org, type, label="", source_ref=None, collection_id=None, **props) -> Node:
    now = datetime.now(timezone.utc)
    return Node(id=uuid.uuid4(), workspace_id=org, type=type, label=label, source_ref=source_ref,
                collection_id=collection_id, properties=props, created_at=now, updated_at=now)


def _e(org, a, b, type="REL") -> Edge:
    return Edge(workspace_id=org, source_id=a.id, target_id=b.id, type=type)


# ── naming and tags ────────────────────────────────────────────────────────────────────────────────

def test_untagged_assets_carry_their_extension_and_tags_are_normalised(org):
    pdf = _n(org, "Source", "Q3 Report.PDF", filename="Q3 Report.PDF")
    table = _n(org, "Table", "public.orders", source_system="sql")
    csv_table = _n(org, "Table", "customers", source_system="file", source_file="customers.csv")
    template = _n(org, "Table", "Invoice", source_system="domain_template")
    tagged = _n(org, "Source", "notes.md", tags=["  Finance ", "finance", "Board   Pack", ""])
    assert gt.asset_tags(pdf) == (["pdf"], True)
    assert gt.asset_tags(table) == (["db"], True)          # not "orders": a schema, not an extension
    assert gt.asset_tags(csv_table) == (["csv"], True)
    assert gt.asset_tags(template) == (["template"], True)
    assert gt.asset_tags(_n(org, "Table", "Table")) == (["table"], True)
    assert gt.asset_tags(tagged) == (["finance", "board pack"], False)
    assert gt.parse_tags('["A", "b"]') == ["a", "b"]
    assert gt.parse_tags("a, b ,, a") == ["a", "b"]
    assert gt.parse_tags("") == [] and gt.parse_tags(None) == []
    assert len(gt.clean_tags([f"t{i}" for i in range(50)])) == gt.TAGS_PER_ASSET


def test_an_asset_source_is_its_collection_else_where_it_came_from(org):
    coll = uuid.uuid4()
    assert gt.asset_source(_n(org, "Source", "a.pdf", collection_id=coll), {str(coll): "Legal"}) == "Legal"
    assert gt.asset_source(_n(org, "Table", "t", source_system="sql", source_key="db:sales"), {}) == "Database · db:sales"
    assert gt.asset_source(_n(org, "Table", "t", source_system="domain_template"), {}) == "Templates"
    assert gt.asset_source(_n(org, "Source", "x", __source="curation"), {}) == "Authored"
    assert gt.asset_source(_n(org, "Source", "a.pdf"), {}) == "Files"


def test_keys_round_trip_any_name_and_the_root_name_is_stable(org):
    k = gt.key("tag", 'Files | "odd" : name', "a,b")
    assert gt.parse_key(k) == ("tag", ['Files | "odd" : name', "a,b"])
    with pytest.raises(ValueError):
        gt.parse_key("tag:not json")
    from opencortyx.knowledge.workspace import made_up_name
    assert made_up_name(org) == made_up_name(org) and " " in made_up_name(org)


# ── what counts as a root ──────────────────────────────────────────────────────────────────────────

def test_only_parts_no_asset_reaches_become_roots(org):
    doc = _n(org, "Source", "report.pdf")
    chunk = _n(org, "Chunk", "", doc.id)
    claim = _n(org, "Claim", "c", chunk.id)             # reached by provenance only
    acme = _n(org, "Organization", "Acme")              # reached by a lineage edge
    alice, bob, team = _n(org, "Person", "Alice"), _n(org, "Person", "Bob"), _n(org, "Team", "Core")
    alone = _n(org, "Term", "ARR")
    nodes = [doc, chunk, claim, acme, alice, bob, team, alone]
    edges = [_e(org, acme, chunk, "EXTRACTED_FROM"), _e(org, alice, team), _e(org, bob, team)]
    team.degree, alice.degree, bob.degree = 2, 1, 1
    roots = gt.unconnected_parts(nodes, edges)
    assert [(r["label"], r["size"]) for r in roots] == [("Core", 3), ("ARR", 1)]
    assert roots[0]["node_id"] == team.id and roots[0]["types"] == {"Person": 2, "Team": 1}


# ── opening an asset and a node ────────────────────────────────────────────────────────────────────

async def test_an_asset_opens_into_its_parts_then_what_they_link_to(org, store):
    doc = _n(org, "Source", "report.pdf")
    chunks = [_n(org, "Chunk", f"c{i}", doc.id) for i in range(3)]
    acme = _n(org, "Organization", "Acme")
    other = _n(org, "Source", "other.pdf")
    day = _n(org, "Date", "2026-09-28")
    await store.upsert_nodes([doc, *chunks, acme, other, day])
    await store.upsert_edges([_e(org, acme, chunks[0], "EXTRACTED_FROM"), _e(org, doc, other, "SIMILAR_TO"),
                              _e(org, doc, day, "CREATED_ON")])
    r = await gt.children(org, gt.key("asset", str(doc.id)))
    assert not r["grouped"] and r["total"] == 4
    assert [(i["label"], i["rel"]) for i in r["items"]] == [("c0", "part"), ("c1", "part"), ("c2", "part"),
                                                            ("Acme", "linked")]
    assert any(e["type"] == "EXTRACTED_FROM" for e in r["edges"])


async def test_a_big_asset_opens_into_groups_that_page_exactly(org, store):
    table = _n(org, "Table", "events")
    rows = [_n(org, "Row", f"r{i:04d}", table.id) for i in range(gt.GROUP_OVER + 50)]
    cols = [_n(org, "Attribute", f"col{i}", table.id) for i in range(4)]
    await store.upsert_nodes([table, *rows, *cols])
    r = await gt.children(org, gt.key("asset", str(table.id)))
    assert r["grouped"] and [(i["label"], i["count"]) for i in r["items"]] == [("Row", len(rows)), ("Attribute", 4)]
    seen = []
    for offset in range(0, len(rows), gt.PAGE):
        page = await gt.children(org, r["items"][0]["key"], offset)
        assert len(page["items"]) <= gt.PAGE and page["total"] == len(rows)
        seen += [i["label"] for i in page["items"]]
    assert seen == sorted(n.label for n in rows)


async def test_a_node_pages_across_its_relationships_and_groups_past_the_threshold(org, store):
    hub = _n(org, "Company", "Acme")
    people = [_n(org, "Person", f"p{i:03d}") for i in range(120)]
    docs = [_n(org, "Document", f"d{i}") for i in range(30)]
    await store.upsert_nodes([hub, *people, *docs])
    await store.upsert_edges([_e(org, p, hub, "WORKS_AT") for p in people]
                             + [_e(org, hub, d, "MENTIONED_IN") for d in docs])
    first = await gt.children(org, gt.key("node", str(hub.id)))
    second = await gt.children(org, gt.key("node", str(hub.id)), gt.PAGE)
    assert not first["grouped"] and first["total"] == 150
    assert first["next"] == gt.PAGE and second["next"] == 150
    ids = [i["id"] for i in first["items"] + second["items"]]
    assert len(ids) == 150 == len(set(ids))

    crowd = [_n(org, "Person", f"q{i}") for i in range(gt.GROUP_OVER)]
    await store.upsert_nodes(crowd)
    await store.upsert_edges([_e(org, p, hub, "WORKS_AT") for p in crowd])
    grouped = await gt.children(org, gt.key("node", str(hub.id)))
    assert grouped["grouped"]
    assert [(i["label"], i["count"]) for i in grouped["items"]] == [
        ("WORKS_AT ← Person", 120 + gt.GROUP_OVER), ("MENTIONED_IN → Document", 30)]
    page = await gt.children(org, grouped["items"][1]["key"])
    assert sorted(i["label"] for i in page["items"]) == sorted(d.label for d in docs)


async def test_paging_counts_relationships_not_the_neighbours_they_reach(org, store):
    a, b = _n(org, "Person", "Ann"), _n(org, "Company", "Acme")
    await store.upsert_nodes([a, b])
    await store.upsert_edges([Edge(workspace_id=org, source_id=a.id, target_id=b.id, type="REL",
                                   properties={"relation": r}) for r in ("works_at", "owns", "founded")])
    page = await gt.children(org, gt.key("node", str(a.id)))
    assert page["total"] == 3 and len(page["items"]) == 1 and page["next"] == 3


async def test_an_asset_reached_as_a_neighbour_opens_as_an_asset(org, store):
    person, doc = _n(org, "Person", "Alice"), _n(org, "Source", "cv.pdf")
    await store.upsert_nodes([person, doc])
    await store.upsert_edges([_e(org, person, doc, "MENTIONED_IN")])
    [item] = (await gt.children(org, gt.key("node", str(person.id))))["items"]
    assert item["kind"] == "asset" and gt.parse_key(item["key"]) == ("asset", [str(doc.id)])


async def test_unknown_keys_are_refused(org, store):
    with pytest.raises(ValueError):
        await gt.children(org, "nonsense:[]")
    with pytest.raises(ValueError):
        await gt.children(org, gt.key("tag", "only one arg"))
