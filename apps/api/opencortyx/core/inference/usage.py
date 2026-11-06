"""The meter: every model call, what it was for, and what it cost - measured where it can be.

`llm.complete` and `llm.stream_complete` are the only doors to a generation model, so the meter sits
in them and nothing else has to remember to count:

    with usage.purpose("extract:Invoices"):        # what the calls inside are for
        await complete(...)                        # one llm_usage row, attributed

Attribution needs no plumbing. The workspace and the ingest run come from the log context the
worker binds for every task (core/observability.bind_context); `purpose` names the use, and without
one the task's own name is used. An answer request sets both itself (`scope`).

MEASURED vs ESTIMATED. A hosted provider returns its own token counts and the in-process model
counts with its tokenizer; those rows are `measured`. A stream from an OpenAI-compatible server
usually reports nothing, and a failed call reports nothing either; those are estimated at four
characters per token and say so. Four characters per token is wrong for CJK text, code and JSON
(often 1-2), so an estimate is never presented as a measurement.

THE BUDGET. `CostBudget` (domain/control.py) caps a workspace's paid tokens per calendar month (UTC).
`check_budget` runs before every PAID call - answers and ingest alike - and raises `BudgetExceeded`
once the month's spend plus what this call may use (`max_tokens` plus the prompt) passes the cap.
Free models (the in-process one, your own server) are never refused. Spend is read from the
database at most every `_SPEND_TTL` seconds per workspace and counted up locally in between, so
several workers may overshoot the cap by what they spend inside that window; that bound is the
price of not querying the ledger before every call.

THE CACHE. A deterministic call (JSON out, temperature <= 0.2, made for extraction) is answered
from `llm_cache` when the exact same request was answered before. Reading an unchanged record
again then costs nothing. The cached answer holds what the model said about a record, so it
carries the asset it was read from and is deleted with that asset.
"""
from __future__ import annotations

import contextlib
import hashlib
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

from opencortyx.core.observability import get_logger

log = get_logger(__name__)

_purpose: ContextVar[Optional[str]] = ContextVar("llm_purpose", default=None)
_workspace: ContextVar[Optional[str]] = ContextVar("llm_workspace", default=None)
_run: ContextVar[Optional[str]] = ContextVar("llm_run", default=None)

#: Characters per token for an estimate. See the module docstring for why this is only an estimate.
CHARS_PER_TOKEN = 4
#: How long a workspace's month spend is trusted before it is read again.
_SPEND_TTL = 10.0
#: Calls made for these purposes may be answered from the cache.
CACHEABLE_PREFIXES = ("extract", "preview")


class BudgetExceeded(RuntimeError):
    """This workspace's paid-token budget for the month is spent, and its hard cap is on."""

    def __init__(self, spent: int, budget: int):
        self.spent, self.budget = int(spent), int(budget)
        super().__init__(
            f"Monthly token budget reached ({self.spent:,} of {self.budget:,} paid tokens). Raise it "
            "in Settings → Cost, turn off the hard cap, or use a local (free) model.")


# ── attribution ───────────────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def purpose(name: str):
    token = _purpose.set(str(name)[:120] if name else None)
    try:
        yield
    finally:
        _purpose.reset(token)


@contextlib.contextmanager
def scope(workspace_id=None, run_id=None, purpose_name: Optional[str] = None):
    """Attribute the calls inside to a workspace (and run, and purpose) explicitly."""
    tokens = [(_workspace, _workspace.set(str(workspace_id) if workspace_id else _workspace.get())),
              (_run, _run.set(str(run_id) if run_id else _run.get()))]
    if purpose_name:
        tokens.append((_purpose, _purpose.set(str(purpose_name)[:120])))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


def bind(workspace_id=None, purpose_name: Optional[str] = None, run_id=None) -> None:
    """Set the attribution for the rest of this task, with no reset: for a request handler or an
    async generator, which may finish in another context than it started in (a context manager's
    reset would then fail). Each request runs in its own task, so nothing leaks between requests."""
    if workspace_id:
        _workspace.set(str(workspace_id))
    if run_id:
        _run.set(str(run_id))
    if purpose_name:
        _purpose.set(str(purpose_name)[:120])


def _ambient() -> dict:
    try:
        from opencortyx.core import observability as obs
        return dict(obs._ctx.get() or {})
    except Exception:                                         # noqa: BLE001
        return {}


def attribution() -> dict:
    """{workspace_id, run_id, purpose} for a call made now."""
    amb = _ambient()
    return {"workspace_id": _workspace.get() or amb.get("workspace_id"),
            "run_id": _run.get() or amb.get("run_id"),
            "purpose": _purpose.get() or amb.get("task") or "other"}


def estimate(text: str | None) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN if text else 0


