"""L4 tests — roles, provenance, budget, and evidence density.

The property throughout: the budget buys DISTINCT EVIDENCE. Optimising for block count is
optimising the wrong thing, and that drift (119 -> 84 tokens per node) is what made a recall
number stop describing the product.
"""
import uuid

from opencortyx.retrieval.contracts import (
    Candidate, ContextBlock, Evidence, Path, PathStep,
)
from opencortyx.retrieval.l0_understanding.planner import build_plan
from opencortyx.retrieval.l3_rerank.reranker import rerank
from opencortyx.retrieval.l4_context.builder import ROLE_PRIMARY, ROLE_RELATIONSHIP, ROLE_TEMPORAL, assign_role, build, carries_content, estimate_tokens, render, retrieved_by
from opencortyx.retrieval.l4_context.optimizer import (
    compress, dedupe, evidence_density, merge_equivalents, optimize,
)


def cand(text="a statement", ntype="Turn", props=None, evidence=(), paths=()):
    c = Candidate(node_id=uuid.uuid4(), node_type=ntype, text=text, properties=props or {})
    for e in evidence:
        c.add(e)
    c.paths = list(paths)
    return c


def path(*scores):
    a = uuid.uuid4(); steps = []; prev = a
    for s in scores:
        nxt = uuid.uuid4()
        steps.append(PathStep(from_id=prev, edge_type="MENTIONS", to_id=nxt, direction="out",
                              edge_score=s))
        prev = nxt
    from opencortyx.retrieval.l2_graph.beam import score_path
    p = Path(seed_id=a, steps=steps)
    p.score = score_path(p)
    return p


def blk(text, role="primary", score=0.5, **kw):
    return ContextBlock(node_id=kw.pop("node_id", uuid.uuid4()), role=role, text=text,
                        score=score, tokens=estimate_tokens(text), **kw)


# ── roles ────────────────────────────────────────────────────────────────────────────────────

def test_role_follows_evidence_not_node_type():
    """Same node type, different evidence, different role."""
    direct = cand(ntype="Turn", evidence=[Evidence("node_embedding", 0, 0.9, "cosine")])
    walked = cand(ntype="Turn", evidence=[Evidence("graph", 0, 0.7, "path")], paths=[path(0.8)])
    rerank([direct, walked])
    assert assign_role(direct) == ROLE_PRIMARY
    assert assign_role(walked) == ROLE_RELATIONSHIP


def test_an_unknown_node_type_is_eligible_for_primary():
    c = cand(ntype="SomethingInventedNextWeek",
             evidence=[Evidence("node_embedding", 0, 0.88, "cosine")])
    rerank([c])
    assert assign_role(c) == ROLE_PRIMARY


def test_temporal_role_requires_a_known_matching_date():
    plan = build_plan("What did Nate do on 25 May, 2022?")
    dated = cand(props={"date_time": "2:01 pm on 25 May, 2022"},
                 evidence=[Evidence("attr_embedding", 0, 0.5, "cosine", "date_time")])
    undated = cand(props={}, evidence=[Evidence("attr_embedding", 0, 0.5, "cosine", "date_time")])
    rerank([dated, undated], plan)
    assert assign_role(dated, plan) == ROLE_TEMPORAL
    assert assign_role(undated, plan) != ROLE_TEMPORAL


def test_strong_direct_match_outranks_a_path_for_the_role():
    both = cand(evidence=[Evidence("node_embedding", 0, 0.92, "cosine"),
                          Evidence("graph", 3, 0.4, "path")], paths=[path(0.4)])
    rerank([both])
    assert assign_role(both) == ROLE_PRIMARY


# ── provenance ───────────────────────────────────────────────────────────────────────────────

def test_provenance_names_the_attribute_that_fired():
    c = cand(evidence=[Evidence("attr_embedding", 0, 0.8, "cosine", "date_time"),
                       Evidence("bm25", 4, 3.0, "bm25")])
    tags = retrieved_by(c)
    assert "attr_embedding(date_time)" in tags
    assert "bm25" in tags


def test_blocks_carry_route_and_score():
    c = cand(evidence=[Evidence("graph", 0, 0.6, "path")], paths=[path(0.9, 0.8)])
    rerank([c])
    b = build([c])[0]
    assert b.path is not None and len(b.path) == 5      # seed + 2×(edge,node)
    assert b.score > 0


def test_matched_attribute_value_is_surfaced_when_it_is_why_the_node_matched():
    """A node retrieved because its date matched must SHOW the date."""
    plan = build_plan("What did Nate do on 25 May, 2022?")
    c = cand(text="We went to the beach.",
             evidence=[Evidence("attr_embedding", 0, 0.9, "cosine", "date_time",
                                matched_text="date time: 2:01 pm on 25 May, 2022")])
    rerank([c], plan)
    b = build([c], plan)[0]
    assert "25 May" in b.text


def test_empty_blocks_are_dropped():
    assert build([cand(text="")]) == []


# ── dedupe / merge ───────────────────────────────────────────────────────────────────────────

