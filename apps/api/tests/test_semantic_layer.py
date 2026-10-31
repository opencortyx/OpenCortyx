"""The semantic layer (knowledge/semantic) and the plan route that reads it (retrieval/l2_data/semantic.py).

No services: the logical tables run on an in-memory DuckDB, mappings are plain objects.
"""
import json
from types import SimpleNamespace

import duckdb
import pytest

from opencortyx.knowledge.semantic import detector, store, views
from opencortyx.knowledge.semantic.store import Binding, Layer, ObjectType, Property
from opencortyx.retrieval.l2_data import planner, semantic
from opencortyx.retrieval.l2_data.schema import Column, Table


# ── logical tables ─────────────────────────────────────────────────────────────────────────────

def _con():
    con = duckdb.connect()
    con.execute("CREATE TABLE billing AS SELECT * FROM (VALUES ('C-1', 'Ann', 'EUR'), ('C-2', 'Bo', 'USD')) "
                "t(customer_id, name, billing_currency)")
    crm = {"customers": [{"customer_id": "C-1", "name": "Ann", "tier": "gold", "contact": {"email": "a@x"}},
                         {"customer_id": "C-3", "name": "Cy", "tier": "silver", "contact": {"email": "c@x"}}]}
    con.execute("CREATE TABLE crm (record_id VARCHAR, raw VARCHAR)")
    con.execute("INSERT INTO crm VALUES ('crm:0', ?)", [json.dumps(crm)])
    return con


def _customer() -> ObjectType:
    return ObjectType(name="Customer", keys=["customer_id"], properties=[
        Property("customer_id"), Property("name"), Property("billing_currency"), Property("tier"),
        Property("contact_email")], bindings=[
        Binding("b", "billing.csv", "structured", "", {"customer_id": "customer_id", "name": "name",
                                                      "billing_currency": "billing_currency"}),
        Binding("c", "crm.json", "semistructured", "$.customers[*]",
                {"customer_id": "customer_id", "name": "name", "tier": "tier", "contact_email": "contact.email"})])


def test_one_entity_from_a_table_and_a_json_list_is_one_row_per_key():
    sql = views.type_sql(_customer(), {"b": "billing", "c": "crm"})
    rows = {r[0]: r for r in _con().execute(f"SELECT customer_id, name, billing_currency, tier, contact_email "
                                            f"FROM ({sql}) ORDER BY 1").fetchall()}
    assert set(rows) == {"C-1", "C-2", "C-3"}
    assert rows["C-1"][2:] == ("EUR", "gold", "a@x")         # the fields of both sources on one record


_CHATS = [{"qid": "q1", "ids": ["s1", "s2"], "dates": ["2023-01-01", "2023-02-01"], "tags": ["a", "b", "c"],
           "sessions": [[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}],
                        [{"role": "user", "content": "x"}]]},
          {"qid": "q2", "ids": ["s3"], "dates": ["2023-03-01"], "tags": ["d"],
           "sessions": [[{"role": "user", "content": "z"}]]}]


def _rows_of(path: str) -> list[dict]:
    raws = [json.dumps(r) for r in _CHATS]
    src = next(s for s in detector._semi_sources("a", "chats.json", "chats", raws) if s.path == path)
    t = ObjectType(name="T", properties=[Property(p) for p in src.fields])
    con = duckdb.connect()
    con.execute("CREATE TABLE chats AS SELECT unnest(?) AS raw", [raws])
    sql = views.binding_sql(t, Binding("a", "chats.json", "semistructured", path, src.fields), "chats")
    cur = con.execute(f"SELECT * FROM ({sql})")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def test_each_inner_list_of_a_list_of_lists_is_a_record_placed_by_the_lists_beside_it():
    rows = sorted(_rows_of("$.sessions[*]"), key=lambda r: r["ids"])
    assert [(r["ids"], r["dates"], r["qid"], r["position"]) for r in rows] == [
        ("s1", "2023-01-01", "q1", "0"), ("s2", "2023-02-01", "q1", "1"), ("s3", "2023-03-01", "q2", "0")]


