"""Tests for the FileTypeDetector and Strategy/Registry Architecture."""
import pytest
from opencortyx.ingest.detection import (
    FileTypeDetector,
    FormatRegistry,
    FormatDefinition,
    Classification,
    ClassificationMode,
    DetectionConfidence,
    ValidationStatus,
)
from opencortyx.ingest.sources import sync
from opencortyx.ingest.walkers import (
    StructuredWalker,
    SemiStructuredWalker,
    UnstructuredWalker,
    walker_for,
)


@pytest.fixture
def detector():
    return FileTypeDetector()


def test_extension_detection_case_insensitivity(detector):
    """Verifies that extension matching works for both lowercase and uppercase."""
    res_csv = detector.detect(filename="customers.csv")
    assert res_csv.classification == Classification.STRUCTURED
    assert res_csv.format == "CSV"
    assert res_csv.confidence == DetectionConfidence.HIGH

    res_csv_upper = detector.detect(filename="DATA.CSV")
    assert res_csv_upper.classification == Classification.STRUCTURED
    assert res_csv_upper.format == "CSV"
    assert res_csv_upper.confidence == DetectionConfidence.HIGH


def test_all_standard_extensions(detector):
    """Verifies standard extensions across Structured, Semi-Structured, and Unstructured."""
    cases = [
        ("records.json", Classification.SEMI_STRUCTURED, "JSON"),
        ("stream.jsonl", Classification.SEMI_STRUCTURED, "JSONL"),
        ("events.ndjson", Classification.SEMI_STRUCTURED, "JSONL"),
        ("feed.xml", Classification.SEMI_STRUCTURED, "XML"),
        ("config.yaml", Classification.SEMI_STRUCTURED, "YAML"),
        ("config.toml", Classification.SEMI_STRUCTURED, "TOML"),
        ("data.parquet", Classification.STRUCTURED, "PARQUET"),
        ("sheet.xlsx", Classification.STRUCTURED, "XLSX"),
        ("document.pdf", Classification.UNSTRUCTURED, "PDF"),
        ("report.docx", Classification.UNSTRUCTURED, "DOCX"),
        ("readme.txt", Classification.UNSTRUCTURED, "TEXT"),
        ("notes.md", Classification.UNSTRUCTURED, "MARKDOWN"),
    ]
    for fname, expected_cls, expected_fmt in cases:
        res = detector.detect(filename=fname)
        assert res.classification == expected_cls, f"Failed for {fname}: got {res.classification}"
        assert res.format == expected_fmt, f"Failed format for {fname}: got {res.format}"


def test_unknown_and_missing_extension(detector):
    """Verifies behavior on unknown extensions or files without extensions."""
    # Unknown extension
    res_unknown = detector.detect(filename="archive.xyz")
    assert res_unknown.classification == Classification.UNKNOWN
    assert res_unknown.format == "XYZ"

    # Missing extension without content
    res_no_ext = detector.detect(filename="customers")
    assert res_no_ext.classification == Classification.UNKNOWN

    # Missing extension with JSON content (Level 3 Content Sniffing)
    res_sniff = detector.detect(filename="customers", content=b'{"id": "1", "name": "Alice"}')
    assert res_sniff.classification == Classification.SEMI_STRUCTURED
    assert res_sniff.format == "JSON"
    assert res_sniff.confidence == DetectionConfidence.HIGH


def test_magic_byte_detection(detector):
    """Verifies binary magic byte sniffing for Parquet and PDF even with misleading names."""
    # Parquet file named .dat
    parquet_header = b"PAR1\x15\x00SomeParquetData"
    res_parquet = detector.detect(filename="data.dat", content=parquet_header)
    assert res_parquet.classification == Classification.STRUCTURED
    assert res_parquet.format == "PARQUET"
    assert res_parquet.confidence == DetectionConfidence.HIGH

    # PDF file named .bin
    pdf_header = b"%PDF-1.4\n%BinaryData"
    res_pdf = detector.detect(filename="report.bin", content=pdf_header)
    assert res_pdf.classification == Classification.UNSTRUCTURED
    assert res_pdf.format == "PDF"
    assert res_pdf.confidence == DetectionConfidence.HIGH


