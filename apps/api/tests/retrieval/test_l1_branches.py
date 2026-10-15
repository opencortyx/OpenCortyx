"""The three retrieval branches, individually and then together.

                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
              ENTITY SEARCH      CONTENT SEARCH      GRAPH SEARCH
              exact/fuzzy        bm25/keyword        seed nodes
              aliases            dense/ANN           edge search
              metadata           ontology            walkers/beam
                                 any attribute       path search

Each branch is tested on its own because each fails differently and a fused result hides which
one was responsible. A candidate that reaches the context can arrive by three independent routes,
and "the pipeline returned something" is not evidence that all three work — it is compatible with
two of them being silently dead, which is exactly the state entity search and the ontology surface
were found in.

The pipeline tests at the bottom then check the property that only holds jointly: that the
branches contribute DIFFERENT candidates, and that a node found by several is visibly found by
several.
"""
from __future__ import annotations

import uuid

import pytest

from opencortyx.retrieval.l0_understanding.planner import build_plan
from opencortyx.retrieval.l0_understanding.vocabulary import (
    load_vocabulary,
)
from opencortyx.retrieval.l1_candidates import fusion
from opencortyx.retrieval.l1_candidates.embedding import EmbeddingRetrieval
from opencortyx.retrieval.l1_candidates.entity_resolver import (
    EntityResolver,
)
from opencortyx.retrieval.l1_candidates.lexical import (
    BM25Surface, FuzzySurface, IdentifierSurface, OntologySurface,
)
from opencortyx.core.substrate import NodeRepo
from opencortyx.retrieval.l2_graph.beam import BeamConfig, BeamSearch

pytestmark = pytest.mark.integration

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


# ── branch 1: ENTITY SEARCH ──────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_entity_branch_binds_a_name_and_refuses_a_non_name():
    """exact / fuzzy / alias / containment, on the real graph.

    Covered in depth in `test_l1_entity_resolution.py`; repeated here as the branch's own
    end-to-end check so a failure localises to this branch rather than to the pipeline.
    """
    r = EntityResolver()
    from opencortyx.retrieval.contracts import EntityMention
    hit = await r.resolve(ORG, EntityMention(text="Joanna"))
    miss = await r.resolve(ORG, EntityMention(text="Zzzzznotathing"))
    if hit.confidence == 0.0:
        pytest.skip("org has no graph loaded")
    assert hit.resolved and "exact" in hit.matched_by
    assert not miss.resolved
    cands = EntityResolver.to_candidates([hit, miss])
    assert len(cands) == 1 and cands[0].evidence[0].surface == "entity"


# ── branch 2: CONTENT SEARCH ─────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_content_branch_bm25_returns_ranked_lexical_hits():
    out = await BM25Surface().retrieve(ORG, "allergic to reptiles and animals with fur",
                                       k=20)
    if not out:
        pytest.skip("org has no graph loaded")
    assert all(c.evidence[0].scale == "bm25" for c in out)
    ranks = [c.evidence[0].rank for c in out]
    assert ranks == sorted(ranks), "bm25 results were not rank-ordered"


@pytest.mark.anyio
async def test_content_branch_identifier_is_literal_not_stemmed():
    out = await IdentifierSurface().retrieve(ORG, "record REC-890998 in DOC-00000731",
                                             k=10)
    assert isinstance(out, list)          # no identifiers in this corpus: empty, never an error


@pytest.mark.anyio
async def test_content_branch_fuzzy_matches_a_varied_spelling():
    out = await FuzzySurface().retrieve(ORG, ["Joana"], k=10)
    if not out:
        pytest.skip("trigram unavailable or org empty")
    assert any("jo" in (c.label or "").lower() for c in out)


@pytest.mark.anyio
async def test_content_branch_ontology_searches_within_the_implicated_types():
    """`ontology` was a registered surface with no retriever — weightable, never contributing.

    Its value is type-local ranking: this corpus is 300,070 `KeyNode` against 1,225 `Fact`, so a
    question about facts competes with 245x more of something else under global ranking.
    """
    vocab = await load_vocabulary(ORG)
    if not vocab:
        pytest.skip("org has no graph loaded")
    plan = build_plan("which facts mention allergies?", vocab)
    out = await OntologySurface().retrieve(ORG, plan.normalized_query,
                                           node_types=plan.node_types, k=30)
    assert plan.node_types, "expected the question to implicate a type"
    for c in out:
        assert c.node_type in plan.node_types, \
            f"ontology returned {c.node_type}, outside the implicated types"
    assert all(c.evidence[0].surface == "ontology" for c in out)