def test_each_element_of_the_inner_lists_carries_its_list_and_record():
    rows = sorted(_rows_of("$.sessions[*][*]"), key=lambda r: (r["ids"], r["position"]))
    assert [(r["ids"], r["qid"], r["position"], r["role"]) for r in rows] == [
        ("s1", "q1", "0", "user"), ("s1", "q1", "1", "assistant"), ("s2", "q1", "0", "user"), ("s3", "q2", "0", "user")]


def test_a_list_of_plain_values_beside_nothing_is_one_record_per_value():
    rows = _rows_of("$.tags[*]")
    assert sorted((r["tags"], r["qid"]) for r in rows) == [("a", "q1"), ("b", "q1"), ("c", "q1"), ("d", "q2")]


def test_an_event_table_is_the_plain_union_of_its_files():
    t = ObjectType(name="Sales", kind="event", properties=[Property("name")],
                   bindings=[Binding("b", "x", "structured", "", {"name": "name"}),
                             Binding("b2", "y", "structured", "", {"name": "name"})])
    sql = views.type_sql(t, {"b": "billing", "b2": "billing"})
    assert _con().execute(f"SELECT count(*) FROM ({sql})").fetchone()[0] == 4


def test_a_missing_column_reads_null_instead_of_failing():
    t = _customer()
    t.bindings[0].fields["tier"] = "no_such_column"
    sql = views.type_sql(t, {"b": "billing", "c": "crm"},
                         {"billing": {"customer_id", "name", "billing_currency"}})
    assert _con().execute(f"SELECT count(*) FROM ({sql})").fetchone()[0] == 3


def test_composite_keys_group_by_every_key():
    t = ObjectType(name="Line", keys=["customer_id", "name"], properties=[Property("customer_id"), Property("name")],
                   bindings=[Binding("b", "x", "structured", "", {"customer_id": "customer_id", "name": "name"})])
    assert "GROUP BY \"customer_id\", \"name\"" in views.type_sql(t, {"b": "billing"})


# ── the Detector's decisions ───────────────────────────────────────────────────────────────────

def test_identifiers_are_written_one_way_and_names_are_not():
    ids = {f"P-{i:03d}" for i in range(10)}
    names = {"Heath Partners rollout 0", "Iris Retail rollout 4", "Zephyr Holdings", "Bo"}
    assert detector._uniformity(ids)[0] == 1.0
    assert detector._uniformity(names)[0] < 0.9


def test_same_shaped_files_are_one_group_and_other_shapes_are_not():
    src = [detector.Source(str(i), f"licences-{i:02d}", "structured", f"v{i}", "", {"licence_id": "licence_id", "fee": "fee"})
           for i in range(5)]
    src.append(detector.Source("o", "orders-00", "structured", "vo", "", {"order_id": "order_id", "amount": "amount"}))
    groups = detector._group(src)
    assert sorted(len(g.sources) for g in groups) == [1, 5]


def _keyed(kind, path, key, values):
    g = detector.Group(kind=kind, path=path, sources=[], fields={key: key}, rows=len(values))
    g.stats = {key: detector.FieldStats(rows=len(values), filled=len(values), distinct=len(values), values=set(values))}
    g.key = key
    return g


def test_groups_whose_keys_hold_the_same_values_merge_into_one_type():
    crm = _keyed("semistructured", "$.customers[*]", "customer_id", [f"C-{i}" for i in range(60)])
    billing = _keyed("semistructured", "", "customer_id", [f"C-{i}" for i in range(60)])
    device = _keyed("semistructured", "$.device", "device_id", [f"D-{i}" for i in range(10)])
    merged = detector._merge([crm, billing, device])
    assert sorted(len(m) for m in merged) == [1, 2]


# ── where the layer is kept ────────────────────────────────────────────────────────────────────

def _mapping(status, entities, relationships=()):
    return SimpleNamespace(status=status, paradigm="semistructured",
                           payload={"knowledge": {"entities": list(entities), "relationships": list(relationships)}})


def _entity(type_name, asset, path="", status="accepted"):
    return {"type": type_name, "keys": ["customer_id"], "kind": "entity", "path": path, "status": status,
            "assets": [{"id": asset, "name": asset, "fields": {"customer_id": "customer_id"}}],
            "properties": [{"name": "customer_id", "role": "key", "value_set": {}}]}


