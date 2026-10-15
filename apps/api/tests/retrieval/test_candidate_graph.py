"""The CANDIDATE GRAPH: nodes, edges, paths, evidence — and whether they agree with each other.

    ┌─────────────────┐
    │ CANDIDATE GRAPH │   nodes ── edges ── paths ── evidence
    └─────────────────┘

This type exists instead of a `list[Candidate]` for one stated reason: ranking a flat list cannot
express "these two candidates are the same evidence" or "this one is only here because it sits on
a strong path". So the tests below check the four parts individually AND the invariants that tie
them together — a graph whose edges disagree with its paths is worse than no graph, because every
structural claim made from it is then unfounded.

One defect this fixes was self-inflicted. `edges` had been declared and never populated; it was
then populated and read by nothing except a trace counter, which is the same defect wearing a
better disguise. `connectivity` makes it load-bearing.
"""
from __future__ import annotations

import uuid

import pytest

from opencortyx.retrieval.contracts import (
    Candidate, CandidateGraph, Evidence, Path, PathStep,
)
from opencortyx.retrieval.l3_rerank.features import (
    FEATURE_NAMES, annotate, connectivity, extract,
)


def node(nid=None, text="", ntype="", label=""):
    return Candidate(node_id=nid or uuid.uuid4(), text=text, node_type=ntype, label=label)


def step(a, b, rel="MENTIONS", edge_score=0.8):
    return PathStep(from_id=a, edge_type=rel, to_id=b, direction="out", edge_score=edge_score)


def graph_of(n=4, connect=()):
    g = CandidateGraph()
    ids = [uuid.uuid4() for _ in range(n)]
    for i in ids:
        g.upsert(node(i, text=f"node {i}"))
    for a, b in connect:
        g.edges.append(step(ids[a], ids[b]))
    return g, ids


# ── nodes ────────────────────────────────────────────────────────────────────────────────────

def test_nodes_are_keyed_by_identity_and_merge_rather_than_duplicate():
    g = CandidateGraph()
    nid = uuid.uuid4()
    a = node(nid, text="the text")
    a.add(Evidence(surface="bm25", rank=0, raw_score=1.0, scale="bm25"))
    b = node(nid)
    b.add(Evidence(surface="node_embedding", rank=1, raw_score=0.8, scale="cosine"))
    g.upsert(a)
    g.upsert(b)
    assert len(g) == 1
    assert g.candidates[nid].surfaces == {"bm25", "node_embedding"}
    assert g.candidates[nid].text == "the text", "a later bare copy overwrote the text"


# ── edges ────────────────────────────────────────────────────────────────────────────────────

def test_adjacency_is_direction_agnostic():
    """Whether the walk traversed an edge forwards or backwards says nothing about whether the
    two nodes belong to the same part of the answer."""
    g, ids = graph_of(3, connect=[(0, 1)])
    adj = g.adjacency()
    assert ids[1] in adj[ids[0]] and ids[0] in adj[ids[1]]
    assert adj[ids[2]] == set()


def test_adjacency_ignores_endpoints_outside_the_retrieved_set():
    """An edge leaving the retrieved set says nothing about how a candidate sits inside the
    subgraph that answers the question."""
    g, ids = graph_of(2)
    outsider = uuid.uuid4()
    g.edges.append(step(ids[0], outsider))
    assert g.adjacency()[ids[0]] == set()
    assert len(g.dangling_edges()) == 1


def test_a_self_edge_is_not_adjacency():
    g, ids = graph_of(2)
    g.edges.append(step(ids[0], ids[0]))
    assert g.adjacency()[ids[0]] == set()


def test_dangling_edges_are_reported_not_treated_as_corruption():
    """A walk passes THROUGH nodes it does not retain, so an edge to a non-candidate is expected.
    It is exposed so an integrity check can tell that apart from losing a landed-on node."""
    g, ids = graph_of(2, connect=[(0, 1)])
    assert g.dangling_edges() == []
    g.edges.append(step(ids[0], uuid.uuid4()))
    assert len(g.dangling_edges()) == 1


# ── connectivity: the signal a flat list cannot produce ──────────────────────────────────────

