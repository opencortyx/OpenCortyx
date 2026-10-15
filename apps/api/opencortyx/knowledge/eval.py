"""Eval harness + feedback loop (§7) — the "no-hallucination" proof.

Two parts, both on the existing substrate (no new tables):
  • Gold suite — store Q→A eval cases as `EvalCase` nodes, run them through the REAL answer pipeline,
    and score groundedness / abstain-accuracy / keyword pass. This is the regression proof that the
    system answers from evidence (or abstains) rather than hallucinating.
  • Feedback loop — 👍/👎 + corrections recorded in the audit log, summarised for review and tuning.
"""

from sqlalchemy import desc, select

from opencortyx.core.contracts import RetrieveContext
from opencortyx.core.db import V2Session
from opencortyx.core.substrate import NodeRepo
from opencortyx.domain.control import AuditLog
from opencortyx.knowledge import authoring

EVAL_TYPE = "EvalCase"


# ── gold Q→A suite (stored as EvalCase nodes) ────────────────────────────────
async def add_case(workspace_id, *, question, expect_keywords=None, should_abstain=False, author=None) -> dict:
    nid = await authoring.create_node(
        workspace_id, type=EVAL_TYPE, label=question[:120],
        properties={"question": question, "text": question,
                    "expect_keywords": expect_keywords or [], "should_abstain": bool(should_abstain),
                    "__internal": True},
        author=author)
    return {"id": str(nid)}


async def list_cases(workspace_id, *, limit=200) -> dict:
    rows = await NodeRepo().find(workspace_id, exact_types=[EVAL_TYPE], order_by="-created_at", limit=limit)
    return {"items": [{
        "id": str(n.id),
        "question": (n.properties or {}).get("question") or n.label,
        "expect_keywords": (n.properties or {}).get("expect_keywords") or [],
        "should_abstain": bool((n.properties or {}).get("should_abstain")),
    } for n in rows]}


async def delete_case(workspace_id, node_id) -> dict:
    ok = await authoring.delete_node(workspace_id, node_id)
    return {"status": "ok" if ok else "not_found"}


# Starter gold set. Operators edit/extend it; quality depends on domain knowledge. Idempotent.
_GOLD = [
    {"question": "What is the cash handling limit at the Berlin branch?", "expect_keywords": ["50,000"]},
    {"question": "What hobbies does Alice Chen have?", "expect_keywords": ["Photography"]},
    {"question": "What is Alice Chen's eye health status?", "expect_keywords": ["myopia"]},
    {"question": "What is the resting heart rate column in the fitness log?", "expect_keywords": ["resting_hr"]},
    {"question": "What is the population of the planet Zarthax in our data?", "should_abstain": True},
]


async def seed_gold(workspace_id, author=None) -> dict:
    """Seed a starter gold suite if those questions are not already present. Idempotent."""
    existing = {c["question"] for c in (await list_cases(workspace_id))["items"]}
    seeded = 0
    for g in _GOLD:
        if g["question"] in existing:
            continue
        await add_case(workspace_id, question=g["question"], expect_keywords=g.get("expect_keywords"),
                       should_abstain=g.get("should_abstain", False), author=author)
        seeded += 1
    return {"seeded": seeded}


