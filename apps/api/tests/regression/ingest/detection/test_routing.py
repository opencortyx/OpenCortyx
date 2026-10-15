"""Extension × content × override routing (ROUT)."""
from __future__ import annotations


import pytest

from tests.regression.framework.matrix import CONTENT_KINDS, scenarios

pytestmark = pytest.mark.regression


# ── ROUT: routing ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", scenarios("ROUT"), ids=str)
def test_routing(scenario, rec):
    from opencortyx.ingest.detection import routing
    p = scenario.params
    name = f"file.{p['ext']}" if p["ext"] else "file"
    content = CONTENT_KINDS[p["kind"]]
    rec.generated(filename=name, content=content[:60].decode("latin-1"))
    rec.step(f"route_for({name!r}, <{p['kind']}>, override={p['override']!r})")
    got = routing.route_for(name, content or None, override=p["override"])
    rec.result(got)
    rec.verify("route", got == p["expect"], f"{got} vs {p['expect']}")
