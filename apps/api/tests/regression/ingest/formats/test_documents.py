"""Document formats × variants through text extraction (FMTD)."""
from __future__ import annotations

import hashlib

import pytest

from tests.regression.framework import data as D
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression


# ── FMTD: documents ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", scenarios("FMTD"), ids=str)
def test_document_format(scenario, rec):
    from opencortyx.ingest.formats.unstructured.documents import UnreadableDocument, parse_content
    fmt, variant = scenario.params["fmt"], scenario.params["variant"]
    if variant == "corrupt":
        content = {"pdf": b"%PDF-1.4\n" + bytes(range(256)) * 8}.get(fmt, b"PK\x03\x04" + bytes(range(256)) * 8)
        expect, text = "reject", ""
    else:
        expect, text = D.DOC_VARIANTS[variant]
        content = D.doc_bytes(fmt, text)
        if content is None:
            rec.block(f"no .{fmt} writer available in the test image")
    rec.generated(format=fmt, variant=variant, size_bytes=len(content))
    rec.step(f"parse_content(<{len(content)} bytes>, filename='doc.{fmt}')")
    try:
        out = parse_content(content, filename=f"doc.{fmt}")
    except UnreadableDocument as e:
        rec.result({"outcome": "unreadable", "reason": str(e)})
        rec.verify("outcome matches expectation", expect == "reject", str(e))
        return
    except Exception as e:                                    # noqa: BLE001
        rec.result({"outcome": "crash", "error": f"{type(e).__name__}: {e}"[:300]})
        rec.verify("fails as UnreadableDocument, not a crash", False, f"{type(e).__name__}: {e}")
        return
    rec.result({k: out[k] for k in ("word_count", "language", "category", "file_size_bytes")})
    rec.verify("outcome matches expectation", expect == "accept", f"read {out['word_count']} words")
    rec.verify("checksum is sha256 of the bytes", out["checksum"] == hashlib.sha256(content).hexdigest())
    rec.verify("size reported", out["file_size_bytes"] == len(content))
    rec.verify("text extracted", D.MARKER in out["raw_text"], out["raw_text"][:120])
