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

WS_ID = uuid.UUID("79f79e1d-2c97-49d3-bc3e-b13e88b0bb67")

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


# ── Add data's options reach a document's plan ─────────────────────────────────────

def test_the_upload_options_become_the_documents_plan():
    from opencortyx.tasks._shared import EXTRACTOR_FEATURE, WHOLE_DATA_FEATURE, _doc_plan_from_features
    assert _doc_plan_from_features(None) is None
    plain = _doc_plan_from_features("no_assets")
    assert plain == {"asset_sections": False}, "nothing read, nothing extra written"
    whole = _doc_plan_from_features(f"knowledge,{WHOLE_DATA_FEATURE}")
    assert whole == {"asset_sections": True, "whole_data": True}, "no text reading: entities come from an extractor"
    custom = _doc_plan_from_features(f"{EXTRACTOR_FEATURE}my:people,{EXTRACTOR_FEATURE}my:people,{EXTRACTOR_FEATURE}")
    assert custom == {"asset_sections": True, "templates": ["my:people"]}, "Custom names the extractors, once each"


def test_a_type_view_asks_only_for_the_fields_its_assets_provide():
    from opencortyx.knowledge.semantic.views import type_sql
    t = ObjectType(name="Customer", keys=["id"], properties=[Property("id", role="key"), Property("name"),
                                                             Property("billing_currency", role="category")],
                   bindings=[Binding("a1", "crm.csv", "structured", fields={"id": "id", "name": "name"})])
    sql = type_sql(t, {"a1": "crm_csv"})
    assert '"name"' in sql and "billing_currency" not in sql, "a property no asset provides is not asked for"


# ── Extract knowledge: a published record carries the knowledge columns ─────────────────────────

def test_a_published_record_carries_the_knowledge_columns_unless_a_person_chose_others():
    t = ObjectType(name="Customer", keys=["customer_id"], label="name", properties=[
        Property("customer_id", role="key"), Property("name", role="text"), Property("region", role="category"),
        Property("amount", role="measure"), Property("notes", role="prose"), Property("since", role="time"),
        Property("account", role="reference")])
    assert t.published_fields() == ["customer_id", "name", "notes", "since", "account"], \
        "identifier, name, text, dates and links; measures and codes stay in Delta"
    t.attributes = ["region"]
    assert t.published_fields() == ["customer_id", "name", "region"], "a person's choice; the identity always"


def test_one_row_is_one_node_identified_or_not():
    from opencortyx.knowledge.semantic import publish
    turn = ObjectType(name="Turn", kind="event", materialize=True, properties=[
        Property("content", role="prose"), Property("session", role="reference"), Property("n", role="measure")])
    a = publish.row_key(turn, {"content": "I met Sarah.", "session": "s1", "n": 1})
    assert a == publish.row_key(turn, {"content": "I met Sarah.", "session": "s1", "n": 99}), \
        "an event is named by its knowledge values: the same turn read again is the same node"
    assert a != publish.row_key(turn, {"content": "I met Bob.", "session": "s1"})
    customer = ObjectType(name="Customer", keys=["id"], properties=[Property("id", role="key")])
    assert publish.row_key(customer, {"id": "C-1"}) == "C-1"
    layer = Layer(types=[turn, customer, ObjectType(name="Order", keys=["oid"])])
    assert [t.name for t in publish._chosen(layer, everything=False)] == ["Turn"], "what a mapping marks, events too"
    assert [t.name for t in publish._chosen(layer, everything=True)] == ["Turn", "Customer", "Order"]


def test_a_turn_is_the_same_turn_when_its_question_s_date_is_corrected():
    """A turn inside a question's sessions carries the question's date (read from "^.") and the
    question it belongs to (a reference). Correcting the date leaves the turn the same node; the
    same words in another question are another turn. It is called by its words, not its speaker."""
    from opencortyx.knowledge.semantic import publish
    turn = ObjectType(name="Turn", kind="event", materialize=True, properties=[
        Property("role", role="text"), Property("content", role="prose"), Property("question_id", role="reference"),
        Property("question_date", role="time")],
        bindings=[Binding("a1", "lme.json", "semistructured", path="$.sessions[*][*]",
                          fields={"role": "role", "content": "content", "question_id": "^.question_id",
                                  "question_date": "^.question_date"})])
    row = {"role": "user", "content": "Only 120 pages are left.", "question_id": "q1", "question_date": "2022-04-04"}
    assert publish.row_key(turn, row) == publish.row_key(turn, {**row, "question_date": "2022-04-05"})
    assert publish.row_key(turn, row) != publish.row_key(turn, {**row, "question_id": "q2"})
    assert publish.row_key(turn, row) != publish.row_key(turn, {**row, "content": "Only 100 pages are left."})
    assert publish.event_label(turn, row) == "Only 120 pages are left."


