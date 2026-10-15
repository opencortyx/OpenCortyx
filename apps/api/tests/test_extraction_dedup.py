"""Extraction deduplication.

Identical chunk text cannot yield different extractions, so the LLM should see each distinct text
once. The critical property is that this is a CACHING optimisation: the graph must come out the same
as an undeduplicated run, because phase 2 still applies the emit per chunk.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencortyx.ingest.extraction.templates import _text_key   # noqa: E402


def test_identical_text_shares_a_key():
    assert _text_key("hello world") == _text_key("hello world")


def test_whitespace_only_differences_share_a_key():
    """Log and export corpora differ in exactly this way; they are the same text to extract."""
    assert _text_key("a b c") == _text_key("a  b   c")
    assert _text_key("a b") == _text_key("a\tb")
    assert _text_key("a b") == _text_key(" a b ")
    assert _text_key("a\nb") == _text_key("a b")
    assert _text_key("line\r\nline") == _text_key("line line")


def test_different_text_does_not_collide():
    assert _text_key("Marie Curie") != _text_key("Ada Lovelace")
    assert _text_key("value: 1") != _text_key("value: 2")


def test_empty_and_none_are_handled():
    assert _text_key("") == _text_key(None)
    assert _text_key("") != _text_key("x")


def test_key_is_stable_across_calls():
    """A run must not depend on hash randomisation — PYTHONHASHSEED would make dedup nondeterministic."""
    assert _text_key("stable input") == _text_key("stable input")
    assert len(_text_key("x")) == 32           # blake2b digest_size=16 -> 32 hex chars


def test_large_text_keys_are_bounded():
    """Keying on a hash, not the text, keeps the map small when chunks are large."""
    assert len(_text_key("x" * 5_000_000)) == 32
