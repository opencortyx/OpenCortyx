"""The Extractors form's model method: one prompt built from what the person says - each kind, field
and link either named by them or left to the model, each with its own instruction - and only what the
form allows read back from the answer."""
import json
import uuid

import pytest

from opencortyx.api.schemas.ingestion.extraction import check_mechanism
from opencortyx.ingest.extraction import data_inputs, templates

WS = uuid.uuid4()

CONTRACTS = {
    "ref": "unstructured.ner",
    "kinds": [
        {"type": "Contract", "ask": "the agreement itself",
         "fields": [{"key": "term", "ask": "how long it runs"}, {"key": "payment_terms", "ask": "e.g. net 30"}]},
        {"type": "Organization", "ask": "parties to it", "fields": []},
        {"type": "", "ask": "people and courts it names", "fields_by_model": True, "fields_ask": "dates and roles"},
    ],
    "links": [{"from": "Contract", "to": "Organization", "relation": "HAS_PARTY", "ask": "who signs or is bound"},
              {"from": "~", "to": "*", "relation": "", "ask": "disputes: who claims against whom"}],
    "pairs": [{"from": "Organization", "to": "Contract", "relation": "PARTY_TO"}],
    "input_fields": [{"key": "risk_level", "ask": "low, medium or high"}],
    "vocabulary": "known",
}

ANSWER = {
    "things": [
        {"name": "MSA-2026", "type": "contract", "fields": {"term": "24 months", "payment_terms": "",
                                                         "invented": "x"}},
        {"name": "Acme Ltd", "type": "Organization"},
        {"name": "Globex", "type": "ORGANIZATION"},
        {"name": "J. Patel", "type": "person", "fields": {"Signed On": "1 March 2026", "name": "dup", "role": ["CFO"]}},
        {"name": "<name>", "type": "Person"},
    ],
    "links": [
        {"subject": "MSA-2026", "relation": "has party", "object": "Acme Ltd"},
        {"subject": "MSA-2026", "relation": "OWNS", "object": "Acme Ltd"},          # no link asks for this
        {"subject": "J. Patel", "relation": "disputes with", "object": "Globex"},   # the model's to name
        {"subject": "Acme Ltd", "relation": "SUES", "object": "Nobody"},          # not a thing it found
    ],
    "input": {"risk_level": "low", "other": "dropped"},
}


@pytest.fixture
def model(monkeypatch):
    prompts = []

    async def complete(prompt, **kw):
        prompts.append(prompt)
        return json.dumps(ANSWER)

    async def none(ws):
        return {}

    async def relations(ws):
        return ["GOVERNED_BY"]
    monkeypatch.setattr(templates, "complete", complete)
    monkeypatch.setattr(templates, "_known_types", none)
    monkeypatch.setattr(templates, "_known_relations", relations)
    return prompts


@pytest.mark.anyio
async def test_every_instruction_reaches_the_prompt(model):
    await templates._model_records("This agreement…", CONTRACTS, workspace_id=WS)
    p = model[0]
    for said in ("the agreement itself", '"term" - how long it runs', "parties to it", "people and courts it names",
                 "dates and roles", '"HAS_PARTY" from a "Contract" to a "Organization": who signs or is bound',
                 "a relationship you name by its verb", "disputes: who claims against whom",
                 '"risk_level": low, medium or high', "Relationships already used here: GOVERNED_BY"):
        assert said in p, said
    assert p.endswith("TEXT:\nThis agreement…")


@pytest.mark.anyio
async def test_only_what_the_form_allows_is_read_back(model):
    out = await templates._model_records("This agreement…", CONTRACTS, workspace_id=WS)
    things = {r["name"]: r for r in out["records"]}
    assert set(things) == {"MSA-2026", "Acme Ltd", "Globex", "J. Patel"}, "a placeholder is not a thing"
    assert things["MSA-2026"] == {"name": "MSA-2026", "type": "Contract", "term": "24 months"}, \
        "a named kind keeps its spelling and only its declared, filled fields"
    assert things["Globex"]["type"] == "Organization"
    assert things["J. Patel"] == {"name": "J. Patel", "type": "Person", "signed_on": "1 March 2026", "role": "CFO"}, \
        "the model's fields in snake_case, never one a thing uses for itself"
    rels = {(x["name"], x["relation"], x["object"]) for x in out["links"]}
    assert ("MSA-2026", "HAS_PARTY", "Acme Ltd") in rels, "a named relationship in its own spelling"
    assert ("J. Patel", "DISPUTES_WITH", "Globex") in rels, "the model names it where it may"
    assert not any(r == "OWNS" or o == "Nobody" for _, r, o in rels), "nothing the form does not ask for"
    assert {("Acme Ltd", "PARTY_TO", "MSA-2026"), ("Globex", "PARTY_TO", "MSA-2026")} <= rels, \
        "linked whenever both are found"
    assert out["risk_level"] == "low" and "other" not in out, "only the input fields asked for"


