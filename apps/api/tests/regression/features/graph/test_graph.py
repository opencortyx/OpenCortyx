"""Nodes, edges and relationships: write through the API, read the graph back, verify the state (GRPH)."""
from __future__ import annotations

import uuid

import pytest

from tests.regression.framework.api import Client, principal_headers
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2"


@pytest.fixture
def a(live_api, tenants, rec):
    c = Client(live_api, principal_headers("admin", tenants["A"]), rec)
    yield c
    c.close()


@pytest.fixture
def b(live_api, tenants, rec):
    c = Client(live_api, principal_headers("admin", tenants["B"]), rec)
    yield c
    c.close()


def _node(c, type_="Thing", label=None, props=None):
    r = c.post(f"{V2}/nodes", json={"type": type_, "label": label or f"n-{uuid.uuid4().hex[:8]}", "properties": props})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _neighbours(c, nid, hops=1):
    # focus=true skips time-bookkeeping edges: every node shares Date nodes, which would connect everything
    g = c.get(f"{V2}/nodes/{nid}", params={"hops": hops, "focus": "true"}).json().get("graph", {})
    return {n["id"] for n in g.get("nodes", [])} - {nid}, g.get("edges", [])


@pytest.mark.parametrize("scenario", scenarios("GRPH"), ids=str)
def test_graph(scenario, rec, a, b):
    wf = scenario.workflow
    if wf.startswith("create node"):
        _node_lifecycle(scenario.params, rec, a)
    elif wf in ("create edge", "create relationship"):
        _edge(scenario.params, rec, a, b)
    elif wf == "delete node with edges":
        _delete_cascade(scenario.params, rec, a)
    else:
        _search(scenario.params, rec, a, b)


