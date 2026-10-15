"""Model registry: role × provider × endpoint — stored, key never echoed, test fails cleanly (MODL)."""
import time
import uuid

import pytest

from tests.regression.features.conftest import items
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2/models"
KEY = "sk-regression-key-marker"


@pytest.mark.parametrize("scenario", scenarios("MODL"), ids=str)
def test_model(scenario, rec, admin, other_tenant):
    p = scenario.params
    body = {"name": f"model-{uuid.uuid4().hex[:8]}", "role": p["role"], "provider": p["provider"],
            "model_id": "regression/model", "api_key": KEY, "params": {},
            "base_url": None if p["url"] == "none" else "http://10.255.255.1:9/v1"}
    rec.generated(body={k: ("<key>" if k == "api_key" else v) for k, v in body.items()})
    r = admin.post(V2, json=body)
    rec.result({"status": r.status_code, "body": r.text[:200].replace(KEY, "<LEAKED>")})
    rec.verify("no server error", r.status_code < 500, r.text[:200])
    rec.verify("create never echoes the key", KEY not in r.text)
    if not p["ok"]:
        rec.verify("refused", r.status_code in (400, 422), f"{r.status_code} {r.text[:120]}")
        return
    rec.verify("registered", r.status_code == 200, r.text[:200])
    mid = r.json()["id"]
    listed = admin.get(V2, params={"role": p["role"]})
    rec.verify("listed under its role", mid in {m["id"] for m in items(listed)})
    rec.verify("list never echoes the key", KEY not in listed.text)
    rec.verify("invisible to another tenant", mid not in {m["id"] for m in items(other_tenant.get(V2))})
    if p["url"] == "unreachable":
        import httpx
        t0 = time.perf_counter()
        try:
            t = admin.post(f"{V2}/{mid}/test", timeout=75)
        except httpx.TimeoutException:
            rec.verify("answers within 60 s", False, "no answer after 75 s: the dependency call is unbounded")
        took = time.perf_counter() - t0
        rec.result({"test": t.status_code, "test_body": t.text[:160], "seconds": round(took, 2)})
        rec.verify("unreachable endpoint reported, not a server error", t.status_code < 500, t.text[:160])
        rec.verify("reported as a failure", t.json().get("ok") is False or t.status_code >= 400, t.text[:160])
        rec.verify("answers within 60 s", took < 60, took)
    r = admin.put(f"{V2}/{mid}", json={**body, "is_default": True})
    rec.verify("made default", r.status_code == 200, r.text[:160])
    defaults = [m for m in items(admin.get(V2, params={"role": p["role"]})) if m.get("is_default")]
    rec.verify("exactly one default for the role", len(defaults) == 1 and defaults[0]["id"] == mid,
               [m["id"] for m in defaults])
    rec.verify("deleted", admin.delete(f"{V2}/{mid}").status_code == 200)
    rec.verify("gone", mid not in {m["id"] for m in items(admin.get(V2))})
