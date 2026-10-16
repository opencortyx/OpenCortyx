"""Drafting: the sample, the three drafters, and the promises the API makes about a draft.

These run offline. Every model call goes through `authoring.llm.ask_json`, which is monkeypatched
here — partly so the suite does not need a model server, but mostly because the interesting cases are the
ones where the model is WRONG: unreachable, unparseable, or confidently naming a type that cannot
exist. Those are the paths that decide whether a bad answer becomes a bad extractor or a warning,
and they cannot be provoked by a model that is behaving.

Two promises are load-bearing and asserted directly rather than implied:

  a draft never writes    — drafting runs on an upload, so a drafter that persisted something
                            would make "show me what you'd do" indistinguishable from "do it".
  a draft is a request    — `Draft.payload` is the exact body its `save_endpoint` accepts, so
                            saving is one POST and there is no second write path to drift.
"""
from __future__ import annotations

import json
import uuid

import pytest

from opencortyx.drafting import registry
from opencortyx.drafting.models import (
    Confidence, Draft, DraftKind, DraftRequest, Evidence, Sample,
)
from opencortyx.drafting.sampling import build_sample

ORG = uuid.UUID("00000000-0000-4000-8000-0000000000aa")

CONTRACTS = [
    {"id": "c-1", "vendor": "Northwind Supplies", "signed_on": "2026-01-14",
     "body": "Northwind Supplies shall deliver 400 units per month for twenty four months, "
             "invoiced monthly in arrears at a rate agreed in schedule two of this agreement."},
    {"id": "c-2", "vendor": "Acme Freight", "signed_on": "2026-02-02",
     "body": "Acme Freight is engaged to move pallets between the Leeds and Bristol depots on a "
             "next-day basis, with liability capped at the declared value of each consignment."},
    {"id": "c-3", "vendor": "Borealis Cloud", "signed_on": "2026-02-19", "renewal_notice_days": 90,
     "body": "Borealis Cloud provides hosting on a rolling annual term which renews automatically "
             "unless either party gives written notice before the notice period expires."},
]

IDS_ONLY = [{"id": i, "account_id": 100 + i, "ts": f"2026-03-{i:02d}"} for i in range(1, 6)]


def _req(kind: DraftKind, sample: Sample, **kw) -> DraftRequest:
    return DraftRequest(workspace_id=ORG, kind=kind, sample=sample, **kw)


def _json_sample(records: list, name: str = "contracts.json") -> Sample:
    return build_sample(json.dumps(records).encode("utf-8"), name)


#: Every name `ask_json` is reachable under. The drafters do `from ...llm import ask_json`, which
#: binds the function into their own module at import — so patching only `authoring.llm.ask_json`
#: patches a name nobody reads. That mistake does not fail loudly here: this suite runs inside the
#: api container, the real call reaches the model server, and the test passes or fails on what a live model
#: happened to say. Patch every binding, or the isolation is imaginary.
_ASK_JSON = ("opencortyx.drafting.llm.ask_json",
             "opencortyx.drafting.drafters.mapping.ask_json",
             "opencortyx.drafting.drafters.extractor.ask_json")


def _patch_ask_json(monkeypatch, fn) -> None:
    for target in _ASK_JSON:
        monkeypatch.setattr(target, fn)


@pytest.fixture
def no_model(monkeypatch):
    """Every drafter with inference turned off — the measured half, alone."""
    async def _down(*_a, **_kw):
        return {}, "the model was not reachable (ConnectError); kept the measured reading"
    _patch_ask_json(monkeypatch, _down)
    return _down


def _answers(monkeypatch, obj: dict):
    """Point `ask_json` at a fixed answer, wherever a drafter imported it from."""
    async def _fixed(*_a, **_kw):
        return obj, ""
    _patch_ask_json(monkeypatch, _fixed)


# ── sampling ────────────────────────────────────────────────────────────────────────────────────
def test_a_json_file_is_sampled_as_records():
    sample = _json_sample(CONTRACTS)
    assert sample.route == "semistructured"
    assert len(sample.records) == 3
    assert sample.records[0]["vendor"] == "Northwind Supplies"


