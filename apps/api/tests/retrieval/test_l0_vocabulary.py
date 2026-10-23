"""L0 — node-type and edge-type hypotheses.

The two rules this engine is built around are enforced HERE as tests, not as intentions:

  1. No node-type lookup table.  -> `test_an_unseen_type_name_is_scoreable_immediately`
                                    `test_an_unargued_type_is_neutral_never_demoted`
  2. No hardcoded edge weights.  -> `test_edge_scores_come_from_the_question_not_a_constant`

And the property that keeps a hypothesis from becoming a lie:

  3. No evidence means no opinion. -> `test_a_question_about_nothing_in_the_corpus_yields_nothing`
                                     `test_rarity_alone_can_never_propose_a_type`
"""
from __future__ import annotations

import uuid

import pytest

from opencortyx.retrieval.l0_understanding.planner import build_plan
from opencortyx.retrieval.l0_understanding.vocabulary import (
    MIN_TYPE_EVIDENCE, TypeVocabulary, W_RARITY, answer_head_nouns, _profiles, score_vocabulary,
)
from opencortyx.retrieval.common.text_norm import (
    content_tokens, humanize, singularize, stem,
)


def vocab(nodes: dict[str, int] | None = None, edges: dict[str, int] | None = None,
          ) -> TypeVocabulary:
    """A vocabulary as if discovered from a corpus with these type counts."""
    return TypeVocabulary(node_types=_profiles(list((nodes or {}).items())),
                          edge_types=_profiles(list((edges or {}).items())))


#: The real LoCoMo vocabulary, abbreviated. Used so unit tests exercise the shapes that actually
#: exist — a structural relation covering the whole graph, a rare semantic one, a multi-word name.
REAL = vocab(
    nodes={"KeyNode": 300070, "Chunk": 44265, "Document": 35392, "Turn": 5882, "Entity": 3478,
           "Fact": 1225, "Session": 287, "Dialogue": 10},
    edges={"CREATED_ON": 397512, "CONTAINS": 144265, "NEXT": 101062, "HAS_DOCUMENT": 35395,
           "NEXT_TURN": 5610, "MENTIONS": 2642, "EXTRACTED_FROM": 1225, "HAS_SKILL": 16,
           "HAS_RISK": 13, "HAS_CONSUMER": 12, "HAS_INCIDENT": 11},
)


# ── rule 1: types are discovered, never declared ─────────────────────────────────────────────

def test_an_unseen_type_name_is_scoreable_immediately():
    """A type nobody wrote code for must be rankable the moment an ingest creates it.

    This is the property `_TYPE_ORDER = {"Turn": 1, ...}` destroyed: a `Fact` type absent from
    the dict took the default and was demoted below every turn, and an entire extracted layer
    became invisible to retrieval without an error.
    """
    exotic = vocab(nodes={"Zorblat": 40}, edges={"XENO_LINKED_TO": 7})
    nodes, edges, _ = score_vocabulary("which zorblats are xeno linked to Acme?", exotic)
    assert "Zorblat" in nodes, "a brand-new node type was not scoreable"
    assert "XENO_LINKED_TO" in edges, "a brand-new relation was not scoreable"


def test_the_scorer_holds_no_names_of_its_own():
    """Nothing is known about a type except what the corpus reported and the question said."""
    nodes, edges, _ = score_vocabulary("which documents mention Germany?", vocab())
    assert nodes == {} and edges == {}, "types were produced from an empty corpus vocabulary"


def test_an_unargued_type_is_neutral_never_demoted():
    """A type the question said nothing about ranks as if there were no plan at all.

    The old default (`_TYPE_ORDER.get(t, 5)`) assigned the WORST rank to anything unlisted, so
    silence about a type was indistinguishable from a judgement against it.
    """
    plan = build_plan("which documents mention Germany?", REAL)
    assert plan.type_prior("Document") > 0.0
    assert plan.type_prior("Fact") == 0.0
    assert plan.type_prior("NeverHeardOfIt") == plan.type_prior("Fact"), \
        "an unargued type must be indistinguishable from an unknown one"
    assert plan.edge_prior("HAS_INCIDENT") == 0.0
    assert all(v >= 0.0 for v in plan.node_types.values()), "a prior must never be a penalty"


