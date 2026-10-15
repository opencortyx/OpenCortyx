"""Score-adaptive truncation: keep what the ranking is confident about, not what fits.

The guards matter more than the cut. A confidence threshold over a distribution that does not
separate is arithmetic pretending to be judgement, and this engine has already paid for that
once - 64 near-identical cosines rank-normalised into 0.97/0.95/0.94 of pure noise.
"""
import uuid

from opencortyx.retrieval.contracts import ContextBlock
from opencortyx.retrieval.l4_context import optimizer
from opencortyx.retrieval import experiments


def _b(score, text="some evidence text here"):
    return ContextBlock(node_id=uuid.uuid4(), node_type="Turn", role="primary",
                        text=text, score=score, tokens=10)


def test_blocks_below_tau_of_the_best_are_discarded():
    blocks = [_b(1.0, "a"), _b(0.9, "b"), _b(0.75, "c"), _b(0.2, "d"), _b(0.05, "e")]
    kept, why = optimizer.adaptive_cut(blocks)
    assert [b.score for b in kept] == [1.0, 0.9, 0.75], why


def test_the_cut_is_relative_to_the_top_not_an_absolute_070():
    """`final_score` ranges move with the question, so an absolute floor means nothing."""
    blocks = [_b(0.30), _b(0.28), _b(0.24), _b(0.02)]
    kept, _ = optimizer.adaptive_cut(blocks)
    assert [b.score for b in kept] == [0.30, 0.28, 0.24]
    assert all(b.score < optimizer.ADAPTIVE_TAU for b in kept), "all below an absolute 0.70"


def test_a_flat_distribution_is_not_cut_at_all():
    """Spread below the guard means the ranking has no opinion; cutting would discard at random."""
    blocks = [_b(0.500), _b(0.499), _b(0.498), _b(0.497), _b(0.496)]
    kept, why = optimizer.adaptive_cut(blocks)
    assert len(kept) == len(blocks)
    assert "flat" in why


def test_the_reason_is_always_reported():
    """A truncation that silently did nothing is the failure shape this engine has paid for."""
    for blocks in ([_b(1.0), _b(0.9), _b(0.1), _b(0.05)], [_b(0.5)] * 5, [_b(1.0)]):
        _, why = optimizer.adaptive_cut(list(blocks))
        assert why and isinstance(why, str)


def test_it_never_empties_the_window():
    """An adaptive cut that returns nothing turns arithmetic into an abstention, which is L3's
    decision on evidence and not L4's on scores."""
    blocks = [_b(1.0, "alpha")] + [_b(0.01, "noise %d" % i) for i in range(9)]
    kept, _ = optimizer.adaptive_cut(blocks)
    assert len(kept) >= optimizer.ADAPTIVE_MIN_BLOCKS


def test_a_short_window_is_left_alone():
    blocks = [_b(1.0), _b(0.1)]
    kept, why = optimizer.adaptive_cut(blocks)
    assert kept == blocks and "declined" in why


def test_unscored_blocks_do_not_set_the_top():
    """`score=None` means NOT ASSESSED. Treating it as 0.0 is the absent-is-not-zero bug."""
    blocks = [_b(1.0), _b(0.9), _b(None), _b(0.05)]
    kept, _ = optimizer.adaptive_cut(blocks)
    assert any(b.score == 1.0 for b in kept)


def test_optimize_is_unchanged_when_the_flag_is_off(monkeypatch):
    """The baseline branch must reproduce the frozen result exactly, or no delta means anything."""
    experiments.reset_for_tests()
    monkeypatch.delenv("WFV2_EXP", raising=False)
    blocks = [_b(1.0, "alpha evidence"), _b(0.9, "beta evidence"),
              _b(0.02, "gamma evidence"), _b(0.01, "delta evidence")]
    assert len(optimizer.optimize(list(blocks), 8000)) == 4
    experiments.reset_for_tests()


def test_optimize_truncates_when_the_flag_is_on(monkeypatch):
    """Four high-confidence blocks survive; the eight weak ones are dropped even though the
    8,000-token budget had ample room for all twelve. That is the whole point - the window comes
    out SHORT rather than full."""
    experiments.reset_for_tests()
    monkeypatch.setenv("WFV2_EXP", "adaptive_truncation")
    strong = ["Joanna is allergic to reptiles", "Nate owns a Nintendo Switch",
              "Gina lost her job at Door Dash", "Jon worked as a banker"]
    weak = ["the weather turned cold in October", "they agreed to meet again soon",
            "a photo was shared of the garden", "dinner plans were discussed",
            "someone mentioned a favourite song", "a package arrived on Tuesday",
            "the dog needed a walk", "traffic was heavy downtown"]
    blocks = ([_b(1.0 - i * 0.05, t) for i, t in enumerate(strong)] +
              [_b(0.10 - i * 0.01, t) for i, t in enumerate(weak)])
    kept = optimizer.optimize(list(blocks), 8000)
    assert len(kept) == 4, [b.score for b in kept]
    assert sum(b.tokens for b in kept) < 8000
    experiments.reset_for_tests()
