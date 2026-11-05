"""The Mapper's view of a document: how its layout was read, from its rows in the data store."""
from types import SimpleNamespace

from opencortyx.ingest.unstructured import reading


def _row(i, kind, text, path="", level=None):
    return {"ordinal": i, "element_type": kind, "level": level, "section_path": path, "text": text}


def test_a_documents_reading_lists_its_sections_and_its_passages():
    rows = [
        _row(2, "Text", "IT setup on day one. A buddy from the same team.", "Onboarding"),
        _row(0, "Title", "Onboarding", "Onboarding", 0),
        _row(3, "Title", "Benefits", "Benefits", 0),
        _row(4, "Text", "x" * 790, "Benefits"),                 # a piece of a split paragraph
        _row(5, "Text", "y" * 300, "Benefits"),
        _row(6, "TableEntity", "| a | b |", "Benefits"),
        _row(1, "Text", "Read me first.", ""),
    ]
    r = reading.summarize(rows, split_at=800)
    assert [(s["title"], s["items"]) for s in r["sections"]] == [("Onboarding", 1), ("Benefits", 3)]
    assert r["outside_sections"] == 1
    assert r["passages"] == 4, "prose only: headings and tables are not split"
    assert r["longest"] == 790 and r["split_at"] == 800
    assert r["samples"][0] == "Read me first.", "in reading order"
    assert {e["type"]: e["count"] for e in r["elements"]} == {"Text": 4, "Title": 2, "TableEntity": 1}


def test_an_empty_document_reads_as_nothing():
    r = reading.summarize([], split_at=800)
    assert r["passages"] == 0 and r["longest"] == 0 and r["sections"] == [] and r["average"] == 0


def test_a_document_is_read_again_only_from_the_file_the_server_still_keeps(tmp_path, monkeypatch):
    from opencortyx.core import uploads
    monkeypatch.setattr(uploads, "root", lambda: tmp_path)
    ref = uploads.put(b"# Onboarding\\n\\nNew hires complete IT setup on day one.")
    kept = SimpleNamespace(metadata_={"checksum": ref[len(uploads.PREFIX):]})
    assert reading.upload_ref(kept) == ref
    assert reading.upload_ref(SimpleNamespace(metadata_={"checksum": "0" * 64})) is None
    assert reading.upload_ref(SimpleNamespace(metadata_={})) is None