def test_dedupe_keeps_the_stronger_copy_of_a_node():
    nid = uuid.uuid4()
    a = blk("text", score=0.3, node_id=nid)
    b = blk("text", score=0.9, node_id=nid)
    out = dedupe([a, b])
    assert len(out) == 1 and out[0].score == 0.9


def test_merge_unions_provenance_rather_than_discarding_it():
    a = blk("joanna is allergic to reptiles and animals with fur", score=0.9)
    a.retrieved_by = ["node_embedding"]
    b = blk("joanna is allergic to reptiles and animals with fur too", score=0.8)
    b.retrieved_by = ["attr_embedding(description)"]
    out = merge_equivalents([a, b])
    assert len(out) == 1
    assert set(out[0].retrieved_by) == {"node_embedding", "attr_embedding(description)"}


def test_distinct_statements_are_not_merged():
    a = blk("joanna adopted a dog from the shelter", score=0.9)
    b = blk("nate won a video game tournament", score=0.8)
    assert len(merge_equivalents([a, b])) == 2


# ── budget ───────────────────────────────────────────────────────────────────────────────────

def test_optimize_respects_the_budget():
    blocks = [blk("word " * 60, score=1.0 - i * 0.01) for i in range(40)]
    out = optimize(blocks, budget_tokens=200)
    assert sum(b.tokens for b in out) <= 200
    assert out


def test_role_floors_prevent_starvation():
    """A dominant role must not consume the whole window."""
    prim = [blk("primary statement number %d here" % i, role="primary", score=0.99)
            for i in range(30)]
    rel = [blk("relationship statement %d" % i, role="relationship", score=0.30)
           for i in range(5)]
    out = optimize(prim + rel, budget_tokens=180)
    assert any(b.role == "relationship" for b in out), "relationship evidence was starved"


def test_no_starvation_when_a_role_is_absent():
    out = optimize([blk("only primary here", role="primary", score=0.9)], budget_tokens=100)
    assert len(out) == 1


def test_compress_cuts_at_a_sentence_boundary():
    t = "First sentence here. Second sentence follows. Third one trails."
    c = compress(t, 40)
    assert c.endswith(".") and len(c) <= 45
    assert compress("short", 100) == "short"


def test_tiny_remainder_is_not_filled_with_a_stub():
    big = blk("word " * 100, score=0.9)
    small = blk("word " * 100, score=0.8)
    out = optimize([big, small], budget_tokens=big.tokens + 3, min_block_tokens=12)
    assert sum(b.tokens for b in out) <= big.tokens + 3


def test_evidence_density_counts_sources_not_just_blocks():
    """Fifty blocks from three sources is a narrower context than twelve from twelve."""
    narrow = [blk("t%d" % i, score=0.5) for i in range(6)]
    for b in narrow:
        b.retrieved_by = ["bm25"]
    wide = [blk("t%d" % i, score=0.5) for i in range(6)]
    for i, b in enumerate(wide):
        b.retrieved_by = [f"surface_{i}"]
    assert evidence_density(narrow)["distinct_sources"] == 1
    assert evidence_density(wide)["distinct_sources"] == 6


def test_density_on_empty_input():
    d = evidence_density([])
    assert d["n_blocks"] == 0 and d["tokens"] == 0


# ── rendering ────────────────────────────────────────────────────────────────────────────────

def test_render_sections_by_role_with_primary_first():
    p = blk("the direct answer", role="primary", score=0.9)
    r = blk("the connecting route", role="relationship", score=0.8)
    text = render([r, p])
    assert text.index("PRIMARY EVIDENCE") < text.index("RELATIONSHIP EVIDENCE")
    assert "the direct answer" in text and "the connecting route" in text


def test_render_includes_provenance_when_asked():
    b = blk("a statement", score=0.5)
    b.retrieved_by = ["attr_embedding(date_time)"]
    assert "via=attr_embedding(date_time)" in render([b])
    assert "via=" not in render([b], include_provenance=False)


# ── content gating (regression: bare entity labels ranked FIRST) ──────────────────────────────

def test_a_bare_entity_label_is_not_evidence():
    """MEASURED: the node "Joanna" ranked first for "What is Joanna allergic to?" at 0.405, and
    "Nate" first for "What Console does Nate own?" — concept labels answering nothing."""
    bare = cand(text="Joanna", ntype="Entity")
    bare.label = "Joanna"
    assert not carries_content(bare, "Joanna")
    assert build([bare]) == []


def test_a_short_fact_triple_IS_evidence():
    """Gated on substance, not length or type — a terse fact still asserts something."""
    fact = cand(text="Nate owns Xenoblade", ntype="Fact")
    assert carries_content(fact, "Nate owns Xenoblade")
    assert len(build([fact])) == 1


def test_content_gate_does_not_consult_node_type():
    """An invented type carrying a statement must pass; the same type carrying a name must not."""
    assert carries_content(cand(text="the shipment cleared customs", ntype="Whatever"),
                           "the shipment cleared customs")
    lbl = cand(text="Acme", ntype="Whatever")
    lbl.label = "Acme"
    assert not carries_content(lbl, "Acme")
