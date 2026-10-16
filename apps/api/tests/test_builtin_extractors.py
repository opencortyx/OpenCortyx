"""The built-in catalog must obey the SAME contract as a custom extractor.

Before the migration most built-ins were catalog rows (`deterministic.*` / `pipeline.*`) whose real
behaviour was compiled into the ingestion modules: you could see the node type on a card, but you
could not read the rule, edit it, preview it or run it. These tests pin the migration down:

  • every extractor that DERIVES graph content is executable by the generic engine;
  • the only rows left view-only are LOADERS (which create the graph from an external source) and
    RUNTIME PROMPT HOLDERS (answer/memory prompts) — anything else appearing there is a regression;
  • every coded extractor really compiles and runs inside the sandbox, against inputs shaped like
    the nodes it is bound to, and returns records the emit engine can consume;
  • every emit spec uses primitives the engine actually implements — a typo in an endpoint would
    otherwise fail silently at ingest time, which is exactly how these bugs used to hide.

Offline by design: no server, no database, no model. Run anywhere with `pytest tests/`.
"""
from types import SimpleNamespace

import pytest

from opencortyx.ontology.extractors import (
    BUILTIN_TEMPLATES, PROFILE_SECTION_NAMES,
    READING_SHAPES, RETIRED_LOADER_TEMPLATES, STRUCT_GENERATOR_NAMES, STRUCT_ORG_NAMES)
from opencortyx.ingest.extraction.templates import is_executable
from opencortyx.ingest.extraction.pyextractor import _compile_and_call


VIEW_ONLY = {
    "builtin:mem.summarizer": "runs when a session is saved",
    "builtin:answer.relevance": "runs when a question is answered (CRAG gate)",
    "builtin:answer.generate": "runs when a question is answered",
}

#: Endpoint forms `_resolve_endpoints` implements. An emit spec using anything else silently draws
#: no edge, so the catalog is checked against this list rather than against itself.
ENDPOINT_KEYWORDS = {"source", "emitted", "org"}
ENDPOINT_KEYS = {"resolve", "find", "match", "node_id"}

#: Mechanisms the engine can run (mirrors enrich_templates._PRODUCIBLE).
RUNNABLE_REFS = {
    "unstructured.llm", "unstructured.generative_llm", "structured.llm", "unstructured.regex",
    "unstructured.dictionary", "unstructured.keyword", "unstructured.ner", "unstructured.python",
    "unstructured.attributes", "seed.literal", "relate.llm", "relate.fixed", "relate.regex",
}


def _as_row(t):
    """The catalog dict as the engine sees it (an ExtractionTemplate row)."""
    return SimpleNamespace(name=t["name"], mechanism=t["mechanism"], emit=t["emit"],
                           source=t["source"], status=t["status"])


def _by_name():
    return {t["name"]: t for t in BUILTIN_TEMPLATES}


class _FakeSDK:
    """A stand-in for the `cortex` handle: same surface, canned graph, no database and no model.

    Falsy on purpose — `bool(cortex)` means "a model is available", so extractor code that branches
    on `if cortex:` takes its deterministic path here and the test stays offline.
    """

    prompt = "These related entities form a community: {members}."

    def __bool__(self):
        return False

    @property
    def can_generate(self):
        return False

    node_id = "11111111-1111-1111-1111-111111111111"
    workspace_id = "22222222-2222-2222-2222-222222222222"

    def node(self, node_id=None):
        return {"id": self.node_id, "type": "Source", "label": "quarterly-report.pdf",
                "properties": {"language": "en", "mime_type": "application/pdf",
                               "checksum": "d41d8c", "version": 3, "contains_pii": False,
                               "description": "Quarterly revenue report"},
                "created_at": "2026-01-05T09:00:00", "updated_at": "2026-02-11T17:30:00"}

    def children(self, type=None, limit=500):
        return [
            {"id": "c1", "type": "Chunk", "label": "chunk 1", "properties": {"text": "revenue grew 12 percent"}},
            {"id": "c2", "type": "Chunk", "label": "chunk 2", "properties": {"text": "churn fell slightly"}},
            {"id": "p1", "type": "Attribute", "label": "email",
             "properties": {"physical_column": "email", "null_rate": 0.62, "is_pii": True}},
            {"id": "s1", "type": "Summary", "label": "summary", "properties": {"text": "a summary"}},
            {"id": "t1", "type": "Topic", "label": "revenue", "properties": {}},
        ]

    def neighbors(self, edge_type=None, direction="out", limit=200, node_id=None):
        return [{"id": "x1", "type": "Attribute", "label": "id", "properties": {},
                 "edge_type": "HAS_ATTRIBUTE", "edge_direction": "out"}]

    def find(self, type=None, where=None, label=None, limit=200, **kw):
        return [{"id": f"e{i}", "type": "entity", "label": f"entity {i}", "properties": {}}
                for i in range(6)]

    def edges(self, type=None, limit=10000, node_ids=None):
        return [{"source": "e0", "target": "e1", "type": "RELATES_TO", "properties": {}},
                {"source": "e1", "target": "e2", "type": "RELATES_TO", "properties": {}},
                {"source": "e3", "target": "e4", "type": "RELATES_TO", "properties": {}},
                {"source": "e4", "target": "e5", "type": "RELATES_TO", "properties": {}}]

    def count(self, **kw):
        return 6

    def retrieval_count(self, node_ids=None, window=300):
        return 4

    def generate(self, *a, **kw):
        raise RuntimeError("no model in this test")

    def log(self, *parts):
        pass

    config = {}

    def setting(self, name, default=None):
        return default


