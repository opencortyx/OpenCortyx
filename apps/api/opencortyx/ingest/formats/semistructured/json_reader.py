"""JSON documents → the JSON tree the walker reads (.json .fhir .har .geojson .ipynb .jsonc).

JSON Lines has its own reader (`jsonl_reader.py`).

Separate parsers, not one with a flag, because they are different data models. A JSON document is a
single tree with one root. A JSONL file is an unbounded STREAM of independent records whose line
numbers are their identity — conflating them loses the provenance that makes a bad export
diagnosable.
"""
from __future__ import annotations

import json
import re

from ..base import Parser, ParseError, ParseResult, Record

_SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F][0-9a-fA-F]{2}")


def _reject_constant(name: str):
    """`NaN`, `Infinity` and `-Infinity` are not JSON (RFC 8259), whatever Python's reader allows."""
    raise ValueError(f"{name} is not a JSON value")


def _loads(text: str):
    """Strict JSON: no NaN/Infinity, and no lone UTF-16 surrogate, which no store can hold."""
    data = json.loads(text, parse_constant=_reject_constant)
    if _SURROGATE_ESCAPE.search(text):
        try:
            json.dumps(data, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("a string holds a lone UTF-16 surrogate") from None
    return data


class JsonParser(Parser):
    name = "json"
    label = "JSON"
    extensions = ("json", "fhir", "har", "geojson", "ipynb", "jsonc")
    mime_types = ("application/json", "text/json")
    # Above YAML: YAML is a superset of JSON, so a JSON document must be claimed here first.
    priority = 90
    fallbacks = ("jsonl",)

    @classmethod
    def sniff(cls, content: bytes) -> float:
        head = content[:512].lstrip()
        if not head:
            return 0.0
        if head[:1] in (b"{", b"["):
            # A single line starting with { could equally be JSONL; the line count decides.
            body = content[:8192].strip()
            if body.count(b"\n") >= 2 and body.split(b"\n")[0].rstrip().endswith((b"}", b"]")):
                return 0.4          # looks more like a record stream — let JSONL outrank us
            return 0.95
        return 0.0

    def parse(self, content: bytes, filename: str = "") -> ParseResult:
        try:
            data = _loads(self.decode_text(content))
        except json.JSONDecodeError as e:
            raise ParseError(f"invalid JSON at line {e.lineno} column {e.colno}: {e.msg}") from e
        except (ValueError, RecursionError) as e:
            raise ParseError(f"invalid JSON: {e}") from e
        if not self.is_container(data):
            raise ParseError("JSON parsed to a bare scalar, not an object or array")

        # A top-level array IS a record collection; name it so the walker creates siblings under a
        # parent rather than one node holding a list attribute.
        records, root = [], "document"
        if isinstance(data, list):
            records = [Record(data=r, index=i, location=f"[{i}]") for i, r in enumerate(data)]
            data = {"records": data}
            root = "records"
        return ParseResult(data=data, parser=self.name, records=records, root_name=root,
                           metadata={"top_level": "array" if records else "object"})
