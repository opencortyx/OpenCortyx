"""Hybrid retrieval on a live stack: every search surface, on data whose answers are known.

The engine picks its surfaces per question (attribute vectors, BM25, identifier, fuzzy, entity,
metadata, type-scoped, graph) and fuses what they return, so a surface that silently returns
nothing does not fail anything: the others answer, worse. That is how a document with no vectors
(an embedding job lost to a worker restart) looked healthy until a semantic question found nothing.
This test asks one question aimed at each surface, in a throwaway workspace, and checks both that
the known answer comes back and that the surface it was aimed at actually ran and contributed.

Run inside the api container against a stack whose first-run setup is done:

    docker compose exec api python -m pytest tests/integration/test_hybrid_retrieval.py -m integration -v -s
"""
import csv
import io
import json
import os
import time

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE = os.environ.get("OPENCORTYX_API_URL", "http://localhost:8000").rstrip("/") + "/api/v2"
INGEST_TIMEOUT = float(os.environ.get("OPENCORTYX_INGEST_TIMEOUT", "900"))

EMPLOYEES = [
    {"employee_id": "E-101", "full_name": "Priya Nair", "department": "Engineering", "city": "Pune",
     "start_date": "2025-04-01"},
    {"employee_id": "E-102", "full_name": "Rahul Menon", "department": "Finance", "city": "Kochi",
     "start_date": "2025-09-15"},
    {"employee_id": "E-103", "full_name": "Anita Desai", "department": "Engineering", "city": "Bengaluru",
     "start_date": "2026-01-10"},
]
TICKETS = [
    {"ticket_id": "TKT-7781", "owner": "Priya Nair", "status": "open", "priority": "high",
     "summary": "VPN drops every evening for the remote team"},
    {"ticket_id": "TKT-7782", "owner": "Rahul Menon", "status": "closed", "priority": "low",
     "summary": "Expense report template needs the new tax field"},
]
#: Named entities, as an extraction pipeline or a curator adds them. Uploads alone make none (a
#: table row is labelled by its table, its values are properties), and the entity, fuzzy and
#: type-scoped surfaces search exactly these: names and types.
ENTITIES = [
    {"type": "Person", "label": "Priya Nair", "properties": {"department": "Engineering", "city": "Pune"}},
    {"type": "Person", "label": "Rahul Menon", "properties": {"department": "Finance", "city": "Kochi"}},
    {"type": "Ticket", "label": "TKT-7781", "properties": {"status": "open", "owner": "Priya Nair",
                                                         "summary": "VPN drops every evening for the remote team"}},
]
HANDBOOK = [
    ("Employee Handbook", 22),
    ("1. Joining", 15),
    ("New joiners receive their laptop on the first day and complete the security induction.", 10.5),
    ("2. Leaving the company", 15),
    ("An employee who resigns must serve a notice period of 60 days, which the manager may shorten.", 10.5),
    ("3. Working hours", 15),
    ("The core collaboration hours are 11:00 to 16:00 on working days.", 10.5),
]


def _pdf() -> bytes:
    from tests.pdfwriter import make_pdf
    lines = [(50, 35, "Hybrid Test Corp - Handbook", 8, False)]
    y = 90
    for text, size in HANDBOOK:
        lines.append((50, y, text, size, size > 11))
        y += size * 2.4
    lines.append((50, 812, "Internal - Page 1", 8, False))
    return make_pdf([lines])


def _csv(rows) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode()


@pytest.fixture(scope="module")
def corpus():
    """A throwaway workspace holding a handbook PDF, an employee table, a ticket JSON and a few named
    entities, fully ingested and embedded."""
    from tests.conftest import _drop_org, mint_token
    import uuid

    workspace_id = uuid.uuid4()
    headers = {"Authorization": f"Bearer {mint_token(workspace_id)}", "X-OpenCortyx-Capture": "client"}
    files = [("hybrid-handbook.pdf", _pdf(), "application/pdf"),
             ("hybrid-employees.csv", _csv(EMPLOYEES), "text/csv"),
             ("hybrid-tickets.json", json.dumps(TICKETS).encode(), "application/json")]
    with httpx.Client(base_url=BASE, headers=headers, timeout=120) as c:
        runs = []
        for name, data, mime in files:
            r = c.post("/sources/upload", files={"file": (name, data, mime)})
            r.raise_for_status()
            runs.append(r.json()["run_id"])
        deadline = time.time() + INGEST_TIMEOUT
        while time.time() < deadline:
            states = [c.get(f"/progress/{rid}/outcome").json() for rid in runs]
            if all(not s.get("pending") and s.get("status") in ("completed", "failed") for s in states):
                break
            time.sleep(5)
        else:
            pytest.fail(f"ingestion did not finish within {INGEST_TIMEOUT:.0f}s")
        assert all(s.get("status") == "completed" for s in states), states
        for spec in ENTITIES:
            c.post("/nodes", json=spec).raise_for_status()
        _wait_for_vectors(workspace_id)
        yield c
    _drop_org(workspace_id)


