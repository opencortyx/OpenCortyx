"""Where a retrieved node came from — against the in-memory graph store, no services.

The contract the chat and every API client rely on: each node has an `asset_id` or an `origin` saying
why it has none; a node inside an asset resolves to that asset however deep it sits; and a SHARED
entity mentioned in several assets lists all of them, the one whose mentioning chunk best matches the
question first, with that chunk as the evidence.
"""
import uuid
from datetime import datetime, timezone

from opencortyx.core.substrate.provenance import assets_of, provenance, sources_page
from opencortyx.stores.graph.adapters.memory import MemoryGraphStore
from opencortyx.stores.graph.models import Edge, Node

ORG = uuid.uuid4()


def _n(type, label="", source_ref=None, **props) -> Node:
    now = datetime.now(timezone.utc)
    return Node(id=uuid.uuid4(), workspace_id=ORG, type=type, label=label, source_ref=source_ref,
                properties=props, created_at=now, updated_at=now)


def _lineage(entity: Node, chunk: Node) -> Edge:
    return Edge(workspace_id=ORG, source_id=entity.id, target_id=chunk.id, type="EXTRACTED_FROM")


async def _graph():
    g = MemoryGraphStore()
    report = _n("Source", "report.pdf")
    news = _n("Source", "news.md")
    table = _n("Table", "customers")
    a1 = _n("Chunk", "", report.id, text="Acme revenue grew 12% in 2025, driven by cloud sales.")
    b1 = _n("Chunk", "", news.id, text="Acme opened a new office in Paris last spring.")
    row = _n("Row", "row 7", table.id)
    # a JSON payload: each record points at the record above it, the root at the asset
    doc = _n("Source", "orders.json")
    rec1 = _n("Record", "order", doc.id)
    rec2 = _n("Record", "line item", rec1.id)
    acme = _n("Organization", "Acme", a1.id)          # shared entity: source_ref is its first chunk
    curated = _n("Term", "ARR", __source="curation")
    concept = _n("Person", "Person", __template="person", __concept=True)
    memory = _n("Decision", "ship on Friday", __memory="true", __source="user")
    loop_a = _n("Record", "a")
    loop_b = _n("Record", "b", loop_a.id)
    loop_a.source_ref = loop_b.id                      # a corrupt cycle must not hang the walk
    nodes = [report, news, table, a1, b1, row, doc, rec1, rec2, acme, curated, concept, memory, loop_a, loop_b]
    await g.upsert_nodes(nodes)
    await g.upsert_edges([_lineage(acme, a1), _lineage(acme, b1)])
    return g, {n.label or n.properties.get("text", "")[:10]: n for n in nodes} | {"a1": a1, "b1": b1}


async def test_a_node_inside_an_asset_resolves_to_it_at_any_depth():
    g, n = await _graph()
    p = await provenance(ORG, [n["a1"].id, n["row 7"].id, n["line item"].id, n["report.pdf"].id], store=g)
    assert p[str(n["a1"].id)]["asset_id"] == str(n["report.pdf"].id)
    assert p[str(n["row 7"].id)]["asset_id"] == str(n["customers"].id)
    assert p[str(n["line item"].id)]["asset_id"] == str(n["orders.json"].id)
    assert p[str(n["report.pdf"].id)]["asset_id"] == str(n["report.pdf"].id)   # an asset is its own


async def test_shared_entity_ranks_its_assets_by_the_question():
    g, n = await _graph()
    acme = str(n["Acme"].id)

    revenue = (await provenance(ORG, [acme], query="What was Acme's revenue in 2025?", store=g))[acme]
    assert revenue["asset_id"] == str(n["report.pdf"].id)
    assert revenue["source_count"] == 2
    assert revenue["sources"][0]["via"] == str(n["a1"].id), "the evidence is the chunk that says it"
    assert "revenue" in revenue["sources"][0]["snippet"]

    office = (await provenance(ORG, [acme], query="Where did Acme open an office?", store=g))[acme]
    assert office["asset_id"] == str(n["news.md"].id)
    assert {s["asset_id"] for s in office["sources"]} == {str(n["report.pdf"].id), str(n["news.md"].id)}


async def test_every_node_has_an_asset_or_says_why_not():
    g, n = await _graph()
    p = await provenance(ORG, [n[k].id for k in ("ARR", "Person", "ship on Friday", "a")], store=g)
    assert p[str(n["ARR"].id)]["origin"] == "curation"
    assert p[str(n["Person"].id)]["origin"] == "template"
    assert p[str(n["ship on Friday"].id)]["origin"] == "memory"
    assert p[str(n["a"].id)]["asset_id"] is None                  # the cycle ends, it does not hang
    for entry in p.values():
        assert entry["asset_id"] is not None or entry["origin"] != "asset"