def is_paid(spec: Optional[dict]) -> bool:
    from opencortyx.core.config import LOCAL_FREE_PROVIDERS
    prov = ((spec or {}).get("provider") or "").lower()
    return bool(prov) and prov not in LOCAL_FREE_PROVIDERS


def price(spec: Optional[dict], tokens_in: int, tokens_out: int) -> float:
    """$ for a call, from the model's own registry row (`params.price_in_per_1m` /
    `params.price_out_per_1m`). No price on the row: 0, which the UI shows as "no price set"."""
    params = (spec or {}).get("params") or {}
    try:
        pin = float(params.get("price_in_per_1m") or 0.0)
        pout = float(params.get("price_out_per_1m") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return round(tokens_in * pin / 1e6 + tokens_out * pout / 1e6, 8)


# ── the ledger ────────────────────────────────────────────────────────────────────────────────

def _uuid(v):
    if not v:
        return None
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (ValueError, AttributeError):
        return None


async def record(spec: Optional[dict], *, tokens_in: int, tokens_out: int, measured: bool, ms: int,
                 ok: bool = True, cached: bool = False) -> None:
    """Write one llm_usage row. Never raises: the meter must not fail the call it measured."""
    att = attribution()
    paid = is_paid(spec) and not cached
    cost = price(spec, tokens_in, tokens_out) if paid else 0.0
    ws = _uuid(att["workspace_id"])
    try:
        from opencortyx.core.db import V2Session
        from opencortyx.domain.governance import LlmUsage
        async with V2Session() as db:
            db.add(LlmUsage(workspace_id=ws, run_id=(str(att["run_id"])[:80] if att["run_id"] else None),
                            purpose=str(att["purpose"])[:120], role=(spec or {}).get("role"),
                            provider=(spec or {}).get("provider"), model=((spec or {}).get("model_id") or "")[:200],
                            tokens_in=int(tokens_in), tokens_out=int(tokens_out), measured=bool(measured),
                            paid=paid, cached=cached, ok=ok, ms=int(ms), cost_usd=cost))
            await db.commit()
    except Exception as e:                                    # noqa: BLE001
        log.debug("usage.record_failed", error=str(e)[:200])
    if paid and ws is not None:
        _spent_add(ws, tokens_in + tokens_out)


# ── the budget ────────────────────────────────────────────────────────────────────────────────

_spend: dict = {}        # workspace -> (read_at, month, spent)
_budgets: dict = {}      # workspace -> (read_at, (budget, hard_cap) | None)


def month_start(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _spent_add(ws, n: int) -> None:
    hit = _spend.get(ws)
    if hit and hit[1] == month_start():
        _spend[ws] = (hit[0], hit[1], hit[2] + int(n))


async def month_spend(workspace_id, *, paid_only: bool = True) -> int:
    """Tokens this workspace has spent this calendar month (UTC), from the ledger."""
    from sqlalchemy import func, select
    from opencortyx.core.db import V2Session
    from opencortyx.domain.governance import LlmUsage
    q = select(func.coalesce(func.sum(LlmUsage.tokens_in + LlmUsage.tokens_out), 0)).where(
        LlmUsage.workspace_id == _uuid(workspace_id), LlmUsage.ts >= month_start(),
        LlmUsage.cached.is_(False))
    if paid_only:
        q = q.where(LlmUsage.paid.is_(True))
    async with V2Session() as db:
        return int((await db.execute(q)).scalar() or 0)


async def _budget(ws) -> Optional[tuple]:
    hit = _budgets.get(ws)
    if hit and time.monotonic() - hit[0] < _SPEND_TTL:
        return hit[1]
    from opencortyx.core.db import V2Session
    from opencortyx.domain.control import CostBudget
    async with V2Session() as db:
        row = await db.get(CostBudget, ws)
    value = (int(row.monthly_token_budget or 0), bool(row.hard_cap)) if row else None
    _budgets[ws] = (time.monotonic(), value)
    return value


def forget_budget(workspace_id) -> None:
    """Drop the cached budget and spend after the budget is edited."""
    ws = _uuid(workspace_id)
    _budgets.pop(ws, None)
    _spend.pop(ws, None)


async def check_budget(spec: Optional[dict], reserve_tokens: int) -> None:
    """Raise BudgetExceeded when a PAID call would pass the month's hard cap. Free models pass."""
    if not is_paid(spec):
        return
    ws = _uuid(attribution()["workspace_id"])
    if ws is None:
        return
    try:
        b = await _budget(ws)
    except Exception:                                         # noqa: BLE001 - no table yet, no cap
        return
    if not b or not b[1] or b[0] <= 0:
        return
    month = month_start()
    hit = _spend.get(ws)
    if not hit or hit[1] != month or time.monotonic() - hit[0] >= _SPEND_TTL:
        try:
            spent = await month_spend(ws)
        except Exception:                                     # noqa: BLE001
            return
        _spend[ws] = (time.monotonic(), month, spent)
    spent = _spend[ws][2]
    if spent + max(0, int(reserve_tokens)) > b[0]:
        raise BudgetExceeded(spent, b[0])


# ── the cache ─────────────────────────────────────────────────────────────────────────────────

def cacheable(json_mode: bool, temperature: float) -> bool:
    if not json_mode or float(temperature or 0.0) > 0.2:
        return False
    from opencortyx.core.config import runtime as rc
    try:
        if not rc.get("llm_cache"):
            return False
    except Exception:                                         # noqa: BLE001
        return False
    p = attribution()["purpose"] or ""
    return any(p.startswith(x) for x in CACHEABLE_PREFIXES)


def cache_key(spec: Optional[dict], prompt: str, system: Optional[str], max_tokens: int,
              temperature: float, json_mode: bool) -> str:
    s = spec or {}
    raw = "\x1f".join(str(x) for x in (s.get("provider"), s.get("model_id"), s.get("base_url"),
                                       system or "", prompt or "", max_tokens, temperature, json_mode))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


async def cache_get(key: str) -> Optional[tuple]:
    """(response, tokens_in, tokens_out) or None."""
    ws = _uuid(attribution()["workspace_id"])
    if ws is None:
        return None
    try:
        from sqlalchemy import update
        from opencortyx.core.db import V2Session
        from opencortyx.domain.governance import LlmCache
        async with V2Session() as db:
            row = await db.get(LlmCache, key)
            if row is None or row.workspace_id != ws:
                return None
            await db.execute(update(LlmCache).where(LlmCache.key == key).values(used_at=datetime.now(timezone.utc)))
            await db.commit()
            return row.response, int(row.tokens_in or 0), int(row.tokens_out or 0)
    except Exception:                                         # noqa: BLE001
        return None


async def cache_put(key: str, response: str, tokens_in: int, tokens_out: int) -> None:
    ws = _uuid(attribution()["workspace_id"])
    if ws is None or not response:
        return
    asset = None
    try:
        from opencortyx.core.dataplane import evidence
        cur = evidence.current()
        asset = _uuid(cur.asset_id) if cur is not None else None
    except Exception:                                         # noqa: BLE001
        asset = None
    try:
        from sqlalchemy.dialects.postgresql import insert
        from opencortyx.core.db import V2Session
        from opencortyx.domain.governance import LlmCache
        async with V2Session() as db:
            await db.execute(insert(LlmCache).values(
                key=key, workspace_id=ws, asset_id=asset, response=response,
                tokens_in=int(tokens_in), tokens_out=int(tokens_out)).on_conflict_do_nothing())
            await db.commit()
    except Exception:                                         # noqa: BLE001
        pass


async def drop_cache(workspace_id, asset_id=None) -> int:
    """Delete cached answers: one asset's, or the whole workspace's."""
    from sqlalchemy import delete
    from opencortyx.core.db import V2Session
    from opencortyx.domain.governance import LlmCache
    q = delete(LlmCache).where(LlmCache.workspace_id == _uuid(workspace_id))
    if asset_id is not None:
        q = q.where(LlmCache.asset_id == _uuid(asset_id))
    async with V2Session() as db:
        res = await db.execute(q)
        await db.commit()
    return int(res.rowcount or 0)


# ── one call, measured ────────────────────────────────────────────────────────────────────────

class Meter:
    """Times one call and records it. `done(text, measured_counts)` on success, `failed()` on error."""

    def __init__(self, spec: Optional[dict], prompt: str, system: Optional[str]):
        self.spec, self.t0 = spec, time.monotonic()
        self.est_in = estimate(prompt) + estimate(system)

    def _ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    async def done(self, text: str, counts: Optional[tuple] = None, *, cached: bool = False) -> None:
        if counts and counts[0] is not None and counts[1] is not None:
            await record(self.spec, tokens_in=int(counts[0]), tokens_out=int(counts[1]), measured=True,
                         ms=self._ms(), cached=cached)
        else:
            await record(self.spec, tokens_in=self.est_in, tokens_out=estimate(text), measured=False,
                         ms=self._ms(), cached=cached)

    async def failed(self) -> None:
        # A failed call is often billed for its prompt; recorded as an estimate so it is not invisible.
        await record(self.spec, tokens_in=self.est_in, tokens_out=0, measured=False, ms=self._ms(), ok=False)


__all__ = ["BudgetExceeded", "Meter", "attribution", "bind", "cache_get", "cache_key", "cache_put", "cacheable",
           "check_budget", "drop_cache", "estimate", "forget_budget", "is_paid", "month_spend",
           "month_start", "price", "purpose", "record", "scope"]
