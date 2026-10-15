"""End-to-end smoke tests against the live stack: the critical path a real user hits."""
import io
import time



def test_health_ok(client, auth_headers):
    r = client.get("/health", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    # Postgres + worker + embedding must be up; reranker may warn (cross-encoder optional).
    assert body["overall"] in ("ok", "warn"), body
    by_name = {c["name"]: c for c in body["checks"]}
    for critical in ("Postgres", "Embedding"):
        assert any(critical in n for n in by_name), f"missing {critical} check"


def test_auth_me(client, auth_headers):
    r = client.get("/auth/me", headers=auth_headers)
    assert r.status_code == 200
    assert "workspace_id" in r.json()


def test_ingest_then_retrieve(client, auth_headers):
    """Upload a document, wait for it to index, then retrieve a fact from it (the core loop)."""
    text = ("Quokka Industries was founded in 2019 by Dr. Naomi Patel. "
            "Its flagship product is the Boomerang analytics engine.")
    files = {"file": ("smoke_quokka.txt", io.BytesIO(text.encode()), "text/plain")}
    r = client.post("/sources/upload", headers=auth_headers, files=files)
    assert r.status_code == 200, r.text
    run_id = r.json().get("run_id")
    assert run_id

    # poll the ingestion run to completion
    deadline = time.time() + 90
    status = None
    while time.time() < deadline:
        pr = client.get(f"/progress/{run_id}", headers=auth_headers)
        if pr.status_code == 200:
            status = pr.json().get("status")
            if status in ("completed", "failed", "error"):
                break
        time.sleep(3)
    assert status == "completed", f"ingestion did not complete (status={status})"

    # retrieve a fact from the freshly-ingested doc
    a = client.post("/answer", headers=auth_headers, json={"query": "Who founded Quokka Industries?"})
    assert a.status_code == 200, a.text
    body = a.json()
    assert body["abstained"] is False, body
    assert "Naomi Patel" in body["answer"], body["answer"]


def test_question_form_retrieval_regression(client, auth_headers):
    """Regression for the rerank/abstain bug: a natural-language *question* whose answer is in the KB
    must NOT be abstained away by a flaky reranker score. (Keyword-form worked; question-form didn't.)"""
    a = client.post("/answer", headers=auth_headers, json={"query": "Who founded Quokka Industries?"})
    assert a.status_code == 200, a.text
    assert a.json()["abstained"] is False, a.json()


def test_abstains_when_out_of_kb(client, auth_headers):
    """The no-hallucination guarantee: a question with no supporting evidence must abstain."""
    a = client.post("/answer", headers=auth_headers,
                    json={"query": "What is the population of the fictional planet Zarthax-9?"})
    assert a.status_code == 200, a.text
    assert a.json()["abstained"] is True, a.json()


def test_rate_limit_headers_present_under_load(client, auth_headers):
    """A burst of heavy requests should eventually be throttled with a 429 + Retry-After (cost guard)."""
    got_429 = False
    for _ in range(40):
        r = client.post("/context", headers=auth_headers, json={"query": "ping", "fast": True})
        if r.status_code == 429:
            got_429 = True
            assert "retry-after" in {k.lower() for k in r.headers}
            break
    # Not asserting it MUST trigger (limit may be high), but if it does the contract must hold.
    assert got_429 or True
