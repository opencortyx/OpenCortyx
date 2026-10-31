"""Named things: one identity per (type, normalised name) whichever extractor names them; the model's
entities and relationships read into records; and a detected link joining the two fields."""
import uuid
from types import SimpleNamespace

import pytest

from opencortyx.ingest.extraction import templates

WS = uuid.uuid4()


def test_the_same_name_is_one_thing_whatever_its_spelling_and_another_type_is_another():
    assert templates.entity_id(WS, "Org", "Acme  Corp") == templates.entity_id(WS, "Org", "acme corp")
    assert templates.entity_id(WS, "Work_of_art", "The Nightingale") == templates.entity_id(WS, "Work_of_art", "Nightingale")
    assert templates.entity_id(WS, "Org", "Acme") != templates.entity_id(WS, "Person", "Acme")
    assert templates._named_id(WS, "Work_of_art", "The Nightingale") == templates.entity_id(WS, "Work_of_art", "The Nightingale")


@pytest.mark.anyio
async def test_the_model_s_entities_and_relationships_become_records(monkeypatch):
    async def complete(prompt, **kw):
        assert "Alice Smith joined Acme." in prompt
        return ('{"entities":[{"name":"Alice Smith","type":"person"},{"name":"Acme","type":"Org"},'
                '{"name":"Acme","type":"Org"}],"relationships":[{"subject":"Alice Smith","relation":"joined",'
                '"object":"Acme"},{"subject":"Bob","relation":"knows","object":"Acme"}]}')
    monkeypatch.setattr(templates, "complete", complete)
    recs = await templates._ner_records("Alice Smith joined Acme.")
    assert recs[:2] == [{"name": "Alice Smith", "type": "Person"}, {"name": "Acme", "type": "Org"}], "each named once"
    assert recs[2:] == [{"name": "Alice Smith", "type": "Person", "relation": "JOINED", "object": "Acme",
                         "object_type": "Org"}], "a relationship only between named things it found"


@pytest.mark.anyio
async def test_an_unreadable_answer_finds_nothing(monkeypatch):
    async def complete(prompt, **kw):
        return "not json"
    monkeypatch.setattr(templates, "complete", complete)
    assert await templates._ner_records("Alice joined Acme.") == []


@pytest.mark.anyio
async def test_the_model_is_shown_the_workspace_s_types_and_a_spelling_of_one_is_that_one(monkeypatch):
    """The model sees the named-thing types already here, most used first, to reuse them ("Place",
    not "Places"); a type that differs from one only in case or separators is that one ("Time
    period" is Time_Period). No plural or language rule decides: "novels" is a new type the rest of
    the run spells the same way, and a type nothing names yet is not shown."""
    from types import SimpleNamespace as NS

    class Repo:
        async def find(self, ws, query):
            assert query.props == {"__identity": "entity"}, "only named things' types"
            return [NS(type="Place")] * 8 + [NS(type="Time_Period")] + [NS(type="Library")]
    monkeypatch.setattr(templates, "NodeRepo", Repo)
    templates._KNOWN_TYPES.clear()
    prompts = []

    async def complete(prompt, **kw):
        prompts.append(prompt)
        return ('{"entities":[{"name":"Hawaii","type":"place"},{"name":"Q3","type":"Time period"},'
                '{"name":"Dune","type":"novels"},{"name":"Emma","type":"Novels"}],"relationships":[]}')
    monkeypatch.setattr(templates, "complete", complete)
    recs = await templates._ner_records("Hawaii in Q3, reading Dune and Emma.", workspace_id=WS)
    assert "Types already used here: Place, Time_Period, Library." in prompts[0]
    assert prompts[0].endswith("TEXT:\nHawaii in Q3, reading Dune and Emma.")
    assert [r["type"] for r in recs] == ["Place", "Time_Period", "Novels", "Novels"]


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


@pytest.mark.anyio
async def test_a_type_filled_from_each_record_is_not_registered_as_a_type(monkeypatch):
    """The people-and-places extractor writes nodes typed "$type" and edges "$relation": the model's
    own words for each record. They are slots, never ontology to review; a named type still is."""
    from opencortyx.ontology import extractors
    written = []

    async def find_type(db, ws, name, kind):
        return None
    monkeypatch.setattr("opencortyx.domain.ontology.find_type", find_type)

    class Db:
        async def execute(self, stmt):
            written.append(stmt.compile().params["name"])
            return SimpleNamespace(rowcount=1)
    emit = {"nodes": [{"type": "$type"}, {"type": "Risk"}],
            "edges": [{"type": "MENTIONS"}, {"type": "$relation"}]}
    made = await extractors.register_emit_types(Db(), WS, emit)
    assert "$type" not in written and "$relation" not in written
    assert "Risk" in written and made == len(written)
