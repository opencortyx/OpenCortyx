"""Every request-body field broken every way the schema forbids: refused as 422, never a 5xx (CONT)."""
import pytest

from tests.regression.framework.api import Client, fill, principal_headers
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression


@pytest.fixture
def admin(live_api, tenants, rec):
    c = Client(live_api, principal_headers("admin", tenants["A"]), rec)
    yield c
    c.close()


@pytest.mark.parametrize("scenario", scenarios("CONT"), ids=str)
def test_request_contract(scenario, rec, admin):
    p = scenario.params
    body = dict(p["baseline"])
    if p["value"] == "<missing>":
        body.pop(p["field"], None)
    else:
        body[p["field"]] = p["value"]
    rec.generated(field=p["field"], value=p["value"] if not isinstance(p["value"], str) else p["value"][:40],
                  body_keys=sorted(body))
    r = admin.request(p["method"], fill(p["path"]), json=body)
    rec.result({"status": r.status_code, "body": r.text[:200]})
    rec.verify("never a server error", r.status_code < 500, f"{r.status_code} {r.text[:200]}")
    if p["expect"] == "422":
        rec.verify("schema violation refused with 422", r.status_code == 422, f"{r.status_code} {r.text[:160]}")
        rec.verify("the refusal names the field", p["field"] in r.text, r.text[:160])