def test_two_files_that_disagree_on_a_record_are_flagged_for_review_not_overwritten():
    """crm.csv and billing.csv both name customer C-1; billing says another region. The logical table
    still shows one row; the disagreement comes back with each value and its file, and becomes a
    CONTRADICTS decision for Review: Existing is what the node carries, New the other file's."""
    import duckdb
    from opencortyx.knowledge.semantic import publish
    from opencortyx.knowledge.semantic.views import conflicts_sql, type_sql
    db = duckdb.connect()
    db.execute("CREATE VIEW crm AS SELECT * FROM (VALUES ('C-1', 'Acme', 'EU'), ('C-2', 'Bolt', 'US')) t(id, name, region)")
    db.execute("CREATE VIEW billing AS SELECT * FROM (VALUES ('C-1', 'Acme', 'APAC'), ('C-2', 'Bolt', 'US')) t(id, name, region)")
    t = ObjectType(name="Customer", keys=["id"], properties=[
        Property("id", role="key"), Property("name", role="text"), Property("region", role="category")],
        bindings=[Binding("a1", "crm.csv", "structured", fields={"id": "id", "name": "name", "region": "region"}),
                  Binding("a2", "billing.csv", "structured", fields={"id": "id", "name": "name", "region": "region"})])
    views = {"a1": "crm", "a2": "billing"}
    assert len(db.execute(type_sql(t, views)).fetchall()) == 2, "one record per customer still"
    found = [dict(zip([c[0] for c in d.description], r)) for d in [db.execute(conflicts_sql(t, views))]
             for r in d.fetchall()]
    assert [(r["id"], r["attribute"]) for r in found] == [("C-1", "region")], "only what differs"
    assert sorted((s["value"], s["asset"]) for s in found[0]["seen"]) == [("APAC", "billing.csv"), ("EU", "crm.csv")]
    nid = publish.entity_id(WS_ID, "Customer", "C-1")
    [d] = publish.conflict_decisions(WS_ID, [t], {"Customer": found}, {"crm.csv": "00000000-0000-0000-0000-0000000000a1",
                                                                      "billing.csv": "00000000-0000-0000-0000-0000000000a2"},
                                     {nid: {"region": "EU"}})
    assert (d["relation"], d["subject"], d["attribute"], d["needs_review"]) == ("CONTRADICTS", nid, "region", True)
    assert (d["evidence"]["other"], d["evidence"]["value"]) == ("EU", "APAC"), "Existing is the node's value"
    assert str(d["source_id"]).endswith("a2"), "the file that says the other value"
    one = ObjectType(name="Customer", keys=["id"], properties=t.properties, bindings=t.bindings[:1])
    assert conflicts_sql(one, views) is None, "one file cannot disagree with itself here"


def test_a_field_only_a_deleted_file_gave_leaves_the_record():
    """The correction file gave question 8077ef71 its text and answer; deleted, the question keeps
    only what lme_e2e.json gives. A field the type never had, and the record's own, stay."""
    from opencortyx.knowledge.semantic import publish
    t = ObjectType(name="Question", keys=["question_id"], properties=[
        Property("question_id", role="key"), Property("question_date", role="time"),
        Property("question", role="prose"), Property("answer", role="text")])
    nid = uuid.uuid4()
    specs = [{"node_id": str(nid), "properties": {"question_id": "8077ef71", "question_date": "2022-04-05"}}]
    node = SimpleNamespace(id=nid, properties={"question_id": "8077ef71", "question_date": "2022-04-05",
                                               "question": "How many days ago…", "answer": "26 days",
                                               "__sources": "lme_e2e.json", "summary": "kept"})
    assert publish.fields_gone(t, specs, [node]) == {("answer", "question"): [nid]}
    assert publish.fields_gone(t, specs, [SimpleNamespace(id=nid, properties=specs[0]["properties"])]) == {}


