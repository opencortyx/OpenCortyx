"""Routes: LLM token spend and the monthly budget."""
from fastapi import APIRouter, Depends

from opencortyx.api.schemas.system.cost import BudgetSpec
from opencortyx.api.services.knowledge.context import month_token_spend
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.control import CostBudget

router = APIRouter()


@router.get("/cost")
async def cost_status(user: CurrentUser = Depends(require_auth)):
    """Current month's LLM spend vs. the org budget, with alert / over flags and optional $ estimate."""
    async with V2Session() as db:
        b = await db.get(CostBudget, user.workspace_id)
        spent = await month_token_spend(db, user.workspace_id)
    budget = b.monthly_token_budget if b else 0
    thr = b.alert_threshold if b else 0.8
    rate = b.usd_per_1k_tokens if b else 0.0
    frac = (spent / budget) if budget else 0.0
    return {
        "monthly_token_budget": budget, "alert_threshold": thr,
        "hard_cap": bool(b.hard_cap) if b else False, "usd_per_1k_tokens": rate,
        "spent_tokens": spent, "fraction": round(frac, 4),
        "alert": bool(budget) and frac >= thr, "over": bool(budget) and spent >= budget,
        "spent_usd": round(spent / 1000 * rate, 4) if rate else None,
        "budget_usd": round(budget / 1000 * rate, 2) if (rate and budget) else None,
    }


@router.put("/cost/budget")
async def set_budget(spec: BudgetSpec, user: CurrentUser = Depends(require_write)):
    """Set this org's monthly token budget, alert threshold, hard cap and optional $ rate."""
    async with V2Session() as db:
        b = await db.get(CostBudget, user.workspace_id)
        if not b:
            b = CostBudget(workspace_id=user.workspace_id)
            db.add(b)
        b.monthly_token_budget = max(0, int(spec.monthly_token_budget))
        b.alert_threshold = min(1.0, max(0.0, float(spec.alert_threshold)))
        b.hard_cap = bool(spec.hard_cap)
        b.usd_per_1k_tokens = max(0.0, float(spec.usd_per_1k_tokens))
        await db.commit()
    return {"status": "ok"}