def test_a_csv_file_is_sampled_as_columns_and_rows():
    csv = b"invoice_id,customer_id,amount\n1,7,100.50\n2,9,250.00\n"
    sample = build_sample(csv, "invoices.csv")
    assert sample.route == "structured"
    assert sample.columns == ["invoice_id", "customer_id", "amount"]
    assert len(sample.rows) == 2


def test_an_unparseable_file_yields_an_empty_sample_not_an_exception():
    """Someone dragged in a file. A stack trace is not an answer; a reason is."""
    sample = build_sample(b"\x00\x01\x02\x03 not anything", "mystery.json")
    assert sample.is_empty or not sample.records
    assert sample.warnings, "an unreadable file must say why"


def test_an_empty_file_is_reported_as_empty():
    sample = build_sample(b"", "nothing.json")
    assert sample.is_empty
    assert any("empty" in w for w in sample.warnings)


def test_the_sample_is_bounded():
    """A 6 MB file must cost what a 6 KB one costs."""
    big = json.dumps([CONTRACTS[0]] * 20_000).encode("utf-8")
    sample = build_sample(big, "big.json")
    assert len(sample.records) <= 20
    assert sample.truncated


# ── (C) mapping drafts ──────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_json_sample_becomes_a_saveable_mapping(no_model):
    drafts = await _run(DraftKind.MAPPING, _json_sample(CONTRACTS))
    assert len(drafts) == 1, "only the semi-structured drafter should claim a JSON sample"
    draft = drafts[0]
    assert draft.save_endpoint == "/api/v2/schema-mappings"
    assert set(draft.payload) >= {"name", "paradigm", "payload", "status"}
    assert draft.payload["paradigm"] == "semistructured"
    assert draft.payload["payload"]["structures"], "a mapping with no structure reads nothing"


@pytest.mark.asyncio
async def test_the_mapping_payload_is_exactly_what_the_endpoint_accepts(no_model):
    """The promise that makes saving one POST. If this drifts, every client has to re-wrap."""
    from opencortyx.api.schemas.ingestion.schema_mappings import SchemaMappingSpec

    for sample in (_json_sample(CONTRACTS),
                   build_sample(b"invoice_id,customer_id,amount\n1,7,100.50\n", "invoices.csv"),
                   build_sample(b"# Onboarding\n\nWelcome to the team.\n", "onboarding.md")):
        for draft in await _run(DraftKind.MAPPING, sample):
            SchemaMappingSpec(**draft.payload)      # raises if the shape is wrong


@pytest.mark.asyncio
async def test_a_mapping_draft_explains_every_key_it_read(no_model):
    draft = (await _run(DraftKind.MAPPING, _json_sample(CONTRACTS)))[0]
    claims = " ".join(e.claim for e in draft.evidence)
    assert "body" in claims and "signed_on" in claims
    assert all(e.basis for e in draft.evidence), "a claim with no basis cannot be checked"


@pytest.mark.asyncio
async def test_records_that_disagree_are_reported_not_smoothed_over(no_model):
    """`renewal_notice_days` is on the third contract only. Reading record one alone loses it."""
    draft = (await _run(DraftKind.MAPPING, _json_sample(CONTRACTS)))[0]
    paths = draft.payload["payload"]["structures"][0]["paths"]
    assert "renewal_notice_days" in paths, "the widest record must win, not the first"


@pytest.mark.asyncio
async def test_a_thin_sample_lowers_confidence(no_model):
    draft = (await _run(DraftKind.MAPPING, _json_sample(CONTRACTS[:1])))[0]
    assert draft.confidence is Confidence.LOW
    assert any("record" in w for w in draft.warnings)


@pytest.mark.asyncio
async def test_a_csv_becomes_one_node_per_row_with_its_foreign_key(no_model):
    sample = build_sample(b"invoice_id,customer_id,amount\n1,7,100.50\n2,9,250.00\n",
                          "invoices.csv")
    draft = (await _run(DraftKind.MAPPING, sample))[0]
    tables = draft.payload["payload"]["table_mappings"]
    assert tables["nodes"][0]["nodeType"] == "Invoice", "singularised from the file name"
    assert any(e["edgeType"] == "HAS_CUSTOMER" for e in tables["edges"]), \
        "customer_id names what it points at"


