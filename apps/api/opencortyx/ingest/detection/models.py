"""Detection models and enums for OpenCortyx File Type Detection System."""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class Classification(str, Enum):
    """Core data classifications."""
    STRUCTURED = "STRUCTURED"
    SEMI_STRUCTURED = "SEMI_STRUCTURED"
    UNSTRUCTURED = "UNSTRUCTURED"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


class ClassificationMode(str, Enum):
    """User-specified classification override mode."""
    AUTO = "auto"
    STRUCTURED = "structured"
    SEMI_STRUCTURED = "semistructured"
    UNSTRUCTURED = "unstructured"


class DetectionConfidence(str, Enum):
    """Confidence level of a detection signal or final result."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


class ValidationStatus(str, Enum):
    """Status of content structure/syntax validation."""
    VALID = "VALID"
    INVALID = "INVALID"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class DetectionSignal(BaseModel):
    """Individual signal emitted by a detection strategy."""
    detector: str
    format: Optional[str] = None
    classification: Classification = Classification.UNKNOWN
    confidence: DetectionConfidence = DetectionConfidence.NONE
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DetectionResult(BaseModel):
    """Aggregated and validated file type detection result."""
    classification: Classification
    format: str = "UNKNOWN"
    confidence: DetectionConfidence = DetectionConfidence.NONE
    signals: List[str] = Field(default_factory=list)
    validation_status: ValidationStatus = ValidationStatus.SKIPPED
    warnings: List[str] = Field(default_factory=list)
    details: Dict[str, Any] = Field(default_factory=dict)

    @property
    def is_supported(self) -> bool:
        return self.classification not in (Classification.UNKNOWN, Classification.UNSUPPORTED)

    @property
    def route_name(self) -> str:
        """Translates classification into the OpenCortyx route name convention."""
        if self.classification == Classification.STRUCTURED:
            return "structured"
        if self.classification == Classification.SEMI_STRUCTURED:
            return "semistructured"
        if self.classification == Classification.UNSTRUCTURED:
            return "document"
        return "unknown"