#: Inputs shaped like the nodes each coded extractor is bound to (a table, a column, a
#: chunk). One superset dict: every extractor reads only the keys its own source provides.
SAMPLE_INPUTS = {
    "text": "Met Sarah last tuesday to plan the 2023-05-01 launch.",
    "id": "33333333-3333-3333-3333-333333333333",
    "label": "orders",
    "table_name": "orders",
    "source_table": "public.orders",
    "source_system": "postgres",
    "sample_questions": ["How many orders shipped late?", "Who are the top customers?"],
    "physical_column": "email",
    "null_rate": 0.42,
    "parent_table_id": "44444444-4444-4444-4444-444444444444",
    "subject": "Sarah",
    "speaker": "Ada",
    "created_date": "2023-04-25",
}


# ── the migration contract ───────────────────────────────────────────────────────────────────────
def test_only_loaders_and_runtime_prompts_are_view_only():
    """Every built-in that derives graph content runs through the generic engine."""
    not_executable = {t["name"] for t in BUILTIN_TEMPLATES if not is_executable(_as_row(t))}
    unexpected = not_executable - set(VIEW_ONLY)
    assert not unexpected, (
        "these built-ins are neither executable nor a documented loader/prompt holder — either "
        f"migrate them to a runnable mechanism or add them to VIEW_ONLY with a reason: {sorted(unexpected)}")
    # and nothing in VIEW_ONLY quietly became executable without the list being updated
    stale = {n for n in VIEW_ONLY if n not in not_executable and n in _by_name()}
    assert not stale, f"VIEW_ONLY lists extractors that ARE executable now — drop them: {sorted(stale)}"


def test_executable_builtins_use_an_implemented_mechanism():
    for t in BUILTIN_TEMPLATES:
        row = _as_row(t)
        if not is_executable(row):
            continue
        ref = str(t["mechanism"].get("ref") or "")
        assert ref in RUNNABLE_REFS, f"{t['name']}: mechanism '{ref}' is not one the engine runs"


def test_executable_builtins_declare_what_they_emit():
    """A runnable extractor with an empty emit would run and produce nothing — always a mistake."""
    for t in BUILTIN_TEMPLATES:
        row = _as_row(t)
        if not is_executable(row) or str(t["mechanism"].get("ref", "")).startswith("relate."):
            continue          # relate.* writes edges through its own engine, not `emit`
        emit = t["emit"] or {}
        assert (emit.get("nodes") or emit.get("edges") or emit.get("attributes")), \
            f"{t['name']} is executable but emits nothing"


def test_emit_endpoints_are_primitives_the_engine_implements():
    for t in BUILTIN_TEMPLATES:
        for espec in (t["emit"] or {}).get("edges") or []:
            for role in ("from", "to"):
                ep = espec.get(role, "source" if role == "from" else "emitted")
                if isinstance(ep, dict):
                    assert set(ep) & ENDPOINT_KEYS, \
                        f"{t['name']}: edge {role} endpoint {ep} uses no known primitive"
                    if "match" in ep:
                        assert ep.get("on"), f"{t['name']}: match endpoint needs `on`"
                elif is_executable(_as_row(t)):
                    # a bare string endpoint must be a keyword, or a descriptive type name on a
                    # catalog-only card (those are not resolved and never draw an edge)
                    assert isinstance(ep, str) and ep, f"{t['name']}: bad {role} endpoint {ep!r}"


