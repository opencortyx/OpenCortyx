"""JSON Lines streams → one record per line (.jsonl .ndjson .ldjson).

A separate reader from JSON documents because it is a different data model: an unbounded STREAM of
independent records whose line numbers are their identity.
"""
from __future__ import annotations

import json

from ..base import Parser, ParseError, ParseResult, Record
from .json_reader import _loads


class JsonLinesParser(Parser):
    name = "jsonl"
    label = "JSON Lines"
    extensions = ("jsonl", "ndjson", "ldjson")
    mime_types = ("application/x-ndjson", "application/jsonl")
    priority = 92          # above JSON: a .json file that is really NDJSON should parse, not fail
    fallbacks = ("json",)

    @classmethod
    def sniff(cls, content: bytes) -> float:
        lines = [ln for ln in content[:8192].split(b"\n") if ln.strip()]
        if len(lines) < 2:
            return 0.0
        ok = 0
        for ln in lines[:-1]:                      # the last line may be cut by the sample window
            try:
                json.loads(ln)
                ok += 1
            except Exception:                      # noqa: BLE001
                return 0.0
        return 0.9 if ok >= 2 else 0.0

    def parse(self, content: bytes, filename: str = "") -> ParseResult:
        """Each line is an independent record; a bad line is reported, not fatal.

        Real NDJSON exports routinely end mid-line or carry a few corrupt rows. Failing the whole
        file over one of 400,000 lines throws away good data; silently dropping them hides a broken
        producer. So: parse what parses, and report exactly which lines did not.
        """
        records, warnings = [], []
        for lineno, raw in enumerate(self.decode_text(content).splitlines(), start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = _loads(raw)
            except (ValueError, RecursionError) as e:
                if len(warnings) < 50:             # bounded: a wholly corrupt file must not OOM
                    warnings.append(f"line {lineno}: {getattr(e, 'msg', e)}")
                continue
            records.append(Record(data=obj, index=len(records), location=f"line {lineno}"))
        if not records:
            raise ParseError(
                f"no valid JSON lines ({len(warnings)} malformed"
                + (f"; first: {warnings[0]}" if warnings else "") + ")")
        return ParseResult(
            data={"records": [r.data for r in records]},
            parser=self.name, records=records, root_name="records",
            metadata={"lines_parsed": len(records), "lines_malformed": len(warnings)},
            warnings=warnings)
