"""Chunking strategies × sizes × overlaps × texts (CHNK)."""
from __future__ import annotations


import pytest

from tests.regression.framework import data as D
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression


# ── CHNK: chunking ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", scenarios("CHNK"), ids=str)
def test_chunking(scenario, rec):
    from opencortyx.ingest.unstructured.chunking import chunk_document
    p = scenario.params
    text = D.CHUNK_TEXTS[p["text"]]
    rec.generated(text=p["text"], chars=len(text))
    rec.step(f"chunk_document(text, {p['strategy']!r}, chunk_size={p['size']}, overlap={p['overlap']})")
    chunks = chunk_document(text, p["strategy"], chunk_size=p["size"], overlap=p["overlap"])
    rec.result({"chunks": len(chunks), "max_len": max((len(c["text"]) for c in chunks), default=0)})
    rec.verify("text produces chunks", bool(chunks) == bool(text), len(chunks))
    rec.verify("no empty chunk", all(c["text"].strip() for c in chunks) or p["strategy"] in ("sliding_window", "unknown_strategy"))
    squeeze = lambda s: "".join(s.split())
    strat = p["strategy"]
    has_heading = any(ln.lstrip().startswith("#") for ln in text.splitlines())
    if strat in ("sliding_window", "unknown_strategy"):
        rec.verify("every window within size", all(len(c["text"]) <= p["size"] for c in chunks))
        rebuilt = chunks[0]["text"] + "".join(c["text"][p["overlap"]:] for c in chunks[1:]) if chunks else ""
        rec.verify("windows rebuild the text exactly", rebuilt == text, f"{len(rebuilt)} vs {len(text)} chars")
    elif strat in ("sentence", "paragraph") or (strat == "markdown_heading" and not has_heading):
        rec.verify("no content lost or reordered", squeeze("".join(c["text"] for c in chunks)) == squeeze(text))
    else:
        headed = [c for c in chunks if c["attrs"].get("heading")]
        heads = [ln.lstrip("# ").strip() for ln in text.splitlines() if ln.lstrip().startswith("#")]
        seen = []
        for c in headed:
            if not seen or seen[-1] != c["attrs"]["heading"]:
                seen.append(c["attrs"]["heading"])
        rec.verify("every heading becomes a section, in order", seen == heads, f"{seen[:4]} vs {heads[:4]}")
        over = [len(c["text"]) for c in headed if len(c["text"]) > p["size"]]
        rec.verify("every headed chunk within size", not over, over[:5])
