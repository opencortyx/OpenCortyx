"""Custom extractors through the API: preview what they extract, refuse bad definitions, lifecycle (EXTR)."""
from __future__ import annotations

import pytest

from tests.regression.features.extractors.scenarios import expected_records, PREVIEW_MAX_NODES, VALID_STATUSES
from tests.regression.framework.api import Client, principal_headers
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
BASE = "/api/v2/extraction-templates"


@pytest.fixture
def admin(live_api, tenants, rec):
    c = Client(live_api, principal_headers("admin", tenants["A"]), rec)
    yield c
    c.close()


@pytest.mark.parametrize("scenario", scenarios("EXTR"), ids=str)
def test_extractor(scenario, rec, admin):
    p = scenario.params
    wf = scenario.workflow
    if wf.startswith("preview"):
        _preview(p, rec, admin)
    elif wf == "save extractor":
        r = admin.post(BASE, json=p["body"])
        rec.result({"status": r.status_code, "body": r.text[:200]})
        rec.verify("definition refused", r.status_code == p["status"], r.status_code)
        rec.verify("refusal explains itself", bool(r.json().get("detail")) if r.status_code == 422 else True, r.text[:120])
        listed = admin.get(BASE).json().get("templates", [])
        name = p["body"].get("name")
        rec.verify("nothing was saved", not any(t.get("name") == name for t in listed) or not name or not str(name).strip())
    else:
        _lifecycle(p, rec, admin)


def _preview(p, rec, admin):
    mech, text = p["mechanism"], p["text"]
    rec.generated(mechanism=mech, sample_chars=len(text))
    r = admin.post(f"{BASE}/preview", json={"name": "regression", "mechanism": mech, "emit": p["emit"],
                                            "sample_text": text})
    rec.verify("preview answered", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    out = r.json()
    labels = sorted({n.get("label") for n in out.get("nodes", [])})
    rec.result({"nodes": len(out.get("nodes", [])), "labels": labels[:10], "error": out.get("error"),
                "warnings": out.get("warnings")})
    rec.verify("counts agree with the nodes listed", out["counts"]["nodes"] == len(out.get("nodes", [])))
    ref = mech["ref"]
    exp = expected_records(ref, mech, text)
    if exp is not None:
        # preview shows a sample of at most PREVIEW_MAX_NODES; every node shown must be a correct one
        rec.verify("sample size", len(labels) == min(len(exp), PREVIEW_MAX_NODES), f"{len(labels)} vs {len(exp)} expected")
        rec.verify("every extracted entity is correct", set(labels) <= set(exp), sorted(set(labels) - set(exp))[:6])
    elif ref == "unstructured.keyword":
        rec.verify("keyword count bounded by top_n", len(labels) <= p["top_n"], len(labels))
        rec.verify("keywords come from the text", all(str(l).lower() in text.lower() for l in labels), labels[:5])
    elif ref == "unstructured.python":
        if p["python"] in ("raises", "wrong_type"):
            rec.verify("failure reported, nothing emitted", not labels and bool(out.get("error") or out.get("warnings")), out)
        elif p["python"] == "returns_two":
            rec.verify("records become nodes", labels == ["a", "b"], labels)
        elif p["python"] == "returns_empty":
            rec.verify("no records, no nodes", not labels, labels)
        else:
            want = sorted(set(text.split()))[:5]
            rec.verify("records computed from the text", labels == sorted(want), f"{labels} vs {want}")


def _lifecycle(p, rec, admin):
    import uuid
    name = f"regression-{uuid.uuid4().hex[:8]}"
    r = admin.post(BASE, json={"name": name, "mechanism": p["mechanism"],
                               "emit": {"nodes": [{"type": "Extracted", "label": "$match", "id": ["match"]}]}})
    rec.verify("created", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    tid = r.json().get("id")
    rec.verify("create returns an id", bool(tid), r.text[:200])
    listed = {t["name"]: t for t in admin.get(BASE).json().get("templates", [])}
    rec.verify("listed after create", name in listed)
    before = listed[name].get("status")
    r = admin.patch(f"{BASE}/{tid}", json={"status": p["status"]})
    after = {t["name"]: t for t in admin.get(BASE).json().get("templates", [])}.get(name, {}).get("status")
    rec.result({"patch_status": r.status_code, "status_before": before, "status_after": after})
    if p["status"] not in VALID_STATUSES:
        rec.verify("invalid status refused", r.status_code in (400, 422), r.status_code)
        rec.verify("status unchanged", after == before, f"{before} -> {after}")
    else:
        rec.verify("status change accepted", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        rec.verify("status persisted", after == p["status"], f"{after} vs {p['status']}")
    r = admin.post(f"{BASE}/{tid}/reset")
    rec.check("reset answered", r.status_code < 500, r.status_code)
    r = admin.delete(f"{BASE}/{tid}")
    rec.verify("deleted", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    gone = name not in {t["name"] for t in admin.get(BASE).json().get("templates", [])}
    rec.verify("gone after delete", gone)
    r = admin.delete(f"{BASE}/{tid}")
    rec.verify("second delete is a clean not-found", r.status_code in (200, 404) and r.status_code < 500, r.status_code)
