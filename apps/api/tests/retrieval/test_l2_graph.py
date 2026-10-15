"""L2 tests — edge scoring and beam search.

The property that matters most: NO relation is scored by a lookup. Every test here asserts that
relevance follows from the question and the corpus, so a graph with unfamiliar relation names
still gets a sensible ordering.
"""
import math
import uuid

import pytest

from opencortyx.retrieval.contracts import Path, PathStep
from opencortyx.retrieval.l2_graph.beam import (
    LENGTH_DECAY, BeamConfig, score_path,
)
from opencortyx.retrieval.l2_graph.edge_scoring import (
    EdgeRelevance, EdgeScorer, RelationProfile, endpoint_bonus, humanize,
)


# ── relation naming ──────────────────────────────────────────────────────────────────────────

def test_humanize_reads_machine_names_as_language():
    assert humanize("EXTRACTED_FROM") == "extracted from"
    assert humanize("TRANSACTED_WITH") == "transacted with"
    assert humanize("hasAuthor") == "has author"
    assert humanize("MENTIONS") == "mentions"
    assert humanize("") == ""


# ── rarity ───────────────────────────────────────────────────────────────────────────────────

def _profiles(counts: dict[str, int]) -> dict[str, RelationProfile]:
    total = sum(counts.values()) or 1
    out = {}
    for rel, c in counts.items():
        rarity = math.log(1.0 + total / max(c, 1)) / math.log(1.0 + total)
        out[rel] = RelationProfile(rel, c, max(0.0, min(1.0, rarity)), humanize(rel))
    return out


def test_ubiquitous_relations_are_less_discriminating():
    p = _profiles({"CREATED_ON": 400_000, "STATES_FACT": 500})
    assert p["STATES_FACT"].rarity > p["CREATED_ON"].rarity


# ── scoring without a table ──────────────────────────────────────────────────────────────────

class _FakeEmbed:
    """Deterministic stand-in: vectors are built so that relations sharing words with the query
    are close. Keeps the test independent of any model while exercising the real code path."""

    VOCAB = ["transact", "work", "mention", "extract", "locate", "company", "germany",
             "date", "own", "with", "from", "in"]

    def _vec(self, text: str):
        t = (text or "").lower()
        return [1.0 if w in t else 0.0 for w in self.VOCAB] or [0.0]

    async def __call__(self, texts):
        return [self._vec(t) for t in texts]


@pytest.mark.anyio
async def test_relation_relevance_follows_the_question():
    """The same graph, two questions, different walkable relations — with no table consulted."""
    scorer = EdgeScorer(_FakeEmbed())
    profiles = _profiles({"TRANSACTED_WITH": 1000, "WORKED_WITH": 1000,
                          "MENTIONED_IN": 1000, "LOCATED_IN": 1000})
    q_transact = _FakeEmbed()._vec("companies john transacted with")
    er = await scorer.score_relations(uuid.uuid4(), q_transact, profiles=profiles)
    top = er.top(1)[0][0]
    assert top == "TRANSACTED_WITH", er.top(4)

    q_locate = _FakeEmbed()._vec("companies located in germany")
    er2 = await scorer.score_relations(uuid.uuid4(), q_locate, profiles=profiles)
    assert er2.top(1)[0][0] == "LOCATED_IN", er2.top(4)


@pytest.mark.anyio
async def test_unknown_relation_names_are_still_scored():
    """A relation nobody anticipated must be orderable — the property a weight table removes."""
    scorer = EdgeScorer(_FakeEmbed())
    profiles = _profiles({"WIBBLE_FROB": 10, "TRANSACTED_WITH": 10})
    er = await scorer.score_relations(uuid.uuid4(), _FakeEmbed()._vec("transacted with"),
                                      profiles=profiles)
    assert "WIBBLE_FROB" in er.scores           # present, not dropped
    assert er.score("TRANSACTED_WITH") > er.score("WIBBLE_FROB")


@pytest.mark.anyio
async def test_empty_graph_yields_no_relations():
    scorer = EdgeScorer(_FakeEmbed())
    er = await scorer.score_relations(uuid.uuid4(), [1.0], profiles={})
    assert er.scores == {} and er.walkable() == []


def test_walkable_applies_a_floor_not_a_deny_list():
    er = EdgeRelevance(scores={"A": 0.9, "B": 0.2, "C": 0.01})
    assert er.walkable(0.12) == ["A", "B"]
    assert "C" not in er.walkable(0.12)


def test_endpoint_bonus_compares_text_never_looks_up():
    assert endpoint_bonus("Company", "companies") > 0
    assert endpoint_bonus("Turn", "companies") == 0.0
    assert endpoint_bonus("", "companies") == 0.0
    assert endpoint_bonus("Company", "") == 0.0


# ── path scoring ─────────────────────────────────────────────────────────────────────────────

def _path(*scores):
    a = uuid.uuid4()
    steps, prev = [], a
    for s in scores:
        nxt = uuid.uuid4()
        steps.append(PathStep(from_id=prev, edge_type="R", to_id=nxt, direction="out",
                              edge_score=s))
        prev = nxt
    return Path(seed_id=a, steps=steps)


def test_one_weak_hop_invalidates_a_route():
    """Product, not sum: a chain is as strong as its weakest link."""
    strong = _path(0.9, 0.9)
    broken = _path(0.9, 0.02)
    assert score_path(strong) > score_path(broken) * 10


def test_shorter_route_to_equal_evidence_scores_higher():
    short = _path(0.8)
    long_ = _path(0.8, 1.0)
    assert score_path(short) > score_path(long_)
    assert abs(score_path(long_) - 0.8 * LENGTH_DECAY) < 1e-9


def test_empty_path_scores_zero():
    assert score_path(Path(seed_id=uuid.uuid4())) == 0.0


def test_beam_config_defaults_are_bounded():
    """Every dimension of expansion must have a ceiling; unbounded growth is the fan-out bomb."""
    c = BeamConfig()
    assert c.width > 0 and c.max_hops > 0 and c.fanout > 0
    assert c.max_terminals > c.width
