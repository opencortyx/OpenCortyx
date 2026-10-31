"""Entities and relationships read from text without an LLM, and the schema links drawn between fields.

  - the model's own labels name the types and the sentence's verb names the relationship - nothing
    listed by hand; a bare number is a value, not an entity;
  - one entity per (type, name) across the workspace; the text's holder MENTIONS it; every entity and
    relationship cites its passage and sentence; the new words reach the ontology as candidates;
  - a detected link becomes a JOINS_TO between the two fields' own nodes, in every pair of assets.

The writing tests use fakes; the grammar test reads with the real model when it is installed.
"""
import uuid
from types import SimpleNamespace

import pytest

from opencortyx.ingest.extraction import text_reader as tr

WS = uuid.uuid4()


# ── naming comes from the model and the sentence ──────────────────────────────────────────────

def test_types_and_relations_are_named_by_the_model_and_the_sentence():
    assert tr._type("PERSON") == "Person" and tr._type("ORG") == "Org" and tr._type("") == "Entity"
    verb = SimpleNamespace(lemma_="work")
    assert tr._relation(verb, None) == "WORK"
    assert tr._relation(verb, SimpleNamespace(lemma_="at")) == "WORK_AT"
    assert tr._relation(SimpleNamespace(lemma_="sign-off"), None) == "SIGN_OFF"


def test_a_bare_number_is_a_value_not_an_entity():
    tok = lambda **kw: SimpleNamespace(**{"like_num": False, "is_punct": False, "is_space": False,
                                          "is_currency": False, **kw})
    assert tr._is_value([tok(like_num=True)])
    assert tr._is_value([tok(is_currency=True), tok(like_num=True)])
    assert not tr._is_value([tok(), tok(like_num=True)]), "March 2021 is a date, not a bare number"


def test_the_same_name_is_one_entity_whatever_its_spelling():
    assert tr.entity_id(WS, "Org", "Acme  Corp") == tr.entity_id(WS, "Org", "acme corp")
    assert tr.entity_id(WS, "Org", "Acme") != tr.entity_id(WS, "Person", "Acme")


# ── writing what was read ─────────────────────────────────────────────────────────────────────

class _Repo:
    def __init__(self):
        self.nodes, self.edges = {}, []

    async def upsert_node(self, *, node_id, workspace_id, type, label, properties, **kw):
        self.nodes[node_id] = SimpleNamespace(id=node_id, type=type, label=label, properties=properties)
        return self.nodes[node_id]

    async def add_edge(self, *, workspace_id, source_id, target_id, type, properties=None, **kw):
        self.edges.append((source_id, type, target_id, properties or {}))
        return True

    async def find(self, workspace_id, *, types=None, limit=None, **kw):
        return [n for n in self.nodes.values() if not types or n.type in types]

    async def add_edges(self, *, workspace_id, edges, **kw):
        self.edges += [(e["source_id"], e["type"], e["target_id"], e.get("properties") or {}) for e in edges]


@pytest.mark.anyio
async def test_what_is_read_is_written_once_mentioned_cited_and_linked(monkeypatch):
    from contextlib import asynccontextmanager

    from opencortyx.core.dataplane import evidence
    repo = _Repo()
    units, registered, linked = [], {}, []

    async def model(_):
        return {"model_id": "fake/model"}

    def read(nlp, texts):
        return [tr.Found(entities=[("Alice Smith", "PERSON"), ("Acme", "ORG")],
                         relations=[(0, "JOIN", 1, "Alice Smith joined Acme.")],
                         sentences={0: "Alice Smith joined Acme.", 1: "Alice Smith joined Acme."}),
                tr.Found(entities=[("ACME", "ORG")], sentences={0: "ACME grew."}),
                tr.Found()]

    @asynccontextmanager
    async def collect(ws, **kw):
        yield None

    real_unit = evidence.unit

    def unit(asset, kind, key, **kw):
        units.append((asset, kind, key, kw.get("quote")))
        return real_unit(asset, kind, key, **kw)

    async def register(db, ws, emit, **kw):
        registered.update(emit)

    async def link(ws, nodes, repo=None):
        linked.extend(nodes)
        return 1

    class _Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            pass

    monkeypatch.setattr(tr, "model", model)
    monkeypatch.setattr(tr, "load", lambda m: object())
    monkeypatch.setattr(tr, "read", read)
    monkeypatch.setattr(evidence, "collect", collect)
    monkeypatch.setattr(evidence, "unit", unit)
    monkeypatch.setattr("opencortyx.core.substrate.NodeRepo", lambda: repo)
    monkeypatch.setattr("opencortyx.ontology.extractors.register_emit_types", register)
    monkeypatch.setattr("opencortyx.resolution.mentions.link_mentions", link)
    monkeypatch.setattr("opencortyx.core.db.V2Session", _Db)

    doc = uuid.uuid4()
    res = await tr.read_inputs(WS, [tr.Input("Alice Smith joined Acme.", str(doc), "chunk", "p1", holder=doc),
                                    tr.Input("ACME grew.", str(doc), "chunk", "p2", holder=doc),
                                    tr.Input("Nothing named here.", str(doc), "chunk", "p3", holder=doc),
                                    tr.Input("   ", str(doc), "chunk", "p4", holder=doc)])
    assert res["texts"] == 3, "an empty text is not read"
    assert res["entities"] == 2, "Acme and ACME are one entity"
    assert {n.type for n in repo.nodes.values()} == {"Person", "Org"}
    mentions = [e for e in repo.edges if e[1] == "MENTIONS"]
    assert len(mentions) == 3 and all(e[0] == doc for e in mentions)
    (rel,) = [e for e in repo.edges if e[1] == "JOIN"]
    assert repo.nodes[rel[0]].label == "Alice Smith" and repo.nodes[rel[2]].label == "Acme"
    assert ("p1" in {u[2] for u in units}) and any(u[3] == "Alice Smith joined Acme." for u in units)
    assert registered == {"nodes": [{"type": "Org"}, {"type": "Person"}], "edges": [{"type": "JOIN"}]}
    assert len(linked) == 2 and res["linked"] == 1


