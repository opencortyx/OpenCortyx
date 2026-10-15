"""File Type Detection and Classification System for OpenCortyx."""
from __future__ import annotations

from .models import (
    Classification,
    ClassificationMode,
    DetectionConfidence,
    DetectionResult,
    DetectionSignal,
    ValidationStatus,
)
from .registry import FormatDefinition, FormatRegistry, registry
from .strategies.base import DetectorStrategy
from .strategies.extension import ExtensionDetector
from .strategies.mime import MimeDetector
from .strategies.content import ContentDetector
from .detector import FileTypeDetector

__all__ = [
    "Classification",
    "ClassificationMode",
    "DetectionConfidence",
    "DetectionResult",
    "DetectionSignal",
    "ValidationStatus",
    "FormatDefinition",
    "FormatRegistry",
    "registry",
    "DetectorStrategy",
    "ExtensionDetector",
    "MimeDetector",
    "ContentDetector",
    "FileTypeDetector",
]
