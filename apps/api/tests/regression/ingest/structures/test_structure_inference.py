"""Structure inference on every corpus file's first record (SCHM)."""
from __future__ import annotations

import pathlib

import pytest

from tests.regression.framework.matrix import CORPUS, read_capped, scenarios
from tests.regression.framework.support import walked_data

pytestmark = pytest.mark.regression


def _load(scenario, rec) -> bytes:
    if not CORPUS:
        rec.block("OPENCORTYX_CORPUS is not mounted")
    p = pathlib.Path(CORPUS) / scenario.params["path"]
    content = read_capped(p)
    rec.generated(file=scenario.params["path"], size_bytes=len(content))
    return content


def _parse(content: bytes, name: str):
    from opencortyx.ingest.formats import semistructured as P
    return P.parse(content, name)


# ── SCHM ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", scenarios("SCHM") or [pytest.param(None, marks=pytest.mark.skip("no corpus"))], ids=str)
def test_structure_inference(scenario, rec):
    from opencortyx.ingest.semistructured import schema as ss
    content = _load(scenario, rec)
    data = walked_data(_parse(content, scenario.params["path"]))
    first = next((r for r in (data if isinstance(data, list) else [data]) if isinstance(r, dict)), None)
    if first is None:
        rec.block("no object record")
    rec.step("build_structure(first record) ×2 → fingerprint → compile_walk_spec → match_structure")
    a, b = ss.build_structure(first, name="s"), ss.build_structure(first, name="s")
    rec.verify("structure inference is deterministic", a == b)
    rec.verify("fingerprint is deterministic", ss.fingerprint(first) == ss.fingerprint(first))
    spec = ss.compile_walk_spec(ss.normalize_structures([a]))
    rec.verify("card compiles to a walk spec", isinstance(spec, dict), type(spec).__name__)
    got, score = ss.match_structure(first, [a])
    rec.result({"matched": got is not None, "score": score, "keys": len(a.get("keys") or [])})
    rec.verify("a record matches its own card, or nothing", got is None or (got is a and score >= 0.999),
               f"score {score}")
