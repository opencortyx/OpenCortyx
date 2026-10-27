"""Services: grounded context retrieval and cited answers."""
import uuid

from fastapi import HTTPException, Request
from sqlalchemy import select

from opencortyx.domain.control import AuditLog
from opencortyx.core.security.auth import CurrentUser
from opencortyx.core.contracts import RetrieveContext
from opencortyx.core.db import V2Session
from opencortyx.core.observability import get_logger

log = get_logger(__name__)


def _groups(user) -> list:
    """Derive policy groups from the caller (default empty → full org access at 'normal')."""
    g = []
    for attr in ("role", "groups", "team"):
        v = getattr(user, attr, None)
        if isinstance(v, str):
            g.append(v)
        elif isinstance(v, (list, tuple)):
            g.extend(v)
    return g


async def scoped_ctx(user, payload):
    """Build a scoped RetrieveContext from the request, carrying the per-query retrieval config."""
    from opencortyx.core.security.scope import resolve_scope
    allowed, max_sens = await resolve_scope(user.workspace_id, _groups(user))
    if payload.collection_ids:   # explicit request narrows within what's allowed
        req = [uuid.UUID(c) for c in payload.collection_ids]
        allowed = req if allowed is None else [c for c in req if c in set(allowed)]
    return RetrieveContext(
        workspace_id=user.workspace_id,
        collection_ids=allowed, max_sensitivity=max_sens,
        retrievers=payload.retrievers, rerank=payload.rerank,
        k=payload.k, abstain_threshold=payload.abstain_threshold,
        as_of=getattr(payload, "as_of", None), as_of_ingest=getattr(payload, "as_of_ingest", None),
        fast=getattr(payload, "fast", None),
        decompose=getattr(payload, "decompose", None),
        # Default "knowledge": the assistant answers from INGESTED data unless asked otherwise, so a
        # workspace's own chat history can never become the evidence for a question about its data.
        scope=(getattr(payload, "scope", None) or "knowledge"))


def humanize_backend_error(e: Exception) -> str:
    """Turn a raw retrieval/answer exception into a clear, actionable message for the UI (instead of
    a generic 500), above all a model that is missing, unreachable or misnamed."""
    from opencortyx.core.inference.registry import NoModelConfigured
    if isinstance(e, NoModelConfigured):
        return str(e)                        # already says what to add, and where
    s = str(e)
    low = s.lower()
    if "out of memory" in low or "cudamalloc" in low or "oom" in low:
        return ("The model server ran out of memory. Use a smaller model, a shorter context, or give "
                "the server more memory.")
    if "model" in low and ("does not exist" in low or "not found" in low or "404" in low):
        return ("The model server is running but does not serve the model this workspace asks for. "
                "Set the Model ID on the Models page to a name the server lists.")
    if ("connecterror" in low or "connection refused" in low or "unreachable" in low
            or "all connection attempts failed" in low or "apiconnectionerror" in low):
        return ("The model server is unreachable. Start it and check its address on the Models page "
                "(from inside Docker, a server on this computer is host.docker.internal, not "
                "localhost; OpenAI-compatible addresses end in /v1).")
    if "embed" in low:
        return f"Embedding backend error: {s[:200]}"
    if "timeout" in low or "timed out" in low:
        return ("The model server timed out. It may still be loading the model; retry in a moment.")
    return f"Retrieval/answer failed: {s[:240]}"