@pytest.mark.anyio
async def test_the_ontology_surface_stays_silent_without_a_hypothesis():
    """No type hypothesis means nothing to search within — and must mean nothing returned,
    never a fallback to some default type."""
    out = await OntologySurface().retrieve(ORG, "anything at all", node_types={}, k=10)
    assert out == []


# ── branch 2b: EMBEDDING RETRIEVAL (node ANN + attribute ANN) ────────────────────────────────

@pytest.mark.anyio
async def test_any_attribute_is_searchable_not_an_allow_listed_few():
    """The claim the attribute surface exists to make.

    The previous engine embedded `label` concatenated with a fixed key list, so every field
    outside it was unsearchable by any retriever — `date_time` sat on all 5,882 turns and was
    reachable by nothing. Here the indexed attributes are whatever the nodes actually carry, so
    the structured fields of a `Fact` (`subject`, `predicate`, `object`) are each their own
    surface without anyone having declared them.
    """
    attrs = set(await EmbeddingRetrieval(_embed).load_specificity(ORG))
    if not attrs:
        pytest.skip("attribute surfaces not built — run index_cli")

    # Fields no allow-list would have contained, indexed because the data carries them.
    unanticipated = {"subject", "predicate", "object", "date_time"} & attrs
    assert unanticipated, f"only conventional fields are indexed: {sorted(attrs)}"


@pytest.mark.anyio
async def test_an_attribute_only_match_reaches_a_node_whose_prose_never_says_it():
    """`date_time` is stored on every turn and appears in almost no turn's prose. If the
    attribute surface works, a question naming a month reaches those turns; if it does not, the
    timestamp is stored and unqueryable, which is the state that motivated this surface."""
    er = EmbeddingRetrieval(_embed)
    qv = await _embed("date_time: January 2022")
    hits = await er.attr_ann(ORG, qv, k=25, attrs=["date_time"])
    if not hits:
        pytest.skip("date_time surface not built for this org")
    assert all(e.detail == "date_time" for c in hits for e in c.evidence)


@pytest.mark.anyio
async def test_attribute_specificity_is_measured_not_declared():
    """`speaker` holds two values across 629 turns and matched every turn one person spoke.
    Its weight has to come from the data, not from a judgement about the field's name."""
    er = EmbeddingRetrieval(_embed)
    spec = await er.load_specificity(ORG)
    if not spec:
        pytest.skip("attribute surfaces not built")
    assert all(0.0 < v <= 1.0 for v in spec.values())
    if "speaker" in spec and "text" in spec:
        assert spec["speaker"] < spec["text"], \
            "a two-valued field was rated as discriminating as free text"


# ── branch 3: GRAPH SEARCH ───────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_graph_branch_scores_relations_against_the_query_then_walks():
    from opencortyx.retrieval.l2_graph.edge_scoring import (
        EdgeScorer, profile_relations_for,
    )

    seeds = [str(t.id) for t in await NodeRepo().find(ORG, exact_types=["Turn"], limit=8)]
    if not seeds:
        pytest.skip("org has no graph loaded")

    profiles = await profile_relations_for(ORG, seeds)
    qv = await _embed("what is someone allergic to")
    rel = await EdgeScorer(_embed_batch).score_relations(ORG, qv, profiles=profiles)
    assert rel.scores, "no relation was scored for a seed set that has edges"

    edges: list = []
    found = await BeamSearch(BeamConfig(max_hops=2)).run(
        ORG, [uuid.UUID(s) for s in seeds], rel, out_edges=edges)

    assert found, "beam search discovered nothing from seeds that have neighbours"
    assert all(c.paths for c in found), "a discovered node carried no path"
    assert edges, "the traversed subgraph was not recorded"