# ── rule 2: edge relevance is computed against the question ──────────────────────────────────

def test_edge_scores_come_from_the_question_not_a_constant():
    """The same relation scores differently for different questions, and that is the whole point.

    The previous engine gave membership of `_PROVENANCE_EDGES` a flat 0.9 and everything else a
    flat 0.05, so a question about WHEN and a question about WHO walked identically.
    """
    _, skills, _ = score_vocabulary("what skills does Nate have?", REAL)
    _, mentions, _ = score_vocabulary("which documents mention Germany?", REAL)
    assert skills.get("HAS_SKILL", 0) > 0
    assert mentions.get("HAS_SKILL", 0) == 0, "a relation scored without the question supporting it"
    assert mentions.get("MENTIONS", 0) > 0
    assert skills.get("MENTIONS", 0) == 0


def test_a_connective_in_a_name_never_matches_on_its_own():
    """"What has happened?" must not implicate all forty `HAS_*` relations.

    `HAS_SKILL` and `HAS_RISK` are told apart by `skill` and `risk`; `has` distinguishes nothing,
    so it is counted on neither side.
    """
    _, edges, _ = score_vocabulary("what has happened here?", REAL)
    has_star = [e for e in edges if e.startswith("HAS_")]
    assert has_star == [], f"connective-only match fired: {has_star}"


def test_partial_coverage_scores_below_a_whole_name():
    """Matching one token of a two-token name is weaker evidence than matching the phrase."""
    _, whole, _ = score_vocabulary("show me things extracted from the report", REAL)
    _, part, _ = score_vocabulary("what was extracted?", REAL)
    assert whole["EXTRACTED_FROM"] > part["EXTRACTED_FROM"]


def test_a_multiword_name_matched_as_a_phrase_scores_top():
    _, edges, _ = score_vocabulary("what is the next turn after this one?", REAL)
    assert edges["NEXT_TURN"] >= 0.9


# ── rule 3: no evidence means no opinion ─────────────────────────────────────────────────────

def test_a_question_about_nothing_in_the_corpus_yields_nothing():
    """The measured failure this exists to prevent.

    "What is Joanna allergic to?" scored `HAS_INCIDENT`, `HAS_ATTENDEE` and `HAS_CONSUMER` as its
    top relations under global profiling — none of which touch a conversation turn — because
    rank-normalising 64 near-identical cosines manufactured 0.97/0.95/0.94 out of noise. Beam
    search walked them and discovered nothing on two of three queries.

    A ranking invented from an absent signal is worse than an empty one, because an empty one is
    visibly empty.
    """
    nodes, edges, notes = score_vocabulary("What is Joanna allergic to?", REAL)
    assert nodes == {}, f"invented node hypotheses: {nodes}"
    assert edges == {}, f"invented edge hypotheses: {edges}"
    assert notes == []


def test_rarity_alone_can_never_propose_a_type():
    """A vanishingly rare type is still not a hypothesis until the question mentions it.

    Rarity breaks ties between types the question supports equally; on its own it is a fact about
    the corpus, not about the question.
    """
    rare = vocab(edges={"OBSCURE_RELATION": 1, "CREATED_ON": 999_999})
    _, edges, _ = score_vocabulary("tell me what happened yesterday", rare)
    assert edges == {}
    assert W_RARITY < MIN_TYPE_EVIDENCE, \
        "rarity is weighted high enough to propose a type with zero word overlap"


