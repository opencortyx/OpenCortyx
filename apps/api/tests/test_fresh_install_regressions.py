"""Regressions found by ingesting into a fresh install: each of these read fine and wrote the wrong
graph. They run without a stack.

  * a DOCX was decoded as UTF-8 and ingested as its compressed archive
  * a document's own paragraphs contradicted each other on their position (`ordinal: 1 vs 3`)
  * the Extractor Studio's "an existing node, found by a field" edge never resolved
"""
import io
import uuid
import zipfile
from types import SimpleNamespace

import pytest

from opencortyx.capabilities.modules.contradiction import (
    Settings, StatementView, Verdict, decide, judge_by_rules,
)
from opencortyx.ingest.extraction.templates import _emit_edges
from opencortyx.ingest.formats.unstructured.documents import WHOLE_FILE_EXTENSIONS, _office_xml_text


def _docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document><w:body>{body}</w:body></w:document>')
        z.writestr("word/styles.xml", "<w:styles/>")
    return buf.getvalue()


def test_docx_text_comes_from_the_document_xml_not_the_archive_bytes():
    text = _office_xml_text(_docx("Vendor Management Policy", "Fees &amp; charges apply."), "docx")
    assert text.splitlines() == ["Vendor Management Policy", "Fees & charges apply."]
    assert "PK" not in text and "styles" not in text


def test_a_corrupt_office_file_says_so_instead_of_returning_bytes():
    from opencortyx.ingest.formats.unstructured.documents import UnreadableDocument
    with pytest.raises(UnreadableDocument, match="not a readable .docx"):
        _office_xml_text(b"PK\x03\x04 not really a zip", "docx")


def test_zip_and_pdf_formats_are_sampled_whole():
    assert {"docx", "pptx", "pdf"} <= WHOLE_FILE_EXTENSIONS


def _element(nid, text, ordinal):
    # a node carrying the reader's position fields; not a reading unit itself (those are not
    # statements at all — see test_document_passages_are_not_judged_as_statements)
    return {"id": nid, "type": "Clause", "label": f"Clause #{ordinal}",
            "properties": {"text": text, "label": "text", "ordinal": ordinal, "page_number": 1,
                           "created_date": "2026-09-01", "modified_date": f"2026-09-0{ordinal}"},
            "confidence": 1.0}


def test_reader_position_fields_are_not_asserted_values():
    cfg = Settings()
    a = StatementView.of(_element("a", "The policy is owned by Procurement.", 1), cfg)
    b = StatementView.of(_element("b", "Every vendor passes a security review.", 3), cfg)
    assert not set(a.values) & {"ordinal", "page_number", "label", "created_date", "modified_date"}
    a.origin = b.origin = "linked"            # siblings under one section: the subject is "shared"
    assert judge_by_rules(a, b, cfg) is None   # used to be a certain contradiction on `ordinal`


def test_studio_match_on_endpoint_becomes_a_match_spec():
    emit = {"edges": [{"type": "SAME_AS", "from": "Vendor", "to": "match_on",
                       "toMatchNode": "Supplier", "toMatchField": "company_name",
                       "toMatchValue": "$vendor"},
                      {"type": "PROVIDES", "from": "Vendor", "to": "Service"}]}
    same_as, provides = _emit_edges(emit)
    assert same_as["to"] == {"match": "Supplier", "on": {"company_name": "$vendor"}}
    assert same_as["from"] == "Vendor"
    assert provides == emit["edges"][1]
    assert emit["edges"][0]["to"] == "match_on"          # the saved spec itself is not mutated


def test_an_incomplete_match_on_resolves_to_nothing_rather_than_a_literal():
    (edge,) = _emit_edges({"edges": [{"type": "X", "from": "match_on", "fromMatchNode": "Supplier"}]})
    assert edge["from"] is None