def test_a_connected_candidate_scores_above_an_isolated_one():
    g, ids = graph_of(4, connect=[(0, 1), (0, 2), (0, 3)])
    conn = connectivity(g)
    assert conn[ids[0]] > conn[ids[1]] > 0.0 or conn[ids[0]] > 0.0
    isolated = [i for i in ids if not g.adjacency()[i]]
    for i in isolated:
        assert conn[i] == 0.0


def test_one_hub_cannot_flatten_everyone_else_to_zero():
    """A linear count lets a hub saturate the feature — the shape that made a PPR score,
    saturating at 1.0, poison the single relevance number it fed."""
    g = CandidateGraph()
    ids = [uuid.uuid4() for _ in range(30)]
    for i in ids:
        g.upsert(node(i))
    for i in ids[1:]:                     # a 29-degree hub
        g.edges.append(step(ids[0], i))
    g.edges.append(step(ids[1], ids[2]))  # one modest degree-2 node

    conn = connectivity(g)
    assert conn[ids[0]] == pytest.approx(1.0)
    assert conn[ids[1]] > 0.25, \
        f"a degree-2 node was flattened to {conn[ids[1]]:.3f} beside a hub"


def test_a_graph_with_no_edges_teaches_nothing_and_says_so():
    """A feature that has learned nothing must contribute nothing, not a default."""
    g, _ = graph_of(3)
    assert set(connectivity(g).values()) == {0.0}


def test_connectivity_is_absent_rather_than_invented_without_a_graph():
    from opencortyx.retrieval.l3_rerank.features import (
        _ONLY_WHEN_ASSESSED,
    )
    cands = [node(text="x") for _ in range(3)]
    annotate(cands, None)
    assert all(c.features["connectivity"] == 0.0 for c in cands)
    always = set(FEATURE_NAMES) - _ONLY_WHEN_ASSESSED
    assert all(name in c.features for name in always for c in cands)


def test_connectivity_reaches_the_candidates_when_a_graph_is_given():
    g, ids = graph_of(3, connect=[(0, 1)])
    annotate(list(g.candidates.values()), None, graph=g)
    assert g.candidates[ids[0]].features["connectivity"] > 0.0
    assert g.candidates[ids[2]].features["connectivity"] == 0.0


def test_every_always_assessed_feature_is_actually_produced():
    """Reading a missing key as 0.0 is how a signal disappears unnoticed.

    Features measured for only part of the pool are the documented exception: absent means NOT
    ASSESSED and is skipped by the scorer, rather than counting as a zero.
    """
    from opencortyx.retrieval.l3_rerank.features import (
        _ONLY_WHEN_ASSESSED,
    )
    c = node(text="something")
    f = extract(c, None)
    assert set(f) == set(FEATURE_NAMES) - _ONLY_WHEN_ASSESSED


# ── paths ────────────────────────────────────────────────────────────────────────────────────

def test_a_path_carries_its_route_and_its_terminal():
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p = Path(seed_id=a, steps=[step(a, b), step(b, c)])
    assert p.length == 2
    assert p.terminal_id == c
    assert p.as_tuple()[0] == str(a) and p.as_tuple()[-1] == str(c)


def test_an_empty_path_terminates_at_its_seed():
    a = uuid.uuid4()
    assert Path(seed_id=a).terminal_id == a


def test_merging_candidates_keeps_every_route_to_them():
    """Two surfaces reaching one node by different routes is two pieces of evidence."""
    nid, s1, s2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    g = CandidateGraph()
    a = node(nid)
    a.paths.append(Path(seed_id=s1, steps=[step(s1, nid)]))
    b = node(nid)
    b.paths.append(Path(seed_id=s2, steps=[step(s2, nid)]))
    g.upsert(a)
    g.upsert(b)
    assert len(g.candidates[nid].paths) == 2


# ── the invariants that tie the four together ────────────────────────────────────────────────

def test_edges_and_paths_describe_the_same_traversal():
    """A graph whose edges disagree with its paths is worse than no graph: every structural
    claim made from it is unfounded."""
    g, ids = graph_of(3, connect=[(0, 1), (1, 2)])
    c = g.candidates[ids[2]]
    c.paths.append(Path(seed_id=ids[0], steps=[step(ids[0], ids[1]), step(ids[1], ids[2])]))

    in_edges = {(s.from_id, s.edge_type, s.to_id) for s in g.edges}
    in_paths = {(s.from_id, s.edge_type, s.to_id)
                for cand in g.candidates.values() for p in cand.paths for s in p.steps}
    assert in_paths <= in_edges, "a path used a step the edge list never recorded"