@pytest.mark.anyio
async def test_only_the_workspace_s_names_when_the_vocabulary_is_closed(model, monkeypatch):
    async def known(ws):
        return {"organization": (3, "Organization")}
    monkeypatch.setattr(templates, "_known_types", known)
    out = await templates._model_records("…", {**CONTRACTS, "vocabulary": "only"}, workspace_id=WS)
    assert "Person" not in {r["type"] for r in out["records"]}, "a type the workspace does not have is left out"
    assert "Use only these types: Contract, Organization." in model[0]
    assert "DISPUTES_WITH" not in {x["relation"] for x in out["links"]}


@pytest.mark.anyio
async def test_an_answer_missing_a_brace_is_still_read(model, monkeypatch):
    """Seen from the small built-in model: a thing's object left open before the list closes."""
    async def complete(prompt, **kw):
        return ('{"things": [{"name": "MSA-2026", "type": "Contract", "fields": {"term": "24 months"}], '
                '"links": [], "input": {"risk_level": "medium"}')
    monkeypatch.setattr(templates, "complete", complete)
    out = await templates._model_records("…", CONTRACTS, workspace_id=WS)
    assert out["records"] == [{"name": "MSA-2026", "type": "Contract", "term": "24 months"}]
    assert out["risk_level"] == "medium"


@pytest.mark.parametrize("raw,fixed", [
    ('{"a": [{"b": 1], "c": 2}', '{"a": [{"b": 1}], "c": 2}'),
    ('{"a": "x}]"', '{"a": "x}]"}'),
    ('{"a": 1}}', '{"a": 1}'),
    ('{"a": "unterminated', '{"a": "unterminated"}'),
])
def test_brackets_are_put_right_and_strings_left_alone(raw, fixed):
    assert templates._balanced(raw) == fixed
    assert json.loads(templates._balanced(raw)) == json.loads(fixed)


@pytest.mark.anyio
async def test_nothing_to_read_asks_nothing(model):
    assert await templates._model_records("   ", CONTRACTS) == {"records": [], "links": []}
    assert model == []


def test_links_saved_by_the_old_form_read_the_link_rows():
    emit = {"edges": [{"type": "$relation", "from": "emitted", "when": "$relation",
                       "to": {"resolve": "entity", "name": "$object", "type": "$object_type"}}]}
    [e] = templates._emit_edges(emit)
    assert e["over"] == "links" and e["from"] == {"resolve": "entity", "name": "$name", "type": "$type"}


def test_an_edge_limited_to_a_kind():
    mech = {"kinds": CONTRACTS["kinds"]}
    assert templates._of_kind({"type": "Contract"}, "contract", mech)
    assert not templates._of_kind({"type": "Contract"}, "Organization", mech)
    assert templates._of_kind({"type": "Person"}, "~", mech), "a kind the model named"
    assert not templates._of_kind({"type": "Contract"}, "~", mech)
    assert templates._of_kind({"type": "Anything"}, None, mech)


