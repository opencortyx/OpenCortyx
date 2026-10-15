"""Pipelines: create with every kind alias × stage set, read back canonical, update, delete (PIPE)."""
import uuid

import pytest

from tests.regression.features.conftest import items
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2/pipelines"
DEFAULT_IDS = {"sql": "00000000-0000-4000-8000-000000000001", "json": "00000000-0000-4000-8000-000000000002",
               "doc": "00000000-0000-4000-8000-000000000003"}


@pytest.mark.parametrize("scenario", scenarios("PIPE"), ids=str)
def test_pipeline(scenario, rec, admin):
    p = scenario.params
    if scenario.workflow == "system default pipeline":
        pid = DEFAULT_IDS[p["slug"]]
        before = admin.get(f"{V2}/{pid}")
        rec.verify("default pipeline readable", before.status_code == 200, before.text[:160])
        r = admin.put(f"{V2}/{pid}", json={"name": "hijacked"}) if p["action"] == "update" else admin.delete(f"{V2}/{pid}")
        rec.result({"status": r.status_code, "body": r.text[:160]})
        rec.verify("refused", r.status_code in (400, 403, 404, 409), r.status_code)
        after = admin.get(f"{V2}/{pid}")
        rec.verify("default unchanged", after.status_code == 200 and after.json().get("name") == before.json().get("name"),
                   after.text[:160])
        return
    name = f"reg-{uuid.uuid4().hex[:8]}"
    rec.generated(name=name, kind=p["kind"], stages=p["stages"])
    r = admin.post(V2, json={"name": name, "kind": p["kind"], "stages": p["stages"]})
    rec.result({"status": r.status_code, "body": r.text[:200]})
    rec.verify("no server error", r.status_code < 500, r.text[:200])
    if p["canon"] is None or p["types"] is None:
        rec.verify("refused", r.status_code in (400, 422), f"{r.status_code} {r.text[:120]}")
        return
    rec.verify("created", r.status_code == 200, r.text[:200])
    pid = r.json()["id"]
    got = admin.get(f"{V2}/{pid}").json()
    rec.verify("kind stored canonical", got.get("kind") == p["canon"], got.get("kind"))
    rec.verify("stages stored canonical", [s.get("type") for s in got.get("stages", [])] == p["types"],
               [s.get("type") for s in got.get("stages", [])])
    if p["stages"] and "auto_min_chars" in str(p["stages"]):
        rec.verify("config key canonical", "auto_text_min_chars" in str(got["stages"]), got["stages"])
    listed = {x["id"] for x in items(admin.get(V2, params={"kind": p["canon"]}))}
    rec.verify("listed under its kind", pid in listed)
    r = admin.put(f"{V2}/{pid}", json={"name": name + "-v2", "stages": []})
    rec.verify("updated", r.status_code == 200, r.text[:160])
    got = admin.get(f"{V2}/{pid}").json()
    rec.verify("update persisted", got.get("name") == name + "-v2" and got.get("stages") == [], got)
    rec.verify("deleted", admin.delete(f"{V2}/{pid}").status_code == 200)
    rec.verify("gone", admin.get(f"{V2}/{pid}").status_code == 404)
