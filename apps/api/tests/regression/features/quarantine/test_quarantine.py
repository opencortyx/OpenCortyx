"""Quarantine: the write-accounting summary, and actions on unknown items (QUAR)."""
import uuid

import pytest

from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2/quarantine"


@pytest.mark.parametrize("scenario", scenarios("QUAR"), ids=str)
def test_quarantine(scenario, rec, admin):
    a = scenario.params["action"]
    if a == "summary":
        items_ = admin.get(V2)
        writes = admin.get(f"{V2}/writes")
        rec.verify("both readable", items_.status_code == 200 and writes.status_code == 200)
        s = writes.json()["summary"]
        rec.result(s)
        rec.verify("counts are non-negative integers", all(isinstance(s[k], int) and s[k] >= 0
                                                           for k in ("recovered", "pending", "unrecoverable")), s)
        rec.verify("by_reason is a mapping of reasons", isinstance(s.get("by_reason"), dict), s.get("by_reason"))
        rec.verify("a fresh tenant has nothing quarantined", s["pending"] == 0 and s["unrecoverable"] == 0, s)
        return
    nid = str(uuid.uuid4())
    r = {"delete": lambda: admin.delete(f"{V2}/{nid}"),
         "retry": lambda: admin.post(f"{V2}/{nid}/retry", json={"json_data": "{}"}),
         "rerun": lambda: admin.post(f"{V2}/{nid}/rerun"),
         "retry_invalid_json": lambda: admin.post(f"{V2}/{nid}/retry", json={"json_data": "{not json"})}[a]()
    rec.result({"status": r.status_code, "body": r.text[:160]})
    rec.verify("never a server error", r.status_code < 500, f"{r.status_code} {r.text[:160]}")
    rec.verify("unknown item refused", r.status_code in (400, 404, 422), r.status_code)
