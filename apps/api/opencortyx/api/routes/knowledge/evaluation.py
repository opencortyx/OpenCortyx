"""Routes: evaluation cases, runs and answer feedback."""
import uuid

from fastapi import APIRouter, Depends, HTTPException

from opencortyx.api.schemas.knowledge.evaluation import EvalCaseSpec, FeedbackSpec
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write

router = APIRouter()


@router.get("/eval/cases")
async def list_eval_cases_ep(user: CurrentUser = Depends(require_auth)):
    from opencortyx.knowledge import eval as evalmod
    return await evalmod.list_cases(user.workspace_id)


@router.post("/eval/cases")
async def add_eval_case_ep(spec: EvalCaseSpec, user: CurrentUser = Depends(require_write)):
    from opencortyx.knowledge import eval as evalmod
    return await evalmod.add_case(user.workspace_id, question=spec.question,
                                  expect_keywords=spec.expect_keywords,
                                  should_abstain=spec.should_abstain, author=user.user_id)


@router.delete("/eval/cases/{node_id}")
async def delete_eval_case_ep(node_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    from opencortyx.knowledge import eval as evalmod
    return await evalmod.delete_case(user.workspace_id, node_id)


@router.post("/eval/run")
async def run_eval_ep(user: CurrentUser = Depends(require_write)):
    """Run the gold suite through the real answer pipeline and score groundedness / abstain / keywords."""
    from opencortyx.knowledge import eval as evalmod
    return await evalmod.run_suite(user.workspace_id)


@router.post("/eval/run-case/{case_id}")
async def run_eval_case_ep(case_id: str, user: CurrentUser = Depends(require_write)):
    """Score ONE gold case (lets the UI show per-case progress + expand the answer/citations it got)."""
    from opencortyx.knowledge import eval as evalmod
    cases = (await evalmod.list_cases(user.workspace_id))["items"]
    case = next((c for c in cases if c["id"] == case_id), None)
    if not case:
        raise HTTPException(status_code=404, detail="eval case not found")
    return await evalmod.score_case(user.workspace_id, case)


@router.post("/eval/seed")
async def seed_eval_ep(user: CurrentUser = Depends(require_write)):
    """Seed a starter gold suite (idempotent). Operators then edit and extend it for their domains."""
    from opencortyx.knowledge import eval as evalmod
    return await evalmod.seed_gold(user.workspace_id, author=user.user_id)


@router.post("/feedback")
async def feedback_ep(spec: FeedbackSpec, user: CurrentUser = Depends(require_write)):
    from opencortyx.knowledge import eval as evalmod
    return await evalmod.record_feedback(user.workspace_id, query=spec.query, rating=spec.rating,
                                         answer=spec.answer, correction=spec.correction,
                                         actor=user.user_id)


@router.get("/feedback")
async def feedback_summary_ep(user: CurrentUser = Depends(require_auth)):
    from opencortyx.knowledge import eval as evalmod
    return await evalmod.feedback_summary(user.workspace_id)