def _node_lifecycle(p, rec, a):
    label = f"node-{uuid.uuid4().hex[:8]}"
    rec.generated(type=p["type"], label=label, properties=p["props"])
    r = a.post(f"{V2}/nodes", json={"type": p["type"], "label": label, "properties": p["props"]})
    if p["expect"] == "reject":
        rec.result({"status": r.status_code, "body": r.text[:160]})
        rec.verify("blank type refused", r.status_code in (400, 422), r.status_code)
        return
    rec.verify("created", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    nid = r.json()["id"]
    got = a.get(f"{V2}/nodes/{nid}").json()["node"]
    rec.result({"type": got["type"], "label": got["label"], "keys": sorted(got["properties"])[:12]})
    rec.verify("type persisted", got["type"] == p["type"], got["type"])
    rec.verify("label persisted", got["label"] == label, got["label"])
    for k, v in (p["props"] or {}).items():
        if k.startswith("__"):
            continue
        rec.verify(f"property {k!r} persisted", got["properties"].get(k) == v, got["properties"].get(k))
    if p["pname"] == "reserved_keys":
        rec.verify("caller cannot forge provenance", got["properties"].get("__source") != "forged",
                   got["properties"].get("__source"))
    r = a.patch(f"{V2}/nodes/{nid}", json={"label": label + "-v2", "properties": {"patched": "yes"}})
    rec.verify("patch accepted", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    got2 = a.get(f"{V2}/nodes/{nid}").json()["node"]
    rec.verify("patched label", got2["label"] == label + "-v2", got2["label"])
    rec.verify("patched property added", got2["properties"].get("patched") == "yes")
    kept = [k for k in (p["props"] or {}) if not k.startswith("__") and k not in got2["properties"]]
    rec.verify("patch merges, keeps other properties", not kept, kept[:5])
    r = a.delete(f"{V2}/nodes/{nid}")
    rec.verify("deleted", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    r = a.get(f"{V2}/nodes/{nid}")
    rec.verify("gone after delete", r.status_code == 404 or r.json().get("status") == "not_found", r.text[:120])


def _edge(p, rec, a, b):
    src = _node(a, "Person")
    tgt = {"valid_pair": lambda: _node(a, "Company"), "self_loop": lambda: src,
           "missing_target": lambda: str(uuid.uuid4()), "missing_source": lambda: _node(a, "Company"),
           "other_tenant_target": lambda: _node(b, "Company"), "malformed_id": lambda: "not-a-uuid"}[p["endpoints"]]()
    s = str(uuid.uuid4()) if p["endpoints"] == "missing_source" else src
    if p.get("relationship"):
        body, path = {"source_id": s, "target_id": tgt, "relation": p["etype"]}, f"{V2}/relationships"
    else:
        body, path = {"source_id": s, "target_id": tgt, "type": p["etype"]}, f"{V2}/edges"
    rec.generated(body=body)
    r = a.post(path, json=body)
    rec.result({"status": r.status_code, "body": r.text[:200]})
    if p["endpoints"] not in ("valid_pair", "self_loop", "malformed_id"):
        near, _ = _neighbours(a, src if p["endpoints"] != "missing_source" else tgt)
        rec.check("no ghost edge", tgt not in near and s not in near, len(near))
    rec.verify("no server error", r.status_code < 500, f"{r.status_code} {r.text[:200]}")
    if p["endpoints"] == "self_loop":
        rec.verify("self-loop refused as invalid input", r.status_code == 422, f"{r.status_code} {r.text[:120]}")
        return
    if p["endpoints"] == "valid_pair":
        rec.verify("created", r.status_code == 200, r.status_code)
        near, edges = _neighbours(a, src)
        if p["endpoints"] == "valid_pair":
            rec.verify("target visible from source", tgt in near, len(near))
            rec.verify("visible from target too", src in _neighbours(a, tgt)[0])
        else:
            rec.verify("self-loop recorded", any(e["source"] == src and e["target"] == src for e in edges), edges[:3])
    elif p["endpoints"] == "malformed_id":
        rec.verify("malformed id refused as invalid input", r.status_code in (400, 404, 422), r.status_code)
    else:
        rec.verify("no ghost edge", all(c["ok"] for c in rec.checks if c["check"] == "no ghost edge"))
        rec.verify("refused as not found (HTTP status, not a 200 body flag)", r.status_code == 404,
                   f"{r.status_code} {r.text[:120]}")


def _delete_cascade(p, rec, a):
    x, y, z = _node(a, "Hop"), _node(a, "Hop"), _node(a, "Hop")
    for s, t in ((x, y), (y, z)):
        assert a.post(f"{V2}/edges", json={"source_id": s, "target_id": t, "type": "NEXT_HOP"}).status_code == 200
    near_before, _ = _neighbours(a, x, p["hops"])
    rec.verify("chain readable", y in near_before, len(near_before))
    rec.verify("deleted", a.delete(f"{V2}/nodes/{y}").status_code == 200)
    near_x, edges_x = _neighbours(a, x, p["hops"])
    near_z, _ = _neighbours(a, z, p["hops"])
    rec.result({"x_neighbours": len(near_x), "z_neighbours": len(near_z)})
    rec.verify("deleted node gone from neighbours", y not in near_x and y not in near_z)
    rec.verify("its edges gone", not any(y in (e["source"], e["target"]) for e in edges_x))
    rec.verify("chain broken, x no longer reaches z", z not in near_x)


def _search(p, rec, a, b):
    label = f"Zeppelin{uuid.uuid4().hex[:6]}"
    nid = _node(a, "Searchable", label)
    q = {"exact_label": label, "prefix": label[:9], "lowercase": label.lower(), "other_tenant": label}[p["q"]]
    who = b if p["q"] == "other_tenant" else a
    r = who.get(f"{V2}/search", params={"q": q})
    ids = {x["id"] for x in r.json().get("results", [])}
    rec.result({"status": r.status_code, "hits": len(ids)})
    if p["q"] == "other_tenant":
        rec.verify("another tenant's node is never returned", nid not in ids)
    else:
        rec.verify("own node found", nid in ids, f"{len(ids)} hits")
