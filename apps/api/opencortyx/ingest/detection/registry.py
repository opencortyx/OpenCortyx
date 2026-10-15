"""Centralized Format Registry for OpenCortyx.

Maintains format definitions across Structured, Semi-Structured, and Unstructured data.
Allows registering new formats cleanly without modifying detection or walker code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from opencortyx.ingest.formats.catalog import FAMILIES, SEMISTRUCTURED, STRUCTURED

from .models import Classification


@dataclass(frozen=True)
class FormatDefinition:
    """Specification for a supported file format."""
    name: str                                  # Canonical format name, e.g. "CSV", "JSON", "PDF"
    classification: Classification             # STRUCTURED | SEMI_STRUCTURED | UNSTRUCTURED
    extensions: tuple[str, ...]                # Lowercase extensions without leading dot: ("csv",)
    mime_types: tuple[str, ...] = ()           # Known MIME types: ("text/csv",)
    magic_bytes: tuple[bytes, ...] = ()        # Byte signatures (e.g. b"PAR1", b"%PDF")
    decoder: str = ""                          # Semi-structured parser name if applicable
    label: str = ""                            # UI-friendly label
    validator: Optional[Callable[[bytes], bool]] = None # Bounded syntax validator

    @property
    def display_name(self) -> str:
        return self.label or self.name


class FormatRegistry:
    """Central registry of format definitions and fast lookup indexes."""

    def __init__(self):
        self._formats: Dict[str, FormatDefinition] = {}
        self._ext_index: Dict[str, FormatDefinition] = {}
        self._mime_index: Dict[str, FormatDefinition] = {}

    def register(self, fmt: FormatDefinition) -> None:
        """Register a format definition and update lookup indexes."""
        self._formats[fmt.name.upper()] = fmt
        for ext in fmt.extensions:
            self._ext_index[ext.lower().lstrip(".")] = fmt
        for mime in fmt.mime_types:
            self._mime_index[mime.lower().strip()] = fmt

    def unregister(self, name: str) -> None:
        """Remove a format from the registry."""
        name_up = name.upper()
        if name_up in self._formats:
            fmt = self._formats.pop(name_up)
            for ext in fmt.extensions:
                self._ext_index.pop(ext.lower().lstrip("."), None)
            for mime in fmt.mime_types:
                self._mime_index.pop(mime.lower().strip(), None)

    def get(self, name: str) -> Optional[FormatDefinition]:
        return self._formats.get((name or "").upper())

    def get_by_extension(self, ext: str) -> Optional[FormatDefinition]:
        clean_ext = (ext or "").lower().lstrip(".")
        return self._ext_index.get(clean_ext)

    def get_by_mime(self, mime: str) -> Optional[FormatDefinition]:
        clean_mime = (mime or "").split(";")[0].strip().lower()
        return self._mime_index.get(clean_mime)

    def all_formats(self) -> List[FormatDefinition]:
        return list(self._formats.values())

    def by_classification(self, classification: Classification) -> List[FormatDefinition]:
        return [f for f in self._formats.values() if f.classification == classification]


# Global Default Registry
registry = FormatRegistry()


def _init_default_formats(reg: FormatRegistry) -> None:
    """Every family in the format catalog — the one place formats are declared."""
    kind = {STRUCTURED: Classification.STRUCTURED, SEMISTRUCTURED: Classification.SEMI_STRUCTURED}
    for f in FAMILIES:
        reg.register(FormatDefinition(
            name=f.name, classification=kind.get(f.route, Classification.UNSTRUCTURED),
            extensions=f.extensions, mime_types=f.mime_types, magic_bytes=f.magic_bytes,
            decoder=f.decoder, label=f.label))


_init_default_formats(registry)
