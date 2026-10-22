"""Content-based detection strategy with bounded byte inspection and validation."""
from __future__ import annotations

import json
from typing import Optional
from .base import DetectorStrategy
from ..models import Classification, DetectionConfidence, DetectionSignal

# Strictly bound inspection to avoid memory issues on GB+ files
DEFAULT_SAMPLE_SIZE = 4096


class ContentDetector(DetectorStrategy):
    """Detects file type via bounded byte sniffing (magic bytes, syntax inspection)."""

    def __init__(self, registry, sample_size: int = DEFAULT_SAMPLE_SIZE):
        super().__init__(registry)
        self.sample_size = sample_size

    @property
    def name(self) -> str:
        return "CONTENT"

    def detect(
        self,
        filename: str = "",
        content: Optional[bytes] = None,
        mime_type: Optional[str] = None
    ) -> Optional[DetectionSignal]:
        if not content:
            return None

        # Sample only the head for scalability
        sample = content[:self.sample_size].lstrip()
        if not sample:
            return None

        # ── 1. Magic Bytes Check ────────────────────────────────────────────────
        for fmt in self.registry.all_formats():
            for magic in fmt.magic_bytes:
                if sample.startswith(magic) or (magic == b"ORC\x00" and content[-3:] == b"ORC"):
                    return DetectionSignal(
                        detector=self.name,
                        format=fmt.name,
                        classification=fmt.classification,
                        confidence=DetectionConfidence.HIGH,
                        metadata={"magic_matched": True, "magic": str(magic)}
                    )

        # Zip-based Office formats (DOCX, XLSX, PPTX all share PK\x03\x04)
        if sample.startswith(b"PK\x03\x04"):
            # If filename gives extension hint, confirm zip container
            ext = (filename or "").lower().rsplit(".", 1)[-1]
            fmt = self.registry.get_by_extension(ext)
            if fmt and fmt.name in ("DOCX", "XLSX", "PPTX"):
                return DetectionSignal(
                    detector=self.name,
                    format=fmt.name,
                    classification=fmt.classification,
                    confidence=DetectionConfidence.HIGH,
                    metadata={"container": "zip_office"}
                )

        # ── 2. JSON & NDJSON Inspection ─────────────────────────────────────────
        if sample[:1] in (b"{", b"["):
            # Check if it parses as standard JSON (if entire content fits or try sample)
            try:
                json.loads(content if len(content) <= 1024 * 1024 else sample.decode("utf-8", errors="ignore"))
                return DetectionSignal(
                    detector=self.name,
                    format="JSON",
                    classification=Classification.SEMI_STRUCTURED,
                    confidence=DetectionConfidence.HIGH,
                    metadata={"syntax": "json"}
                )
            except Exception:
                # If it didn't parse fully, starts with { or [ is still a medium signal for JSON
                return DetectionSignal(
                    detector=self.name,
                    format="JSON",
                    classification=Classification.SEMI_STRUCTURED,
                    confidence=DetectionConfidence.MEDIUM,
                    metadata={"syntax": "json_prefix"}
                )

        # Check for NDJSON / JSONL: one object or array per line, every sampled line. Counting any line
        # `json.loads` accepts called a Python file JSONL: a bare number, "string", true or null is
        # valid JSON, and two of them in a source file's first lines are common.
        lines = [ln.strip() for ln in sample.split(b"\n") if ln.strip()]
        if len(lines) >= 2:
            valid_json_lines = 0
            for ln in lines[:4]:
                try:
                    if isinstance(json.loads(ln), (dict, list)):
                        valid_json_lines += 1
                except Exception:
                    pass
            if valid_json_lines >= 2 and valid_json_lines == len(lines[:4]):
                return DetectionSignal(
                    detector=self.name,
                    format="JSONL",
                    classification=Classification.SEMI_STRUCTURED,
                    confidence=DetectionConfidence.HIGH,
                    metadata={"syntax": "ndjson"}
                )

        # ── 3. XML vs HTML Inspection ───────────────────────────────────────────
        if sample[:1] == b"<":
            sample_low = sample[:512].lower()
            if b"<html" in sample_low or b"<!doctype html" in sample_low:
                return DetectionSignal(
                    detector=self.name,
                    format="HTML",
                    classification=Classification.UNSTRUCTURED,
                    confidence=DetectionConfidence.HIGH,
                    metadata={"syntax": "html"}
                )
            return DetectionSignal(
                detector=self.name,
                format="XML",
                classification=Classification.SEMI_STRUCTURED,
                confidence=DetectionConfidence.HIGH,
                metadata={"syntax": "xml"}
            )

        # ── 4. Delimited Text Inspection (CSV / TSV) ────────────────────────────
        if self._looks_delimited(sample):
            sep = "\t" if sample.count(b"\t") > sample.count(b",") else ","
            fmt_name = "TSV" if sep == "\t" else "CSV"
            return DetectionSignal(
                detector=self.name,
                format=fmt_name,
                classification=Classification.STRUCTURED,
                confidence=DetectionConfidence.HIGH,
                metadata={"delimiter": sep}
            )

        # ── 5. Registered Parser Sniffing (YAML / TOML / INI) ───────────────────
        try:
            from opencortyx.ingest.formats import semistructured as _parsers
            res = _parsers.parse(sample)
            # Only the formats no check above covers. JSON, JSON Lines and XML were decided above by
            # strict rules; the parsers here are lenient (the JSON Lines reader skips lines it cannot
            # read), so two numbers in a source file's first lines were enough to call it JSON Lines.
            if res and res.parser in ("yaml", "toml", "ini"):
                return DetectionSignal(
                    detector=self.name,
                    format=res.parser.upper(),
                    classification=Classification.SEMI_STRUCTURED,
                    confidence=DetectionConfidence.HIGH,
                    metadata={"parser": res.parser}
                )
        except Exception:
            pass

        # ── 6. Plain Text Fallback ──────────────────────────────────────────────
        try:
            sample.decode("utf-8")
            return DetectionSignal(
                detector=self.name,
                format="TEXT",
                classification=Classification.UNSTRUCTURED,
                confidence=DetectionConfidence.LOW,
                metadata={"encoding": "utf-8"}
            )
        except UnicodeDecodeError:
            return None

    def _looks_delimited(self, sample: bytes, min_lines: int = 2) -> bool:
        """Determines whether content has consistent delimiter counts across lines."""
        try:
            text = sample.decode("utf-8", errors="ignore")
        except Exception:
            return False

        lines = [ln for ln in text.splitlines() if ln.strip()][:6]
        if len(lines) < min_lines:
            return False

        for delim in (",", "\t", ";", "|"):
            counts = {ln.count(delim) for ln in lines}
            # All lines have the exact same count and count >= 1
            if len(counts) == 1 and counts.pop() >= 1:
                return True
        return False