async def capture_assistant_chat(user: "CurrentUser", query: str, res: dict,
                                 request: "Request" = None, chat_id=None, title: str = None) -> None:
    """Auto-save a real-time Q&A into the graph, attributed to WHO ASKED.

    With a `chat_id` the exchange joins that chat's Session (one per thread); without one it joins
    the caller's rolling session for the day (see `memory.session_key`).

    Two callers reach the same endpoint and they are not the same conversation:
      • a human in the web Assistant  → `assistant-chat`, via `web`   ('Capture Assistant chats')
      • a CONNECTED AGENT via MCP     → `agent-activity`, via `MCP`   ('Capture agent sessions')

    The agent branch is what was missing. An MCP client could only be recorded if it remembered to
    call `/memory/log-activity` itself, so a connected agent's whole conversation went unrecorded
    unless it opted in — the opposite of an audit trail. Attribution comes from the CREDENTIAL
    (`actor_type == "agent"` means an API key), and `log_activity` resolves the agent's display name
    from that key's `app`, so each connected client is a distinct identity with its own rolling
    session id — the conversation root the Sessions view groups turns under.

    Best-effort: never blocks or fails the answer."""
    try:
        # `rc` is not a module-level name in this file — every other user imports it locally, and this
        # function did not, so it raised NameError on EVERY answer and the whole capture was dead.
        from opencortyx.core.config import runtime as rc
        try:
            await rc.load()      # without this the accessors return the CODE defaults, not the saved
        except Exception:        # settings — `capture_agent_sessions` defaults False, so MCP capture
            pass                 # would stay off even with the toggle switched on in Settings.
        if (request is not None
                and (request.headers.get("x-opencortyx-capture") or "").lower() == "client"):
            return
        is_agent = getattr(user, "actor_type", "user") == "agent"
        if is_agent and not rc.capture_agent_sessions():
            return
        if not is_agent and not rc.capture_assistant_chats():
            return
        from opencortyx.knowledge import memory
        cites = [c for c in (res.get("citations") or res.get("trace") or []) if isinstance(c, dict)]
        # Only what the answer RESTS on is linked: `_mark_support` flags it, and every other citation
        # is a candidate that merely resembled the question.
        used = [c for c in cites if c.get("supports", True)]
        await memory.log_activity(
            user.workspace_id, actor_id=user.user_id, actor_type=getattr(user, "actor_type", "user"),
            tool="mcp_answer" if is_agent else "assistant_chat",
            query=query, output=res.get("answer"),     # an abstain is recorded as one, not dropped
            documents=[c.get("asset_label") or c.get("label") or c.get("node_id") for c in used][:20],
            document_ids=[c.get("node_id") for c in used if c.get("node_id")],
            agent=None,          # a key names its agent; a person here is using the built-in Assistant
            project="agent-activity" if is_agent else "assistant-chat",
            via="MCP" if is_agent else "web",
            key_prefix="agent" if is_agent else "assistant",
            chat_id=chat_id, title=title)
    except Exception as e:                       # noqa: BLE001 — must never fail the answer
        import traceback
        log.warning("context.session_capture_failed", error=repr(e))
        traceback.print_exc()


async def month_token_spend(db, workspace_id) -> int:
    """Total LLM tokens recorded in the audit log this calendar month (provider-neutral)."""
    import datetime as _dt
    start = _dt.datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    metas = (await db.execute(select(AuditLog.meta).where(
        AuditLog.workspace_id == workspace_id, AuditLog.ts >= start))).scalars().all()
    total = 0
    for m in metas:
        try:
            total += int(((m or {}).get("tokens") or {}).get("total") or 0)
        except Exception:
            pass
    return total


async def enforce_budget(workspace_id):
    """Refuse paid LLM answers when a hard cap is set and this month's token budget is exceeded.
    Self-hosted models (your own server and the in-process embedders) cost nothing per token, so the cap only
    bites when the active generation model is billed by a third party."""
    from opencortyx.domain.control import CostBudget
    async with V2Session() as db:
        b = await db.get(CostBudget, workspace_id)
        if not b or not b.hard_cap or not b.monthly_token_budget:
            return
        spent = await month_token_spend(db, workspace_id)
        if spent < b.monthly_token_budget:
            return
    try:                                                   # don't block free local inference
        from opencortyx.core.inference.registry import resolve_model
        spec = await resolve_model(workspace_id, "generation", None)
        from opencortyx.core.config import LOCAL_FREE_PROVIDERS
        prov = ((spec or {}).get("provider") or "").lower()
        if not prov or prov in LOCAL_FREE_PROVIDERS:
            return
    except Exception:
        return
    raise HTTPException(status_code=429, detail=(
        f"Monthly token budget reached ({spent:,}/{b.monthly_token_budget:,}). Raise it in "
        "Settings → Cost, turn off the hard cap, or switch to a local (free) model."))
