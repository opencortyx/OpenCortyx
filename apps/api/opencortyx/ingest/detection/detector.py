"""FileTypeDetector orchestrator implementing progressive detection, policy, and conflict resolution."""
from __future__ import annotations

from typing import List, Optional
from .models import (
    Classification,
    ClassificationMode,
    DetectionConfidence,
    DetectionResult,
    DetectionSignal,
    ValidationStatus,
)
from .registry import FormatRegistry, registry as default_registry
from .strategies.base import DetectorStrategy
from .strategies.content import ContentDetector
from .strategies.extension import ExtensionDetector
from .strategies.mime import MimeDetector


class FileTypeDetector:
    """Orchestrates detection strategies using progressive levels and conflict resolution policy."""

    def __init__(
        self,
        format_registry: Optional[FormatRegistry] = None,
        strategies: Optional[List[DetectorStrategy]] = None,
    ):
        self.registry = format_registry or default_registry
        self.strategies = strategies or [
            ExtensionDetector(self.registry),
            MimeDetector(self.registry),
            ContentDetector(self.registry),
        ]

    def detect(
        self,
        filename: str = "",
        content: Optional[bytes] = None,
        mime_type: Optional[str] = None,
        mode: ClassificationMode = ClassificationMode.AUTO,
    ) -> DetectionResult:
        """Progressively detects the file type, resolving conflicts and applying classification modes."""
        signals: List[DetectionSignal] = []
        warnings: List[str] = []

        # ── 0. Handle Empty File ────────────────────────────────────────────────
        if content is not None and len(content) == 0:
            return DetectionResult(
                classification=Classification.UNKNOWN,
                format="EMPTY",
                confidence=DetectionConfidence.NONE,
                signals=["EMPTY_FILE"],
                validation_status=ValidationStatus.FAILED,
                warnings=["File is empty (0 bytes)"],
                details={"size": 0}
            )

        # ── Level 1: Extension Strategy ─────────────────────────────────────────
        ext_detector = next((s for s in self.strategies if s.name == "EXTENSION"), None)
        ext_signal = ext_detector.detect(filename=filename) if ext_detector else None
        if ext_signal:
            signals.append(ext_signal)

        # ── Level 2: MIME Strategy ──────────────────────────────────────────────
        mime_detector = next((s for s in self.strategies if s.name == "MIME"), None)
        mime_signal = mime_detector.detect(filename=filename, mime_type=mime_type) if mime_detector else None
        if mime_signal:
            signals.append(mime_signal)

        # ── Level 3: Content Strategy (Bounded Sniffing & Validation) ───────────
        # Run content check if content is provided AND (no extension, extension unknown, or mismatch)
        should_check_content = content is not None and (
            not ext_signal
            or ext_signal.classification == Classification.UNKNOWN
            or (mime_signal and ext_signal.format != mime_signal.format)
        )
        # Also check content if file is text/json/xml/csv to validate syntax
        if content is not None and not should_check_content:
            # Quick sanity check on magic bytes or first byte
            content_detector = next((s for s in self.strategies if s.name == "CONTENT"), None)
            if content_detector:
                content_signal = content_detector.detect(filename=filename, content=content, mime_type=mime_type)
                if content_signal:
                    signals.append(content_signal)
        elif should_check_content:
            content_detector = next((s for s in self.strategies if s.name == "CONTENT"), None)
            if content_detector:
                content_signal = content_detector.detect(filename=filename, content=content, mime_type=mime_type)
                if content_signal:
                    signals.append(content_signal)

        # ── Aggregation & Conflict Resolution ───────────────────────────────────
        result = self._aggregate_signals(signals, warnings)

        # ── Classification Mode Overrides ───────────────────────────────────────
        result = self._apply_classification_mode(result, mode)

        return result

    def _aggregate_signals(
        self,
        signals: List[DetectionSignal],
        warnings: List[str]
    ) -> DetectionResult:
        """Resolves signals into a final DetectionResult using the detection policy."""
        if not signals:
            return DetectionResult(
                classification=Classification.UNKNOWN,
                format="UNKNOWN",
                confidence=DetectionConfidence.NONE,
                signals=[],
                validation_status=ValidationStatus.SKIPPED,
                warnings=["No detection signals available"],
                details={}
            )

        signal_names = [f"{s.detector}:{s.format}({s.classification.value})" for s in signals]

        # Check for conflicts between Extension and Content
        ext_s = next((s for s in signals if s.detector == "EXTENSION"), None)
        content_s = next((s for s in signals if s.detector == "CONTENT"), None)
        mime_s = next((s for s in signals if s.detector == "MIME"), None)

        # Plain TEXT is what the content sniffer says when it recognises NO structure — every file
        # is text. That is not evidence against a specific extension: a header-only or ragged .csv
        # sniffs as TEXT and was filed as a document. The extension's own reader decides then, and
        # says precisely what is wrong if the file does not parse.
        generic_content = bool(content_s and (content_s.format or "").upper() == "TEXT"
                               and ext_s and ext_s.classification != Classification.UNSTRUCTURED)
        if ext_s and content_s and not generic_content \
                and ext_s.classification != Classification.UNKNOWN and content_s.classification != Classification.UNKNOWN:
            if ext_s.format != content_s.format:
                warnings.append(
                    f"Conflict detected: Extension indicates {ext_s.format}, but content indicates {content_s.format}."
                )
                # Content inspection of the actual data wins over the filename label
                return DetectionResult(
                    classification=content_s.classification,
                    format=content_s.format or "UNKNOWN",
                    confidence=DetectionConfidence.MEDIUM,
                    signals=signal_names,
                    validation_status=ValidationStatus.VALID,
                    warnings=warnings,
                    details={"primary_signal": "CONTENT", "conflicting_extension": ext_s.format}
                )

        if ext_s and mime_s and ext_s.classification != Classification.UNKNOWN:
            if ext_s.format != mime_s.format and mime_s.classification != Classification.UNKNOWN:
                warnings.append(
                    f"MIME mismatch: Extension indicates {ext_s.format}, but MIME indicates {mime_s.format}."
                )

        # Normal Agreement or Single Winner (prioritize known classifications over UNKNOWN)
        known_signals = [s for s in signals if s.classification != Classification.UNKNOWN]
        primary = ext_s if generic_content else content_s if (content_s and content_s.confidence == DetectionConfidence.HIGH) else (
            known_signals[0] if known_signals else (ext_s or content_s or mime_s)
        )

        # Determine confidence
        confidence = DetectionConfidence.HIGH
        if len(warnings) > 0:
            confidence = DetectionConfidence.MEDIUM
        elif primary and primary.confidence != DetectionConfidence.HIGH:
            confidence = primary.confidence

        classification = primary.classification if primary else Classification.UNKNOWN
        fmt_name = primary.format if (primary and primary.format) else "UNKNOWN"

        validation_status = ValidationStatus.VALID if (content_s and content_s.confidence == DetectionConfidence.HIGH) else ValidationStatus.SKIPPED

        return DetectionResult(
            classification=classification,
            format=fmt_name,
            confidence=confidence,
            signals=signal_names,
            validation_status=validation_status,
            warnings=warnings,
            details={"primary_signal": primary.detector if primary else "NONE"}
        )

    def _apply_classification_mode(
        self,
        result: DetectionResult,
        mode: ClassificationMode
    ) -> DetectionResult:
        """Applies forced classification override if requested."""
        if mode == ClassificationMode.AUTO:
            return result

        forced_map = {
            ClassificationMode.STRUCTURED: Classification.STRUCTURED,
            ClassificationMode.SEMI_STRUCTURED: Classification.SEMI_STRUCTURED,
            ClassificationMode.UNSTRUCTURED: Classification.UNSTRUCTURED,
        }

        target_classification = forced_map.get(mode)
        if target_classification:
            if result.classification != target_classification and result.classification != Classification.UNKNOWN:
                result.warnings.append(
                    f"Classification forced to {target_classification.value} (overriding auto-detected {result.classification.value})."
                )
            result.classification = target_classification
            result.details["forced_mode"] = mode.value

        return result
