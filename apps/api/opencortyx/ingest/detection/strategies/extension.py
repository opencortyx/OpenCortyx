"""Extension-based detection strategy."""
from __future__ import annotations

import os
from typing import Optional
from .base import DetectorStrategy
from ..models import Classification, DetectionConfidence, DetectionSignal


class ExtensionDetector(DetectorStrategy):
    """Detects file type purely from filename extension. Fast, zero I/O."""

    @property
    def name(self) -> str:
        return "EXTENSION"

    def detect(
        self,
        filename: str = "",
        content: Optional[bytes] = None,
        mime_type: Optional[str] = None
    ) -> Optional[DetectionSignal]:
        if not filename:
            return None

        ext = os.path.splitext(filename.lower())[1].lstrip(".")
        if not ext:
            return None

        fmt = self.registry.get_by_extension(ext)
        if fmt is not None:
            return DetectionSignal(
                detector=self.name,
                format=fmt.name,
                classification=fmt.classification,
                confidence=DetectionConfidence.HIGH,
                metadata={"extension": ext}
            )

        return DetectionSignal(
            detector=self.name,
            format=ext.upper(),
            classification=Classification.UNKNOWN,
            confidence=DetectionConfidence.NONE,
            metadata={"unrecognized_extension": ext}
        )