@pytest.mark.anyio
async def test_the_traversed_subgraph_is_deduplicated_and_matches_the_paths():
    """`CandidateGraph.edges` was declared and never populated: the steps existed only inside
    individual paths, so nothing held the retrieved subgraph AS a subgraph."""
    from opencortyx.retrieval.l2_graph.edge_scoring import (
        EdgeScorer, profile_relations_for,
    )

    seeds = [str(t.id) for t in await NodeRepo().find(ORG, exact_types=["Turn"], limit=6)]
    if not seeds:
        pytest.skip("org has no graph loaded")
    profiles = await profile_relations_for(ORG, seeds)
    qv = await _embed("who said what to whom")
    rel = await EdgeScorer(_embed_batch).score_relations(ORG, qv, profiles=profiles)
    edges: list = []
    found = await BeamSearch(BeamConfig(max_hops=2)).run(
        ORG, [uuid.UUID(s) for s in seeds], rel, out_edges=edges)

    keys = [(str(e.from_id), e.edge_type, str(e.to_id), e.direction) for e in edges]
    assert len(keys) == len(set(keys)), "the same edge was listed once per route through it"

    in_paths = {(str(s.from_id), s.edge_type, str(s.to_id), s.direction)
                for c in found for p in c.paths for s in p.steps}
    assert set(keys) == in_paths, "the recorded subgraph disagrees with the paths that built it"


# ── all three together ───────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_the_branches_contribute_different_candidates():
    """If two branches only ever return the same nodes, one of them is paying latency for
    nothing — and `surface_report` is the measurement that decides it."""
    er = EmbeddingRetrieval(_embed)
    qv = await _embed("what is Joanna allergic to")
    lists = [
        await er.attr_ann(ORG, qv, k=40),
        await BM25Surface().retrieve(ORG, "Joanna allergic reptiles fur", k=40),
        await FuzzySurface().retrieve(ORG, ["Joanna"], k=20),
    ]
    if not any(lists):
        pytest.skip("org has no graph loaded")

    graph = fusion.merge(lists)
    report = fusion.surface_report(graph)
    assert len(report) >= 3, f"only {sorted(report)} contributed"
    contributing = [s for s, r in report.items() if r["unique"] > 0]
    assert len(contributing) >= 2, \
        f"only one branch found anything the others did not: {report}"


@pytest.mark.anyio
async def test_a_node_found_by_several_branches_says_so():
    """Evidence accumulates rather than overwrites — the property that lets the reranker prefer
    agreement across independent surfaces over one retriever's guess."""
    er = EmbeddingRetrieval(_embed)
    qv = await _embed("Joanna allergic reptiles")
    lists = [
        await er.attr_ann(ORG, qv, k=60),
        await BM25Surface().retrieve(ORG, "Joanna allergic reptiles", k=60),
    ]
    if not all(lists):
        pytest.skip("org has no graph loaded")
    graph = fusion.merge(lists)
    multi = [c for c in graph.candidates.values() if len(c.surfaces) > 1]
    assert multi, "no node was found by more than one branch on a question both should answer"
    c = multi[0]
    assert len(c.evidence) >= 2 and len({e.surface for e in c.evidence}) >= 2


@pytest.mark.anyio
async def test_the_full_pipeline_uses_more_than_one_branch():
    """The joint property: a real question through the orchestrator draws on several branches,
    and the trace names which."""
    from opencortyx.retrieval.orchestrator import (
        RetrievalWorkflow, WorkflowConfig,
    )
    wf = RetrievalWorkflow(_embed, _embed_batch,
                           WorkflowConfig(candidates_per_surface=40, budget_tokens=4000))
    result = await wf.run(ORG, "What is Joanna allergic to?")

    if not result.blocks:
        pytest.skip("org has no graph loaded")
    surfaces = {e.surface for c in result.graph.candidates.values() for e in c.evidence}
    assert len(surfaces) >= 3, f"the pipeline drew on only {sorted(surfaces)}"
    # Every branch that contributed is named on the blocks, so an answer can be defended.
    assert any(b.retrieved_by for b in result.blocks)


@pytest.mark.anyio
async def test_no_two_pieces_of_evidence_are_indistinguishable():
    """Two entries identical in surface, rank AND detail cannot be audited apart.

    Measured on this query before the fix: 18 such entries, because the embedding surface runs
    once per query representation and `attr_ann` did not record which one fired.
    """
    from collections import Counter
    from opencortyx.retrieval.orchestrator import (
        RetrievalWorkflow, WorkflowConfig,
    )
    wf = RetrievalWorkflow(_embed, _embed_batch,
                           WorkflowConfig(candidates_per_surface=40, budget_tokens=4000))
    r = await wf.run(ORG, "What is Joanna allergic to?")
    if not r.graph.candidates:
        pytest.skip("org has no graph loaded")

    dupes = 0
    for cand in r.graph.candidates.values():
        counts = Counter((e.surface, e.rank, e.detail) for e in cand.evidence)
        dupes += sum(v - 1 for v in counts.values() if v > 1)
    assert dupes == 0, f"{dupes} indistinguishable evidence entries survived fusion"