@pytest.mark.asyncio
async def test_a_document_becomes_a_saveable_mapping(no_model):
    sample = build_sample(b"# Onboarding\n\nWelcome to the team. " * 40, "onboarding.md")
    assert sample.route == "document"
    drafts = await _run(DraftKind.MAPPING, sample)
    assert len(drafts) == 1, "document mapping drafter should claim a document sample"
    draft = drafts[0]
    assert draft.save_endpoint == "/api/v2/schema-mappings"
    assert draft.payload["paradigm"] == "unstructured"
    assert "editor" in draft.payload["payload"]
    editor = draft.payload["payload"]["editor"]
    labels = [n["nodeLabel"] for n in editor["nodeRules"]]
    # The sample has a heading, so it is read by layout — the only strategy that keeps structure.
    assert editor["chunking"]["strategy"] == "layout"
    assert "Document" in labels and "Title" in labels
    claims = " ".join(e.claim for e in draft.evidence)
    assert "document becomes a Document node" in claims
    assert "read by layout" in claims
    assert "Topic" not in claims


@pytest.mark.asyncio
async def test_a_document_without_headings_is_still_read_by_layout(no_model):
    """Layout is the reading for every document: one with no headings keeps its paragraphs as
    elements of the Document, and the draft promises no headings it did not find."""
    sample = build_sample(b"Welcome to the team. We are glad you are here. " * 60, "welcome.txt")
    draft = (await _run(DraftKind.MAPPING, sample))[0]
    editor = draft.payload["payload"]["editor"]
    labels = [n["nodeLabel"] for n in editor["nodeRules"]]
    assert editor["chunking"]["strategy"] == "layout"
    assert labels[0] == "Document" and "Chunk" not in labels
    assert not {"Title", "SectionHeader"} & set(labels)


@pytest.mark.asyncio
async def test_a_plain_text_document_drafts_document_and_its_elements(no_model):
    sample = build_sample(b"Mars Exploration Rover mission landed on Mars in 2004.", "mars.txt")
    drafts = await _run(DraftKind.MAPPING, sample)
    assert len(drafts) == 1
    draft = drafts[0]
    labels = [n["nodeLabel"] for n in draft.payload["payload"]["editor"]["nodeRules"]]
    assert labels[0] == "Document" and "Text" in labels
    claims = " ".join(e.claim for e in draft.evidence)
    assert "Topic" not in claims