async def test_hub_entity_is_capped_and_paged():
    g = MemoryGraphStore()
    hub = _n("Organization", "Globex")
    nodes, edges = [hub], []
    for i in range(12):
        src = _n("Source", f"doc-{i:02d}.pdf")
        chunk = _n("Chunk", "", src.id, text=f"Globex note {i}")
        nodes += [src, chunk]
        edges.append(_lineage(hub, chunk))
    await g.upsert_nodes(nodes)
    await g.upsert_edges(edges)

    p = (await provenance(ORG, [hub.id], store=g, lineage_cap=4))[str(hub.id)]
    assert p["source_count_capped"] and p["source_count"] == 4 and len(p["sources"]) <= 5

    page1 = await sources_page(ORG, hub.id, store=g, offset=0, limit=5)
    page3 = await sources_page(ORG, hub.id, store=g, offset=10, limit=5)
    assert page1["total"] == 12 and not page1["capped"]
    assert len(page1["items"]) == 5 and len(page3["items"]) == 2
    assert not {s["asset_id"] for s in page1["items"]} & {s["asset_id"] for s in page3["items"]}


async def test_context_and_citations_carry_the_source():
    """What an API or MCP caller receives: every packed block names its asset (or its origin) inside
    `context` itself, and every citation carries the ranked sources."""
    import json
    from opencortyx.retrieval.adapter import _citations, _serialise
    from opencortyx.retrieval.contracts import ContextBlock

    g, n = await _graph()
    acme, row, arr = n["Acme"], n["row 7"], n["ARR"]
    blocks = [ContextBlock(node_id=x.id, role="primary", text=x.label, node_type=x.type) for x in (acme, row, arr)]
    prov = await provenance(ORG, [b.node_id for b in blocks], query="Acme revenue", store=g)

    lines = [json.loads(line) for line in _serialise(blocks, prov)[0].splitlines()]
    by_id = {line["node_id"]: line for line in lines}
    assert by_id[str(acme.id)]["asset"] == "report.pdf" and by_id[str(acme.id)]["also_in"] == "1 more assets"
    assert by_id[str(row.id)]["asset_id"] == str(n["customers"].id) and "also_in" not in by_id[str(row.id)]
    assert by_id[str(arr.id)]["origin"] == "curation" and "asset" not in by_id[str(arr.id)]

    cites = _citations(blocks, prov)
    assert all(c["asset_id"] or c["origin"] not in ("asset", None) for c in cites)
    assert cites[0]["source_count"] == 2 and cites[0]["sources"][0]["via"] == str(n["a1"].id)


async def test_walked_json_record_climbs_containment_edges_to_its_file():
    """A walked JSON payload nests records with HAS_* edges, not `source_ref`; the record still
    resolves to its file, and a NEXT reading-order edge is never mistaken for containment."""
    g = MemoryGraphStore()
    doc = _n("Source", "deep.json")
    other = _n("Source", "other.json")
    programme = _n("KeyNode", "programme", __source="json")
    project = _n("KeyNode", "Payments Core Rewrite", __source="json",
                 __text="project_id: PRJ-11\nname: Payments Core Rewrite")
    sibling = _n("KeyNode", "Atlas", __source="json")
    await g.upsert_nodes([doc, other, programme, project, sibling])
    await g.upsert_edges([
        Edge(workspace_id=ORG, source_id=doc.id, target_id=programme.id, type="HAS_PROGRAMME"),
        Edge(workspace_id=ORG, source_id=programme.id, target_id=project.id, type="HAS_PROJECT"),
        Edge(workspace_id=ORG, source_id=sibling.id, target_id=project.id, type="NEXT"),
        Edge(workspace_id=ORG, source_id=other.id, target_id=sibling.id, type="HAS_PROJECT"),
    ])
    p = (await provenance(ORG, [project.id], query="payments", store=g))[str(project.id)]
    assert p["asset_id"] == str(doc.id) and p["source_count"] == 1
    assert p["sources"][0]["snippet"] == "project_id: PRJ-11 · name: Payments Core Rewrite"   # its fields, not its label


async def test_assets_of_lists_the_assets_nodes_sit_in_and_skips_the_rest():
    # what "re-check existing data" queues: each asset once, nothing for nodes outside any asset
    g, n = await _graph()
    ids = [n["line item"].id, n["order"].id, n["row 7"].id, n["ARR"].id, n["a"].id]
    assert await assets_of(ORG, ids, store=g) == {str(n["orders.json"].id), str(n["customers"].id)}
