"""Curation through the API, each write read back as graph state (CURA)."""
from __future__ import annotations

import uuid

import pytest

from tests.regression.features.curation.scenarios import KINDS, TEXTS
from tests.regression.framework.api import Client, principal_headers
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2"


@pytest.fixture
def admin(live_api, tenants, rec):
    c = Client(live_api, principal_headers("admin", tenants["A"]), rec)
    yield c
    c.close()


def _node(c, type_="Asset"):
    return c.post(f"{V2}/nodes", json={"type": type_, "label": f"t-{uuid.uuid4().hex[:6]}"}).json()["id"]


def _read(c, nid):
    r = c.get(f"{V2}/nodes/{nid}", params={"hops": 1, "focus": "true"})
    return r.json() if r.status_code == 200 else {}


@pytest.mark.parametrize("scenario", scenarios("CURA"), ids=str)
def test_curation(scenario, rec, admin):
    wf, p = scenario.workflow, scenario.params
    if wf.startswith("curate "):
        path, body_of = KINDS[p["kind"]]
        term, definition = TEXTS[p["text"]]
        target = {"none": None, "existing_node": lambda: _node(admin), "missing_node": lambda: str(uuid.uuid4())}[p["targets"]]
        tgt = target() if callable(target) else None
        body = body_of(term, definition, [tgt] if tgt else [])
        rec.generated(body={k: (v[:40] if isinstance(v, str) else v) for k, v in body.items()})
        r = admin.post(f"{V2}{path}", json=body)
        rec.result({"status": r.status_code, "body": r.text[:200]})
        rec.verify("no server error", r.status_code < 500, r.text[:200])
        if p["expect"] == "reject":
            rec.verify("refused", r.status_code in (400, 422), f"{r.status_code} {r.text[:120]}")
            return
        if p["expect"] == "unresolved":
            body = r.json()
            rec.verify("stored", r.status_code == 200, r.text[:160])
            rec.verify("missing target not counted as linked", body.get("targets") == 0, body)
            rec.verify("missing target reported", tgt in (body.get("unresolved_targets") or []), body)
            return
        rec.verify("stored", r.status_code == 200, r.text[:200])
        nid = r.json().get("id")
        got = _read(admin, nid)
        rec.verify("readable as a node", bool(got.get("node")), str(got)[:160])
        text = str(got.get("node", {}).get("label", "")) + " " + str(got.get("node", {}).get("properties", {}))
        rec.verify("text persisted", term.strip() in text, text[:160])
        if tgt:
            linked = {n["id"] for n in got.get("graph", {}).get("nodes", [])}
            rec.verify("linked to its target", tgt in linked, len(linked))
    elif wf == "ontology type lifecycle":
        _ontology(p, rec, admin)
    else:
        _comment(p, rec, admin)


def _ontology(p, rec, admin):
    name = f"{p['name'][:76]}{uuid.uuid4().hex[:4]}" if p["name"] else ""   # stays within the 80-char limit
    r = admin.post(f"{V2}/ontology/types", json={"name": name, "kind": p["kind"], "description": "regression type"})
    rec.result({"status": r.status_code, "body": r.text[:200]})
    rec.verify("no server error", r.status_code < 500, r.text[:200])
    if not p["valid"]:
        rec.verify("refused", r.status_code in (400, 422), r.status_code)
        return
    rec.verify("registered", r.status_code == 200, r.text[:200])
    onto = admin.get(f"{V2}/ontology").json()
    rec.verify("listed in the ontology", name in str(onto))
    r = admin.patch(f"{V2}/ontology/types", params={"name": name, "kind": p["kind"], "description": "updated description"})
    rec.verify("updated", r.status_code == 200, r.text[:160])
    rec.verify("update persisted", "updated description" in str(admin.get(f"{V2}/ontology").json()))
    r = admin.delete(f"{V2}/ontology/types", params={"name": name, "kind": p["kind"]})
    rec.verify("deleted", r.status_code == 200, r.text[:160])
    rec.verify("gone from the ontology", name not in str(admin.get(f"{V2}/ontology").json()))


def _comment(p, rec, admin):
    asset = _node(admin, "Source")
    r = admin.post(f"{V2}/assets/{asset}/comments", json={"text": p["text"]})
    rec.result({"status": r.status_code, "body": r.text[:200]})
    rec.verify("no server error", r.status_code < 500, r.text[:200])
    if not p["text"]:
        rec.verify("empty comment refused", r.status_code in (400, 422), r.status_code)
        return
    rec.verify("posted", r.status_code == 200, r.text[:160])
    items = admin.get(f"{V2}/assets/{asset}/comments").json()
    items = items.get("items", items.get("comments", [])) if isinstance(items, dict) else items
    mine = [c for c in items if c.get("text") == p["text"]]
    rec.verify("listed on the asset", len(mine) == 1, len(items))
    cid = mine[0]["id"] if mine else r.json().get("id")
    rec.verify("deleted", admin.delete(f"{V2}/comments/{cid}").status_code == 200)
    items = admin.get(f"{V2}/assets/{asset}/comments").json()
    items = items.get("items", items.get("comments", [])) if isinstance(items, dict) else items
    rec.verify("gone after delete", not any(c.get("id") == cid for c in items))