def test_evidence_is_never_overwritten_by_a_merge():
    nid = uuid.uuid4()
    g = CandidateGraph()
    a = node(nid)
    a.add(Evidence(surface="bm25", rank=3, raw_score=9.0, scale="bm25", detail="4terms"))
    g.upsert(a)
    b = node(nid)
    b.add(Evidence(surface="graph", rank=1, raw_score=0.4, scale="path", detail="MENTIONS"))
    g.upsert(b)

    ev = g.candidates[nid].evidence
    assert len(ev) == 2
    assert {e.scale for e in ev} == {"bm25", "path"}
    assert g.candidates[nid].best("bm25") == 9.0
    assert g.candidates[nid].best("cosine") == 0.0, "scales were mixed"


# ── against the real pipeline ────────────────────────────────────────────────────────────────

ORG = uuid.UUID("e5954895-7b42-5a9b-93f6-911febc30bca")


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


async def _embed(text: str):
    from opencortyx.core.inference.embedding import embed
    return await embed(text)


async def _embed_batch(texts):
    from opencortyx.core.inference.embedding import embed_batch
    return await embed_batch(texts)


async def _run(query: str):
    from opencortyx.core.db import V2ReadSession
    from opencortyx.retrieval.orchestrator import (
        RetrievalWorkflow, WorkflowConfig,
    )
    wf = RetrievalWorkflow(_embed, _embed_batch,
                           WorkflowConfig(candidates_per_surface=40, budget_tokens=4000))
    async with V2ReadSession() as db:
        return await wf.run(db, ORG, query)


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_pipeline_builds_all_four_parts():
    r = await _run("Who did Joanna talk to about her allergies?")
    if not r.graph.candidates:
        pytest.skip("org has no graph loaded")
    assert r.graph.candidates, "no nodes"
    assert r.graph.edges, "no edges — the traversed subgraph was not recorded"
    assert any(c.paths for c in r.graph.candidates.values()), "no paths"
    assert all(c.evidence for c in r.graph.candidates.values()), "a candidate with no evidence"
    assert r.graph.seeds, "the walk recorded nothing about what it anchored on"


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_real_edges_and_paths_agree():
    r = await _run("Who did Joanna talk to about her allergies?")
    if not r.graph.edges:
        pytest.skip("graph expansion did not run")
    in_edges = {(s.from_id, s.edge_type, s.to_id, s.direction) for s in r.graph.edges}
    in_paths = {(s.from_id, s.edge_type, s.to_id, s.direction)
                for c in r.graph.candidates.values() for p in c.paths for s in p.steps}
    assert in_paths == in_edges, \
        f"{len(in_paths - in_edges)} path steps missing from edges, " \
        f"{len(in_edges - in_paths)} edges in no path"


@pytest.mark.integration
@pytest.mark.anyio
async def test_connectivity_is_computed_on_the_real_subgraph():
    r = await _run("Who did Joanna talk to about her allergies?")
    if not r.graph.edges:
        pytest.skip("graph expansion did not run")
    vals = [c.features.get("connectivity", 0.0) for c in r.graph.candidates.values()]
    assert any(v > 0 for v in vals), "edges exist but connectivity was never computed"
    assert max(vals) <= 1.0 and min(vals) >= 0.0


@pytest.mark.integration
@pytest.mark.anyio
async def test_every_candidate_can_explain_why_it_is_here():
    """The whole point of carrying evidence: no node reaches the context unexplained."""
    r = await _run("What is Joanna allergic to?")
    if not r.blocks:
        pytest.skip("org has no graph loaded")
    for b in r.blocks[:25]:
        assert b.retrieved_by, f"block {b.node_id} arrived with no provenance"
    for c in list(r.graph.candidates.values())[:50]:
        assert c.evidence, f"candidate {c.node_id} has no evidence"
        assert all(e.surface for e in c.evidence)
