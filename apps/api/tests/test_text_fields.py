"""Text inside records, and what is read from it, connected to the records themselves.

  - the Detector tells free text (prose) from a short value, and finds the field that names a record;
  - a person's "publish to graph" survives detection rewriting the structure, and only its change
    triggers work;
  - a free-text field is read for its record: a published record MENTIONS what it names, else its
    asset does;
  - an extracted entity is linked to the one published record its name names, never guessed between two.

No services: fakes stand in for the graph and the model.
"""
import uuid
from types import SimpleNamespace

import pytest

from opencortyx.knowledge.semantic import detector, store
from opencortyx.knowledge.semantic.store import Binding, Layer, LinkType, ObjectType, Property

WS = uuid.uuid4()


def _stats(**kw):
    return detector.FieldStats(**{"rows": 10, "filled": 10, "distinct": 10, **kw})


# ── the Detector ──────────────────────────────────────────────────────────────────────────────

def test_free_text_is_prose_and_a_short_value_is_not():
    assert detector._role("notes", _stats(words=23.0), "id", set()) == "prose"
    assert detector._role("name", _stats(words=2.0), "id", set()) == "text"
    assert detector._role("status", _stats(words=1.0, value_set={"open": 6, "closed": 4}), "id", set()) == "category"


def test_a_key_repeats_nowhere_within_a_file_and_may_recur_across_files():
    g = detector.Group(kind="structured", path="", sources=[], fields={}, rows=8)
    g.stats = {"customer_id": _stats(rows=8, filled=8, distinct=4, repeats=0),   # in the CRM and in billing
               "region": _stats(rows=8, filled=8, distinct=2, repeats=3),
               "email": _stats(rows=8, filled=7, distinct=7, repeats=0),         # one missing
               "joined": _stats(rows=8, filled=8, distinct=8, repeats=0, data_type="date")}
    assert detector._unique(g) == ["customer_id"]


@pytest.mark.anyio
async def test_the_naming_field_is_a_nearly_unique_short_text_the_model_picks():
    t = ObjectType(name="Customer", keys=["id"], properties=[
        Property("id", role="key"), Property("name"), Property("email"), Property("notes", role="prose"),
        Property("tier", role="category")])
    stats = {"id": _stats(data_type="number", values={"1", "2"}),
             "name": _stats(values={"Alice Smith", "Bob Lee"}), "email": _stats(values={"a@x.io", "b@x.io"}),
             "notes": _stats(values={"long text"}), "tier": _stats(distinct=2, values={"gold"})}
    seen = {}

    async def choose(prompt, options, baseline):
        seen["options"] = options
        return "name", 0.9
    assert await detector._label(t, stats, choose) == "name"
    assert seen["options"] == ["name", "email"], "prose, categories and a numeric key are not names"

    async def no_model(*a):
        return None
    assert await detector._label(t, stats, no_model) in ("name", "email")
    assert await detector._label(ObjectType(name="Event", kind="event"), stats, choose) == ""

    # Repeated across files but never within one: still a name (one customer in thirty billing files).
    stats["name"] = _stats(distinct=2, filled=10, repeats=0, values={"Alice Smith", "Bob Lee"})

    # A phrase names a record; a token (an email) does not: decided without the model.
    stats["name"].words, stats["email"].words = 2.0, 1.0
    async def wrong(*a):
        return "email", 0.9
    assert await detector._label(t, stats, wrong) == "name"


# ── "publish to graph" survives detection, and only its change triggers work ──────────────────

def test_detection_keeps_what_a_person_chose():
    t = ObjectType(name="Customer", keys=["id"], properties=[Property("id", role="key"), Property("notes", role="prose")])
    fresh = store._entity_dict(t, "", [Binding("a1", "crm.csv", "structured")])
    assert store._keep_choices(dict(fresh), {"materialize": True})["materialize"] is True
    assert store._keep_choices(dict(fresh), {})["materialize"] is False


def test_only_a_changed_publish_choice_is_acted_on():
    base = {"entities": [{"type": "Customer", "materialize": False}]}
    assert not store.publish_changed(base, {"entities": [{"type": "Customer", "materialize": False,
                                                          "description": "edited"}]})
    assert store.publish_changed(base, {"entities": [{"type": "Customer", "materialize": True}]})
    assert store.publish_changed({}, {"entities": [{"type": "New", "materialize": True}]})


def test_redetecting_one_mapping_takes_its_own_assets_and_keeps_links_and_choices():
    m = SimpleNamespace(payload={"knowledge": {"entities": [
        {"type": "Customer", "path": "rows", "assets": [{"id": "a1", "name": "crm.csv"}], "materialize": True}]}})
    cust = ObjectType(name="Customer", keys=["id"], bindings=[Binding("a1", "crm.csv", "structured"),
                                                              Binding("a9", "other.csv", "structured")],
                      properties=[Property("id", role="key"), Property("notes", role="prose")])
    order = ObjectType(name="Order", keys=["oid"], bindings=[Binding("a2", "orders.csv", "structured")])
    layer = Layer(types=[cust, order], links=[LinkType("PLACED_BY", "Customer", "id", "Order", "oid")])
    k = store.redetected(m, layer)
    assert [e["type"] for e in k["entities"]] == ["Customer"]
    assert [a["id"] for a in k["entities"][0]["assets"]] == ["a1"], "only this mapping's assets"
    assert k["entities"][0]["materialize"] is True and k["curated"] is False
    assert k["relationships"][0]["to_type"] == "Order", "a link to another file is kept"
    assert store.redetected(SimpleNamespace(payload={"knowledge": {"entities": [
        {"type": "X", "assets": [{"id": "zz"}]}]}}), layer) is None