def test_sibling_records_are_peers_not_one_subject():
    """Rows of one file hang off one parent. That link must not make their differing values a
    certain contradiction — which is what made one shipment supersede another."""
    cfg = Settings()
    def row(nid, ref, weight, marker):
        return StatementView.of({"id": nid, "type": "Shipment", "label": ref,
                                 "properties": {"text": f"Shipment {ref}", "ref": ref,
                                                "weight_kg": weight, **marker}}, cfg)
    for marker in ({"__source": "json"}, {"__source_key": "file:shipments"}):
        a, b = row("a", "DL-1", 120, marker), row("b", "DL-2", 80, marker)
        a.origin = b.origin = "linked"
        assert a.record and b.record
        verdict = judge_by_rules(a, b, cfg)
        assert verdict is not None and verdict.relation == "unrelated"   # never compared as statements


# ── the JSON walker honours a declared mapping (regressed in 392e9ee: `walk_spec` was accepted and
#    ignored, so every record was a KeyNode whatever the Mapper said) ──────────────────────────────
class _FakeRepo:
    """The four NodeRepo calls the walker makes, in memory."""

    def __init__(self):
        self.nodes, self.edges = {}, set()

    async def upsert_node(self, *, workspace_id, type, label="", properties=None, source_ref=None,
                          collection_id=None, node_id=None, confidence=1.0, **_):
        old = self.nodes.get(node_id)
        props = {**((old or {}).get("properties") or {}), **(properties or {})}
        self.nodes[node_id] = {"id": node_id, "type": (old or {}).get("type") or type, "label": label,
                               "properties": props, "source_ref": source_ref}
        return SimpleNamespace(id=node_id, properties=props)

    async def add_edge(self, *, workspace_id, source_id, target_id, type, properties=None, **_):
        key = (source_id, target_id, type)
        new = key not in self.edges
        self.edges.add(key)
        return new

    async def get(self, workspace_id, node_id):
        n = self.nodes.get(node_id)
        return SimpleNamespace(id=node_id, properties=n["properties"]) if n else None

    async def upsert_many(self, workspace_id, specs, **_):
        """NodeRepo.upsert_many: the same writes, one record at a time."""
        return [await self.upsert_node(workspace_id=workspace_id, **s) for s in specs]

    async def add_edges(self, *, workspace_id, edges, **_):
        for e in edges:
            await self.add_edge(workspace_id=workspace_id, source_id=e["source_id"], target_id=e["target_id"], type=e["type"])
        return len(edges)

    async def patch_properties(self, workspace_id, node_ids, values=None, **_):
        for i in node_ids:
            if i in self.nodes:
                self.nodes[i]["properties"].update(values or {})
        return len(node_ids)


DELIVERIES = [
    {"id": "DL-1", "status": "delivered", "weight_kg": 120,
     "carrier": {"name": "DHL Express", "code": "DHL"},
     "items": [{"sku": "A-100", "qty": 2}, {"sku": "B-200", "qty": 5}]},
    {"id": "DL-2", "status": "in_transit", "weight_kg": 80,
     "carrier": {"name": "FedEx", "code": "FDX"}, "items": [{"sku": "A-100", "qty": 1}]},
]
DECLARED = [{"name": "Deliveries", "status": "declared", "keys": [
    {"path": "", "kind": "node", "node_type": "Shipment", "decision": "node"},
    {"path": "id", "kind": "attribute", "role": "id"},
    {"path": "status", "kind": "attribute", "role": "skip"},
    {"path": "carrier", "kind": "node", "node_type": "Carrier", "edge_type": "SHIPPED_BY",
     "decision": "node"},
    {"path": "items[]", "kind": "node", "node_type": "LineItem", "decision": "node"},
]}]


@pytest.fixture
def fake_repo(monkeypatch):
    from opencortyx.ingest.semistructured import ingest as walker_module
    repo = _FakeRepo()
    monkeypatch.setattr(walker_module, "_repo", repo)
    return repo


