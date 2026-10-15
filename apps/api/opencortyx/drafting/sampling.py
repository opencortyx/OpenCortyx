"""Bytes → `Sample`: the one place a file is looked at on behalf of a drafter.

Every drafter reads a `Sample` and nothing else. That is the whole reason this module exists: a
drafter that opened files itself would each grow its own decoding, its own size cap and its own
idea of what a record is — and then disagree with ingestion about all three, which is how a draft
comes to describe a file that ingestion reads differently.

So the decoding is not reimplemented here. The route comes from `routing.describe()` (the same
decision the upload endpoint makes), records come from the `parsers` registry, tables from
`tabular.preview_file`, prose from `parsing.parse_content` — the same four components that run at
ingest. This module only bounds them and normalises the result.

BOUNDED BY CONSTRUCTION. Sampling runs inside a web request, so a 2 GB JSONL must cost what a 2 KB
one costs: the content is truncated to `SAMPLE_BYTES` before anything parses it, and the caps on
records/rows/characters apply after. `Sample.truncated` records that this happened, so a drafter
can lower its own confidence rather than generalise silently from the first page.
"""
from __future__ import annotations

import json

from opencortyx.drafting.models import Sample
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

#: How much of a file is read at all. Large enough that a record-per-line file yields many whole
#: records; small enough that a hundred concurrent drafts cannot exhaust the API container.
SAMPLE_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 20
MAX_ROWS = 20
MAX_TEXT_CHARS = 8_000
#: Below this, "the sample was too thin to measure" — drafters drop to LOW confidence.
THIN_RECORDS = 3


def build_sample(content: bytes, filename: str = "", *, mime: str = "",
                 route_override: str | None = None) -> Sample:
    """Read a bounded prefix of `content` into the facet its route calls for.

    Never raises: an unparseable file still produces a Sample — an empty one carrying the reason in
    `warnings`. A drafter can then say "I cannot read this" with the parser's own words, which is
    a better answer than a 500 for someone who has just dragged in a file.
    """
    from opencortyx.ingest.formats import catalog as formats
    from opencortyx.ingest.detection import routing

    head = content[:SAMPLE_BYTES]
    truncated = len(content) > len(head)
    decision = routing.describe(filename, head, override=route_override)
    sample = Sample(filename=filename or "upload", route=decision.get("route") or "",
                    format=decision.get("format") or "", truncated=truncated,
                    warnings=list(decision.get("warnings") or []))

    if not head:
        sample.warnings.append("file is empty")
        return sample

    try:
        if sample.route == formats.SEMISTRUCTURED:
            _fill_records(sample, head, filename, mime, truncated)
        elif sample.route == formats.STRUCTURED:
            _fill_table(sample, content, filename)
        else:
            from opencortyx.ingest.formats.unstructured.documents import WHOLE_FILE_EXTENSIONS
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            whole = ext in WHOLE_FILE_EXTENSIONS
            if whole:
                sample.truncated = False       # read in full; _fill_text flags a text cap itself
            _fill_text(sample, content if whole else head, filename, mime)
            sample.document = content if whole else head
    except Exception as exc:                        # noqa: BLE001 — a Sample always comes back
        log.warning("authoring.sample_failed", filename=filename, route=sample.route,
                    error=f"{type(exc).__name__}: {exc}")
        sample.warnings.append(f"could not read the file: {type(exc).__name__}: {exc}")
    return sample


def _fill_records(sample: Sample, head: bytes, filename: str, mime: str, truncated: bool) -> None:
    """Top-level records, seen the way `runner` sees them.

    A truncated JSON document is invalid JSON, so the whole-document parse fails on exactly the
    large files sampling exists for. The line fallback handles the record-per-line formats where
    a prefix IS still parseable; for the rest, an honest warning beats a guess.
    """
    from opencortyx.ingest.formats import semistructured as parsers
    from opencortyx.ingest.semistructured.runner import _unwrap_record_collection

    try:
        result = parsers.parse(head, filename=filename, mime=mime)
    except Exception:                               # noqa: BLE001 — fall through to the line reader
        if truncated and _fill_records_by_line(sample, head):
            return
        raise

    data = _unwrap_record_collection(result.data)
    records = data if isinstance(data, list) else [data]
    sample.records = [r for r in records[:MAX_RECORDS] if r is not None]
    sample.warnings.extend(str(w)[:200] for w in (result.warnings or [])[:3])
    if len(records) > MAX_RECORDS:
        sample.truncated = True


def _fill_records_by_line(sample: Sample, head: bytes) -> bool:
    """JSONL-shaped rescue for a truncated file: whole lines parse even when the prefix does not.

    The last line is dropped unconditionally — in a truncated prefix it is a partial object, and a
    half-read record would contribute phantom keys to every structure the drafter derives.
    """
    lines = head.decode("utf-8", errors="ignore").splitlines()[:-1]
    out = []
    for line in lines:
        line = line.strip().rstrip(",")
        if not line or line in ("[", "]", "{", "}"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= MAX_RECORDS:
            break
    if not out:
        return False
    sample.records = out
    sample.truncated = True
    sample.warnings.append("file too large to parse whole; sampled complete lines from the start")
    return True


def _fill_table(sample: Sample, content: bytes, filename: str) -> None:
    """Columns and rows via StructuredWalker introspection (DuckDB)."""
    from opencortyx.ingest.walkers.structured import StructuredWalker

    preview = StructuredWalker.introspect(content, filename=filename, sample_n=MAX_ROWS)
    columns = preview.get("columns") or []
    sample.columns = [str(c.get("name")) for c in columns]
    rows = preview.get("sample_rows") or []
    sample.rows = [[r.get(name) for name in sample.columns] for r in rows[:MAX_ROWS]]
    # The measured column facts the drafter uses — DuckDB typed every column and guessed its role.
    sample.records = [dict(c) for c in columns]
    total = preview.get("row_count")
    if isinstance(total, int) and total > len(sample.rows):
        sample.truncated = True


def _fill_text(sample: Sample, head: bytes, filename: str, mime: str) -> None:
    from opencortyx.ingest.formats.unstructured.documents import UnreadableDocument, parse_content

    try:
        parsed = parse_content(head, mime_type=mime or "text/plain", filename=filename)
    except UnreadableDocument as e:
        sample.warnings.append(str(e))
        return
    text = str(parsed.get("raw_text") or "")
    sample.text = text[:MAX_TEXT_CHARS]
    if len(text) > MAX_TEXT_CHARS:
        sample.truncated = True
    if not sample.text.strip():
        sample.warnings.append("no readable text was extracted from this file")


__all__ = ["MAX_RECORDS", "MAX_ROWS", "MAX_TEXT_CHARS", "SAMPLE_BYTES", "THIN_RECORDS",
           "build_sample"]