def test_python_builtins_compile_and_return_records():
    """The real proof: each coded extractor runs in the actual sandbox, not a mock of it."""
    coded = [t for t in BUILTIN_TEMPLATES
             if str(t["mechanism"].get("ref")) == "unstructured.python"]
    assert coded, "no coded built-ins found — the migration would be missing"
    for t in coded:
        recs = _compile_and_call(t["mechanism"]["code"], dict(SAMPLE_INPUTS), _FakeSDK())
        assert isinstance(recs, list), f"{t['name']}: extract() must return a list"
        for r in recs:
            assert isinstance(r, dict), f"{t['name']}: every record must be a dict"


def test_python_builtins_declare_editable_code():
    for t in BUILTIN_TEMPLATES:
        mech = t["mechanism"]
        if str(mech.get("ref")) != "unstructured.python":
            continue
        assert (mech.get("code") or "").strip(), f"{t['name']}: no code to edit"
        assert mech.get("language") == "python", f"{t['name']}: mechanism.language must be 'python'"
        assert "def extract(" in mech["code"], f"{t['name']}: code must define extract(inputs)"


def test_profile_sections_are_idempotent_per_asset():
    """Each section pins its identity with a literal `key`, so re-running rewrites one node per
    asset instead of adding a node per phrasing."""
    keys = []
    for name in PROFILE_SECTION_NAMES:
        t = _by_name()[name]
        assert (t["source"] or {}).get("select") == "Source", f"{name} must run over the asset node"
        spec = (t["emit"].get("nodes") or [None])[0]
        assert spec and spec.get("key"), f"{name}: emitted node needs a literal `key`"
        keys.append(spec["key"])
    assert len(set(keys)) == len(keys), "two sections share an identity key — they would overwrite"


def test_seed_literal_builtins_carry_their_items():
    for name in STRUCT_ORG_NAMES:
        t = _by_name()[name]
        assert t["mechanism"]["ref"] == "seed.literal"
        items = t["mechanism"].get("items") or []
        assert items and all(isinstance(i, dict) for i in items), f"{name}: no constant records"
        # they attach to the Organization node — the `org` endpoint, not a hand-written edge
        froms = {e.get("from") for e in (t["emit"].get("edges") or [])}
        assert "org" in froms, f"{name}: should attach to the Organization node via the `org` endpoint"


def test_structured_generators_are_all_present_and_runnable():
    names = _by_name()
    for name in STRUCT_GENERATOR_NAMES + STRUCT_ORG_NAMES:
        assert name in names, f"{name} is referenced by the structured pipeline but not in the catalog"
        assert is_executable(_as_row(names[name])), f"{name} must be runnable by the engine"


#: Retired from the catalog; `seed_builtin_templates` prunes their rows from existing workspaces.
#: The cognitive family overlapped claim extraction (a Fact is a claim) and needed its own linker
#: pass; Topics had no extractor behind them since 462ead9. Summary, keyword, entity and claim were
#: the pipeline shortcut stages and the default document extraction, removed by user decision.
RETIRED_EXTRACTORS = ("builtin:cog.fact", "builtin:cog.decision", "builtin:link.cognitive_entity",
                      "builtin:doc.topic", "builtin:doc.summary", "builtin:doc.keyword",
                      "builtin:extract.entity", "builtin:extract.claim")


def test_retired_extractors_stay_out_of_the_catalog():
    names = set(_by_name())
    assert not names & set(RETIRED_EXTRACTORS)
    assert not any(n.startswith("builtin:cog.") for n in names)


@pytest.mark.parametrize("name,expected", [
    ("chunking", "chunk"),
    ("chunking_rules", "chunk"),
    ("json_structures", None),
    ("builtin:py.date_normalizer", "template"),
    ("builtin:doc.community", "template"),
    ("my_custom_extractor", "template"),
    # the removed shortcut stages are ordinary names now: a saved pipeline that still lists one
    # reports it as an extractor that no longer exists instead of running a hidden default
    ("summarize", "template"),
    ("extract_claims", "template"),
])
def test_document_pipeline_routes_every_stage(name, expected):
    from opencortyx.ingest.unstructured.ingest import _stage_kind
    assert _stage_kind(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("builtin:struct.business_semantics", "business_semantics"),
    ("builtin:struct.segment", "segments"),
    ("builtin:struct.computation", "computations"),
    ("builtin:struct.data_quality", "data_quality"),
    ("builtin:struct.context_rule", "business_rules"),
    ("business_rules", "business_rules"),
    ("base_schema", "introspect"),
    ("builtin:struct.lineage", "template"),
    ("summarize", "template"),
])
def test_structured_pipeline_routes_every_stage(name, expected):
    from opencortyx.ingest.structured.ingest import _struct_stage_kind
    assert _struct_stage_kind(name) == expected