async def test_declared_json_mapping_shapes_the_graph(fake_repo):
    from opencortyx.ingest.semistructured.ingest import ingest_json
    from opencortyx.ingest.semistructured.schema import compile_walk_spec

    org = uuid.uuid4()
    res = await ingest_json(org, DELIVERIES, source_label="deliveries.json",
                            walk_spec=compile_walk_spec(DECLARED), auto_zip=False)
    by_type = {}
    for n in fake_repo.nodes.values():
        by_type.setdefault(n["type"], []).append(n)
    assert sorted(n["label"] for n in by_type["Shipment"]) == ["DL-1", "DL-2"]
    assert len(by_type["Carrier"]) == 2 and len(by_type["LineItem"]) == 3
    assert "KeyNode" not in by_type
    assert all("status" not in n["properties"] for n in by_type["Shipment"])

    types = {n["id"]: n["type"] for n in fake_repo.nodes.values()}
    edges = {(types[s], t, types[d]) for s, d, t in fake_repo.edges}
    assert ("Source", "HAS_RECORD", "Shipment") in edges
    assert ("Shipment", "SHIPPED_BY", "Carrier") in edges
    assert ("Shipment", "HAS_ITEM", "LineItem") in edges
    assert ("LineItem", "NEXT", "LineItem") in edges

    src = uuid.UUID(res["source_id"])
    assert all(n["source_ref"] == src for n in by_type["Shipment"])     # records trace to the file
    assert res["records"] == 2


async def test_auto_json_walk_keeps_keynodes_with_a_semantic_label(fake_repo):
    from opencortyx.ingest.semistructured.ingest import ingest_json

    await ingest_json(uuid.uuid4(), DELIVERIES, source_label="auto.json")
    labels = {n["properties"].get("Label") for n in fake_repo.nodes.values() if n["type"] == "KeyNode"}
    assert {"Carrier", "Items"} <= labels      # the plain walker labels by key, unsingularised


def _line_item(nid, qty, source="json"):
    return StatementView.of({"id": nid, "type": "LineItem", "label": "A-100",
                             "properties": {"text": f"LineItem A-100. sku=A-100; qty={qty}",
                                            "sku": "A-100", "qty": qty, "__source": source}},
                            Settings())


def test_a_model_verdict_between_walked_records_is_proposed_not_applied():
    cfg = Settings()
    sure = Verdict("contradiction", 0.93, "llm", "qty differs", lane="complex")
    assert decide(_line_item("a", 2), _line_item("b", 1), sure, cfg).kind == "propose"
    certain = Verdict("contradiction", 1.0, "rule", "qty: 2 vs 1", lane="clear")
    assert decide(_line_item("a", 2), _line_item("b", 1), certain, cfg).kind == "propose"
    # prose statements keep the workspace's auto-apply policy
    assert decide(_line_item("a", 2, "enriched"), _line_item("b", 1, "enriched"), sure, cfg).kind \
        == "supersede"


