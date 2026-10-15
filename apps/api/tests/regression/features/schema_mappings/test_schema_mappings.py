"""Schema mappings: paradigm × payload × status, usage and conformance on a fresh mapping (MAPS)."""
import uuid

import pytest

from tests.regression.features.conftest import items
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2/schema-mappings"


@pytest.mark.parametrize("scenario", scenarios("MAPS"), ids=str)
def test_schema_mapping(scenario, rec, admin):
    p = scenario.params
    name = f"map-{uuid.uuid4().hex[:8]}"
    body = {"name": name, "paradigm": p["paradigm"], "payload": p["payload"], "summary": {}, "status": p["status"]}
    rec.generated(body=body)
    r = admin.post(V2, json=body)
    rec.result({"status": r.status_code, "body": r.text[:200]})
    rec.verify("no server error", r.status_code < 500, r.text[:200])
    if not p["ok"]:
        rec.verify("refused", r.status_code in (400, 422), f"{r.status_code} {r.text[:120]}")
        return
    rec.verify("created", r.status_code == 200, r.text[:200])
    mid = r.json()["id"]
    got = admin.get(f"{V2}/{mid}").json()
    rec.verify("paradigm persisted", got.get("paradigm") == p["paradigm"], got.get("paradigm"))
    rec.verify("status persisted", got.get("status") == p["status"], got.get("status"))
    rec.verify("payload persisted", all(got.get("payload", {}).get(k) == v for k, v in p["payload"].items()))
    listed = {m["id"] for m in items(admin.get(V2, params={"paradigm": p["paradigm"]}))}
    rec.verify("listed under its paradigm", mid in listed)
    u = admin.get(f"{V2}/{mid}/usage")
    rec.verify("usage answered", u.status_code == 200, u.text[:160])
    rec.verify("a new mapping is used by no pipeline", u.json().get("pipelines") == [], u.json())
    c = admin.get(f"{V2}/{mid}/conformance", params={"asset_id": str(uuid.uuid4())})
    rec.verify("conformance for an unknown asset is a clean refusal", c.status_code < 500, c.text[:160])
    r = admin.put(f"{V2}/{mid}", json={"name": name + "-v2", "status": p["status"]})
    rec.verify("updated", r.status_code == 200, r.text[:160])
    rec.verify("update persisted", admin.get(f"{V2}/{mid}").json().get("name") == name + "-v2")
    rec.verify("deleted", admin.delete(f"{V2}/{mid}").status_code == 200)
    rec.verify("gone", admin.get(f"{V2}/{mid}").status_code == 404)