def test_conflict_resolution_wrong_extension(detector):
    """Verifies conflict resolution when extension says .json but content is CSV."""
    csv_content = b"id,name,age\n1,Alice,30\n2,Bob,25\n3,Charlie,35"
    res = detector.detect(filename="customers.json", content=csv_content)

    # Content detector detects delimited CSV with high confidence
    assert res.classification == Classification.STRUCTURED
    assert res.format == "CSV"
    assert len(res.warnings) > 0
    assert any("Conflict detected" in w for w in res.warnings)


def test_mime_mismatch_warning(detector):
    """Verifies warning emitted when extension and transport MIME type disagree."""
    res = detector.detect(filename="customers.json", mime_type="text/csv")
    assert res.format == "JSON"
    assert len(res.warnings) > 0
    assert any("MIME mismatch" in w for w in res.warnings)


def test_empty_file_handling(detector):
    """Verifies empty files are flagged with failed validation and unknown classification."""
    res = detector.detect(filename="empty.csv", content=b"")
    assert res.classification == Classification.UNKNOWN
    assert res.format == "EMPTY"
    assert res.validation_status == ValidationStatus.FAILED
    assert "File is empty" in res.warnings[0]


def test_large_file_bounded_reading(detector):
    """Verifies that large files are safely checked via bounded buffer rather than loaded entirely."""
    # Simulate a 10 MB payload
    header = b'{"items": [' + b'{"id": 1}, ' * 500
    large_payload = header + b"\x00" * (10 * 1024 * 1024)

    # Detector must run quickly and boundedly
    res = detector.detect(filename="huge_file.dat", content=large_payload)
    assert res.classification == Classification.SEMI_STRUCTURED
    assert res.format == "JSON"


def test_classification_modes(detector):
    """Verifies ClassificationMode AUTO vs FORCE overrides."""
    json_bytes = b'{"name": "Alice", "role": "admin"}'

    # AUTO mode
    res_auto = detector.detect(filename="data.json", content=json_bytes, mode=ClassificationMode.AUTO)
    assert res_auto.classification == Classification.SEMI_STRUCTURED

    # Force Structured
    res_force_struct = detector.detect(filename="data.json", content=json_bytes, mode=ClassificationMode.STRUCTURED)
    assert res_force_struct.classification == Classification.STRUCTURED
    assert any("Classification forced to STRUCTURED" in w for w in res_force_struct.warnings)

    # Force Unstructured
    res_force_unstruct = detector.detect(filename="data.json", content=json_bytes, mode=ClassificationMode.UNSTRUCTURED)
    assert res_force_unstruct.classification == Classification.UNSTRUCTURED
    assert any("Classification forced to UNSTRUCTURED" in w for w in res_force_unstruct.warnings)


def test_dynamic_format_registration():
    """Verifies that adding a new format requires ONLY a registry call, without altering detector code."""
    custom_reg = FormatRegistry()
    custom_reg.register(FormatDefinition(
        name="KML",
        classification=Classification.SEMI_STRUCTURED,
        extensions=("kml",),
        mime_types=("application/vnd.google-earth.kml+xml",),
        label="Keyhole Markup Language"
    ))

    custom_detector = FileTypeDetector(format_registry=custom_reg)
    res = custom_detector.detect(filename="locations.kml")
    assert res.classification == Classification.SEMI_STRUCTURED
    assert res.format == "KML"
    assert res.confidence == DetectionConfidence.HIGH


def test_detected_kind_selects_the_default_walker():
    """Each detected kind is read by its default walker."""
    assert isinstance(walker_for(sync.file_kind("data.csv")), StructuredWalker)
    assert isinstance(walker_for(sync.file_kind("data.json")), SemiStructuredWalker)
    assert isinstance(walker_for(sync.file_kind("document.pdf")), UnstructuredWalker)