def test_no_module_defines_a_top_level_name_twice():
    """A second `def` silently replaces the first. That is how extraction broke: a new two-argument
    helper was shadowed by an older one-argument helper of the same name further down the module."""
    import ast
    import collections
    import pathlib

    import opencortyx
    root = pathlib.Path(opencortyx.__file__).parent
    dups = {}
    for f in root.rglob("*.py"):
        body = ast.parse(f.read_text()).body
        names = collections.Counter(n.name for n in body
                                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        twice = [n for n, c in names.items() if c > 1]
        if twice:
            dups[str(f.relative_to(root))] = twice
    assert not dups, dups


def test_extractor_reads_stored_node_text_before_the_label():
    from opencortyx.ingest.extraction.templates import _extractor_text
    row = SimpleNamespace(properties={"__text": "Order OR-1. amount=120.5"}, label="OR-1")
    assert _extractor_text(row, {}) == "Order OR-1. amount=120.5"
    chunk = SimpleNamespace(properties={"text": "prose"}, label="Chunk #0")
    assert _extractor_text(chunk, {}) == "prose"
    bare = SimpleNamespace(properties={}, label="customers")
    assert _extractor_text(bare, {}) == "customers"


# ── a pending proposal the certain tier no longer supports is withdrawn on re-judgment (proposals
#    written between walked records before the peer rule stayed in the queue forever) ───────────────
class _ProposalDeps:
    def __init__(self, nodes):
        self.nodes, self.cleared = nodes, []

    async def load(self, workspace_id, node_id):
        return self.nodes.get(str(node_id))

    async def clear_props(self, workspace_id, node_id, keys):
        self.cleared.append((node_id, tuple(keys)))


def _proposed(nid, winner, marker, **extra):
    return SimpleNamespace(id=nid, type="LineItem", label=nid, properties={
        "text": f"LineItem {nid}", "sku": "A-100", "superseded": "proposed",
        "superseded_by": winner, **marker, **extra})


@pytest.mark.asyncio
async def test_a_stale_proposal_between_records_is_withdrawn():
    from opencortyx.capabilities.modules.contradiction import RESERVED, _recheck_proposal
    cfg = Settings()
    loser = _proposed("a", "b", {"__source": "json"})
    deps = _ProposalDeps({"b": _proposed("b", "", {"__source": "json"})})
    assert await _recheck_proposal("org", loser, StatementView.of(loser, cfg), cfg, deps)
    assert deps.cleared == [("a", RESERVED.PROPOSAL_KEYS)]


@pytest.mark.asyncio
async def test_a_proposal_rules_cannot_settle_and_a_version_proposal_are_kept():
    from opencortyx.capabilities.modules.contradiction import _recheck_proposal
    cfg = Settings()
    prose = _proposed("a", "b", {})
    deps = _ProposalDeps({"b": _proposed("b", "", {})})
    assert not await _recheck_proposal("org", prose, StatementView.of(prose, cfg), cfg, deps)
    version = _proposed("a", "b", {"__source": "json"}, resolution_kind="version")
    assert not await _recheck_proposal("org", version, StatementView.of(version, cfg), cfg, deps)
    assert deps.cleared == []


# ── a settings write is checked against the field's declaration (range, kind, options) ────────────
def test_setting_values_are_validated_against_their_declaration():
    from opencortyx.core.config.runtime import _validated
    k = {"key": "k", "kind": "int", "min": 1, "max": 100}
    assert _validated("k", "12", k) == 12
    for bad in (-5, 101, 2.5, "abc", True):
        with pytest.raises(ValueError):
            _validated("k", bad, k)
    with pytest.raises(ValueError):
        _validated("m", "galaxy", {"key": "m", "kind": "select", "options": ["person", "team"]})
    assert _validated("b", "false", {"key": "b", "kind": "bool"}) is False
    with pytest.raises(ValueError):
        _validated("b", "maybe", {"key": "b", "kind": "bool"})


def test_every_opencortyx_from_import_names_something_that_exists():
    """A renamed helper left `from … import _get_cross_encoder` in two lazy imports, so the reranker's
    Test button and the L3 cross-encoder failed only when called."""
    import ast
    import importlib
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "opencortyx"
    missing = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.ImportFrom) and node.module
                    and node.module.startswith("opencortyx") and node.level == 0):
                continue
            try:
                mod = importlib.import_module(node.module)
            except Exception:                            # optional heavy deps: judged elsewhere
                continue
            for alias in node.names:
                if alias.name != "*" and not hasattr(mod, alias.name):
                    try:
                        importlib.import_module(f"{node.module}.{alias.name}")
                    except Exception:
                        missing.append(f"{path.relative_to(root)}: {node.module}.{alias.name}")
    assert not missing, "\n".join(missing)


def test_a_settings_save_reports_only_what_moved():
    """The form submits every field; audit + side effects (mode → materialize) key off real changes."""
    from opencortyx.core.config.runtime import changes
    before = {"mode": "person", "default_retrieve_k": 12, "contradiction_types": []}
    after = {"mode": "person", "default_retrieve_k": 10, "contradiction_types": []}
    assert changes(before, after, after.keys()) == {"default_retrieve_k": {"from": 12, "to": 10}}
    assert changes(before, before, before.keys()) == {}


