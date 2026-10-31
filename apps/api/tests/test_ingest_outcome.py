"""What a run produced, projected from the run row and the graph.

The projection exists because every ingest path records its own `result` keys, so there was no
answer to "what is in my system now" that did not start by asking which paradigm wrote it. These
tests pin the parts of that projection that are decisions rather than plumbing: what counts as a
FINDING versus the scaffolding of an ingest, what happens when a path never recorded an asset id,
and which next step a given outcome earns.

The database and the graph are both stubbed. The point is not that SQLAlchemy works — it is that a
run producing 128 Chunks and no entities offers "tell it what to read", and a run producing 42
Vendors offers "ask a question", because those are the two moments where a user can tell the
difference and nothing else in the product asks them.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from opencortyx.ingest.runs import outcome as mod

ORG = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
OTHER_ORG = uuid.UUID("00000000-0000-4000-8000-0000000000cc")
STARTED = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)


class _Run:
    def __init__(self, **kw):
        self.id = kw.get("id", uuid.uuid4())
        self.workspace_id = kw.get("workspace_id", ORG)
        self.status = kw.get("status", "completed")
        self.asset = kw.get("asset", "contracts.json")
        self.kind = kw.get("kind", "semistructured")
        self.source_ref = kw.get("source_ref", str(uuid.uuid4()))
        self.result = kw.get("result", {"nodes": 3})
        self.error = kw.get("error", "")
        self.started_at = kw.get("started_at", STARTED)
        self.created_at = kw.get("created_at", STARTED)
        self.finished_at = kw.get("finished_at", STARTED + timedelta(seconds=12))


class _Node:
    def __init__(self, type_: str, label: str = ""):
        self.type, self.label = type_, label


def _install(monkeypatch, run, nodes, *, graph_raises=False, published=(), pending=False):
    """Stub the reads the projection makes: the run row, the graph (`published`: records that point
    at the asset with FROM_ASSET), and whether detection still owes the upload its records."""
    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def get(self, _model, _id): return run

    class _Store:
        async def find_nodes(self, workspace_id, query):
            if graph_raises:
                raise RuntimeError("nebula is down")
            _Store.seen = query
            return nodes

        async def find_edges(self, workspace_id, query):
            if "FROM_ASSET" in (query.types or []):
                return [type("E", (), {"source_id": n.id})() for n in published]
            return []

        async def get_nodes(self, workspace_id, ids):
            return [n for n in published if str(n.id) in {str(i) for i in ids}]

    monkeypatch.setattr("opencortyx.tasks.maintenance.knowledge_pending", lambda *a: pending)
    monkeypatch.setattr("opencortyx.core.db.V2Session", _Session)
    monkeypatch.setattr("opencortyx.stores.graph.get_graph_store", lambda: _Store())
    return _Store


# ── attribution ─────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_findings_are_separated_from_the_plumbing(monkeypatch):
    """A user who imported contracts wants "42 Vendors", not "1 Source, 128 Chunks"."""
    nodes = ([_Node("Vendor", f"v{i}") for i in range(42)]
             + [_Node("Chunk") for _ in range(128)] + [_Node("Source", "contracts.json")])
    _install(monkeypatch, _Run(), nodes)

    result = await mod.ingest_outcome(ORG, uuid.uuid4())
    assert [t.type for t in result.found] == ["Vendor"]
    assert {t.type for t in result.structural} == {"Chunk", "Source"}
    assert result.total_nodes == 171


@pytest.mark.asyncio
async def test_examples_are_carried_so_the_count_is_checkable(monkeypatch):
    _install(monkeypatch, _Run(), [_Node("Vendor", f"Vendor {i}") for i in range(9)])
    result = await mod.ingest_outcome(ORG, uuid.uuid4())
    assert result.found[0].examples[:2] == ["Vendor 0", "Vendor 1"]
    assert len(result.found[0].examples) <= mod.MAX_EXAMPLES


@pytest.mark.asyncio
async def test_a_run_with_an_asset_id_is_attributed_exactly(monkeypatch):
    asset = "3f2b6c1e-8d4a-4c52-9a8f-2c1d7e5b9a10"          # asset ids are node ids (uuids)
    run = _Run(source_ref=asset)
    store = _install(monkeypatch, run, [_Node("Vendor", "v")])
    await mod.ingest_outcome(ORG, run.id)
    assert store.seen.source_refs == [asset]
    assert store.seen.created_after is None, "an exact handle must not be widened by a time window"


@pytest.mark.asyncio
async def test_a_sql_run_is_attributed_by_its_source_key(monkeypatch):
    """A SQL run records a source KEY, not a node id; read as an id it matched nothing."""
    run = _Run(source_ref="sqlite:qa_shop.sqlite")
    store = _install(monkeypatch, run, [_Node("Table", "customers")])
    result = await mod.ingest_outcome(ORG, run.id)
    assert store.seen.source_key == "sqlite:qa_shop.sqlite"
    assert store.seen.created_after is None
    assert result.total_nodes == 1


@pytest.mark.asyncio
async def test_a_run_without_an_asset_id_falls_back_to_time_and_says_so(monkeypatch):
    """Creation time over-counts when two ingests overlap. Reported, not presented as exact."""
    run = _Run(source_ref=None)
    store = _install(monkeypatch, run, [_Node("Vendor", "v")])
    result = await mod.ingest_outcome(ORG, run.id)
    assert store.seen.created_after == STARTED - mod.CLOCK_SKEW
    assert any("may include another run" in n for n in result.notes)


@pytest.mark.asyncio
async def test_another_orgs_run_is_not_visible(monkeypatch):
    _install(monkeypatch, _Run(workspace_id=OTHER_ORG), [_Node("Vendor", "v")])
    assert await mod.ingest_outcome(ORG, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_a_missing_run_is_none_not_an_empty_outcome(monkeypatch):
    _install(monkeypatch, None, [])
    assert await mod.ingest_outcome(ORG, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_a_malformed_run_id_is_a_value_error(monkeypatch):
    """So the route can answer 422. A 404 would send someone hunting for a run that could never
    have existed under that string."""
    _install(monkeypatch, _Run(), [])
    with pytest.raises(ValueError):
        await mod.ingest_outcome(ORG, None)


@pytest.mark.asyncio
async def test_an_unreachable_graph_still_returns_what_the_worker_reported(monkeypatch):
    _install(monkeypatch, _Run(result={"nodes": 3, "chunks": 9}), [], graph_raises=True)
    result = await mod.ingest_outcome(ORG, uuid.uuid4())
    assert result.reported == {"nodes": 3, "chunks": 9}
    assert any("graph could not be read" in n for n in result.notes)


@pytest.mark.asyncio
async def test_duration_comes_from_the_run_not_the_clock(monkeypatch):
    _install(monkeypatch, _Run(), [_Node("Vendor", "v")])
    assert (await mod.ingest_outcome(ORG, uuid.uuid4())).duration_seconds == 12.0


@pytest.mark.asyncio
async def test_a_running_run_answers_with_what_exists_so_far(monkeypatch):
    """The UI polls this while the spinner turns; refusing would make it useless there."""
    _install(monkeypatch, _Run(status="running", finished_at=None),
             [_Node("Vendor", "v")])
    result = await mod.ingest_outcome(ORG, uuid.uuid4())
    assert result.status == "running" and result.found[0].count == 1
    assert result.next_steps == [], "nothing is offered until the run has finished"


# ── next steps ──────────────────────────────────────────────────────────────────────────────────
def _actions(result) -> list[str]:
    return [s.action for s in result.next_steps]


@pytest.mark.asyncio
async def test_entities_found_leads_with_asking_a_question(monkeypatch):
    _install(monkeypatch, _Run(), [_Node("Vendor", "Acme"), _Node("Chunk")])
    result = await mod.ingest_outcome(ORG, uuid.uuid4())
    assert _actions(result)[0] == "ask"
    assert "Acme" in result.next_steps[0].payload["suggested"]


@pytest.mark.asyncio
async def test_chunks_but_no_entities_leads_with_fixing_the_reading(monkeypatch):
    """The case worth catching: the ingest "worked" and found nothing, which is almost always a
    field being kept as a label when it is really the text."""
    _install(monkeypatch, _Run(), [_Node("Chunk") for _ in range(128)])
    result = await mod.ingest_outcome(ORG, uuid.uuid4())
    assert _actions(result)[0] == "refine_mapping"
    assert "128" in result.next_steps[0].reason


@pytest.mark.asyncio
async def test_a_completely_empty_run_still_offers_the_way_out(monkeypatch):
    _install(monkeypatch, _Run(), [])
    result = await mod.ingest_outcome(ORG, uuid.uuid4())
    assert _actions(result)[0] == "refine_mapping"


@pytest.mark.asyncio
async def test_a_failed_run_offers_only_a_retry_carrying_the_reason(monkeypatch):
    _install(monkeypatch, _Run(status="failed", error="decode failed at line 88214"), [])
    result = await mod.ingest_outcome(ORG, uuid.uuid4())
    assert _actions(result) == ["retry"]
    assert "88214" in result.next_steps[0].reason


@pytest.mark.asyncio
async def test_a_duplicate_points_at_what_is_already_there(monkeypatch):
    _install(monkeypatch, _Run(status="duplicate", source_ref="src-9"), [])
    result = await mod.ingest_outcome(ORG, uuid.uuid4())
    # open what is there, or replace it — for the copy whose knowledge went missing
    assert _actions(result) == ["open_asset", "replace"]
    assert result.next_steps[0].href.endswith("src-9")


@pytest.mark.asyncio
async def test_every_next_step_can_actually_be_taken(monkeypatch):
    """A step with no href and no payload is a sentence pretending to be a button."""
    for nodes in ([_Node("Vendor", "Acme")], [_Node("Chunk")], []):
        _install(monkeypatch, _Run(), nodes)
        result = await mod.ingest_outcome(ORG, uuid.uuid4())
        for step in result.next_steps:
            assert step.action and step.label and step.reason
            assert step.href or step.payload, f"{step.action} goes nowhere"


@pytest.mark.asyncio
async def test_the_outcome_serialises_whole(monkeypatch):
    _install(monkeypatch, _Run(), [_Node("Vendor", "Acme"), _Node("Chunk")])
    payload = (await mod.ingest_outcome(ORG, uuid.uuid4())).to_dict()
    assert set(payload) >= {"run_id", "status", "found", "structural", "next_steps", "reported"}
    assert payload["found"][0]["type"] == "Vendor"
    assert payload["next_steps"][0]["action"] == "ask"


# ── Extract knowledge: records published after the run ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_records_published_from_the_file_are_what_it_found(monkeypatch):
    """Extract knowledge puts a file's records in the graph after detection; they point at the file
    (FROM_ASSET) rather than sitting in it, and are what the upload found."""
    projects = [_Node("Project", f"P{i}") for i in range(3)]
    for n in projects:
        n.id = uuid.uuid4()
    _install(monkeypatch, _Run(), [_Node("Source", "atlas.json")], published=projects)
    result = await mod.ingest_outcome(ORG, uuid.uuid4())
    assert [(t.type, t.count) for t in result.found] == [("Project", 3)]
    assert not result.pending


@pytest.mark.asyncio
async def test_an_upload_waiting_for_detection_is_pending_not_empty(monkeypatch):
    """Finished reading, records not in the graph yet: "reading what it holds", never "nothing
    came out"."""
    _install(monkeypatch, _Run(), [_Node("Source", "atlas.json")], pending=True)
    result = await mod.ingest_outcome(ORG, uuid.uuid4())
    assert result.pending and any("detection" in n for n in result.notes)