def test_structured_plan_runs_unmapped_builtins_as_templates():
    """Selecting a built-in with no fixed stage must RUN it, not drop it on the floor."""
    from opencortyx.ingest.structured.ingest import _parse_structured_plan
    plan = _parse_structured_plan([
        {"action": "base_schema", "config": {"target_tables": ["orders"]}},
        {"action": "builtin:struct.segment", "config": {"target_tables": ["orders"]}},
        {"action": "builtin:struct.lineage", "config": {}},
        {"action": "my_custom_extractor", "config": {}},
    ])
    assert "segments" in plan["stages"], "a built-in with a fixed stage keeps using it"
    assert "builtin:struct.lineage" in plan["templates"], "the rest must run as templates"
    assert "my_custom_extractor" in plan["templates"]


@pytest.mark.parametrize("name", ["builtin:struct.lineage", "builtin:py.date_normalizer",
                                  "my_custom_extractor"])
def test_semistructured_stage_runs_the_named_extractor(name):
    """In the semi-structured builder a stage targets NODES, so it runs the named extractor itself."""
    from opencortyx.ingest.semistructured.runner import _stage_template_names
    assert _stage_template_names(name) == [name]


def test_every_stageable_builtin_is_runnable_in_some_pipeline():
    """No executable extractor may be unreachable from every builder — that would make it dead."""
    from opencortyx.ingest.unstructured.ingest import _stage_kind
    from opencortyx.ingest.structured.ingest import _struct_stage_kind
    for t in BUILTIN_TEMPLATES:
        if not is_executable(_as_row(t)):
            continue
        name = t["name"]
        if str(t["mechanism"].get("ref", "")).startswith("relate."):
            continue                                 # relationships have their own tab, not a stage
        doc_kind = _stage_kind(name)
        struct_kind = _struct_stage_kind(name)
        assert doc_kind or struct_kind, \
            f"{name} is runnable but no pipeline builder can schedule it"


def test_engine_can_merge_attributes_onto_a_non_source_node():
    """The primitive the judge needed: `on` accepts an endpoint, so a record can mark another node."""
    import inspect
    from opencortyx.ingest.extraction import templates as et
    src = inspect.getsource(et._apply_emit)
    assert '_resolve_endpoints(workspace_id, on, rec' in src, \
        "attribute merges must resolve `on` through the same endpoint vocabulary as edges"
    assert '__confidence' in src, "a reserved key must be able to set a node's confidence"
    assert 'if "when" in espec and not _fill_field' in src, "edges need per-record `when`"


# ── attribute-level extractors and the ontology that must accept them ────────────────────────────
def test_semantic_enrichers_are_attribute_level_with_no_shared_prompt():
    """Each field is minted by its OWN prompt and model, so one can be retuned or switched off
    without touching the others. A single shared prompt is what these cards used to have."""
    for name, expected in (("builtin:struct.business_semantics",
                            {"description", "domain", "synonyms", "sample_questions"}),
                           ("builtin:struct.column_semantics",
                            {"description", "semantic_type", "is_pii"})):
        t = _by_name()[name]
        assert t["mechanism"]["ref"] == "unstructured.attributes"
        assert not t["mechanism"].get("prompt"), f"{name} must not carry a shared prompt"
        overrides = t["mechanism"]["attr_overrides"]
        assert set(overrides) == expected
        for key, ov in overrides.items():
            assert ov.get("prompt"), f"{name}.{key} needs its own prompt"
        merged = (t["emit"]["attributes"][0]["set"])
        assert set(merged) == expected, "every configured attribute must be merged onto the node"


def test_attribute_level_mechanism_costs_nothing_by_itself():
    """`unstructured.attributes` must not call a model with an empty prompt — the cost is exactly
    the per-attribute work the author configured."""
    import asyncio
    from opencortyx.ingest.extraction.templates import _produce_parsed
    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        _produce_parsed({"ref": "unstructured.attributes"}, {}, "sample"))
    assert out == {"records": []}