@pytest.mark.asyncio
async def test_resolved_entities_are_names_not_statements():
    """Two entity names never contradict; a proposal left between them is withdrawn on re-check."""
    from opencortyx.capabilities.modules.contradiction import RESERVED, _recheck_proposal
    cfg = Settings()
    ent = {"id": "e1", "type": "Entity", "label": "vendor register",
           "properties": {"__identity": "resolver", "description": "A record of approved vendors.",
                          "superseded": "proposed", "superseded_by": "e2"}}
    assert StatementView.of(ent, cfg) is None
    deps = _ProposalDeps({})
    assert await _recheck_proposal("org", ent, None, cfg, deps)
    assert deps.cleared == [("e1", RESERVED.PROPOSAL_KEYS)]


# ── requests are validated at the boundary: a bad value is a 422 with the reason, never silently
#    coerced (an unknown pipeline kind became "document", an unknown paradigm "semistructured") ────
def _rejects(factory) -> str:
    from pydantic import ValidationError
    try:
        factory()
    except ValidationError as e:
        return str(e)
    raise AssertionError("accepted")


def test_request_schemas_refuse_what_nothing_can_run():
    from opencortyx.api.schemas.ingestion.connectors import ConnectorSpec
    from opencortyx.api.schemas.knowledge.context import ContextQuery
    from opencortyx.api.schemas.knowledge.evaluation import EvalCaseSpec
    from opencortyx.api.schemas.ingestion.extraction import TemplateSpec
    from opencortyx.api.schemas.ingestion.pipelines import PipelineSpec
    from opencortyx.api.schemas.ingestion.schema_mappings import SchemaMappingSpec
    assert "kind must be one of" in _rejects(lambda: PipelineSpec(name="p", kind="video"))
    assert PipelineSpec(name="p", kind="unstructured").kind == "document"
    assert "stage 0" in _rejects(lambda: PipelineSpec(name="p", stages=["oops"]))
    assert "paradigm" in _rejects(lambda: SchemaMappingSpec(name="m", paradigm="hologram"))
    assert "unknown connector kind" in _rejects(lambda: ConnectorSpec(name="c", kind="floppy-disk"))
    assert ConnectorSpec(name="c", kind="SQLite").kind == "sqlite"
    assert "question is required" in _rejects(lambda: EvalCaseSpec(question="  "))
    assert "a question is required" in _rejects(lambda: ContextQuery(query=" "))
    assert "ISO date" in _rejects(lambda: ContextQuery(query="q", as_of="yesterday"))
    _rejects(lambda: ContextQuery(query="q", abstain_threshold=7))
    assert "name is required" in _rejects(lambda: TemplateSpec(name=" ", mechanism={"ref": "unstructured.llm", "prompt": "x"}))
    assert "unknown mechanism" in _rejects(lambda: TemplateSpec(name="t", mechanism={"ref": "nonsense"}))
    assert "needs a prompt" in _rejects(lambda: TemplateSpec(name="t", mechanism={"ref": "unstructured.llm", "prompt": ""}))
    assert "does not compile" in _rejects(lambda: TemplateSpec(name="t", mechanism={"ref": "unstructured.python", "code": "def f(:"}))
    assert "regular expression" in _rejects(lambda: TemplateSpec(name="t", mechanism={"ref": "unstructured.regex", "pattern": "([A-Z"}))
    # a built-in's prompt edit sends only the prompt, and stays accepted
    assert TemplateSpec(name="builtin:struct.segment", mechanism={"prompt": "tuned {text}"})


def test_mapping_paradigm_lists_agree():
    from opencortyx.api.schemas.ingestion.schema_mappings import MAPPING_PARADIGMS as schema_side
    from opencortyx.api.services.ingestion.schema_mappings import MAPPING_PARADIGMS as service_side
    assert tuple(schema_side) == tuple(service_side)


