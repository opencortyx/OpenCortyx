"""End-to-end smoke test of a running stack: auth, ingestion, retrieval, memory, extractors, MCP.

On a fresh stack pass admin credentials so the test can complete first-run setup:

    docker compose exec -e OPENCORTYX_ADMIN_EMAIL=admin@example.com -e OPENCORTYX_ADMIN_PASSWORD=... \
        api python -m pytest tests/integration/test_smoke_e2e.py -m integration -v
"""
import json
import os
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE = os.environ.get("OPENCORTYX_API_URL", "http://localhost:8000").rstrip("/")
MCP_URL = os.environ.get("OPENCORTYX_MCP_URL", "http://mcp:8001/mcp")
EMAIL = os.environ.get("OPENCORTYX_ADMIN_EMAIL", "")
PASSWORD = os.environ.get("OPENCORTYX_ADMIN_PASSWORD", "")
INGEST_TIMEOUT = float(os.environ.get("OPENCORTYX_INGEST_TIMEOUT", "1200"))
TAG = uuid.uuid4().hex[:8]

DOCUMENT = f"""# Project Orion {TAG}

Project Orion is a satellite programme led by Dana Whitfield.
The Orion satellite is scheduled to launch in March 2027 from the Kourou spaceport.
Its budget is 42 million euros and its prime contractor is Helix Aerospace.
"""

CSV = "customer_id,name,country,plan\nC1,Acme Corp,Germany,enterprise\nC2,Globex,France,starter\nC3,Initech,Spain,pro\n"

JSON_DOC = {"orders": [{"order_id": f"O-{TAG}-1", "customer_id": "C1", "total": 1200},
                       {"order_id": f"O-{TAG}-2", "customer_id": "C2", "total": 80}]}