@pytest.mark.asyncio
async def test_a_pdf_is_previewed_from_the_file_not_its_text(no_model):
    """A PDF's extracted text has no Markdown `#` headings, so a preview that read only the text
    (which is what it did) showed a PDF with real headings as sliding-window chunks."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    y = 60
    for text, size, font in (("Leave Policy", 20, "hebo"), ("Annual leave", 14, "hebo"),
                             ("Employees get 25 days of paid leave each year.", 10, "helv"),
                             ("Sick leave", 14, "hebo"),
                             ("Up to ten days a year with a doctor's note.", 10, "helv")):
        y += 24 if size > 10 else 4
        page.insert_text((50, y), text, fontsize=size, fontname=font)
        y += size * 1.6
    sample = build_sample(doc.tobytes(), "leave.pdf")
    assert sample.document, "the sample must keep the file itself for a structure preview"
    editor = (await _run(DraftKind.MAPPING, sample))[0].payload["payload"]["editor"]
    labels = [n["nodeLabel"] for n in editor["nodeRules"]]
    assert editor["chunking"]["strategy"] == "layout"
    assert "Chunk" not in labels
    assert {"Title", "SectionHeader"} & set(labels), f"no heading found in {labels}"



@pytest.mark.asyncio
async def test_document_pipeline_has_no_irrelevant_date_warning(no_model):
    sample = build_sample(b"Mars Exploration Rover mission landed on Mars in 2004.", "mars.txt")
    pipeline_draft = (await _run(DraftKind.PIPELINE, sample))[0]
    assert not any("date field was recognised" in w for w in pipeline_draft.warnings)


@pytest.mark.asyncio
async def test_a_model_that_renames_the_entity_is_recorded_as_evidence(monkeypatch):
    _answers(monkeypatch, {"entity": "Payment"})
    sample = build_sample(b"invoice_id,customer_id,amount\n1,7,100.50\n", "tbl_0042.csv")
    draft = (await _run(DraftKind.MAPPING, sample))[0]
    assert draft.payload["payload"]["table_mappings"]["nodes"][0]["nodeType"] == "Payment"
    assert any("Payment" in e.claim for e in draft.evidence)


@pytest.mark.asyncio
async def test_a_model_that_answers_with_a_sentence_is_ignored(monkeypatch):
    """A node type is spelled out by every later query. A name needing repair is not a name."""
    _answers(monkeypatch, {"entity": "this looks like a list of invoices"})
    sample = build_sample(b"invoice_id,customer_id,amount\n1,7,100.50\n", "invoices.csv")
    draft = (await _run(DraftKind.MAPPING, sample))[0]
    assert draft.payload["payload"]["table_mappings"]["nodes"][0]["nodeType"] == "Invoice"


# ── (C) pipeline drafts ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_pipeline_draft_starts_from_the_shipped_default(no_model):
    from opencortyx.api.schemas.ingestion.pipelines import PipelineSpec

    draft = (await _run(DraftKind.PIPELINE, _json_sample(CONTRACTS)))[0]
    PipelineSpec(**draft.payload)
    assert draft.payload["kind"] == "semistructured"
    assert any("Universal JSON" in e.claim for e in draft.evidence)


@pytest.mark.asyncio
async def test_every_drafted_stage_is_one_the_executors_know(no_model):
    """An invented token saves, runs, reports success and writes nothing — so none are invented."""
    from opencortyx.ingest.pipelines.defaults import DEFAULT_PIPELINES, normalize_stage

    known = {normalize_stage(s).get("type")
             for spec in DEFAULT_PIPELINES for s in spec["stages"]}
    for sample in (_json_sample(CONTRACTS), _json_sample(IDS_ONLY, "events.json"),
                   build_sample(b"a,b\n1,2\n", "t.csv")):
        for draft in await _run(DraftKind.PIPELINE, sample):
            for stage in draft.payload.get("stages") or []:
                assert stage.get("type") in known, f"{stage.get('type')} is not a real stage"


@pytest.mark.asyncio
async def test_a_bound_mapping_is_written_in_the_engines_own_spelling(no_model):
    mapping_id = str(uuid.uuid4())
    req = _req(DraftKind.PIPELINE, _json_sample(CONTRACTS), context={"mapping_id": mapping_id})
    draft = (await registry.candidates(req)[0].draft(req))
    step1 = next(s for s in draft.payload["stages"] if s["type"] == "json_structures")
    assert step1["config"]["mapping_ids"] == [mapping_id]


# ── (E) extractor drafts ────────────────────────────────────────────────────────────────────────
GOAL = "track which vendors we owe money to"
PLAN = {"entity": "Vendor",
        "fields": [{"name": "vendor_name", "description": "the company owed money"},
                   {"name": "amount_owed", "description": "how much"},
                   {"name": "currency", "description": "the currency"}],
        "relationship": "OWES",
        "instruction": "Find every supplier the document says money is owed to."}


@pytest.mark.asyncio
async def test_a_goal_becomes_a_saveable_extractor(monkeypatch):
    from opencortyx.api.schemas.ingestion.extraction import TemplateSpec

    _answers(monkeypatch, PLAN)
    _no_preview(monkeypatch)
    draft = await _one(DraftKind.EXTRACTOR, Sample(), goal=GOAL)
    TemplateSpec(**draft.payload)
    assert draft.save_endpoint == "/api/v2/extraction-templates"
    assert draft.payload["emit"]["nodes"][0]["type"] == "Vendor"
    assert draft.payload["emit"]["edges"][0]["type"] == "OWES"


@pytest.mark.asyncio
async def test_the_extractor_uses_a_mechanism_the_engine_implements(monkeypatch):
    """The failure this whole design exists to prevent: a ref with no branch saves, runs,
    reports success and writes nothing."""
    from opencortyx.drafting.drafters.extractor import MECHANISM_REF
    from opencortyx.ingest.extraction.templates import DEPENDENT_MECHANISM_REFS

    assert MECHANISM_REF in DEPENDENT_MECHANISM_REFS
    _answers(monkeypatch, PLAN)
    _no_preview(monkeypatch)
    draft = await _one(DraftKind.EXTRACTOR, Sample(), goal=GOAL)
    assert draft.payload["mechanism"]["ref"] in DEPENDENT_MECHANISM_REFS


@pytest.mark.asyncio
async def test_the_prompt_reads_the_node_it_runs_on(monkeypatch):
    """Without `{text}` the mechanism extracts from nothing and reports success."""
    _answers(monkeypatch, PLAN)
    _no_preview(monkeypatch)
    draft = await _one(DraftKind.EXTRACTOR, Sample(), goal=GOAL)
    assert "{text}" in draft.payload["mechanism"]["prompt"]


@pytest.mark.asyncio
async def test_emitted_nodes_are_resolved_so_one_vendor_is_one_node(monkeypatch):
    _answers(monkeypatch, PLAN)
    _no_preview(monkeypatch)
    draft = await _one(DraftKind.EXTRACTOR, Sample(), goal=GOAL)
    node = draft.payload["emit"]["nodes"][0]
    assert node["resolve"] == "entity", "without this the graph grows a node per mention"
    assert node["label"] == "$vendor_name"


@pytest.mark.asyncio
async def test_an_unusable_entity_name_is_refused_rather_than_repaired(monkeypatch):
    _answers(monkeypatch, {**PLAN, "entity": "things we owe money for"})
    draft = await _one(DraftKind.EXTRACTOR, Sample(), goal=GOAL)
    assert draft.payload == {}, "a half-built extractor is worse than none"
    assert draft.warnings and draft.confidence is Confidence.LOW


@pytest.mark.asyncio
async def test_an_unusable_relationship_still_yields_the_nodes(monkeypatch):
    """A bad edge name loses the citation, not the extractor."""
    _answers(monkeypatch, {**PLAN, "relationship": "owes money to, roughly"})
    _no_preview(monkeypatch)
    draft = await _one(DraftKind.EXTRACTOR, Sample(), goal=GOAL)
    assert draft.payload["emit"]["nodes"][0]["type"] == "Vendor"
    assert "edges" not in draft.payload["emit"]
    assert any("relationship" in w for w in draft.warnings)


@pytest.mark.asyncio
async def test_fields_are_deduplicated_and_capped(monkeypatch):
    from opencortyx.drafting.drafters.extractor import MAX_FIELDS

    noisy = [{"name": "vendor_name"}, {"name": "vendor_name"}, {"name": "Not A Field"},
             {"name": ""}, *({"name": f"f{i}"} for i in range(30))]
    _answers(monkeypatch, {**PLAN, "fields": noisy})
    _no_preview(monkeypatch)
    draft = await _one(DraftKind.EXTRACTOR, Sample(), goal=GOAL)
    names = draft.payload["emit"]["nodes"][0]["attributes"]
    assert names[0] == "vendor_name" and len(names) == len(set(names))
    assert len(names) <= MAX_FIELDS
    assert "Not A Field" not in names


@pytest.mark.asyncio
async def test_a_preview_that_finds_nothing_warns_instead_of_looking_confident(monkeypatch):
    _answers(monkeypatch, PLAN)
    monkeypatch.setattr("opencortyx.ingest.extraction.templates.preview_template",
                        _preview_returning({"nodes": [], "edges": []}))
    draft = await _one(DraftKind.EXTRACTOR, _json_sample(CONTRACTS), goal=GOAL)
    assert draft.confidence is Confidence.LOW
    assert any("found nothing" in w for w in draft.warnings)


@pytest.mark.asyncio
async def test_a_preview_that_finds_something_raises_confidence(monkeypatch):
    _answers(monkeypatch, PLAN)
    monkeypatch.setattr("opencortyx.ingest.extraction.templates.preview_template",
                        _preview_returning({"nodes": [{"type": "Vendor", "label": "Acme"}],
                                            "edges": [{"type": "OWES"}]}))
    draft = await _one(DraftKind.EXTRACTOR, _json_sample(CONTRACTS), goal=GOAL)
    assert draft.confidence is Confidence.HIGH
    assert any("found" in e.claim for e in draft.evidence)


@pytest.mark.asyncio
async def test_a_preview_that_blows_up_leaves_the_draft_standing(monkeypatch):
    _answers(monkeypatch, PLAN)

    async def _boom(*_a, **_kw):
        raise RuntimeError("vLLM said no")
    monkeypatch.setattr("opencortyx.ingest.extraction.templates.preview_template", _boom)
    draft = await _one(DraftKind.EXTRACTOR, _json_sample(CONTRACTS), goal=GOAL)
    assert draft.payload["emit"]["nodes"], "an unverified draft is still a draft"
    assert any("unverified" in w for w in draft.warnings)


@pytest.mark.asyncio
async def test_an_unreachable_model_is_a_warning_not_an_exception(no_model):
    draft = await _one(DraftKind.EXTRACTOR, Sample(), goal=GOAL)
    assert draft.payload == {}
    assert any("not reachable" in w for w in draft.warnings)


@pytest.mark.asyncio
async def test_no_goal_means_no_extractor_drafter_claims_the_request():
    req = _req(DraftKind.EXTRACTOR, _json_sample(CONTRACTS))
    assert registry.candidates(req) == []


# ── the registry ────────────────────────────────────────────────────────────────────────────────
def test_every_registered_drafter_declares_its_contract():
    for drafter in registry.all():
        assert drafter.name and drafter.description
        assert isinstance(drafter.kind, DraftKind)
        assert drafter.describe()["kind"] == drafter.kind.value


def test_a_drafter_without_a_name_is_refused():
    from opencortyx.drafting.registry import DrafterRegistry
    from opencortyx.drafting.port import Drafter

    class Nameless(Drafter):
        kind = DraftKind.MAPPING

        def supports(self, req): return True

        async def draft(self, req): raise AssertionError("never called")

    with pytest.raises(ValueError):
        DrafterRegistry().register(Nameless())


def test_a_drafter_that_raises_while_deciding_does_not_take_the_others_down():
    from opencortyx.drafting.registry import DrafterRegistry
    from opencortyx.drafting.port import Drafter

    class Broken(Drafter):
        name, kind = "broken", DraftKind.MAPPING

        def supports(self, req): raise RuntimeError("third-party drafter bug")

        async def draft(self, req): raise AssertionError("never called")

    class Fine(Drafter):
        name, kind = "fine", DraftKind.MAPPING

        def supports(self, req): return True

        async def draft(self, req): raise AssertionError("never called")

    reg = DrafterRegistry()
    reg.register(Broken())
    reg.register(Fine())
    assert [d.name for d in reg.candidates(_req(DraftKind.MAPPING, Sample()))] == ["fine"]


def test_confidence_takes_the_worst_of_what_it_is_given():
    assert Confidence.worst(Confidence.HIGH, Confidence.LOW) is Confidence.LOW
    assert Confidence.worst(Confidence.HIGH, Confidence.MEDIUM) is Confidence.MEDIUM
    assert Confidence.worst(Confidence.HIGH) is Confidence.HIGH


def test_a_draft_serialises_to_the_wire_shape_the_ui_reads():
    from opencortyx.api.schemas.knowledge.authoring import DraftView

    draft = Draft(kind=DraftKind.MAPPING, name="n", payload={}, drafter="d",
                  evidence=[Evidence(claim="c", basis="b", path="p")])
    DraftView(**draft.to_dict())


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────
async def _run(kind: DraftKind, sample: Sample, **kw) -> list[Draft]:
    req = _req(kind, sample, **kw)
    return [await d.draft(req) for d in registry.candidates(req)]


async def _one(kind: DraftKind, sample: Sample, **kw) -> Draft:
    drafts = await _run(kind, sample, **kw)
    assert len(drafts) == 1, f"expected one {kind.value} draft, got {len(drafts)}"
    return drafts[0]


def _preview_returning(result: dict):
    async def _preview(*_a, **_kw):
        return result
    return _preview


def _no_preview(monkeypatch):
    """No sample text to run against — the draft is built but not verified."""
    monkeypatch.setattr("opencortyx.ingest.extraction.templates.preview_template",
                        _preview_returning({"nodes": [], "edges": []}))
