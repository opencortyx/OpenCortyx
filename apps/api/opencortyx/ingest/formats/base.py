"""Parser plugin contract.

Each FORMAT gets its own parser — not a shared "convert everything to a dict" helper. The difference
matters because formats carry semantics that a generic converter throws away:

  * XML distinguishes attributes from child elements, has namespaces, and allows mixed content
    (text and children in the same element). Flattening all of that into one dict loses the
    distinction between `<m unit="kg">5</m>` and `<m><unit>kg</unit><v>5</v></m>`.
  * JSONL is a stream of independent records; its line number IS provenance. A dict of
    `{"records": [...]}` cannot say "record 40,112 came from line 40,112".
  * CSV has a header row, column types and a row ordinal.
  * YAML has multiple documents per stream and merge keys.

So a parser returns a `ParseResult` that carries the object tree AND the format-specific structure it
recovered, and each parser is free to model its format faithfully.

Adding a format is: write a Parser subclass, register it. Nothing else in the codebase changes —
routing, decoding and the walker all resolve through the registry.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ParseError(ValueError):
    """Bytes could not be parsed by this parser. Message is user-facing and should name a location
    (line/column/offset) whenever the underlying library provides one."""


@dataclass
class Record:
    """One addressable unit of a document, with where it came from.

    Provenance is not decoration: when a 400k-line JSONL export has 12 bad records, "line 88,214" is
    the difference between a fixable report and a shrug.
    """
    data: Any
    index: int = 0
    location: str = ""              # "line 88214", "row 512", "/catalogue/entry[3]"
    kind: str = ""                  # parser-specific record type, when the format has one


@dataclass
class ParseResult:
    """What a parser recovered. `data` feeds the graph walker; the rest describes the parse."""
    data: Any                                    # the object tree
    parser: str = ""                             # which parser produced this
    records: list = field(default_factory=list)  # Record[] when the format is record-oriented
    root_name: str = ""                          # natural name for the document root
    metadata: dict = field(default_factory=dict)  # namespaces, encoding, dialect, sheet names…
    warnings: list = field(default_factory=list)  # non-fatal: skipped lines, coerced values

    @property
    def record_count(self) -> int:
        return len(self.records)

    def summary(self) -> dict:
        return {"parser": self.parser, "root": self.root_name, "records": self.record_count,
                "warnings": len(self.warnings), **self.metadata}


class Parser(ABC):
    """One format, one parser."""

    #: Stable identifier, used in configs, reports and the registry.
    name: str = ""
    #: Extensions this parser owns, without the dot.
    extensions: tuple = ()
    #: MIME types this parser owns.
    mime_types: tuple = ()
    priority: int = 50
    #: Human label for the UI.
    label: str = ""
    #: Parsers allowed to read a file whose extension this parser owns, when this one rejects it —
    #: e.g. a `.json` export that is really JSON Lines. Nothing outside this list is tried: the
    #: extension states the format, and reading `{'a': 0}` as YAML would hide a broken file.
    fallbacks: tuple = ()

    @classmethod
    def handles_extension(cls, ext: str) -> bool:
        return (ext or "").lower().lstrip(".") in cls.extensions

    @classmethod
    def sniff(cls, content: bytes) -> float:
        """Confidence in [0.0, 1.0] that this parser owns these bytes, WITHOUT fully parsing.

        Used to order candidates for an unknown extension. Cheap checks only — magic bytes, a first
        non-space character, a line shape. Returning 0.0 means "not mine"; the registry still falls
        back to trying the parse if nothing claims it.
        """
        return 0.0

    @abstractmethod
    def parse(self, content: bytes, filename: str = "") -> ParseResult:
        """Bytes → ParseResult. Raise ParseError (not a bare exception) when the content is not
        this format, so the registry can move on to the next candidate cleanly."""

    # ── shared helpers ───────────────────────────────────────────────────────
    @staticmethod
    def decode_text(content: bytes) -> str:
        """Bytes → str across the encodings real corpora actually contain.

        UTF-16 is detected by BOM first because such files are not valid UTF-8 and would otherwise
        fail with a byte-level error that tells the user nothing.
        """
        if content[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return content.decode("utf-16")
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace")

    @staticmethod
    def is_container(obj: Any) -> bool:
        """A parse only counts as structure if it produced a dict/list.

        Guards the superset problem: `yaml.safe_load("Hello world")` returns the string unchanged,
        so without this every prose file would be claimed by the YAML parser.
        """
        return isinstance(obj, (dict, list)) and bool(obj)
