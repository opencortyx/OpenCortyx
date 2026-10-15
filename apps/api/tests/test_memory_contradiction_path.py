"""The memory WRITE PATH runs the contradiction engine.

tests/test_contradiction_engine.py pins the engine's logic with fakes and no database. This file pins
the one thing fakes cannot: that `memory.remember()` actually reaches the engine against the real graph
and vector stores, and that the engine can find what the new statement contradicts.

It exists because of a real defect. `authoring.create_node` defaults to `embed="async"` — the node
commits immediately and a background worker writes its vector a moment later. The engine finds
candidates by nearest-vector, so the FIRST of two statements written back to back could still be
absent from the index when the second one was checked against it, and both contradictory memories
survived at full confidence, silently. The statement writes in memory.py therefore pass
`embed="sync"`, which is create_node's documented lever for "the caller needs the node searchable
immediately".

Read the failure mode honestly: it is a RACE, not a deterministic break. Measured on this machine,
5 runs per mode — `embed="sync"`: 5/5 runs green (15/15 tests). `embed="async"`: 3 of the 5 runs had a
failure (12/15). So these tests catch the regression most of the time but not every time, and a single
green run under the async default proves nothing. An attempt to pin it deterministically by asserting
the vector exists right after `remember()` was tried and DELETED: it passed with the bug present 3/3,
because checking from the test gives the background worker more slack than the real in-`remember()`
call does. A test that passes with the bug is worse than no test.

Nothing here asserts the judge is right — the model's answer is stubbed, deliberately, so a failure
means the WIRING broke. Skips when the stores aren't reachable.
"""
import uuid

import pytest

from opencortyx.core.substrate import NodeRepo


# The judge's observation, fixed. The engine turns this into a verdict; the model never runs.
SAYS_DIFFERENT = {"a_subject": "deployment", "a_attribute": "region",
                  "b_subject": "deployment", "b_attribute": "region", "values_differ": True}


@pytest.fixture
def stub_judge():
    """Patch only the model call. Everything else — embeddings, vector search, edge writes — is real."""
    from opencortyx.capabilities.modules import contradiction as engine
    real = engine.GraphDeps.llm_json

    async def _stub(self, workspace_id, prompt, system, cfg):
        return SAYS_DIFFERENT

    engine.GraphDeps.llm_json = _stub
    try:
        yield engine
    finally:
        engine.GraphDeps.llm_json = real


async def _edges_between(org, a, b):
    rows = await NodeRepo().edges(org, node_ids=[uuid.UUID(str(a))])
    ids = {str(a), str(b)}
    return sorted({e.type for e in rows if {str(e.source_id), str(e.target_id)} == ids})


async def _props(org, node_id):
    n = await NodeRepo().get(org, uuid.UUID(str(node_id)))
    return dict(n.properties or {}) if n is not None else {}


async def test_remember_runs_the_engine_and_closes_the_old_window(live_org, stub_judge):
    """Two contradictory memories written back to back: the pair is recorded and the older one's
    validity window is closed. A superseded memory is never deleted — that is what keeps
    'what did we believe in March?' answerable."""
    org = live_org
    from opencortyx.knowledge import memory

    first = await memory.remember(org, text="The deployment region is eu-west-1.", kind="fact")
    second = await memory.remember(org, text="The deployment region is ap-south-1.", kind="fact")

    types = await _edges_between(org, first["id"], second["id"])
    assert "CONTRADICTS" in types, (
        f"the write path did not reach the engine (edges: {types}). If this regressed, check that the "
        f"statement writes in memory.py still pass embed='sync' — with the async default the first "
        f"statement has no vector yet and the second finds nothing to compare against.")

    older = await _props(org, first["id"])
    assert older.get("valid_until"), "the superseded memory's validity window was not closed"
    assert older.get("resolution_about"), "what the pair disagreed about was not recorded"


async def test_rechecking_the_same_statement_writes_no_duplicate_edges(live_org, stub_judge):
    """Idempotency on the real path: post-ingest sweeps and the write hook both call the engine, so a
    statement gets checked more than once. That must not stack edges."""
    org = live_org
    from opencortyx.knowledge import memory

    first = await memory.remember(org, text="The billing currency is EUR.", kind="fact")
    second = await memory.remember(org, text="The billing currency is USD.", kind="fact")
    before = await _edges_between(org, first["id"], second["id"])
    assert before, "nothing was written on the first pass — the rest of this test proves nothing"

    await stub_judge.resolve_statement(org, second["id"])
    after = await _edges_between(org, first["id"], second["id"])
    assert after == before, f"re-running the check changed the edges: {before} -> {after}"


async def test_an_unrelated_memory_is_left_alone(live_org, stub_judge):
    """The negative case, and the one that matters most in production: most memories contradict
    nothing. A statement about a different subject must keep its confidence and its open window even
    though the judge here is hardwired to say 'different values'. The engine's own subject/attribute
    check is what must reject this pair — not the model."""
    org = live_org
    from opencortyx.knowledge import memory

    first = await memory.remember(org, text="The deployment region is eu-west-1.", kind="fact")
    other = await memory.remember(org, text="Priya prefers dark mode in the editor.", kind="preference")

    types = await _edges_between(org, first["id"], other["id"])
    assert "CONTRADICTS" not in types, f"unrelated memories were linked as contradictory: {types}"
    props = await _props(org, first["id"])
    assert not props.get("valid_until"), "an unrelated memory had its validity window closed"
    assert props.get("superseded") in (None, "", False), "an unrelated memory was marked superseded"


async def _mk(org, text, type="Fact", confidence=1.0, **props):
    from opencortyx.knowledge import authoring
    return await authoring.create_node(org, type=type, label=text[:100], confidence=confidence,
                                       properties={"text": text, **props}, embed="sync")


