"""The W3C XML conformance suite against the XML reader (XMLC)."""
from __future__ import annotations

import pathlib

import pytest

from tests.regression.framework.matrix import CORPUS, read_capped, scenarios

pytestmark = pytest.mark.regression


def _load(scenario, rec) -> bytes:
    if not CORPUS:
        rec.block("OPENCORTYX_CORPUS is not mounted")
    p = pathlib.Path(CORPUS) / scenario.params["path"]
    content = read_capped(p)
    rec.generated(file=scenario.params["path"], size_bytes=len(content))
    return content


# ── XMLC ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", scenarios("XMLC") or [pytest.param(None, marks=pytest.mark.skip("no corpus"))], ids=str)
def test_xml_conformance(scenario, rec):
    from opencortyx.ingest.formats import semistructured as P
    content = _load(scenario, rec)
    rec.step(f"parse as XML ({scenario.config})")
    try:
        r = P.parse(content, "conformance.xml")
        outcome = "accepted"
        rec.result({"outcome": outcome, "root": r.root_name})
    except P.ParseError as e:
        outcome = "rejected"
        rec.result({"outcome": outcome, "reason": str(e)[:300]})
    except Exception as e:                                    # noqa: BLE001
        rec.result({"outcome": "crash", "error": f"{type(e).__name__}: {e}"[:300]})
        rec.verify("fails as ParseError, never a crash", False, f"{type(e).__name__}: {e}")
        return
    exp = scenario.expected
    if exp == "accept":
        rec.verify("well-formed document accepted", outcome == "accepted", scenario.params.get("id"))
    elif exp in ("reject", "reject_by_policy"):
        rec.verify("not-well-formed (or entity-declaring) document rejected", outcome == "rejected", scenario.params.get("id"))
    else:
        rec.check("no crash", True)