def test_sqlite_and_other_dialects_build_their_own_urls():
    from opencortyx.core.security.secrets import resolve_connector_dsn
    assert resolve_connector_dsn({"kind": "sqlite", "database": "/data/files/x.sqlite"}) == "sqlite:////data/files/x.sqlite"
    assert resolve_connector_dsn({"kind": "mssql", "host": "db", "database": "d"}).startswith("mssql+pymssql://db:1433/")
    assert resolve_connector_dsn({"kind": "postgres", "host": "db", "database": "d"}) == "postgresql://db:5432/d"


def test_empty_json_payloads_hold_no_records():
    from opencortyx.ingest.walkers.semistructured import _has_records
    assert not _has_records({"records": [[]]}) and not _has_records({"records": [{}]})
    assert _has_records({"records": [{"a": 1}]}) and _has_records({"records": [42]})


def test_blank_and_corrupt_documents_are_unreadable():
    from opencortyx.ingest.formats.unstructured.documents import UnreadableDocument, parse_content
    for content, name in ((b"  \n\t ", "blank.txt"), (b"PK\x03\x04 nope", "broken.docx")):
        with pytest.raises(UnreadableDocument):
            parse_content(content, filename=name)


def test_a_sentence_with_an_invented_number_is_not_grounded():
    from opencortyx.retrieval.answering.trust import _numbers, unsupported_numbers
    ctx = _numbers("Every employee receives 24 days of paid leave. Meals up to 60 EUR; hotels 1,000 EUR at 07:00.")
    assert unsupported_numbers("Employees get 45 days of paid annual leave [15].", ctx) == {"45"}
    assert not unsupported_numbers("Employees get 24 days of paid leave [3].", ctx)
    assert not unsupported_numbers("Hotels are capped at 1000 EUR, served from 7:00 [1, 2].", ctx)


def test_outcomes_name_generic_records_by_what_they_are():
    from opencortyx.ingest.runs.outcome import _display_type
    assert _display_type(SimpleNamespace(type="KeyNode", properties={"key": "Product"})) == "Product"
    assert _display_type(SimpleNamespace(type="KeyNode", properties={})) == "KeyNode"
    assert _display_type(SimpleNamespace(type="Vendor", properties={"key": "x"})) == "Vendor"


# ── a task that keeps killing its worker is given up, not re-delivered forever ──────────────────────
class _FakeRedis:
    def __init__(self):
        self.d = {}

    def incr(self, k):
        self.d[k] = self.d.get(k, 0) + 1
        return self.d[k]

    def expire(self, k, _ttl):
        pass

    def delete(self, k):
        self.d.pop(k, None)


def test_attempts_that_die_mid_run_are_counted_and_capped(monkeypatch):
    import importlib
    ca = importlib.import_module("opencortyx.core.worker.celery_app")   # the module, not the app
    store = _FakeRedis()
    monkeypatch.setattr(ca, "_attempts_store", lambda: store)
    task = SimpleNamespace(request=SimpleNamespace(id="t-1"))
    assert ca._attempt_started(task) == 0          # first attempt
    assert ca._attempt_started(task) == 1          # the first never finished: killed
    assert ca._attempt_started(task) == 2 >= ca.MAX_LOST_ATTEMPTS
    ca._attempt_finished(task)
    assert ca._attempt_started(task) == 0          # a finished attempt leaves no mark


def test_a_given_up_task_fails_its_run_with_the_reason(monkeypatch):
    import importlib
    ca = importlib.import_module("opencortyx.core.worker.celery_app")   # the module, not the app
    importlib.import_module("opencortyx.tasks._shared")    # registers the hook that fails the run
    from opencortyx.ingest.runs import progress
    monkeypatch.setattr(ca, "_attempts_store", lambda: _FakeRedis())
    seen = {}

    async def fake_finish(self, status="completed", result=None, error=None):
        seen.update(run=self.run_id, status=status, error=error)
    monkeypatch.setattr(progress.ProgressReporter, "finish", fake_finish)

    def run(workspace_id, content_b64, filename, run_id=None):
        return None
    task = SimpleNamespace(name="opencortyx.ingest_document", run=run,
                           request=SimpleNamespace(id="t-2"))
    ca._give_up(task, ("org", "b64", "big.pdf"), {"run_id": "run-9"}, 2)
    assert seen["run"] == "run-9" and seen["status"] == "failed"
    assert "memory" in seen["error"]