def test_rarity_only_separates_types_the_question_supports_equally():
    """Both names reduce to `mention`, so the question argues for them identically and only
    rarity is left to separate them."""
    tie = vocab(edges={"MENTIONS": 500_000, "MENTIONED_BY": 5})
    _, edges, _ = score_vocabulary("which records mention Acme?", tie)
    assert set(edges) == {"MENTIONS", "MENTIONED_BY"}
    assert edges["MENTIONED_BY"] > edges["MENTIONS"], "rarity did not break an equal-evidence tie"


# ── the question side ────────────────────────────────────────────────────────────────────────

def test_head_noun_is_what_the_answer_IS():
    assert "documents" in answer_head_nouns("which documents mention Germany?")
    assert "sessions" in answer_head_nouns("how many sessions were there?")
    assert "facts" in answer_head_nouns("list all facts about Nate")


def test_head_noun_ignores_grammar_words():
    """"what is X" must not propose a type called `is`."""
    assert answer_head_nouns("what is Joanna allergic to?") == []
    assert answer_head_nouns("which of the things happened?") == []


def test_head_noun_applies_to_node_types_only():
    """A question asks to be handed things, never relations, so the head noun cannot boost an
    edge. `Turn` is the answer's type here; `NEXT_TURN` only matches token-wise."""
    nodes, edges, _ = score_vocabulary("which turns happened that day?", REAL)
    assert nodes["Turn"] > edges["NEXT_TURN"]


def test_a_plural_question_matches_a_singular_type():
    nodes, _, _ = score_vocabulary("which documents mention Germany?", REAL)
    assert "Document" in nodes


def test_a_stem_connects_an_adjective_to_the_relation_noun():
    """Singularisation alone cannot reach `HAS_ALLERGY` from "allergic"."""
    allergy = vocab(edges={"HAS_ALLERGY": 20})
    _, edges, _ = score_vocabulary("what is Joanna allergic to?", allergy)
    assert "HAS_ALLERGY" in edges
    assert stem("allergic") == stem("allergies") == stem("allergy")


def test_stemming_stays_shallow_enough_not_to_invent_matches():
    """Over-stemming would create false evidence, which is the unsafe failure direction."""
    assert stem("police") != stem("policy")
    assert singularize("companies") == "company"
    assert humanize("KeyNode") == "key node"
    assert content_tokens("HAS_SKILL") == ["skill"]
    assert content_tokens("HAS") == ["has"], "a name of pure connectives must not become empty"


# ── the plan ─────────────────────────────────────────────────────────────────────────────────

def test_a_plan_without_a_vocabulary_is_exactly_the_old_plan():
    """Type hypotheses are additive. Every existing caller passes no vocabulary and must be
    bit-for-bit unaffected."""
    for q in ("What is Joanna allergic to?", "Which documents mention Germany?",
              "How many sessions?", ""):
        a, b = build_plan(q), build_plan(q, None)
        assert a.strategies == b.strategies
        assert a.intent == b.intent and a.max_hops == b.max_hops
        assert a.answer_type_hint == b.answer_type_hint
        assert a.node_types == {} and a.edge_types == {}


def test_a_named_relation_raises_the_graph_surface():
    """The intent classifier reads language; it cannot know "skills" is relational HERE. That
    only follows from `HAS_SKILL` existing in this corpus."""
    plain = build_plan("What skills does Nate have?")
    grounded = build_plan("What skills does Nate have?", REAL)
    assert grounded.strategies["graph"] > plain.strategies["graph"]


def test_no_surface_is_ever_zero_with_a_vocabulary_present():
    """The floor survives type hypotheses. A wrong plan must cost latency, never recall."""
    for q in ("which documents mention Germany?", "what skills does Nate have?",
              "something entirely unlike anything"):
        plan = build_plan(q, REAL)
        assert plan.strategies and min(plan.strategies.values()) >= 0.15


def test_type_hypotheses_fill_an_empty_answer_hint_and_never_overwrite_one():
    named_field = build_plan("What are Joanna's hobbies?", REAL)
    assert named_field.answer_type_hint == "hobbies", "an explicit field target was overwritten"

    from_vocab = build_plan("Which documents mention Germany?", REAL)
    assert from_vocab.answer_type_hint == "Document", \
        "a corpus-grounded answer type did not fill the empty hint"