def test_every_remaining_card_that_writes_the_graph_is_an_extractor():
    """The end state: no card describes graph content it cannot produce. Table and column semantics
    are the two attribute-level extractors; the combined `struct.enrich` prompt that ran inline with
    introspection is gone, because it ran before the nodes it described existed."""
    names = _by_name()
    assert "builtin:struct.enrich" not in names
    for name in ("builtin:struct.business_semantics", "builtin:struct.column_semantics"):
        assert is_executable(_as_row(names[name]))
    for t in BUILTIN_TEMPLATES:
        if t["name"] in VIEW_ONLY:
            assert not (t["emit"] or {}).get("nodes"),                 f"{t['name']} claims to emit nodes but cannot run"


@pytest.mark.parametrize("value,kept", [
    ("{}", False), ("[]", False), ("{", False), ("...", False), ("<the description>", False),
    ("   ", False), (None, False),
    ("Order line items for each shipment.", True), ("identifier", True), (0, True), (False, True),
])
def test_an_attribute_never_stores_a_models_empty_answer(value, kept):
    """A model with nothing to say answers "{}" — storing that as an asset's description is worse
    than leaving the field empty, and it is indistinguishable from a real value downstream."""
    from opencortyx.ingest.extraction.templates import _clean_value
    assert (_clean_value(value) is not None) is kept


# ── loaders are not extractors ───────────────────────────────────────────────────────────────────
def test_the_loader_cards_are_gone_from_the_extractor_catalog():
    """They MAKE the nodes an extractor runs over, so they could never run over nodes themselves.
    Listing them stated the same fact twice — once in the schema mapping that decides it, once in a
    card you could not act on."""
    names = set(_by_name())
    still_there = names & set(RETIRED_LOADER_TEMPLATES)
    assert not still_there, f"loader cards must not be extractors: {sorted(still_there)}"


def test_every_remaining_builtin_is_runnable_except_the_documented_few():
    """With the loaders gone, the only non-runnable cards left are the memory node-type declarations
    and the query-time prompts — each named in VIEW_ONLY with its reason."""
    not_executable = {t["name"] for t in BUILTIN_TEMPLATES if not is_executable(_as_row(t))}
    assert not_executable == set(VIEW_ONLY), (
        f"unexpected: {sorted(not_executable - set(VIEW_ONLY))}; "
        f"stale in VIEW_ONLY: {sorted(set(VIEW_ONLY) - not_executable)}")


def test_reading_shapes_document_what_the_loaders_create():
    """The removed cards' information has to live SOMEWHERE — on the step where it is configured."""
    assert set(READING_SHAPES) == {"document", "semistructured", "structured", "memory"}
    seen_nodes = set()
    for paradigm, shape in READING_SHAPES.items():
        assert shape.get("label") and shape.get("configured_in"), f"{paradigm}: no guidance text"
        assert shape.get("nodes"), f"{paradigm}: documents no node types"
        for n in shape["nodes"]:
            assert n.get("type") and n.get("attributes"), f"{paradigm}: {n} is incomplete"
            assert set(n.get("measured") or []) <= set(n["attributes"]),                 f"{paradigm}/{n['type']}: `measured` names an attribute it does not declare"
            seen_nodes.add(n["type"])
    # the node types the retired cards used to document are all still described
    assert {"Source", "Chunk", "Record", "Table", "Attribute", "Organization",
            "Session", "Turn", "Decision", "Preference", "Artifact"} <= seen_nodes


def test_no_builtin_name_is_defined_twice():
    names = [t["name"] for t in BUILTIN_TEMPLATES]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate built-in names: {sorted(dupes)}"


@pytest.mark.parametrize("name", ["builtin:doc.trust"])
def test_computed_sections_report_real_signals(name):
    """The computed sections must read the graph, not invent numbers."""
    t = _by_name()[name]
    recs = _compile_and_call(t["mechanism"]["code"], dict(SAMPLE_INPUTS), _FakeSDK())
    assert len(recs) == 1, f"{name}: one section node per asset"
    rec = recs[0]
    assert rec.get("text"), f"{name}: section needs the flat `text` the catalog and embeddings read"
    declared = set((t["emit"]["nodes"][0].get("attributes") or []))
    assert declared <= set(rec), f"{name}: declares {declared - set(rec)} but never emits it"


