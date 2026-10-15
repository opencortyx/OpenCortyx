"""Connectors: kind × configuration — stored, secrets never echoed, connection test fails cleanly (CONN)."""
import time
import uuid

import pytest

from tests.regression.features.conftest import items
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2/connectors"
SECRET = "s3cret-marker"


@pytest.mark.parametrize("scenario", scenarios("CONN"), ids=str)
def test_connector(scenario, rec, admin, other_tenant):
    p = scenario.params
    name = f"con-{uuid.uuid4().hex[:8]}"
    body = {"name": name, "kind": p["kind"], **p["cfg"]}
    rec.generated(body={k: ("<secret>" if k == "connection_string" else v) for k, v in body.items()})
    r = admin.post(V2, json=body)
    rec.result({"status": r.status_code, "body": r.text[:200].replace(SECRET, "<LEAKED>")})
    rec.verify("no server error", r.status_code < 500, r.text[:200])
    rec.verify("create response never echoes the secret", SECRET not in r.text)
    if p["kind"] == "bogus":
        rec.verify("unknown kind refused", r.status_code in (400, 422), f"{r.status_code} {r.text[:120]}")
        return
    rec.verify("created", r.status_code == 200, r.text[:200])
    cid = r.json()["id"]
    one, many = admin.get(f"{V2}/{cid}"), admin.get(V2)
    rec.verify("readable", one.status_code == 200)
    rec.verify("GET never echoes the secret", SECRET not in one.text and SECRET not in many.text)
    if "connection_string" in p["cfg"]:
        rec.verify("secret presence reported as a flag", one.json().get("has_connection_string") is True, one.json())
    rec.verify("listed", cid in {c["id"] for c in items(many)})
    rec.verify("invisible to another tenant", cid not in {c["id"] for c in items(other_tenant.get(V2))})
    t0 = time.perf_counter()
    t = admin.post(f"{V2}/{cid}/test")
    took = time.perf_counter() - t0
    rec.result({"test_status": t.status_code, "test": t.text[:200].replace(SECRET, "<LEAKED>"), "test_seconds": round(took, 2)})
    rec.verify("connection test never a server error", t.status_code < 500, t.text[:200])
    rec.verify("connection test never echoes the secret", SECRET not in t.text)
    rec.verify("connection test answers within 60 s", took < 60, took)
    # an advertised kind must be usable: a failure because the driver is not installed is a defect
    rec.verify("driver for this kind is installed", "No module named" not in t.text, t.text[:160])
    if p["cname"] in ("complete_unreachable", "connection_string_only") and p["kind"] in ("postgres", "mysql", "mssql"):
        failed = t.json().get("ok") is False or (t.status_code >= 400 and "fail" in t.text.lower())
        rec.verify("unreachable database reported as a failure", failed, t.text[:160])
    rc = admin.get(f"{V2}/{cid}/recipe")
    rec.verify("recipe export answered", rc.status_code == 200, rc.text[:160])
    rec.verify("recipe never carries the secret", SECRET not in rc.text)
    imp = admin.post(f"{V2}/from-recipe", json={"recipe": rc.json().get("recipe", rc.json())})
    rec.verify("recipe re-import answered", imp.status_code < 500, imp.text[:160])
    if imp.status_code == 200 and imp.json().get("id"):
        twin = admin.get(f"{V2}/{imp.json()['id']}").json()
        rec.verify("recipe round-trips the kind", twin.get("kind") == p["kind"], twin.get("kind"))
        admin.delete(f"{V2}/{imp.json()['id']}")
    rec.verify("deleted", admin.delete(f"{V2}/{cid}").status_code == 200)
    rec.verify("gone", admin.get(f"{V2}/{cid}").status_code == 404)
