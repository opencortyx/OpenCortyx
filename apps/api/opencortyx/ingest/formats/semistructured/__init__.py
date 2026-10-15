"""Semi-structured readers: bytes → the JSON tree the walker reads, one module per format.

JSON and JSON Lines are read natively; XML, YAML, TOML and INI are TRANSLATED into the same tree
(`*_to_json.py`), so everything downstream sees one data model.

The registry resolves bytes to the parser that owns them.

Resolution order, most reliable first:

  1. **Extension.** What the user called the file is the strongest declaration of intent.
  2. **MIME type**, when the transport supplied a real one.
  3. **Sniff confidence.** Each parser rates the bytes cheaply; candidates are tried in
     confidence order, then by priority.
  4. **Exhaustive attempt.** Everything left, highest priority first.

Step 4 is what makes an unrecognised extension a non-event: a vendor export named `.acmedata` that
happens to be XML is parsed BY THE XML PARSER, with XML semantics — not by a generic fallback that
flattens it. There is no "universal" parser; there is a registry of real ones and an ordered search.

Registering a format:

    from .parsers import register
    register(MyFormatParser)

Nothing else changes — routing, ingestion and the walker all resolve through here.
"""
from __future__ import annotations

from ..base import Parser, ParseError, ParseResult, Record       # noqa: F401
from .json_reader import JsonParser
from .jsonl_reader import JsonLinesParser
from .xml_to_json import XmlParser
from .yaml_to_json import YamlParser
from .toml_to_json import TomlParser
from .ini_to_json import IniParser

#: name → parser instance. Instances, not classes: a parser may hold compiled patterns or a
#: configured dialect, and constructing one per call would waste that.
_REGISTRY: dict = {}


def register(parser_cls) -> None:
    """Add a parser. Re-registering the same name replaces it, so a deployment can override a
    built-in parser without editing this file."""
    if not getattr(parser_cls, "name", ""):
        raise ValueError(f"{parser_cls!r} must define a `name`")
    _REGISTRY[parser_cls.name] = parser_cls()


for _p in (JsonParser, JsonLinesParser, XmlParser, YamlParser, TomlParser, IniParser):
    register(_p)


def all_parsers() -> list:
    return sorted(_REGISTRY.values(), key=lambda p: -p.priority)


def get(name: str):
    return _REGISTRY.get((name or "").lower())


def extensions() -> dict:
    """{ext: parser_name} — the declared ownership map, for routing and the UI."""
    out: dict = {}
    for p in all_parsers():                       # highest priority claims a shared extension
        for ext in p.extensions:
            out.setdefault(ext, p.name)
    return out


def for_extension(ext: str):
    name = extensions().get((ext or "").lower().lstrip("."))
    return get(name) if name else None


def for_mime(mime: str):
    m = (mime or "").split(";")[0].strip().lower()
    if not m:
        return None
    for p in all_parsers():
        if m in p.mime_types:
            return p
    return None


def candidates(content: bytes, filename: str = "", mime: str = "") -> list:
    """Parsers to try, best first — declared owner, then MIME, then sniff, then everything else."""
    import os
    ordered, seen = [], set()

    def _add(p):
        if p is not None and p.name not in seen:
            seen.add(p.name)
            ordered.append(p)

    _add(for_extension(os.path.splitext(filename or "")[1]))
    _add(for_mime(mime))
    scored = [(p.sniff(content), p) for p in all_parsers()]
    for score, p in sorted(scored, key=lambda sp: (-sp[0], -sp[1].priority)):
        if score > 0:
            _add(p)
    for p in all_parsers():                       # exhaustive tail
        _add(p)
    return ordered


def parse(content: bytes, filename: str = "", mime: str = "",
          parser: str | None = None) -> ParseResult:
    """Parse with the owning parser, falling back through the ordered candidates.

    `parser` forces one by name and disables the fallback — the caller has stated the format and a
    silent switch to another would hide their mistake.
    """
    if not content:
        raise ParseError("file is empty")
    if parser:
        p = get(parser)
        if p is None:
            raise ParseError(f"unknown parser {parser!r} (have {sorted(_REGISTRY)})")
        return p.parse(content, filename)

    import os
    owner = for_extension(os.path.splitext(filename or "")[1])
    order = [owner, *filter(None, map(get, owner.fallbacks))] if owner else candidates(content, filename, mime)
    tried, errors = [], []
    for p in order:
        try:
            result = p.parse(content, filename)
        except ParseError as e:
            tried.append(p.name)
            errors.append(f"{p.name}: {e}")
            continue
        except Exception as e:                    # noqa: BLE001 — a parser bug must not sink the run
            tried.append(p.name)
            errors.append(f"{p.name}: unexpected {type(e).__name__}: {str(e)[:120]}")
            continue
        if owner is not None and p is not owner and result.warnings:
            # A fallback must read the whole file. One that skips unreadable parts is guessing —
            # a broken JSON array read "as JSON Lines" keeps whichever lines happen to parse.
            tried.append(p.name)
            errors.append(f"{p.name}: {len(result.warnings)} unreadable part(s), first: {result.warnings[0]}")
            continue
        if result.parser != p.name:               # keep attribution honest
            result.parser = p.name
        if len(tried) and result.parser != (tried[0] if tried else ""):
            result.metadata.setdefault("fallback_from", tried[0] if tried else None)
        return result
    raise ParseError("no parser matched (" + "; ".join(errors[:5]) + ")")


def describe() -> list:
    """Registry contents, for the UI and for tests that assert coverage."""
    return [{"name": p.name, "label": p.label or p.name, "extensions": sorted(p.extensions),
             "mime_types": sorted(p.mime_types), "priority": p.priority}
            for p in all_parsers()]
