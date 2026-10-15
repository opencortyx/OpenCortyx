"""MIME-based detection strategy."""
from __future__ import annotations

from typing import Optional
from .base import DetectorStrategy
from ..models import Classification, DetectionConfidence, DetectionSignal


class MimeDetector(DetectorStrategy):
    """Detects file type from explicit transport MIME type or system mimetypes mapping."""

    @property
    def name(self) -> str:
        return "MIME"

    def detect(
        self,
        filename: str = "",
        content: Optional[bytes] = None,
        mime_type: Optional[str] = None
    ) -> Optional[DetectionSignal]:
        candidate_mime = (mime_type or "").split(";")[0].strip().lower()

        if not candidate_mime or candidate_mime == "application/octet-stream":
            return None

        fmt = self.registry.get_by_mime(candidate_mime)
        if fmt is not None:
            return DetectionSignal(
                detector=self.name,
                format=fmt.name,
                classification=fmt.classification,
                confidence=DetectionConfidence.MEDIUM,
                metadata={"mime_type": candidate_mime}
            )

        return DetectionSignal(
            detector=self.name,
            format=candidate_mime,
            classification=Classification.UNKNOWN,
            confidence=DetectionConfidence.NONE,
            metadata={"unrecognized_mime": candidate_mime}
        )