async def score_case(workspace_id, case: dict) -> dict:
    """Run ONE gold case through the real answer pipeline and score it. Returns the verdict PLUS the
    actual answer and citations it retrieved, so the UI can expand a (passing OR failing) case and show
    exactly what came back — e.g. a 'keywords missing' case still surfaces its 8 cites + answer text."""
    from opencortyx.retrieval.answering.answer import answer_query
    ctx = RetrieveContext(workspace_id=workspace_id)
    try:
        res = await answer_query(case["question"], ctx)
    except Exception as e:
        return {"id": case["id"], "question": case["question"], "pass": False,
                "reason": f"error: {e}"[:160], "answer": "", "cites": []}

    abstained = bool(res.get("abstained"))
    answer = str(res.get("answer") or "")
    cites = res.get("citations") or []
    # trim citations to a UI-friendly shape (type · score · snippet · label)
    cites_view = [{"type": c.get("type"), "score": c.get("score"), "label": c.get("label"),
                   "text": (c.get("text") or "")[:240]} for c in cites][:12]

    if case["should_abstain"]:
        ok = abstained
        return {"id": case["id"], "question": case["question"], "pass": ok,
                "abstained": abstained, "should_abstain": True,
                "reason": "" if ok else "should have abstained",
                "answer": answer, "cites": cites_view}

    kw = list(case["expect_keywords"] or [])
    kw_hit = all(k.lower() in answer.lower() for k in kw) if kw else True
    grounded = (not abstained) and len(cites) > 0
    ok = grounded and kw_hit and not abstained
    return {"id": case["id"], "question": case["question"], "pass": ok,
            "abstained": abstained, "grounded": grounded, "keyword_hit": kw_hit,
            "citations": len(cites), "trust_score": res.get("trust_score"),
            "reason": "" if ok else (
                "abstained" if abstained else
                ("no citations" if not cites else "expected keywords missing")),
            "answer": answer, "cites": cites_view}


def summarize_results(cases: list, results: list) -> dict:
    """Aggregate per-case results into the suite summary (shared by the batch + streamed paths)."""
    passed = sum(1 for r in results if r.get("pass"))
    grounded_n = sum(1 for r in results if r.get("grounded"))
    abstain_total = sum(1 for c in cases if c.get("should_abstain"))
    abstain_ok = sum(1 for r in results if r.get("should_abstain") and r.get("pass"))
    total = len(cases) or 1
    answerable = sum(1 for c in cases if not c.get("should_abstain")) or 1
    return {
        "cases": len(cases),
        "passed": passed,
        "pass_rate": round(passed / total, 3),
        "groundedness": round(grounded_n / answerable, 3),
        "abstain_accuracy": round(abstain_ok / abstain_total, 3) if abstain_total else None,
        "results": results,
    }


async def run_suite(workspace_id, *, limit=50) -> dict:
    """Run every gold case through the real answer pipeline and score it (batch)."""
    cases = (await list_cases(workspace_id, limit=limit))["items"]
    results = [await score_case(workspace_id, c) for c in cases]
    return summarize_results(cases, results)


# ── feedback loop (👍/👎 + corrections → audit log) ───────────────────────────
def _norm_rating(rating) -> str:
    return "up" if str(rating).lower() in ("up", "1", "positive", "good", "👍", "true") else "down"


async def record_feedback(workspace_id, *, query, rating, answer=None, correction=None, actor=None) -> dict:
    async with V2Session() as db:
        db.add(AuditLog(workspace_id=workspace_id, actor=(str(actor) if actor else None),
                        action=f"feedback/{_norm_rating(rating)}",
                        meta={"query": query, "answer": (answer or "")[:600], "correction": correction or ""}))
        await db.commit()
    return {"status": "ok", "rating": _norm_rating(rating)}


async def feedback_summary(workspace_id, *, limit=100) -> dict:
    async with V2Session() as db:
        rows = (await db.execute(select(AuditLog).where(
            AuditLog.workspace_id == workspace_id, AuditLog.action.like("feedback/%"))
            .order_by(desc(AuditLog.ts)).limit(limit))).scalars().all()
    up = sum(1 for r in rows if (r.action or "").endswith("/up"))
    down = sum(1 for r in rows if (r.action or "").endswith("/down"))
    recent = [{"rating": (r.action or "").split("/")[-1],
               "query": (r.meta or {}).get("query"),
               "correction": (r.meta or {}).get("correction"),
               "actor": r.actor,
               "at": r.ts.isoformat() if r.ts else None} for r in rows[:25]]
    return {"up": up, "down": down, "total": up + down,
            "satisfaction": round(up / (up + down), 3) if (up + down) else None,
            "recent": recent}
