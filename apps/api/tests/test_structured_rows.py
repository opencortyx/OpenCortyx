"""Row-sampling rules for the structured pipeline.

`max_rows` is the answer to "how much of this table should become nodes?" — a number, or every row.
The parser is deliberately forgiving because the value arrives from a UI field, a pipeline card, and
an API form, and each spells "everything" differently.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencortyx.ingest.structured.ingest import _parse_max_rows   # noqa: E402


@pytest.mark.parametrize("value", [None, "", "all", "ALL", "full", "*", "everything",
                                   "unlimited", "none", 0, "0", -5, True, False])
def test_every_spelling_of_everything_means_no_cap(value):
    assert _parse_max_rows(value) == 0


@pytest.mark.parametrize("value,expected", [
    (5, 5), ("5", 5), (" 5 ", 5), ("5 rows", 5), ("1000", 1000),
    ("1_000", 1000), ("1,000", 1000), (2.9, 2), (10**7, 10**7),
])
def test_numbers_parse(value, expected):
    assert _parse_max_rows(value) == expected


def test_unparseable_means_everything_not_zero():
    """Ingesting nothing is the one outcome the author certainly did not ask for."""
    assert _parse_max_rows("banana") == 0
    assert _parse_max_rows({"weird": 1}) == 0


def test_true_is_not_one_row():
    """`True` from a checkbox must mean 'all', not 'a single row'."""
    assert _parse_max_rows(True) == 0