def test_one_type_across_mappings_is_one_type_with_every_binding():
    layer = store.layer_of([_mapping("auto", [_entity("Customer", "crm", "$.customers[*]")]),
                            _mapping("active", [_entity("Customer", "billing")])])
    assert [t.name for t in layer.types] == ["Customer"]
    assert {b.asset_id for b in layer.types[0].bindings} == {"crm", "billing"}


def test_a_draft_mapping_is_only_a_suggestion_and_a_rejected_entity_is_dropped():
    layer = store.layer_of([_mapping("draft", [_entity("Customer", "crm")]),
                            _mapping("auto", [_entity("Device", "dev", status="rejected")])])
    assert [(t.name, t.status) for t in layer.types] == [("Customer", "suggested")]
    assert layer.accepted().types == []


def test_rows_is_the_record_itself():
    layer = store.layer_of([_mapping("auto", [_entity("Customer", "billing", path="rows")])])
    assert layer.types[0].bindings[0].path == ""


def test_a_changed_knowledge_section_is_a_new_version_and_the_old_one_is_kept():
    p = store.with_version({}, {"entities": [], "relationships": [], "curated": False, "version": 0})
    assert p["knowledge"]["version"] == 1 and "history" not in p
    p2 = store.with_version(p, {"entities": [{"type": "X"}], "relationships": [], "curated": True})
    assert p2["knowledge"]["version"] == 2
    assert p2["history"][0]["version"] == 1
    assert store.with_version(p2, p2["knowledge"]) == p2       # unchanged: no new version


# ── the plan route's data-settled decisions ────────────────────────────────────────────────────

def _licences() -> Table:
    return Table(asset_id="Licences", view="licences", name="Licences", kind="object", storage="logical", format="entity",
                 key_columns=["licence_id"], columns=[
                     Column("licence_id", "VARCHAR", role="key", primary_key=True, semantic=True),
                     Column("status", "VARCHAR", role="category", value_set={"ACTIVE": 10, "EXPIRED": 5}, semantic=True),
                     Column("region", "VARCHAR", role="category", value_set={"EU": 8, "US": 7}, semantic=True),
                     Column("fee", "DOUBLE", role="measure", semantic=True),
                     Column("issued", "DATE", role="time", semantic=True)])


def test_a_one_letter_value_is_not_named_by_an_article():
    t = Table(asset_id="f", view="flags", name="flags", kind="structured", storage="", format="",
              columns=[Column("flag", "VARCHAR", value_set={"a": 3, "b": 2}),
                       Column("region", "VARCHAR", value_set={"North": 2})])
    assert semantic._value_filters("How many days ago did I attend a networking event?", t) == []
    assert semantic._value_filters("How many flags in the North?", t) == [("region", ["North"])]


def test_a_question_using_no_word_of_a_table_is_not_about_it():
    t = _licences()
    assert semantic.grounding("What is the title of the RFC that RFC 9562 obsoletes?", t) == 0
    assert semantic.grounding("How many active licences are there in the EU?", t) > 0


def test_filters_are_the_values_the_question_names():
    assert semantic._value_filters("How many ACTIVE licences in the EU?", _licences()) == [
        ("status", ["ACTIVE"]), ("region", ["EU"])]


@pytest.mark.parametrize("question,lit,sql_part", [
    ("issued in 2024", ("year", "2024"), "< DATE '2025-01-01'"),
    ("revenue in 2025-Q3", ("quarter", ("2025", 3)), ">= DATE '2025-07-01'"),
    ("created 2026-04-11", ("date", "2026-04-11"), "= DATE '2026-04-11'"),
])
def test_dates_are_read_from_their_format(question, lit, sql_part):
    assert semantic._time_literal(question) == lit
    assert sql_part in semantic._time_sql('"issued"', "in", lit)


def test_only_measures_the_question_names_are_offered():
    names = [m[0] for m in semantic._grounded("What is the average fee?", semantic._measures(_licences()),
                                              lambda m: m[2] or None)]
    assert "the average fee" in names and "the number of licences" in names


def test_rounding_reads_like_the_question_asked():
    assert planner.decimals("total fee, rounded to two decimals") == 2
    assert duckdb.sql(f"SELECT {planner.rounded('106001.0', 2)}").fetchone()[0] == "106001.00"


def test_a_layer_without_types_accepts_nothing():
    assert Layer().accepted().types == []