@pytest.mark.anyio
async def test_no_model_is_said_not_guessed(monkeypatch):
    async def none(_):
        return None
    monkeypatch.setattr(tr, "model", none)
    with pytest.raises(RuntimeError, match="Entities"):
        await tr.read_inputs(WS, [tr.Input("Alice joined Acme.", "a", "chunk", "p")])
    assert (await tr.read_inputs(WS, [])) ["texts"] == 0


# ── the real model: entities, and a relationship from the grammar ─────────────────────────────

def test_the_grammar_gives_the_relationship():
    spacy = pytest.importorskip("spacy")
    try:
        nlp = tr.load("spacy/en_core_web_sm")
    except Exception as e:                                    # noqa: BLE001 - no network, no model
        pytest.skip(f"model not available: {e}")
    assert spacy
    (f,) = tr.read(nlp, ["Alice Smith joined Acme Corporation in Paris in 2021."])
    names = {n for n, _ in f.entities}
    assert {"Alice Smith", "Acme Corporation", "Paris"} <= names
    assert "2021" not in names, "a bare year is a value"
    rels = {(f.entities[i][0], r, f.entities[j][0]) for i, r, j, _ in f.relations}
    assert ("Alice Smith", "JOIN", "Acme Corporation") in rels
    assert ("Alice Smith", "JOIN_IN", "Paris") in rels


# ── a detected link between the fields themselves ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_a_detected_link_joins_the_two_fields_in_every_pair_of_assets(monkeypatch):
    from opencortyx.knowledge.semantic import publish
    from opencortyx.knowledge.semantic.store import Binding, Layer, LinkType, ObjectType
    ids = {(a, c): uuid.uuid4() for a in ("orders-1", "orders-2", "crm") for c in ("customer_id", "id")}

    class Repo:
        edges: list = []

        async def find(self, workspace_id, types=None, props=None, **kw):
            a = props["parent_table_id"]
            return [SimpleNamespace(id=ids[(a, c)], label=c, properties={"physical_column": c})
                    for c in ("customer_id", "id")]

        async def add_edges(self, *, workspace_id, edges, _quarantine=True):
            Repo.edges += edges
    monkeypatch.setattr("opencortyx.core.substrate.NodeRepo", Repo)
    order = ObjectType(name="Order", keys=["oid"], bindings=[Binding("orders-1", "o1.csv", "structured"),
                                                             Binding("orders-2", "o2.json", "semistructured")])
    cust = ObjectType(name="Customer", keys=["id"], bindings=[Binding("crm", "crm.csv", "structured")])
    layer = Layer(types=[order, cust], links=[LinkType("CUSTOMER", "Order", "customer_id", "Customer", "id",
                                                        coverage=0.97)])
    assert await publish.link_schema(WS, layer) == 2
    assert {(e["source_id"], e["target_id"]) for e in Repo.edges} == {
        (ids[("orders-1", "customer_id")], ids[("crm", "id")]), (ids[("orders-2", "customer_id")], ids[("crm", "id")])}
    assert all(e["type"] == "JOINS_TO" and e["properties"]["relationship_type"] == "INFERRED_REFERENCE"
               for e in Repo.edges)


def test_an_extractor_and_the_reader_give_one_named_thing_one_id():
    from opencortyx.ingest.extraction import templates
    assert templates._named_id(WS, "Work_of_art", "The Nightingale") == tr.entity_id(WS, "Work_of_art", "The Nightingale")
    assert templates._named_id(WS, "Work_of_art", "The Nightingale") != templates._named_id(WS, "Person", "The Nightingale")
