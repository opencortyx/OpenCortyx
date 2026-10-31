"""Near misses of a name, of one type, are linked; exact names (one node already) and other types are not."""
import uuid
from types import SimpleNamespace

from opencortyx.resolution.names import near_misses


def _n(label, type_="Person", named=True):
    return SimpleNamespace(id=uuid.uuid4(), label=label, type=type_,
                           properties={"__identity": "entity"} if named else {})


def test_a_near_miss_of_one_type_is_linked_with_its_score():
    new, old = _n("Kristen Hannah"), _n("Kristin Hannah")
    ((a, b, score),) = near_misses([new], [old], 0.85)
    assert (a, b) == (new, old) and 0.85 <= score < 1.0


def test_exact_names_other_types_and_far_names_are_not():
    new = _n("Kristen Hannah")
    assert near_misses([new], [_n("kristen hannah.")], 0.85) == [], "the same normalised name is one node already"
    assert near_misses([new], [_n("Kristin Hannah", "Org")], 0.85) == [], "another type is another thing"
    assert near_misses([new], [_n("Kristin Hannah")], 1.0) == [], "1.0: exact names only"
    assert near_misses([_n("Paris")], [_n("Peru")], 0.85) == []


def test_each_pair_once():
    a, b = _n("Hilton Garden Inn"), _n("Hilton Gardens Inn")
    assert len(near_misses([a, b], [a, b], 0.85)) == 1


def test_different_numbers_and_different_words_are_different_things():
    assert near_misses([_n("25 minutes", "Time")], [_n("20 minutes", "Time")], 0.85) == []
    assert near_misses([_n("30-60 minutes", "Time")], [_n("60 minutes", "Time")], 0.85) == []
    assert near_misses([_n("this week", "Date")], [_n("this weekend", "Date")], 0.85) == []
    assert len(near_misses([_n("Recurrent Neural Network", "Org")], [_n("Recurrent Neural Networks", "Org")], 0.85)) == 1


def test_a_short_word_is_matched_exactly():
    assert near_misses([_n("ACL", "Org")], [_n("EACL", "Org")], 0.85) == []
    assert near_misses([_n("3-day", "Date")], [_n("3 days", "Date")], 0.85) == []
