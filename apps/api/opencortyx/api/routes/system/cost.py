"""Routes: what the models cost - this month against the budget, by use, by run, and in advance.

Every number comes from `llm_usage` (core/inference/usage.py), one row per model call. Rows the
provider or the local tokenizer counted are `measured`; the rest are estimates, and each response
says how much of it was measured so an estimate is never read as a bill.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Integer, cast, func, select

from opencortyx.api.schemas.system.cost import BudgetSpec
from opencortyx.core.db import V2Session
from opencortyx.core.inference import usage
from opencortyx.core.security.auth import CurrentUser, require_admin, require_auth, require_write
from opencortyx.domain.control import CostBudget, IngestRun
from opencortyx.domain.governance import LlmUsage

router = APIRouter()

#: What a run read, in the unit its own result counts: records, rows, or the chunks of a document.
_UNIT_KEYS = ("records", "rows", "chunks", "nodes_seen")


def _sums():
    return (func.count(LlmUsage.id),
            func.coalesce(func.sum(LlmUsage.tokens_in), 0),
            func.coalesce(func.sum(LlmUsage.tokens_out), 0),
            func.coalesce(func.sum(LlmUsage.cost_usd), 0.0),
            func.coalesce(func.sum(cast(LlmUsage.measured, Integer)), 0),
            func.coalesce(func.sum(cast(LlmUsage.cached, Integer)), 0),
            func.coalesce(func.sum(cast(LlmUsage.paid, Integer)), 0))


def _row(calls, tin, tout, usd, measured, cached, paid) -> dict:
    calls = int(calls or 0)
    return {"calls": calls, "tokens_in": int(tin or 0), "tokens_out": int(tout or 0),
            "tokens": int(tin or 0) + int(tout or 0), "cost_usd": round(float(usd or 0.0), 6),
            "measured_share": round(int(measured or 0) / calls, 4) if calls else None,
            "cached_calls": int(cached or 0), "paid_calls": int(paid or 0)}


@router.get("/cost")
async def cost_status(user: CurrentUser = Depends(require_auth)):
    """This month's spend against the workspace budget. `spent_tokens` counts PAID tokens (what the
    budget caps); `total_tokens` counts every call, free models included."""
    async with V2Session() as db:
        b = await db.get(CostBudget, user.workspace_id)
        totals = (await db.execute(select(*_sums()).where(
            LlmUsage.workspace_id == user.workspace_id, LlmUsage.ts >= usage.month_start()))).one()
    spent = await usage.month_spend(user.workspace_id, paid_only=True)
    month = _row(*totals)
    budget = b.monthly_token_budget if b else 0
    thr = b.alert_threshold if b else 0.8
    rate = b.usd_per_1k_tokens if b else 0.0
    frac = (spent / budget) if budget else 0.0
    usd = month["cost_usd"] or (round(spent / 1000 * rate, 4) if rate else None)
    return {
        "monthly_token_budget": budget, "alert_threshold": thr,
        "hard_cap": bool(b.hard_cap) if b else False, "usd_per_1k_tokens": rate,
        "spent_tokens": spent, "total_tokens": month["tokens"], "calls": month["calls"],
        "measured_share": month["measured_share"], "cached_calls": month["cached_calls"],
        "fraction": round(frac, 4),
        "alert": bool(budget) and frac >= thr, "over": bool(budget) and spent >= budget,
        "spent_usd": usd,
        "budget_usd": round(budget / 1000 * rate, 2) if (rate and budget) else None,
        "month_start": usage.month_start().isoformat(),
    }


@router.put("/cost/budget")
async def set_budget(spec: BudgetSpec, user: CurrentUser = Depends(require_write)):
    """Set this workspace's monthly token budget, alert threshold, hard cap and optional $ rate."""
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
    usage.forget_budget(user.workspace_id)
    return {"status": "ok"}