async def _link(org, a, b, type="RELATES_TO"):
    from opencortyx.knowledge import authoring
    await authoring.create_edge(org, source_id=a, target_id=b, type=type)


async def test_neighbors_both_walks_outgoing_edges_too(live_org):
    """`direction="both"` had no branch of its own: the assignment was a binary choice, so anything
    that was not "out" silently became "in". A statement pointing AT its entity therefore had every
    one of its edges missed, and the structural candidate source returned nothing at all."""
    org = live_org
    holder = await _mk(org, "Acme Ltd", type="Entity")
    stmt = await _mk(org, "Support response time", sla="4 hours")
    await _link(org, stmt, holder)                         # the statement is the SOURCE

    both = await NodeRepo().neighbors(org, [stmt], direction="both", limit=20)
    assert holder in [n.id for _e, n in both], "an outgoing edge was invisible to direction='both'"


async def test_the_rule_tier_settles_a_conflict_on_the_real_graph_with_no_model_call(live_org):
    """The end the whole rule tier exists for: two statements on one entity, decided by arithmetic."""
    org = live_org
    from opencortyx.capabilities.modules import contradiction as engine

    calls = {"n": 0}
    real = engine.GraphDeps.llm_json

    async def _counting(self, workspace_id, prompt, system, cfg):
        calls["n"] += 1
        return {"a_subject": "x", "a_attribute": "y", "b_subject": "x", "b_attribute": "y",
                "values_differ": False}

    engine.GraphDeps.llm_json = _counting
    try:
        holder = await _mk(org, "Acme Ltd", type="Entity")
        a = await _mk(org, "Support response time", sla="4 hours")
        b = await _mk(org, "Support response time commitment", sla="8 hours")
        await _link(org, a, holder)
        await _link(org, b, holder)
        r = await engine.resolve_statement(org, b)
    finally:
        engine.GraphDeps.llm_json = real

    assert {v["method"] for v in r["verdicts"]} == {"rule"}, str(r["verdicts"])
    assert calls["n"] == 0, "the rule tier must not spend a judge call"
    assert any(o["kind"] in ("supersede", "evolve", "propose") for o in r["outcomes"]), str(r)


async def test_platform_scaffolding_never_makes_two_records_the_same_subject(live_org):
    """THE false positive, and the reason the rule tier is safe to trust structure.

    Every node is auto-linked to a shared Date hub, so "attached to the same thing" without a filter
    makes every statement written on the same DAY a sibling of every other. Measured before the fix:
    two unrelated customers' records became structural candidates through that Date, and the rule
    tier called their differing values a contradiction. A holder the PLATFORM created
    (`__source: "system"`) is not a subject anyone asserted anything about."""
    org = live_org
    from opencortyx.capabilities.modules import contradiction as engine

    acme = await _mk(org, "Acme Ltd", type="Entity")
    globex = await _mk(org, "Globex Inc", type="Entity")
    a = await _mk(org, "Support response time", sla="4 hours")
    b = await _mk(org, "Support response time commitment", sla="12 hours")
    await _link(org, a, acme)
    await _link(org, b, globex)                            # DIFFERENT subjects, same shared Date

    r = await engine.resolve_statement(org, b)
    by_rule = [v for v in r["verdicts"]
               if v["method"] == "rule" and v["relation"] == "contradiction"]
    assert not by_rule, f"two different subjects were called a contradiction by rule: {r['verdicts']}"


async def test_agreement_raises_belief_once_and_only_once(live_org):
    """LAYER 7 end to end: bounded, counted, and idempotent across re-runs."""
    org = live_org
    from opencortyx.capabilities.modules import contradiction as engine

    real = engine.GraphDeps.llm_json

    async def _says_same(self, workspace_id, prompt, system, cfg):
        return {"a_subject": "retention", "a_attribute": "window",
                "b_subject": "retention", "b_attribute": "window", "values_differ": False}

    engine.GraphDeps.llm_json = _says_same
    try:
        holder = await _mk(org, "Retention policy", type="Entity")
        a = await _mk(org, "The retention window is 90 days.", confidence=0.5)
        b = await _mk(org, "Data is retained for ninety days.", confidence=0.5)
        await _link(org, a, holder)
        await _link(org, b, holder)

        await engine.resolve_statement(org, b)
        raised = float((await NodeRepo().get(org, b)).confidence)
        assert raised > 0.5, "corroboration did not raise belief"

        await engine.resolve_statement(org, b)             # a sweep, the peer's write, an edit...
        await engine.resolve_statement(org, a)
        again = float((await NodeRepo().get(org, b)).confidence)
        assert again == raised, f"belief compounded on a re-run: {raised} -> {again}"
    finally:
        engine.GraphDeps.llm_json = real


async def test_salience_is_recorded_and_never_hides_anything(live_org):
    """Belief and salience are separate signals. Usage must only ever RANK — an unused statement is
    still returned when it is the only answer there is."""
    org = live_org
    from opencortyx.core.substrate import NodeRepo

    used = await _mk(org, "The archive bucket is axiom-cold-eu.")
    never = await _mk(org, "The archive tier is glacier.")
    repo = NodeRepo()
    await repo.note_access(workspace_id=org, node_ids=[used])
    await repo.note_access(workspace_id=org, node_ids=[used])
    n = await repo.get(org, used)
    assert int((n.properties or {}).get("__access_count") or 0) == 2

    from opencortyx.core.inference.embedding import embed
    rows = await repo.vector_search(workspace_id=org, query_vec=await embed("The archive bucket is axiom-cold-eu."), k=10)
    assert never in [x.id for x, _ in rows], "a never-used statement was hidden — salience must rank"
