"""The Assistant's chat history: list, open, rename and delete a person's conversations, and append
turns to them as they are answered.

Every read and write is scoped to (org, user): a chat is private to whoever started it, and a thread
id from someone else's sidebar is simply "not found". Turns are appended by the answer endpoints
themselves (see `api/routes/knowledge/context.py`), not by the browser, so a reload mid-answer or a
closed tab still leaves the question and its answer in the history.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import delete, func, or_, select, update

from opencortyx.core.db import V2Session
from opencortyx.domain.chat import ChatMessage, ChatThread

#: What a new thread is called until its first question names it.
UNTITLED = "New chat"
_TITLE_LEN = 80

#: Caps on what one stored answer carries. The trace and the evaluator's context are debugging aids,
#: and uncapped they make one turn megabytes — the answer and its citations are always kept whole.
_TRACE_CAP = 40
_CONTEXT_CAP = 20_000


def _as_uuid(v) -> Optional[uuid.UUID]:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (ValueError, AttributeError, TypeError):
        return None


def title_from(question: str) -> str:
    q = " ".join((question or "").split())
    return (q[:_TITLE_LEN - 1] + "…") if len(q) > _TITLE_LEN else (q or UNTITLED)


def answer_meta(res: dict) -> dict:
    """The part of an answer the UI renders under it — the same shape the chat page builds live, so a
    reopened chat looks exactly like it did."""
    ctx = res.get("retrieved_context")
    return {
        "abstained": res.get("abstained"), "grounded": res.get("grounded"),
        "confidence": res.get("confidence"), "retrievers": res.get("retrievers"),
        "citations": res.get("citations"), "caveats": res.get("caveats"),
        "unsupported": res.get("unsupported_sentences"), "trust_score": res.get("trust_score"),
        "crag": res.get("crag"), "trace": (res.get("trace") or [])[:_TRACE_CAP],
        "retrieved_context": ctx[:_CONTEXT_CAP] if isinstance(ctx, str) else ctx,
        "stage_trace": res.get("stage_trace"), "generation": res.get("generation"),
        "rewritten_query": res.get("rewritten_query"),
    }


def _thread_row(t: ChatThread) -> dict:
    return {"id": str(t.id), "title": t.title, "message_count": t.message_count,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None}


async def _owned(db, workspace_id, user_id, thread_id) -> Optional[ChatThread]:
    tid = _as_uuid(thread_id)
    if tid is None:
        return None
    t = await db.get(ChatThread, tid)
    return t if t is not None and t.workspace_id == workspace_id and t.user_id == user_id else None


async def list_threads(workspace_id, user_id, *, q: str = "", limit: int = 50, offset: int = 0) -> dict:
    """Newest first. `q` matches a title or anything said in the chat."""
    stmt = select(ChatThread).where(ChatThread.workspace_id == workspace_id, ChatThread.user_id == user_id)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        said = select(ChatMessage.thread_id).where(ChatMessage.text.ilike(like))
        stmt = stmt.where(or_(ChatThread.title.ilike(like), ChatThread.id.in_(said)))
    async with V2Session() as db:
        total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
        rows = (await db.execute(stmt.order_by(ChatThread.updated_at.desc())
                                 .offset(max(0, offset)).limit(max(1, min(limit, 200))))).scalars().all()
    return {"items": [_thread_row(t) for t in rows], "total": int(total)}


async def create_thread(workspace_id, user_id, title: Optional[str] = None) -> dict:
    async with V2Session() as db:
        t = ChatThread(workspace_id=workspace_id, user_id=user_id, title=(title or "").strip()[:200] or UNTITLED)
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return _thread_row(t)


async def get_thread(workspace_id, user_id, thread_id) -> Optional[dict]:
    async with V2Session() as db:
        t = await _owned(db, workspace_id, user_id, thread_id)
        if t is None:
            return None
        msgs = (await db.execute(select(ChatMessage).where(ChatMessage.thread_id == t.id)
                                 .order_by(ChatMessage.ordinal))).scalars().all()
        return {**_thread_row(t), "messages": [
            {"id": str(m.id), "role": m.role, "text": m.text, "meta": m.meta,
             "created_at": m.created_at.isoformat() if m.created_at else None} for m in msgs]}


async def rename_thread(workspace_id, user_id, thread_id, title: str) -> Optional[dict]:
    async with V2Session() as db:
        t = await _owned(db, workspace_id, user_id, thread_id)
        if t is None:
            return None
        t.title = (title or "").strip()[:200] or UNTITLED
        await db.commit()
        await db.refresh(t)
        return _thread_row(t)


async def delete_thread(workspace_id, user_id, thread_id) -> bool:
    async with V2Session() as db:
        t = await _owned(db, workspace_id, user_id, thread_id)
        if t is None:
            return False
        await db.execute(delete(ChatMessage).where(ChatMessage.thread_id == t.id))
        await db.delete(t)
        await db.commit()
        return True


async def open_for_question(workspace_id, user_id, thread_id, question: str) -> uuid.UUID:
    """The thread this question belongs to — the given one if the caller owns it, else a new one —
    with the question already appended. Called BEFORE answering, so the question is kept even if
    the answer fails."""
    async with V2Session() as db:
        t = await _owned(db, workspace_id, user_id, thread_id) if thread_id else None
        if t is None:
            t = ChatThread(workspace_id=workspace_id, user_id=user_id, title=title_from(question))
            db.add(t)
            await db.flush()
        elif t.title == UNTITLED:
            t.title = title_from(question)
        db.add(ChatMessage(thread_id=t.id, ordinal=t.message_count, role="user", text=question))
        t.message_count += 1
        t.updated_at = func.now()
        await db.commit()
        return t.id


async def prior_messages(thread_id, limit: int = 6) -> list[tuple[str, str]]:
    """The thread's last `limit` messages BEFORE the question being answered, oldest first, as
    [(role, text)].

    Not ownership-checked: the caller holds a thread id `open_for_question` already resolved for its
    user. `open_for_question` appends the question before answering, so a trailing user message is
    the question itself, not history, and is left out."""
    tid = _as_uuid(thread_id)
    if tid is None or limit <= 0:
        return []
    async with V2Session() as db:
        rows = (await db.execute(select(ChatMessage.role, ChatMessage.text)
                                 .where(ChatMessage.thread_id == tid)
                                 .order_by(ChatMessage.ordinal.desc()).limit(limit + 1))).all()
    msgs = [(r.role, r.text or "") for r in reversed(rows)]
    if msgs and msgs[-1][0] == "user":
        msgs = msgs[:-1]
    return msgs[-limit:]


async def append_answer(thread_id: uuid.UUID, text: str, meta: Optional[dict] = None) -> None:
    """Append the assistant's turn. Ordinal is taken under a row lock so two answers finishing at
    once in the same thread cannot collide."""
    async with V2Session() as db:
        n = (await db.execute(select(ChatThread.message_count).where(ChatThread.id == thread_id)
                              .with_for_update())).scalar()
        if n is None:                                  # deleted while the answer was being written
            return
        db.add(ChatMessage(thread_id=thread_id, ordinal=n, role="assistant", text=text or "",
                           meta=meta))
        await db.execute(update(ChatThread).where(ChatThread.id == thread_id)
                         .values(message_count=n + 1, updated_at=func.now()))
        await db.commit()
