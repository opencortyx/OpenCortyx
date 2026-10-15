"""Tenant isolation: tenant B calls every read operation, aimed at tenant A's real ids (TENA).

Tenant A first creates one object of every kind, each carrying a unique marker. Tenant B then calls
every authenticated GET operation — path parameters filled with A's REAL ids where the parameter names
one — and no response may contain any of A's ids or the marker.
"""
from __future__ import annotations

import re
import uuid

import pytest

from tests.regression.framework.api import Client, principal_headers
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2"
MARKER = f"TenantA-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module")
def tenant_a_objects(live_api, tenants):
    a = Client(live_api, principal_headers("admin", tenants["A"]))
    ids = {}
    try:
        def make(key, path, body):
            r = a.post(f"{V2}{path}", json=body)
            if r.status_code == 200 and isinstance(r.json(), dict) and r.json().get("id"):
                ids[key] = r.json()["id"]
        make("node", "/nodes", {"type": "Secret", "label": MARKER, "properties": {"marker": MARKER}})
        make("node2", "/nodes", {"type": "Secret", "label": MARKER + "-2"})
        make("collection", "/collections", {"name": MARKER})
        make("connector", "/connectors", {"name": MARKER, "kind": "local", "directory": "/host"})
        make("pipeline", "/pipelines", {"name": MARKER, "kind": "document", "stages": []})
        make("mapping", "/schema-mappings", {"name": MARKER, "paradigm": "semistructured", "payload": {}, "summary": {}})
        make("ingestion", "/ingestions", {"name": MARKER, "kind": "unstructured", "connector_id": ids.get("connector")})
        make("template", "/extraction-templates", {"name": MARKER, "mechanism": {"ref": "seed.literal", "items": []}})
        make("glossary", "/glossary", {"term": MARKER, "definition": "private"})
        make("model", "/models", {"name": MARKER, "model_id": "private/model", "role": "generation"})
        if ids.get("node") and ids.get("node2"):
            a.post(f"{V2}/edges", json={"source_id": ids["node"], "target_id": ids["node2"], "type": "PRIVATE"})
        ids["comment_asset"] = ids.get("node")
        if ids.get("node"):
            a.post(f"{V2}/assets/{ids['node']}/comments", json={"text": MARKER})
    finally:
        a.close()
    return ids


PARAM_TO_OBJECT = {"node_id": "node", "asset_id": "node", "source_id": "node", "loser_id": "node",
                   "collection_id": "collection", "connector_id": "connector", "pipeline_id": "pipeline",
                   "mapping_id": "mapping", "ingestion_id": "ingestion", "tid": "template", "model_id": "model",
                   "case_id": "node", "session_id": "node", "run_id": "node"}


@pytest.mark.parametrize("scenario", scenarios("TENA"), ids=str)
def test_tenant_isolation(scenario, rec, live_api, tenants, tenant_a_objects):
    op = scenario.params
    ids = tenant_a_objects
    rec.verify("tenant A holds data to protect", len(ids) >= 6, sorted(ids))
    def concrete(real: bool):
        path = op["path"]
        used = {}
        for param in op["path_params"]:
            value = ids.get(PARAM_TO_OBJECT.get(param)) if real else None
            value = value or str(uuid.uuid4())
            used[param] = value
            path = path.replace("{" + param + "}", value)
        return re.sub(r"\{[^}]+\}", MARKER, path), used

    def call(path):
        b = Client(live_api, principal_headers("admin", tenants["B"]), rec)
        try:
            return b.get(path, params={"q": MARKER, "limit": 200})
        finally:
            b.close()

    real_path, real_ids = concrete(True)
    r = call(real_path)
    leaked = ["marker"] if MARKER in r.text else []
    # differential: the same call aimed at random ids. If B's answer about A's REAL objects differs from its
    # answer about objects that do not exist (ids normalised), B learned something across the boundary.
    if op["path_params"]:
        rand_path, rand_ids = concrete(False)
        r2 = call(rand_path)
        norm = lambda text, used: re.sub("|".join(map(re.escape, used.values())), "<id>", text) if used else text
        if (r.status_code, norm(r.text, real_ids)) != (r2.status_code, norm(r2.text, rand_ids)):
            leaked.append(f"differs from a nonexistent id: {r.status_code} vs {r2.status_code}")
    else:
        leaked += [k for k, v in ids.items() if v and v in r.text]
    rec.result({"status": r.status_code, "leaked": leaked, "bytes": len(r.text)})
    rec.verify("no server error", r.status_code < 500, f"{r.status_code} {r.text[:160]}")
    rec.verify("nothing of tenant A visible to tenant B", not leaked, leaked)