@router.get("/cost/usage")
async def cost_usage(days: int = Query(30, ge=1, le=366), user: CurrentUser = Depends(require_auth)):
    """Spend over the last `days`, by purpose (answers, each extractor, summaries...), by model and
    by day."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    where = (LlmUsage.workspace_id == user.workspace_id, LlmUsage.ts >= since)
    day = func.date_trunc("day", LlmUsage.ts)
    async with V2Session() as db:
        by_purpose = (await db.execute(select(LlmUsage.purpose, *_sums()).where(*where)
                                       .group_by(LlmUsage.purpose).order_by(func.count(LlmUsage.id).desc()))).all()
        by_model = (await db.execute(select(LlmUsage.provider, LlmUsage.model, *_sums()).where(*where)
                                     .group_by(LlmUsage.provider, LlmUsage.model))).all()
        by_day = (await db.execute(select(day, *_sums()).where(*where).group_by(day).order_by(day))).all()
        total = (await db.execute(select(*_sums()).where(*where))).one()
    return {"days": days, "total": _row(*total),
            "by_purpose": [{"purpose": r[0], **_row(*r[1:])} for r in by_purpose],
            "by_model": [{"provider": r[0], "model": r[1], **_row(*r[2:])} for r in by_model],
            "by_day": [{"day": r[0].date().isoformat() if r[0] else None, **_row(*r[1:])} for r in by_day]}


def _units(result: dict) -> Optional[int]:
    for k in _UNIT_KEYS:
        v = (result or {}).get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


@router.get("/cost/runs")
async def cost_runs(limit: int = Query(50, ge=1, le=500), user: CurrentUser = Depends(require_auth)):
    """Model spend of the latest ingest runs, with tokens and $ per 1,000 records read."""
    async with V2Session() as db:
        runs = (await db.execute(select(IngestRun).where(IngestRun.workspace_id == user.workspace_id)
                                 .order_by(IngestRun.created_at.desc()).limit(limit))).scalars().all()
        ids = [str(r.id) for r in runs]
        spent = {r[0]: _row(*r[1:]) for r in (await db.execute(
            select(LlmUsage.run_id, *_sums()).where(LlmUsage.workspace_id == user.workspace_id,
                                                    LlmUsage.run_id.in_(ids))
            .group_by(LlmUsage.run_id))).all()} if ids else {}
    out = []
    for r in runs:
        s = spent.get(str(r.id)) or _row(0, 0, 0, 0, 0, 0, 0)
        units = _units(r.result or {})
        per = (1000.0 / units) if units else None
        out.append({"run_id": str(r.id), "asset": r.asset, "kind": r.kind, "status": r.status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "records": units, **s,
                    "tokens_per_1k_records": round(s["tokens"] * per) if per else None,
                    "cost_usd_per_1k_records": round(s["cost_usd"] * per, 6) if per else None})
    return {"runs": out}


@router.get("/cost/estimate")
async def cost_estimate(purpose: str = Query(..., min_length=1, max_length=120),
                        records: int = Query(..., ge=1, le=100_000_000),
                        user: CurrentUser = Depends(require_auth)):
    """What reading `records` more records with one extractor will cost, from what its last calls
    cost (`purpose` = "extract:<extractor name>"; a preview counts too). One model call per record
    read is assumed, as the extractors make. No history: no estimate - run a preview first."""
    names = {purpose, purpose.replace("extract:", "preview:", 1)}
    async with V2Session() as db:
        recent = (select(LlmUsage.tokens_in, LlmUsage.tokens_out, LlmUsage.cost_usd, LlmUsage.ms,
                         LlmUsage.measured)
                  .where(LlmUsage.workspace_id == user.workspace_id, LlmUsage.purpose.in_(names),
                         LlmUsage.ok.is_(True), LlmUsage.cached.is_(False))
                  .order_by(LlmUsage.ts.desc()).limit(200).subquery())
        row = (await db.execute(select(func.count(), func.avg(recent.c.tokens_in), func.avg(recent.c.tokens_out),
                                       func.avg(recent.c.cost_usd), func.avg(recent.c.ms),
                                       func.avg(cast(recent.c.measured, Integer))))).one()
    n = int(row[0] or 0)
    if not n:
        return {"purpose": purpose, "records": records, "sample_calls": 0, "estimate": None,
                "reason": "No calls of this extractor yet. Run a preview on a few records first."}
    tin, tout, usd, ms = (float(row[1] or 0), float(row[2] or 0), float(row[3] or 0), float(row[4] or 0))
    return {"purpose": purpose, "records": records, "sample_calls": n,
            "measured_share": round(float(row[5] or 0), 4),
            "per_record": {"tokens_in": round(tin), "tokens_out": round(tout), "cost_usd": round(usd, 6),
                           "seconds": round(ms / 1000.0, 2)},
            "estimate": {"tokens": round((tin + tout) * records), "cost_usd": round(usd * records, 4),
                         # sequential: parallel extraction finishes sooner
                         "hours_sequential": round(ms * records / 3_600_000.0, 2)}}


@router.delete("/cost/cache")
async def clear_cache(user: CurrentUser = Depends(require_admin)):
    """Forget every cached extraction answer of this workspace (the next read pays again)."""
    return {"status": "ok", "deleted": await usage.drop_cache(user.workspace_id)}
