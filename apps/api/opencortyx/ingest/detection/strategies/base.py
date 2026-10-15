"""Base detector strategy interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional
from ..models import DetectionSignal
from ..registry import FormatRegistry


class DetectorStrategy(ABC):
    """Common interface for file type detection strategies."""

    def __init__(self, registry: FormatRegistry):
        self.registry = registry

    @property
    @abstractmethod
    def name(self) -> str:
        """Identifier for the strategy (e.g. 'EXTENSION', 'MIME', 'CONTENT')."""
        pass

    @abstractmethod
    def detect(
        self,
        filename: str = "",
        content: Optional[bytes] = None,
        mime_type: Optional[str] = None
    ) -> Optional[DetectionSignal]:
        """Inspect file attributes or bytes and return a DetectionSignal, or None if inconclusive."""
        pass
