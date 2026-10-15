"""Ingestions as a state machine: pairwise configuration × action sequences, state read back (INGS)."""
import uuid

import pytest

from tests.regression.features.conftest import items
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2/ingestions"
DEFAULTS = {"unstructured": "00000000-0000-4000-8000-000000000003", "structured": "00000000-0000-4000-8000-000000000001",
            "semistructured": "00000000-0000-4000-8000-000000000002"}


def _local_connector(admin):
    for c in items(admin.get("/api/v2/connectors")):
        if c.get("kind") == "local":
            return c["id"]
    return admin.post("/api/v2/connectors", json={"name": "reg-local", "kind": "local", "directory": "/host"}).json()["id"]


@pytest.mark.parametrize("scenario", scenarios("INGS"), ids=str)
def test_ingestion(scenario, rec, admin):
    from tests.regression.features.ingestions.scenarios import INGESTION_ACTIONS
    p = scenario.params
    body = {"name": f"ing-{uuid.uuid4().hex[:8]}", "kind": p["kind"], "connector_id": _local_connector(admin),
            "pipeline_id": DEFAULTS[p["kind"]] if p["pipeline"] == "default" else None,
            "selections": [], "schedule": p["schedule"], "directories": ["regression-does-not-exist"]}
    rec.generated(body=body)
    r = admin.post(V2, json=body)
    rec.result({"create": r.status_code, "body": r.text[:200]})
    rec.verify("no server error", r.status_code < 500, r.text[:200])
    if p["schedule"] == "not-a-cron":
        rec.verify("invalid schedule refused", r.status_code in (400, 422), f"{r.status_code} {r.text[:120]}")
        if r.status_code == 200:
            admin.delete(f"{V2}/{r.json()['id']}")
        return
    rec.verify("created", r.status_code == 200, r.text[:200])
    iid = r.json()["id"]
    got = admin.get(f"{V2}/{iid}").json()
    rec.verify("kind persisted", got.get("kind") == p["kind"], got.get("kind"))
    rec.verify("schedule persisted", got.get("schedule") == p["schedule"], got.get("schedule"))
    paused = False
    for action in INGESTION_ACTIONS[p["actions"]]:
        r = admin.post(f"{V2}/{iid}/{action}")
        state = admin.get(f"{V2}/{iid}").json()
        rec.check(f"{action}: answered", r.status_code < 500, f"{r.status_code} {r.text[:120]}")
        assert r.status_code < 500, f"{action}: {r.status_code} {r.text[:200]}"
        if action == "pause":
            rec.verify("pause leaves it paused", bool(state.get("paused")), state.get("paused"))
            paused = True
        elif action == "resume":
            if paused:
                rec.verify("resume unpauses", not state.get("paused"), state.get("paused"))
                paused = False
            else:
                rec.verify("resume when not paused is harmless", not state.get("paused"), state.get("paused"))
        elif action in ("run", "sync"):
            # pause stops the SCHEDULER only ("the beat tick will not auto-run it while paused");
            # a manual run or sync is still the operator's call, and must leave the pause in place
            rec.verify(f"{action} accepted or refused with a reason", r.status_code in (200, 202, 400, 403, 409),
                       f"{r.status_code} {r.text[:120]}")
            if paused:
                rec.verify(f"{action} keeps the ingestion paused", bool(state.get("paused")), state.get("paused"))
    runs = admin.get(f"{V2}/{iid}/runs")
    rec.verify("runs readable", runs.status_code == 200, runs.text[:160])
    rec.verify("deleted", admin.delete(f"{V2}/{iid}").status_code == 200)
    rec.verify("gone", admin.get(f"{V2}/{iid}").status_code == 404)