@pytest.fixture(scope="module")
def token():
    with httpx.Client(base_url=f"{BASE}/api/v2", timeout=60) as c:
        if c.get("/auth/needs-setup").json().get("needs_setup"):
            if not (EMAIL and PASSWORD):
                pytest.skip("fresh stack: set OPENCORTYX_ADMIN_EMAIL and OPENCORTYX_ADMIN_PASSWORD")
            r = c.post("/auth/setup", json={"email": EMAIL, "password": PASSWORD, "adopt_demo": False})
            assert r.status_code == 200, r.text
            return r.json()["access_token"]
        if not (EMAIL and PASSWORD):
            pytest.skip("set OPENCORTYX_ADMIN_EMAIL and OPENCORTYX_ADMIN_PASSWORD")
        r = c.post("/auth/token", json={"username": EMAIL, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return r.json()["access_token"]


@pytest.fixture(scope="module")
def api(token):
    with httpx.Client(base_url=f"{BASE}/api/v2", timeout=300,
                      headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


@pytest.fixture(scope="module")
def api_key(api):
    r = api.post("/apikeys", json={"app": f"smoke-{TAG}", "role": "editor"})
    assert r.status_code == 200, r.text
    body = r.json()
    yield body
    api.delete(f"/apikeys/{body['id']}")


def _wait_for_run(api, body):
    run_id = body.get("run_id") or (body.get("run") or {}).get("id") or body.get("id")
    assert run_id, f"no run id in response: {body}"
    deadline = time.monotonic() + INGEST_TIMEOUT
    while time.monotonic() < deadline:
        run = api.get(f"/progress/{run_id}").json()
        if run.get("status") in ("completed", "failed", "canceled"):
            assert run["status"] == "completed", f"run {run_id} {run['status']}: {run.get('error')}"
            return run
        time.sleep(5)
    pytest.fail(f"run {run_id} did not finish within {INGEST_TIMEOUT}s")


def _wait_until(fn, timeout=600, step=5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(step)
    return last


def test_health():
    r = httpx.get(f"{BASE}/health", timeout=10)
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_unauthenticated_requests_are_rejected():
    assert httpx.get(f"{BASE}/api/v2/stats", timeout=10).status_code == 401


def test_current_user_is_admin(api):
    r = api.get("/auth/me")
    assert r.status_code == 200, r.text
    assert r.json().get("role") == "admin"


def test_api_key_authenticates(api_key):
    r = httpx.get(f"{BASE}/api/v2/stats", headers={"X-API-Key": api_key["key"]}, timeout=30)
    assert r.status_code == 200, r.text


def test_ingest_document(api):
    r = api.post("/sources/upload", files={"file": (f"orion-{TAG}.md", DOCUMENT.encode(), "text/markdown")})
    assert r.status_code == 200, r.text
    _wait_for_run(api, r.json())


def test_ingest_csv(api):
    r = api.post("/structured/ingest-file", files={"file": (f"customers-{TAG}.csv", CSV.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    _wait_for_run(api, r.json())


def test_ingest_json(api):
    r = api.post("/json/ingest", files={"file": (f"orders-{TAG}.json", json.dumps(JSON_DOC).encode(),
                                                 "application/json")})
    assert r.status_code == 200, r.text
    _wait_for_run(api, r.json())


def test_graph_has_nodes_and_embeddings(api):
    stats = _wait_until(lambda: (lambda s: s if s.get("embedded_nodes") else None)(api.get("/stats").json()))
    assert stats and stats["nodes_by_type"], stats
    assert stats["embedded_nodes"] > 0, stats


def test_search_nodes(api):
    results = _wait_until(lambda: api.get("/search", params={"q": "Orion"}).json().get("results"), timeout=120)
    assert results, "no node matched 'Orion'"


def test_context_returns_grounded_blocks(api):
    r = api.post("/context", json={"query": "Who leads Project Orion?"})
    assert r.status_code == 200, r.text
    assert "Whitfield" in json.dumps(r.json()), "retrieved context does not contain the ingested fact"


def test_answer_is_grounded(api):
    r = api.post("/answer", json={"query": "Who leads Project Orion?"})
    assert r.status_code == 200, r.text
    body = r.json()
    text = body.get("answer") or body.get("text") or ""
    assert "Whitfield" in text or "Dana" in text, f"unexpected answer: {body}"


def test_memory_remember_and_recall(api):
    note = f"Smoke test {TAG} prefers green tea"
    r = api.post("/memory/remember", json={"text": note, "kind": "preference"})
    assert r.status_code == 200, r.text
    recalled = _wait_until(lambda: TAG in json.dumps(api.get("/memory/recall", params={"q": "green tea"}).json()),
                           timeout=120)
    assert recalled, "remembered note was not recalled"
    assert api.get("/memory/sessions").status_code == 200


def test_extraction_templates_list_and_preview(api):
    body = api.get("/extraction-templates").json()
    templates = body.get("templates") if isinstance(body, dict) else body
    assert templates, "no built-in extraction templates"
    r = api.post("/extraction-templates/preview", json={
        "name": f"smoke-{TAG}",
        "source": {"select": "Chunk"},
        "mechanism": {"type": "regex", "pattern": r"(\d+) million euros"},
        "emit": {"attributes": [{"name": "budget_meur"}]},
        "sample_text": DOCUMENT,
    })
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("path", ["/graph", "/collections", "/retrievers", "/models/runtime",
                                  "/progress", "/structured", "/ingest/formats"])
def test_read_endpoints(api, path):
    r = api.get(path)
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"


def test_graph_query(api):
    r = api.post("/graph/query", json={"query": 'START query="Project Orion"', "limit": 10})
    assert r.status_code == 200, r.text
    assert "count" in r.json()


def test_graph_query_rejects_invalid_syntax(api):
    assert api.post("/graph/query", json={"query": "Project Orion"}).status_code == 400


def _mcp_call(client, headers, payload):
    r = client.post(MCP_URL, json=payload, headers=headers)
    assert r.status_code in (200, 202), f"{payload.get('method')}: {r.status_code} {r.text[:300]}"
    if r.status_code == 202 or not r.text:
        return r, None
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in r.text.splitlines() if line.startswith("data:")]
        return r, json.loads(data[-1]) if data else None
    return r, r.json()


def test_mcp_tools(api_key):
    headers = {"X-API-Key": api_key["key"], "Accept": "application/json, text/event-stream"}
    with httpx.Client(timeout=300) as c:
        r, init = _mcp_call(c, headers, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "1.0"}}})
        assert init and "result" in init, init
        session = r.headers.get("mcp-session-id")
        if session:
            headers["mcp-session-id"] = session
        _mcp_call(c, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        _, tools = _mcp_call(c, headers, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in tools["result"]["tools"]}
        for expected in ("opencortyx_search", "opencortyx_answer", "opencortyx_stats", "opencortyx_remember"):
            assert expected in names, f"missing MCP tool {expected}"

        for i, (tool, args) in enumerate([("opencortyx_stats", {}),
                                          ("opencortyx_search", {"query": "Who leads Project Orion?"})], 3):
            _, res = _mcp_call(c, headers, {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                            "params": {"name": tool, "arguments": args}})
            assert res and "result" in res and not res["result"].get("isError"), f"{tool}: {res}"


def test_mcp_rejects_missing_credentials():
    r = httpx.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream"}, timeout=30)
    assert r.status_code == 401


def test_revoked_api_key_stops_working(api):
    r = api.post("/apikeys", json={"app": f"revoke-{TAG}", "role": "viewer"})
    key = r.json()
    assert api.delete(f"/apikeys/{key['id']}").status_code == 200
    r = httpx.get(f"{BASE}/api/v2/stats", headers={"X-API-Key": key["key"]}, timeout=30)
    assert r.status_code == 401