def _wait_for_vectors(workspace_id, timeout: float = 600) -> None:
    """Embedding runs after ingestion, in the embedding worker; wait until every node has vectors."""
    import asyncio

    from opencortyx.core.substrate import NodeRepo

    async def pending():
        repo = NodeRepo()
        return len(await repo.unembedded(workspace_id, await repo.find(workspace_id)))

    deadline = time.time() + timeout
    while (left := asyncio.run(pending())) and time.time() < deadline:
        time.sleep(5)
    assert not left, f"{left} node(s) still had no vectors after {timeout:.0f}s: the embedding worker is stuck"


def _ask(c, question: str) -> dict:
    r = c.post("/context", json={"query": question})
    r.raise_for_status()
    return r.json()


def _trace(res: dict, stage: str) -> str:
    for step in res.get("stage_trace") or []:
        if step.get("stage") == stage:
            return str(step.get("detail") or "")
    return ""


#: Trace names that differ from the policy's surface names.
_TRACE_NAME = {"embedding": "attr_embedding"}


def _surfaces_hit(res: dict) -> dict:
    """{surface: candidates it returned}, from each surface's own trace step ("L1.surface:bm25").

    Not from the fuse step: that one counts only what survived into the fused pool, so a surface
    whose finds the others also made can be missing from it although it ran and found them. The
    graph surface walks from the seeds in L2; its count is the nodes the beam discovered."""
    out: dict = {}
    for step in res.get("stage_trace") or []:
        stage = step.get("stage") or ""
        if stage.startswith("L1.surface:") and "FAILED" not in str(step.get("detail") or ""):
            name = stage.split(":", 1)[1]
            name = _TRACE_NAME.get(name, name)
            out[name] = out.get(name, 0) + int(step.get("n_out") or 0)
        elif stage == "L2.beam":
            out["graph"] = out.get("graph", 0) + int(step.get("n_out") or 0)
    return out


def _failed(res: dict) -> list:
    return [f"{s['stage']}: {s.get('detail')}" for s in res.get("stage_trace") or []
            if s.get("stage", "").startswith("L1.surface:") and "FAILED" in str(s.get("detail") or "")]


def _text(res: dict) -> str:
    parts = [res.get("context") or ""]
    parts += [json.dumps(c, default=str) for c in res.get("citations") or []]
    return " ".join(parts).lower()


#: (surface the question is aimed at, question, text the right answer contains)
CASES = [
    ("attr_embedding", "How long must I wait after handing in my resignation?", "60 days"),
    ("bm25", "security induction laptop", "security induction"),
    ("identifier", "TKT-7781", "vpn"),
    ("fuzzy", "Priya Nayr", "priya nair"),
    ("entity", "Which department does Rahul Menon work in?", "finance"),
    ("metadata", "Which employees started after 2025-12-31?", "anita desai"),
    ("ontology", "Which tickets are still open?", "tkt-7781"),
    ("graph", "What does the handbook say in the Leaving the company section?", "notice period"),
]


@pytest.mark.parametrize("surface,question,expected", CASES, ids=[c[0] for c in CASES])
def test_each_surface_finds_its_answer(corpus, surface, question, expected):
    res = _ask(corpus, question)
    hits = _surfaces_hit(res)
    print(f"\n  [{surface}] {question!r}\n    policy: {_trace(res, 'L1.policy')}\n    returned: {hits}"
          f"\n    fused: {_trace(res, 'L1.fuse')}")
    assert not _failed(res), f"a search surface raised: {_failed(res)}"
    assert expected in _text(res), (
        f"the answer ({expected!r}) was not retrieved; surfaces returned {hits}; "
        f"abstained={res.get('abstained')}")
    assert hits.get(surface, 0) > 0, (
        f"the {surface} surface returned nothing for a question aimed at it ({hits}); "
        f"policy: {_trace(res, 'L1.policy') or 'ran every surface'}")


def test_semantic_search_contributes(corpus):
    """The vectors themselves: a paraphrase that shares no keyword with the text must be found by
    the attribute-vector surface, not only by BM25. With the embeddings missing this is 0."""
    res = _ask(corpus, "How long must I wait after handing in my resignation?")
    assert _surfaces_hit(res).get("attr_embedding", 0) > 0, (
        f"attribute vectors contributed nothing ({_surfaces_hit(res)}): are the embeddings there?")