@pytest.mark.anyio
async def test_the_preview_shows_things_links_and_input_fields(model, monkeypatch):
    async def no_model(*a, **k):
        return None
    monkeypatch.setattr(templates, "resolve_model", no_model)
    emit = {"nodes": [{"type": "$type", "dedup": True, "name": "$name"}],
            "edges": [{"type": "$relation", "over": "links",
                       "from": {"resolve": "entity", "name": "$name", "type": "$type"},
                       "to": {"resolve": "entity", "name": "$object", "type": "$object_type"}},
                      {"type": "MENTIONED_IN", "from": "emitted", "to": "source", "per": "record", "only": "Organization"}],
            "attributes": [{"on": "source", "set": {"risk_level": "$risk_level"}}]}
    out = await templates.preview_template(WS, {"mechanism": CONTRACTS, "emit": emit}, "This agreement…")
    assert {n["label"] for n in out["nodes"]} == {"MSA-2026", "Acme Ltd", "Globex", "J. Patel"}
    by = {n["id"]: n["label"] for n in out["nodes"]}
    linked = {(by.get(e["from_id"]), e["rel"], by.get(e["to_id"])) for e in out["edges"] if e["rel"] != "MENTIONED_IN"}
    assert ("MSA-2026", "HAS_PARTY", "Acme Ltd") in linked and ("J. Patel", "DISPUTES_WITH", "Globex") in linked
    mentioned = [e for e in out["edges"] if e["rel"] == "MENTIONED_IN"]
    assert {by[e["from_id"]] for e in mentioned} == {"Acme Ltd", "Globex"} and {e["to_id"] for e in mentioned} == {"source"}
    assert out["source_attributes"] == [{"key": "risk_level", "value": "low", "mechanism": "ner", "model": None,
                                         "filled": True}]
    assert not out["warnings"]


@pytest.mark.anyio
async def test_what_the_form_saves_writes_things_links_input_fields_and_existing_matches(model, memory_repo, monkeypatch):
    """The write path, on an in-memory graph: one node per thing (named things are one node by name),
    an edge per link, an edge limited to one kind, a match on a node already there, a field on the input."""
    from types import SimpleNamespace

    import opencortyx.resolution.engine as resolution
    import opencortyx.tasks.graph as graph_tasks
    from opencortyx.ingest.structured import catalog
    from opencortyx.knowledge import governance
    monkeypatch.setattr(templates, "_repo", memory_repo)
    monkeypatch.setattr(catalog, "_repo", memory_repo)

    async def nothing_off(db, ws):
        return set()

    async def allowed(*a, **k):
        return True, None

    async def none(*a, **k):
        return 0
    unresolved = []

    async def capture(*a, **k):
        unresolved.append(k.get("reason"))
    monkeypatch.setattr(governance, "disabled_types", nothing_off)
    monkeypatch.setattr(templates, "validate_edge", allowed)
    monkeypatch.setattr(templates, "_q_capture", capture)
    monkeypatch.setattr(resolution, "reconcile", none)
    monkeypatch.setattr(graph_tasks, "embed_later", lambda ws, ids: None)

    ws = uuid.uuid4()
    source = await memory_repo.upsert_node(workspace_id=ws, type="Chunk", label="Terms",
                                           properties={"text": "This agreement…"}, date_link=False)
    vendor = await memory_repo.upsert_node(workspace_id=ws, type="Vendor", label="Globex",
                                           properties={"name": "Globex"}, date_link=False)
    emit = {"nodes": [{"type": "$type", "dedup": True, "name": "$name"}],
            "edges": [{"type": "$relation", "over": "links", "mode": "relate.fixed",
                       "from": {"resolve": "entity", "name": "$name", "type": "$type"},
                       "to": {"resolve": "entity", "name": "$object", "type": "$object_type"}},
                      {"type": "MENTIONS", "from": "source", "to": "emitted", "per": "record"},
                      {"type": "SAME_AS", "from": "emitted", "to": {"match": "Vendor", "on": {"name": "$name"}},
                       "per": "record", "only": "Organization"}],
            "attributes": [{"on": "source", "set": {"risk_level": "$risk_level"}}]}
    parsed = await templates._model_records("This agreement…", CONTRACTS, workspace_id=WS)
    counts = await templates._apply_emit_unit(None, ws, SimpleNamespace(name="contracts", mechanism=CONTRACTS, emit=emit),
                                              source_node=source, parsed=parsed, ctx={}, collection_id=None)

    # (the Date node is the platform's: a node given new fields is linked to the day it changed)
    things = {n.label: n.type for n in await memory_repo.find(ws) if n.id not in (source.id, vendor.id) and n.type != "Date"}
    assert things == {"MSA-2026": "Contract", "Acme Ltd": "Organization", "Globex": "Organization", "J. Patel": "Person"}
    by_id = {n.id: n.label for n in await memory_repo.find(ws)}
    edges = {(by_id[e.source_id], e.type, by_id[e.target_id]) for e in await memory_repo.edges(ws)}
    assert {("MSA-2026", "HAS_PARTY", "Acme Ltd"), ("J. Patel", "DISPUTES_WITH", "Globex"),
            ("Acme Ltd", "PARTY_TO", "MSA-2026")} <= edges
    assert {e for e in edges if e[1] == "MENTIONS"} == {("Terms", "MENTIONS", t) for t in things}
    assert {e for e in edges if e[1] == "SAME_AS"} == {("Globex", "SAME_AS", "Globex")}, \
        "only an Organization is matched, and only to the Vendor of its name"
    assert (await memory_repo.get(ws, source.id)).properties["risk_level"] == "low"
    assert counts["nodes"] == 4 and not unresolved, unresolved
    assert counts["unmatched"] == 1, "Acme Ltd has no Vendor of its name: counted, not sent to quarantine"


