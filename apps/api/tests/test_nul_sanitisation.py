"""NUL-byte sanitisation.

Postgres TEXT/JSONB cannot represent U+0000; asyncpg raises `UntranslatableCharacter`, aborting the
transaction and taking a whole document's extraction with it while naming neither the field nor the
byte. NULs turn up in truncated exports, UTF-16 read as UTF-8, and files mislabelled as text.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencortyx.core.substrate.repo import _strip_nuls   # noqa: E402

NUL = chr(0)


def test_strips_from_strings():
    assert _strip_nuls("a" + NUL + "b") == "ab"
    assert _strip_nuls(NUL) == ""


def test_strips_from_dict_keys_and_values():
    """A NUL in a KEY fails exactly the same way as one in a value."""
    assert _strip_nuls({"k" + NUL: "v" + NUL}) == {"k": "v"}


def test_recurses_into_nested_structures():
    """The offending byte is usually deep in a property, not in the label."""
    assert _strip_nuls({"a": [{"b": "x" + NUL + "y"}]}) == {"a": [{"b": "xy"}]}
    assert _strip_nuls([[["deep" + NUL]]]) == [[["deep"]]]


def test_leaves_clean_values_untouched():
    clean = {"text": "no nulls here", "n": 42, "ok": True, "none": None}
    assert _strip_nuls(clean) == clean


def test_preserves_non_string_types():
    assert _strip_nuls(42) == 42
    assert _strip_nuls(3.5) == 3.5
    assert _strip_nuls(True) is True
    assert _strip_nuls(None) is None


def test_preserves_tuple_type():
    out = _strip_nuls(("a" + NUL, "b"))
    assert out == ("a", "b") and isinstance(out, tuple)


def test_source_file_itself_contains_no_nul():
    """Guards the escaping mishap that produced a literal NUL in this module's source."""
    src = (Path(__file__).resolve().parent.parent
           / "opencortyx/core/substrate/repo.py").read_bytes()
    assert b"\x00" not in src