def test_a_pipelines_deferred_stages_run_on_the_extraction_queue():
    """LLM stages of a pipeline upload must not hold the ingest worker: one large PDF's 200 summary
    calls kept every other upload "waiting for a worker" for hours."""
    import importlib
    ca = importlib.import_module("opencortyx.core.worker.celery_app")
    route = ca.celery_app.amqp.router.route({}, "opencortyx.run_document_pipeline")
    assert route["queue"].name == "extraction"
    assert ca.celery_app.amqp.router.route({}, "opencortyx.ingest_document_flow")["queue"].name == "graphrag"


def test_document_passages_are_not_judged_as_statements():
    """Chunks, sections and layout elements are passages; the claims extracted from them are the
    statements. Judging every chunk cost an LLM call each on every upload of a large document."""
    cfg = Settings()
    stamped = {"id": "c1", "type": "Clause", "properties": {"text": "Article 5 …", "__reading_unit": True}}
    legacy = {"id": "c2", "type": "Chunk", "properties": {"text": "Article 6 …"}}
    claim = {"id": "k1", "type": "Claim", "properties": {"text": "Consent must be freely given."}}
    assert StatementView.of(stamped, cfg) is None
    assert StatementView.of(legacy, cfg) is None
    assert StatementView.of(claim, cfg) is not None


def test_a_duplicate_offers_to_replace_and_both_skips_honour_it():
    """An identical re-upload is skipped — so the user must be able to say "read it again anyway",
    for the copy whose knowledge went missing."""
    import inspect
    from opencortyx.ingest.runs.outcome import IngestOutcome, _next_steps
    from opencortyx.ingest.unstructured.ingest import ingest_document
    from opencortyx.ingest.semistructured.ingest import ingest_json
    dup = IngestOutcome(run_id="r1", status="duplicate", asset="gdpr.pdf", kind="document")
    assert [s.action for s in _next_steps(dup, {"source_ref": "s1"})] == ["open_asset", "replace"]
    assert "replace" in inspect.signature(ingest_document).parameters
    assert "replace" in inspect.signature(ingest_json).parameters


def test_the_model_registry_not_the_env_decides_the_models():
    """Models are chosen on the Models page. The env only seeds an empty registry: a row rewritten
    from env on every read, and an Ollama endpoint dropped from an openai_compatible row, both sent
    generation to a vLLM server that does not exist as soon as the LLM keys left `.env`."""
    import asyncio
    import inspect
    from opencortyx.core.inference import llm, registry

    assert "d[\"provider\"]" not in inspect.getsource(registry.ensure_default_models)
    url = "http://host.docker.internal:11434/v1"
    assert registry.coerce_legacy_row("openai_compatible", "mistral", url, "generation")[2] == url
    assert registry.coerce_legacy_row("ollama", "mistral", url, "generation")[2] is None

    chosen = {"provider": "openai_compatible", "model_id": "picked-on-models-page",
              "base_url": "http://models-page:1/v1", "api_key": None, "params": {}}
    seen = {}

    async def fake_active():
        return chosen

    async def fake_fit(prompt, max_tokens, base, mid, discover=True):
        return prompt

    async def fake_openai(prompt, system, max_tokens, json_mode, mid, temp, base_url=None, api_key=None):
        seen.update(model=mid, base=base_url)
        return "ok"

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(registry, "active_generation_spec", fake_active)
        mp.setattr(llm, "_fit_to_window", fake_fit)
        mp.setattr(llm, "_openai_complete", fake_openai)
        assert asyncio.run(llm.complete("hi")) == "ok"
    finally:
        mp.undo()
    assert seen == {"model": "picked-on-models-page", "base": "http://models-page:1/v1"}
