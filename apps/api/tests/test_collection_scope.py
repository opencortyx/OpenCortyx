"""A question scoped to collections reads those collections only - and the relevance judge reads the
whole context. Each test is one leak or misjudgement found on LongMemEval, kept from coming back:

  - passages were indexed with no collection, so every collection's search saw them;
  - graph expansion walked from a scoped seed into another collection's documents;
  - a document in no collection answered a question scoped to one;
  - the data route and graph counting ignored the scope (a chat question answered from a table);
  - the relevance judge read the first 6,000 characters and said NO to an answer further down.

No services: fakes stand in for the graph, the vector store and the model.
"""
import uuid
from types import SimpleNamespace

import pytest

from opencortyx.capabilities.modules import trust
from opencortyx.core.dataplane import passages
from opencortyx.retrieval.contracts import Candidate, CandidateGraph, PathStep
from opencortyx.retrieval.l2_data.schema import Column, Schema, Table
from opencortyx.retrieval.orchestrator import RetrievalWorkflow
from opencortyx.stores.graph.models import Node
from opencortyx.stores.vector.models import search_fields

WS = uuid.uuid4()
MINE, OTHER = uuid.uuid4(), uuid.uuid4()


def _node(nid, *, type="Text", collection=None, source_ref=None, props=None):
    return Node(id=nid, workspace_id=WS, type=type, label=str(nid)[:8], properties=props or {},
                collection_id=collection, source_ref=source_ref)


class _Repo:
    def __init__(self, nodes):
        self.nodes = {str(n.id): n for n in nodes}

    async def get_many(self, workspace_id, ids):
        return [self.nodes[str(i)] for i in ids if str(i) in self.nodes]


class _Timer:
    def __init__(self):
        self.marks = []

    def mark(self, *a, **kw):
        self.marks.append((a, kw))


def _graph(ids):
    g = CandidateGraph()
    for i in ids:
        g.candidates[i] = Candidate(node_id=i)
    g.seeds = list(ids[:1])
    g.edges = [PathStep(from_id=ids[0], edge_type="SAME_AS", to_id=i, direction="out") for i in ids[1:]]
    return g


# ── passages carry their document's collection ────────────────────────────────────────────────

def test_a_passage_point_carries_its_documents_collection():
    row = {"chunk_id": str(uuid.uuid4()), "asset_id": str(uuid.uuid4()), "text": "x", "ordinal": 0,
           "element_type": "Text"}
    node = passages.record(WS, row, collection_id=MINE)
    assert search_fields(node)["collection_id"] == str(MINE)
    assert search_fields(passages.record(WS, row))["collection_id"] is None   # the leak, without one


# ── the assembled graph is cut to the collections asked for ───────────────────────────────────

@pytest.mark.anyio
async def test_expansion_into_another_collection_is_cut_after_assembly():
    doc_mine, doc_other, doc_none = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p_mine, p_other, p_none, date = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    repo = _Repo([
        _node(doc_mine, type="Source", collection=MINE),
        _node(doc_other, type="Source", collection=OTHER),
        _node(doc_none, type="Source"),                            # a document in no collection
        _node(p_mine, source_ref=doc_mine),                        # passages: their document's collection
        _node(p_other, source_ref=doc_other),
        _node(p_none, source_ref=doc_none),
        _node(date, type="Date"),                                  # workspace knowledge, no asset
    ])
    wf = SimpleNamespace(repo=repo)
    t = _Timer()
    g = await RetrievalWorkflow._apply_collections(wf, WS, _graph([p_mine, doc_mine, p_other, doc_other,
                                                                   p_none, doc_none, date]), [MINE], t)
    assert set(g.candidates) == {p_mine, doc_mine, date}
    assert all(e.from_id in g.candidates and e.to_id in g.candidates for e in g.edges)
    assert t.marks, "the cut is in the trace"


@pytest.mark.anyio
async def test_nothing_is_cut_when_everything_is_in_scope():
    a, b = uuid.uuid4(), uuid.uuid4()
    wf = SimpleNamespace(repo=_Repo([_node(a, collection=MINE), _node(b, collection=MINE)]))
    g = _graph([a, b])
    out = await RetrievalWorkflow._apply_collections(wf, WS, g, [MINE], _Timer())
    assert out is g and set(out.candidates) == {a, b}


# ── the data route reads the scoped collections' tables only ──────────────────────────────────

