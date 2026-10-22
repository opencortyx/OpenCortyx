"""A passage longer than one embedding surface is embedded in overlapping windows.

Before, text past ATTR_MAX_CHARS was not embedded at all: a 3,300-character paragraph was findable
by keyword only. Each window is a point of its own, so a match anywhere in the passage finds it.
"""
from opencortyx.core.substrate.document import ATTR_MAX_CHARS, text_windows
from opencortyx.stores.vector.models import point_id


def _sentences(n: int) -> str:
    return " ".join(f"Sentence number {i} says something specific about topic {i}." for i in range(n))


def test_short_text_is_one_window():
    assert text_windows("One short paragraph.") == ["One short paragraph."]


def test_long_text_is_covered_by_windows_within_the_limit():
    text = _sentences(200)
    windows = text_windows(text)
    assert len(windows) > 1
    assert all(len(w) <= ATTR_MAX_CHARS for w in windows)
    for i in (0, 57, 199):                                # every sentence lands whole in a window
        sentence = f"Sentence number {i} says something specific about topic {i}."
        assert any(sentence in w for w in windows), i


def test_windows_overlap_so_a_cut_does_not_split_meaning():
    windows = text_windows(_sentences(200))
    assert windows[1].split(".")[0] in windows[0]


def test_a_sentence_longer_than_a_window_is_still_cut_within_the_limit():
    windows = text_windows("word " * 1500)
    assert len(windows) >= 3 and all(len(w) <= ATTR_MAX_CHARS for w in windows)


def test_window_points_have_their_own_ids_and_the_first_keeps_its_old_one():
    assert point_id("n", "text") == point_id("n", "text", 0)
    assert len({point_id("n", "text", p) for p in range(5)}) == 5


def test_without_overlap_each_sentence_is_in_one_window():
    # The paragraph split into nodes: [-0:] once meant "all of it", repeating each window in the next.
    text = " ".join(f"Sentence {i} is about a part of the system." for i in range(40))
    windows = text_windows(text, 200, overlap=0)
    assert len(windows) > 1 and all(len(w) <= 200 for w in windows)
    assert " ".join(windows) == text