# ── free text in records, read by the text reader ─────────────────────────────────────────────

def test_a_published_record_mentions_what_its_text_names_else_its_asset():
    from opencortyx.knowledge.semantic import text_fields
    from opencortyx.knowledge.semantic.publish import entity_id
    t = ObjectType(name="Customer", keys=["id"], label="name")
    rows = [{"id": 7, "notes": "Asked about the X200 router.", "comment": "Called twice.", "_assets": "crm.csv"},
            {"id": 8, "notes": None, "comment": "", "_assets": "crm.csv"},
            {"id": 9, "notes": "Unknown file.", "_assets": "other.csv"}]
    asset = str(uuid.uuid4())
    pub = text_fields._inputs(WS, t, rows, ["notes", "comment"], True, {"crm.csv": asset}, "row")
    assert [i.text for i in pub] == ["Asked about the X200 router.", "Called twice."], "empty values and unknown files skipped"
    assert all(str(i.holder) == entity_id(WS, "Customer", "7") and i.unit_key == "7" and i.unit_kind == "row"
               and i.asset_id == asset for i in pub)
    (one, _) = text_fields._inputs(WS, t, rows, ["notes", "comment"], False, {"crm.csv": asset}, "record")
    assert str(one.holder) == asset and one.unit_kind == "record", "unpublished: the asset holds the mention"


# ── mentions resolved by name ─────────────────────────────────────────────────────────────────

class _Repo:
    def __init__(self, records):
        self.records, self.edges = records, []

    async def find(self, workspace_id, exact_types=None, name_key=None, limit=None, **kw):
        from opencortyx.stores.graph.query import normalize_name
        return [r for r in self.records if r.type in exact_types and normalize_name(r.label) == name_key][:limit]

    async def add_edges(self, *, workspace_id, edges, _quarantine=True):
        self.edges += edges


def _n(label, type="Person", entity=True):
    return SimpleNamespace(id=uuid.uuid4(), label=label, type=type,
                           properties={"__identity": "entity"} if entity else {})


@pytest.mark.anyio
async def test_a_mention_meets_the_one_record_it_names(monkeypatch):
    from opencortyx.resolution import mentions

    async def published(_):
        return ["Customer"]
    monkeypatch.setattr(mentions, "_published_types", published)
    alice = _n("Alice Smith", "Customer", entity=False)
    twins = [_n("Sam Lee", "Customer", entity=False), _n("Sam Lee", "Customer", entity=False)]
    repo = _Repo([alice, *twins])
    said = [_n("alice  smith"), _n("Sam Lee"), _n("Nobody Known"), _n("Alice Smith", entity=False)]
    assert await mentions.link_mentions(WS, said, repo=repo) == 1
    (e,) = repo.edges
    assert e["target_id"] == alice.id and e["type"] == "SAME_AS" and e["source_id"] == said[0].id
    assert await mentions.link_mentions(WS, [], repo=repo) == 0


@pytest.mark.anyio
async def test_no_published_records_means_no_links(monkeypatch):
    from opencortyx.resolution import mentions

    async def none(_):
        return []
    monkeypatch.setattr(mentions, "_published_types", none)
    repo = _Repo([_n("Alice Smith", "Customer", entity=False)])
    assert await mentions.link_mentions(WS, [_n("Alice Smith")], repo=repo) == 0 and not repo.edges


# ── a document asks nothing of detection ──────────────────────────────────────────────────────

def test_documents_book_no_detection_and_tables_carry_their_asset(monkeypatch):
    from opencortyx.ingest.runs import progress
    from opencortyx.tasks import maintenance
    calls = []
    monkeypatch.setattr(maintenance, "schedule_detection", lambda ws: calls.append(("detect", ws)))
    monkeypatch.setattr(maintenance, "book_after_detection", lambda ws, a, r=None: calls.append(("book", a, r)))
    progress._schedule_detection(WS, "document", "run1", "asset1")
    assert calls == []
    progress._schedule_detection(WS, "structured", "run1", "asset1")
    assert calls == [("book", "asset1", "run1"), ("detect", WS)]


# ── Add data's two graph options reach a document's plan ─────────────────────────────────────

def test_the_upload_options_become_the_documents_plan():
    from opencortyx.tasks._shared import EXTRACTOR_FEATURE, KNOWLEDGE_FEATURE, WHOLE_DATA_FEATURE, _doc_plan_from_features
    assert _doc_plan_from_features(None) is None
    plain = _doc_plan_from_features("no_assets")
    assert plain == {"asset_sections": False}, "nothing read, nothing extra written"
    both = _doc_plan_from_features(f"{KNOWLEDGE_FEATURE},{WHOLE_DATA_FEATURE}")
    assert both == {"asset_sections": True, "read_entities": True, "whole_data": True}
    custom = _doc_plan_from_features(f"{EXTRACTOR_FEATURE}my:people,{EXTRACTOR_FEATURE}my:people,{EXTRACTOR_FEATURE}")
    assert custom == {"asset_sections": True, "templates": ["my:people"]}, "Custom names the extractors, once each"


def test_a_type_view_asks_only_for_the_fields_its_assets_provide():
    from opencortyx.knowledge.semantic.views import type_sql
    t = ObjectType(name="Customer", keys=["id"], properties=[Property("id", role="key"), Property("name"),
                                                             Property("billing_currency", role="category")],
                   bindings=[Binding("a1", "crm.csv", "structured", fields={"id": "id", "name": "name"})])
    sql = type_sql(t, {"a1": "crm_csv"})
    assert '"name"' in sql and "billing_currency" not in sql, "a property no asset provides is not asked for"