@pytest.mark.parametrize("change,says", [
    ({"kinds": []}, "at least one kind"),
    ({"kinds": [{"type": "Two words"}]}, "letters, digits"),
    ({"kinds": [{"type": "A"}, {"type": "a"}]}, "listed twice"),
    ({"kinds": [{"type": ""}, {"type": ""}]}, "one kind the model names"),
    ({"kinds": [{"type": "A", "fields": [{"key": "name"}]}]}, "used by every thing"),
    ({"kinds": [{"type": "A", "fields": [{"key": "x"}, {"key": "X"}]}]}, "listed twice"),
    ({"kinds": [{"type": "A"}], "links": [{"from": "A", "to": "B"}]}, "does not create"),
    ({"kinds": [{"type": "A"}], "links": [{"from": "~", "to": "A"}]}, "no kind is the model's"),
    ({"kinds": [{"type": "A"}], "pairs": [{"from": "A", "to": "A"}]}, "needs a relationship name"),
    ({"kinds": [{"type": "A"}], "input_fields": [{"key": "type"}]}, "used by every thing"),
    ({"kinds": [{"type": "A"}], "vocabulary": "any"}, "vocabulary"),
])
def test_a_form_the_engine_could_not_follow_is_refused_at_save(change, says):
    with pytest.raises(ValueError, match=says):
        check_mechanism({"ref": "unstructured.ner", **change})


def test_the_forms_own_config_and_an_old_one_are_accepted():
    check_mechanism(CONTRACTS)
    check_mechanism({"ref": "unstructured.ner"})


@pytest.mark.parametrize("mech,says", [
    ({"ref": "unstructured.regex", "pattern": ""}, "needs a pattern"),
    ({"ref": "unstructured.regex", "pattern": "("}, "not a valid regular expression"),
    ({"ref": "unstructured.dictionary", "terms": [" "]}, "at least one word"),
    ({"ref": "unstructured.python", "code": "def extract(:"}, "does not compile"),
])
def test_a_pattern_or_code_that_cannot_run_is_refused(mech, says):
    with pytest.raises(ValueError, match=says):
        check_mechanism(mech)


def test_an_extractor_s_own_fields_apply_when_the_upload_chose_none():
    src = {"select": "*", "only": ["notes", "summary"]}
    assert data_inputs.with_own_fields({"asset": "a"}, src) == {"asset": "a", "columns": ["notes", "summary"],
                                                                 "sections": ["notes", "summary"]}
    assert data_inputs.with_own_fields({"asset": "a", "columns": ["x"]}, src) == {"asset": "a", "columns": ["x"]}, \
        "the upload's choice wins"
    assert data_inputs.with_own_fields({"asset": "a"}, {"only": ["$.party.name"]}) == {"asset": "a", "path": "$.party.name"}
    assert data_inputs.with_own_fields({"asset": "a"}, {"select": "*"}) == {"asset": "a"}


def test_a_node_s_named_fields_are_what_is_read():
    assert templates._fields_read({"name": "Acme", "notes": "late payer", "empty": ""}, ["notes", "empty", "name"]) \
        == "notes: late payer\nname: Acme"