def test_a_booked_extractor_reads_the_inputs_chosen_or_what_it_was_built_to_read(monkeypatch):
    import asyncio
    from types import SimpleNamespace as NS
    from opencortyx.tasks import enrich
    ran = []

    class _Db:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, q): return NS(scalar_one_or_none=lambda: tmpl)
        def expunge(self, t): pass

    async def run_template(oid, t, **kw):
        ran.append(t.source["data"])
        return {"status": "ok"}
    monkeypatch.setattr("opencortyx.core.db.V2Session", lambda: _Db())
    monkeypatch.setattr("opencortyx.ingest.extraction.templates.run_template", run_template)
    monkeypatch.setattr("opencortyx.core.worker.utils.async_to_sync", lambda f: (lambda: asyncio.run(f())))
    monkeypatch.setattr(enrich, "async_to_sync", lambda f: (lambda: asyncio.run(f())))
    tmpl = NS(source={"data": {"type": "Turn", "column": "content"}})
    enrich.v2_run_extractor_over.run(str(uuid.uuid4()), "x", {"asset": "a1"})
    tmpl = NS(source={"data": {"type": "Turn", "column": "content"}})
    enrich.v2_run_extractor_over.run(str(uuid.uuid4()), "x", {"asset": "a1", "columns": ["notes"]})
    assert ran == [{"type": "Turn", "column": "content"}, {"asset": "a1", "columns": ["notes"]}]


def test_a_long_run_stops_at_its_deadline_and_the_next_task_reads_only_the_rest(monkeypatch):
    """An extractor over many records outlives one task's time limit: it stops between records,
    each one already committed, and the task queues itself again with what was read."""
    import asyncio
    from types import SimpleNamespace as NS
    from opencortyx.ingest.extraction import templates
    from opencortyx.tasks import enrich
    nodes = [NS(id=uuid.uuid4()) for _ in range(3)]
    clock, read = [0.0], []

    class _Db:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self): pass
        async def rollback(self): pass

    async def none(*a, **k): return None

    async def select(*a, **k): return nodes

    async def ctx(node): return {}

    async def parsed(*a, **k): return {"records": []}

    async def emit(db, ws, t, *, source_node, **k):
        read.append(source_node.id)
        clock[0] += 1                                    # each record takes a "second"
        return {"attributes": 0, "nodes": 1, "edges": 0}
    monkeypatch.setattr(templates, "is_executable", lambda t: True)
    monkeypatch.setattr(templates, "resolve_model", none)
    monkeypatch.setattr("opencortyx.core.inference.embedding.embed", none)
    monkeypatch.setattr(templates, "_select_nodes", select)
    monkeypatch.setattr(templates, "_node_ctx", ctx)
    monkeypatch.setattr(templates, "_extractor_text", lambda n, c: "text")
    monkeypatch.setattr(templates, "_produce_parsed", parsed)
    monkeypatch.setattr(templates, "_apply_emit", emit)
    monkeypatch.setattr(templates, "V2Session", lambda: _Db())
    monkeypatch.setattr(templates, "time", NS(monotonic=lambda: clock[0]))
    t = NS(name="people", mechanism={"ref": "unstructured.ner"}, source={"select": "Turn"})

    first = asyncio.run(templates.run_template(uuid.uuid4(), t, deadline=1.5))
    assert first["status"] == "partial" and first["nodes_seen"] == 2
    assert sorted(first["done"]) == sorted(str(n.id) for n in nodes[:2])
    queued = []
    enrich.continue_run(NS(apply_async=lambda kwargs: queued.append(kwargs)), first, workspace_id="w", name="people")
    assert queued == [{"workspace_id": "w", "name": "people", "done": first["done"]}]

    clock[0] = 0.0
    second = asyncio.run(templates.run_template(uuid.uuid4(), t, deadline=1.5, done=first["done"]))
    assert second["status"] == "ok" and second["nodes_seen"] == 1, "only the rest is read"
    assert read == [n.id for n in nodes], "every record once"
    enrich.continue_run(NS(apply_async=lambda kwargs: queued.append(kwargs)), second)
    assert len(queued) == 1, "a finished run is not queued again"
