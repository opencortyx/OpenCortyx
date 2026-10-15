"""Regression tests for two measured L2 failures.

Both were found by running the pipeline end-to-end against the real 406k-node graph, not by
reasoning about it. Both produced confident-looking output while retrieving nothing.
"""
import math
import uuid

import pytest

from opencortyx.retrieval.l2_graph.edge_scoring import (
    EdgeScorer, RelationProfile, humanize,
)


class _FlatEmbed:
    """Every text embeds to almost the same vector — the situation that produced the bug."""
    async def __call__(self, texts):
        return [[1.0, 0.999, 0.998] for _ in texts]


class _SeparatingEmbed:
    VOCAB = ["mention", "contain", "extract", "next", "turn", "allergic", "who"]

    def vec(self, text):
        t = (text or "").lower()
        return [1.0 if w in t else 0.0 for w in self.VOCAB]

    async def __call__(self, texts):
        return [self.vec(t) for t in texts]


def _profiles(counts):
    total = sum(counts.values()) or 1
    return {r: RelationProfile(r, c,
                               max(0.0, min(1.0, math.log(1.0 + total / max(c, 1))
                                            / math.log(1.0 + total))),
                               humanize(r))
            for r, c in counts.items()}


@pytest.mark.anyio
async def test_a_flat_semantic_signal_is_not_amplified_into_confidence():
    """MEASURED: over 64 relations from unrelated ingests every raw cosine landed in a narrow
    band, and rank-normalising produced 0.97 / 0.95 / 0.94 out of noise. Beam search then walked
    the top of an arbitrary ordering and discovered nothing."""
    scorer = EdgeScorer(_FlatEmbed())
    profiles = _profiles({f"REL_{i}": 100 for i in range(20)})
    er = await scorer.score_relations(uuid.uuid4(), [1.0, 0.999, 0.998], profiles=profiles)
    top = [s for _, s in er.top(5)]
    # Without the spread check these would fan out to ~0.95 down to ~0.75 purely from rank.
    assert max(top) - min(top) < 0.25, f"flat signal was amplified: {er.top(5)}"


@pytest.mark.anyio
async def test_a_separated_signal_is_still_normalised():
    """The guard must not disable ranking when the signal is real."""
    scorer = EdgeScorer(_SeparatingEmbed())
    profiles = _profiles({"MENTIONS": 100, "NEXT_TURN": 100, "CONTAINED_IN": 100})
    q = _SeparatingEmbed().vec("who mentions joanna")
    er = await scorer.score_relations(uuid.uuid4(), q, profiles=profiles)
    assert er.top(1)[0][0] == "MENTIONS", er.top(3)
    assert er.score("MENTIONS") > er.score("NEXT_TURN")


@pytest.mark.integration
@pytest.mark.anyio
async def test_seed_local_profiling_excludes_unreachable_relations():
    """MEASURED: profiling the whole org scored HAS_INCIDENT / HAS_ATTENDEE / HAS_CONSUMER top for
    a conversation question. None of them touch a conversation turn; beam discovered 0 nodes."""
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.retrieval.l2_graph.edge_scoring import (
        profile_relations, profile_relations_for,
    )

    org = uuid.UUID("e5954895-7b42-5a9b-93f6-911febc30bca")
    seeds = [str(t.id) for t in await NodeRepo().find(org, exact_types=["Turn"], limit=12)]
    if not seeds:
        pytest.skip("no Turn nodes in this org")
    local = await profile_relations_for(org, seeds)
    globl = await profile_relations(org)

    assert local, "seed-local profiling found no relations"
    assert len(local) < len(globl), \
        f"seed-local profiling was not narrower: {len(local)} vs {len(globl)}"
    # Everything local must be genuinely incident to a turn.
    assert all(r in globl for r in local)