def test_a_scoped_schema_keeps_only_those_assets_tables():
    t1 = Table(asset_id="a1", view="licences_00", name="licences-00", kind="structured", storage="managed",
               format="csv", columns=[Column("x")])
    t2 = Table(asset_id="a2", view="orders_00", name="orders-00", kind="structured", storage="managed", format="csv")
    obj = Table(asset_id="Licence", view="licence", name="Licence", kind="object", storage="logical",
                format="entity", assets=["a1"])
    s = Schema(tables={"licences_00": t1, "orders_00": t2, "licence": obj},
               refs=[SimpleNamespace(asset_id="a1"), SimpleNamespace(asset_id="a2")],
               logical=[("licence", "SELECT 1")])
    scoped = s.scoped(["a1"])
    assert list(scoped.tables) == ["licences_00"] and scoped.logical == []
    assert [r.asset_id for r in scoped.refs] == ["a1"]
    assert s.scoped([]).empty(), "a collection with no tables has no data route"


# ── graph counting counts the scoped collections only ─────────────────────────────────────────

@pytest.mark.anyio
async def test_graph_counting_is_scoped_and_none_there_is_no_answer(monkeypatch):
    from opencortyx.retrieval.l2_graph import aggregate as A

    class Repo:
        async def find(self, workspace_id, **kw):
            return [_node(uuid.uuid4(), type="Project", collection=OTHER) for _ in range(10)] + \
                   [_node(uuid.uuid4(), type="Project", collection=MINE) for _ in range(2)]

    monkeypatch.setattr(A, "NodeRepo", lambda: Repo())
    spec = A.AggregationSpec(function="count", target_type="Project", grounded=True)
    res = await A.execute(WS, spec, collection_ids=[MINE])
    assert res.scalar == 2.0 and res.usable
    empty = await A.execute(WS, spec, collection_ids=[uuid.uuid4()])
    assert not empty.usable, "a type absent from the collection is not a count of zero"


# ── the relevance judge reads the whole context, graded ───────────────────────────────────────

def test_the_context_is_judged_in_windows_cut_at_lines():
    ctx = "".join(f'{{"text": "block {i} ' + "x" * 900 + '"}\n' for i in range(30))
    ws = trust._windows(ctx)
    assert len(ws) == trust.WINDOWS
    assert all(len(w) <= trust.WINDOW_CHARS for w in ws)
    assert all(w.endswith("\n") for w in ws), "a window never cuts a block in half"


@pytest.mark.anyio
async def test_an_answer_past_the_first_window_is_found(monkeypatch):
    from opencortyx.core.inference import choice

    async def choose(prompt, options, **kw):
        yes = 3.0 if "business administration" in prompt.lower() else -3.0
        return choice.Choice(index=0 if yes > 0 else 1, option="", scores=[yes, -yes], margin=abs(2 * yes))
    monkeypatch.setattr(choice, "choose", choose)
    filler = "".join('{"text": "' + "chat " * 180 + '"}\n' for _ in range(8))
    ctx = filler + '{"text": "I graduated with a degree in Business Administration."}\n'
    assert len(filler) > 6000, "the answer sits where the old judge never looked"
    hit = await trust.score_relevance(WS, "What degree did I graduate with?", ctx, util_spec={"provider": "x"})
    miss = await trust.score_relevance(WS, "What degree did I graduate with?", filler, util_spec={"provider": "x"})
    assert hit["trust_score"] > 0.9 > 0.1 > miss["trust_score"]


@pytest.mark.anyio
async def test_a_judge_that_fails_is_neutral_not_a_veto(monkeypatch):
    from opencortyx.core.inference import choice

    async def boom(*a, **kw):
        raise RuntimeError("model down")
    monkeypatch.setattr(choice, "choose", boom)
    res = await trust.score_relevance(WS, "q", "context", util_spec={"provider": "x"})
    assert res["trust_score"] == 0.5


def test_the_judge_runs_only_where_it_can_decide():
    from opencortyx.retrieval.answering.answer import _crag_rejects
    solid = {"citations": [{}] * 10, "confidence": 0.6}
    thin = {"citations": [{}], "confidence": 0.1}
    assert not _crag_rejects(0.0, 0.3, solid), "solid retrieval: the judge is advisory, so not run"
    assert _crag_rejects(0.0, 0.3, thin), "thin retrieval: the judge decides"


def test_a_low_relevance_context_is_returned_and_the_judge_decides():
    """Retrieval below its similarity floor returns what it found, marked; the judge's verdict then
    decides alone - neither citations nor confidence can vouch for it."""
    from opencortyx.retrieval.answering.answer import _crag_rejects
    low = {"citations": [{}] * 50, "confidence": 0.25, "low_relevance": True}
    assert _crag_rejects(0.0, 0.3, low), "the judge runs"
    assert _crag_rejects(0.1, 0.3, low), "a judge saying no refuses, however many citations"
    assert not _crag_rejects(0.6, 0.3, low), "a judge saying yes answers"