@pytest.mark.anyio
async def test_fusion_keeps_everything_and_leaves_truncation_to_an_explicit_decision():
    """A previous build cut the pool by RRF and then ordered downstream by cosine, discarding
    exactly the candidates the two disagreed about — and they disagree by construction."""
    er = EmbeddingRetrieval(_embed)
    qv = await _embed("Joanna allergic reptiles")
    lists = [await er.attr_ann(ORG, qv, k=40),
             await BM25Surface().retrieve(ORG, "Joanna allergic reptiles", k=40)]
    if not any(lists):
        pytest.skip("org has no graph loaded")
    graph = fusion.merge(lists)
    fusion.rrf(graph)
    distinct_in = len({c.node_id for lst in lists for c in lst})
    assert len(graph) == distinct_in, "fusion dropped candidates"
    assert len(fusion.cut(graph, 0)) == len(graph)
    assert len(fusion.cut(graph, 5)) == 5


@pytest.mark.anyio
async def test_a_node_agreed_on_by_several_surfaces_outranks_one_surface_depth():
    """The reason to rank-fuse at all, checked on real distributions rather than a fixture."""
    er = EmbeddingRetrieval(_embed)
    qv = await _embed("Joanna allergic reptiles")
    lists = [await er.attr_ann(ORG, qv, k=60),
             await BM25Surface().retrieve(ORG, "Joanna allergic reptiles", k=60)]
    if not all(lists):
        pytest.skip("org has no graph loaded")
    graph = fusion.merge(lists)
    fusion.rrf(graph)
    ranked = graph.ranked()
    multi = [c for c in ranked if len(c.surfaces) >= 2]
    single = [c for c in ranked if len(c.surfaces) == 1]
    assert multi and single
    assert ranked.index(multi[0]) < ranked.index(single[0]) or \
        multi[0].fusion_score >= single[0].fusion_score


@pytest.mark.anyio
async def test_what_the_policy_said_would_run_is_what_contributed():
    """The joint L0→L1 property.

    Every surface named in `surface_report` must be one the policy allowed. A surface
    contributing without permission means execution is not actually governed by the plan — which
    was the state before `ExecutionPolicy` existed, when every surface ran on every question.
    The converse is allowed: a permitted surface may legitimately find nothing.
    """
    from opencortyx.retrieval.orchestrator import (
        RetrievalWorkflow, WorkflowConfig,
    )
    wf = RetrievalWorkflow(_embed, _embed_batch,
                           WorkflowConfig(candidates_per_surface=30, budget_tokens=3000))
    r = await wf.run(ORG, "which facts mention allergies?")
    if not r.graph.candidates:
        pytest.skip("org has no graph loaded")

    contributed = {e.surface for c in r.graph.candidates.values() for e in c.evidence}
    allowed = set(r.plan.policy.run) | {"graph"}      # graph is L2's, gated separately
    rogue = contributed - allowed
    assert not rogue, f"surfaces contributed without being planned: {sorted(rogue)}"


@pytest.mark.anyio
async def test_every_context_block_names_each_surface_once():
    """Provenance is grouped by surface: one surface matching three fields is one confirmation,
    not three. Rendering it as three made a single surface read as three-way agreement."""
    from opencortyx.retrieval.orchestrator import (
        RetrievalWorkflow, WorkflowConfig,
    )
    wf = RetrievalWorkflow(_embed, _embed_batch,
                           WorkflowConfig(candidates_per_surface=30, budget_tokens=3000))
    r = await wf.run(ORG, "What is Joanna allergic to?")
    if not r.blocks:
        pytest.skip("org has no graph loaded")
    for b in r.blocks[:20]:
        surfaces = [tag.split("(")[0] for tag in b.retrieved_by]
        assert len(surfaces) == len(set(surfaces)), \
            f"a surface was listed more than once: {b.retrieved_by}"