def test_scores_stay_in_range_and_are_ordered_strongest_first():
    plan = build_plan("which documents mention Germany?", REAL)
    for d in (plan.node_types, plan.edge_types):
        assert all(MIN_TYPE_EVIDENCE <= v <= 1.0 for v in d.values())
        assert list(d.values()) == sorted(d.values(), reverse=True)


def test_an_empty_query_with_a_vocabulary_is_safe():
    plan = build_plan("", REAL)
    assert plan.node_types == {} and plan.edge_types == {}
    assert plan.strategies


# ── integration: the real graph ──────────────────────────────────────────────────────────────

ORG = uuid.UUID("e5954895-7b42-5a9b-93f6-911febc30bca")


async def _db():
    from opencortyx.core.db import V2ReadSession
    return V2ReadSession()


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.mark.integration
@pytest.mark.anyio
async def test_vocabulary_is_read_from_the_real_corpus():
    from opencortyx.retrieval.l0_understanding.vocabulary import (
        clear_vocabulary_cache, load_vocabulary,
    )
    clear_vocabulary_cache()
    async with await _db() as db:
        v = await load_vocabulary(db, ORG)
    if not v:
        pytest.skip("org has no graph loaded")
    assert "Turn" in v.node_types and "Document" in v.node_types
    assert v.node_types["Turn"].count > 1000
    assert "MENTIONS" in v.edge_types
    # A structural relation covering nearly everything must carry almost no rarity.
    assert v.edge_types["CREATED_ON"].rarity < v.edge_types["MENTIONS"].rarity


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_real_corpus_does_not_manufacture_relations_for_an_unsupported_question():
    """The regression, against the actual 64-relation vocabulary that produced it."""
    from opencortyx.retrieval.l0_understanding.vocabulary import (
        load_vocabulary,
    )
    async with await _db() as db:
        v = await load_vocabulary(db, ORG)
    if not v:
        pytest.skip("org has no graph loaded")

    plan = build_plan("What is Joanna allergic to?", v)
    assert plan.edge_types == {}, f"manufactured relations: {plan.edge_types}"
    assert plan.node_types == {}

    grounded = build_plan("What skills does Nate have?", v)
    assert "HAS_SKILL" in grounded.edge_types
    for unrelated in ("HAS_INCIDENT", "HAS_CONSUMER", "HAS_ATTENDEE"):
        assert unrelated not in grounded.edge_types, \
            f"{unrelated} scored on a question that never mentioned it"


async def test_a_stale_vocabulary_is_served_while_one_refresh_runs(monkeypatch):
    """Counting every node and edge by type took seconds on a large workspace; a question must not
    wait for it once a vocabulary exists, and only one refresh runs at a time."""
    import asyncio
    import uuid as _uuid

    from opencortyx.retrieval.l0_understanding import vocabulary as V
    V.clear_vocabulary_cache()
    reads = []

    async def read(ws):
        reads.append(ws)
        await asyncio.sleep(0)
        return V.TypeVocabulary(node_types={"Text": V.TypeProfile(name="Text", count=len(reads), rarity=0.5, text="text")},
                                workspace_id=str(ws), loaded_at=V.time.monotonic())
    monkeypatch.setattr(V, "_read_vocabulary", read)
    ws = _uuid.uuid4()
    first = await V.load_vocabulary(ws)
    assert len(reads) == 1 and await V.load_vocabulary(ws) is first          # fresh: no read
    first.loaded_at -= V._CACHE_TTL_SECONDS + 1                               # now stale
    served = [await V.load_vocabulary(ws) for _ in range(3)]
    assert all(v is first for v in served)                                    # nobody waits
    await asyncio.sleep(0.01)
    assert len(reads) == 2                                                    # one refresh, not three
    assert (await V.load_vocabulary(ws)).node_types["Text"].count == 2
    V.clear_vocabulary_cache()
