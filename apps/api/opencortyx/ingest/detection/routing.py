"""Which pipeline a file goes to: the catalog's routes, decided by FileTypeDetector."""
from __future__ import annotations

from opencortyx.ingest.detection.detector import FileTypeDetector
from opencortyx.ingest.detection.models import ClassificationMode
from opencortyx.ingest.formats.catalog import DOCUMENT, ROUTES, SEMISTRUCTURED, STRUCTURED, extension_of

_MODES = {SEMISTRUCTURED: ClassificationMode.SEMI_STRUCTURED, STRUCTURED: ClassificationMode.STRUCTURED,
          DOCUMENT: ClassificationMode.UNSTRUCTURED}


def _detect(filename, content, override):
    mode = _MODES.get(override, ClassificationMode.AUTO)
    return FileTypeDetector().detect(filename=filename, content=content, mode=mode)


def _route(res) -> str:
    """The detector's route, or the document pipeline — the documented safe default — when it could
    not classify the file ("unknown" is not a pipeline, and every caller had to defend against it)."""
    return res.route_name if res.route_name in ROUTES else DOCUMENT


def route_for(filename: str = "", content: bytes | None = None, override: str | None = None) -> str:
    """Which ingestion pipeline should handle this file."""
    return _route(_detect(filename, content, override))


def describe(filename: str = "", content: bytes | None = None, override: str | None = None) -> dict:
    """The routing decision plus the detection details behind it."""
    res = _detect(filename, content, override)
    return {
        "route": _route(res),
        "classification": res.classification.value,
        "format": res.format,
        "confidence": res.confidence.value,
        "signals": res.signals,
        "warnings": res.warnings,
        "reason": f"{res.confidence.value} confidence: {', '.join(res.signals)}" if res.signals else "default",
        "ext": extension_of(filename) or None,
        "validation_status": res.validation_status.value,
    }