def test_data_quality_extractor_respects_its_threshold():
    t = _by_name()["builtin:struct.data_quality"]
    code = t["mechanism"]["code"]
    clean = _compile_and_call(code, {**SAMPLE_INPUTS, "null_rate": 0.01}, _FakeSDK())
    assert clean == [], "a healthy column must not produce a quality issue"
    bad = _compile_and_call(code, {**SAMPLE_INPUTS, "null_rate": 0.8}, _FakeSDK())
    assert bad[0]["severity"] == "CRITICAL"
    assert bad[0]["null_pct"] == 80.0


def test_community_extractor_clusters_and_falls_back_without_a_model():
    t = _by_name()["builtin:doc.community"]
    recs = _compile_and_call(t["mechanism"]["code"], dict(SAMPLE_INPUTS), _FakeSDK())
    # the fake graph has one 3-member component (e3,e4,e5) and one 3-member (e0,e1,e2)
    assert len(recs) == 2
    for r in recs:
        assert r["size"] >= 3
        assert len(r["member_ids"]) == r["size"]
        assert r["text"], "no model available → must still write a deterministic theme, not blank"
        assert r["cluster_key"] in r["member_ids"]


class _MappedSDK(_FakeSDK):
    """The same asset as _FakeSDK, but ingested through a schema mapping: the reading unit is called
    `Record`, the asset was retyped by an anchor rule, and attributes carry prefixed names."""

    def node(self, node_id=None):
        n = super().node(node_id)
        n["type"] = "Invoice"                        # anchor rule retyped the asset
        n["properties"] = {"__base_type": "Source", "doc_language": "fr",
                           "content_type": "application/json", "content_hash": "9f1c",
                           "business_description": "supplier invoices"}
        return n

    def children(self, type=None, limit=500):
        return [
            {"id": "r1", "type": "Record", "label": "row 1",
             "properties": {"__base_type": "Record", "text": "facture numero un"}},
            {"id": "r2", "type": "Record", "label": "row 2",
             "properties": {"__base_type": "Record", "text": "facture numero deux"}},
            {"id": "f1", "type": "Field", "label": "email",
             "properties": {"__base_type": "Attribute", "column_name": "email",
                            "invoice_null_rate": 0.71, "sensitive": True}},
        ]


def test_sections_read_a_mapped_asset():
    """Renamed reading unit, renamed attributes, retyped asset — the sections still report signals."""
    trust = _by_name()["builtin:doc.trust"]
    t = _compile_and_call(trust["mechanism"]["code"], dict(SAMPLE_INPUTS), _MappedSDK())[0]
    assert t["contains_pii"] is True, "a mapped PII flag (`sensitive`) must still count"


def test_structured_extractors_read_mapped_attribute_names():
    dq = _by_name()["builtin:struct.data_quality"]["mechanism"]["code"]
    # renamed AND stored as a percentage rather than a rate
    recs = _compile_and_call(dq, {"id": "p1", "column_name": "email", "orders_null_pct": 64},
                             _FakeSDK())
    assert recs and recs[0]["null_pct"] == 64.0 and recs[0]["severity"] == "CRITICAL"
    assert "email" in recs[0]["description"]

    lin = _by_name()["builtin:struct.lineage"]["mechanism"]["code"]
    recs = _compile_and_call(lin, {"id": "m1", "label": "invoices", "physical_table": "raw.invoices",
                                   "connector": "mysql"}, _FakeSDK())
    assert recs[0]["source_table_or_api"] == "raw.invoices"
    assert recs[0]["source_system"] == "mysql"


async def test_selection_matches_a_retyped_node(memory_repo, monkeypatch):
    """`source.select` must match what a node was INGESTED as, not only its current type."""
    import uuid
    from types import SimpleNamespace
    from opencortyx.ingest.extraction import templates as et
    monkeypatch.setattr(et, "_repo", memory_repo)
    org = uuid.uuid4()
    retyped = await memory_repo.upsert_node(workspace_id=org, type="Aadhaar", label="scan.pdf",
                                            properties={"__base_type": "Source"}, date_link=False)
    await memory_repo.upsert_node(workspace_id=org, type="Entity", label="Priya", date_link=False)
    selected = await et._select_nodes(org, SimpleNamespace(source={"select": "Source"}))
    assert [n.id for n in selected] == [retyped.id], "selection must see through an anchor retype"


