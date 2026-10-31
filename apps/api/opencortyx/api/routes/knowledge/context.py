"""Routes: grounded context retrieval and cited answers."""
from fastapi import APIRouter, Depends, HTTPException, Request

from opencortyx.api.schemas.knowledge.context import ContextQuery
from opencortyx.api.services.knowledge.context import (
    capture_assistant_chat,
    enforce_budget,
    humanize_backend_error,
    scoped_ctx,
)
from opencortyx.domain.control import AuditLog
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.core.config import RETRIEVAL_INCLUDE_CITATIONS
from opencortyx.knowledge import chat_history

router = APIRouter()


async def _open_chat(user: CurrentUser, payload: ContextQuery, ctx):
    """Record the question in the caller's chat history when they asked for it (see
    `ContextQuery.save_chat`) and make that chat the conversation follow-ups resolve against.
    Returns the chat id, or None when this question is not being saved."""
    if not (payload.save_chat or payload.chat_id):
        return None
    chat_id = await chat_history.open_for_question(user.workspace_id, user.user_id, payload.chat_id,
                                                   payload.query)
    ctx.conversation_id = chat_id
    return chat_id


async def _close_chat(chat_id, text: str, meta: dict = None) -> None:
    """Append the answer. Best-effort: the history must never cost the caller their answer."""
    if chat_id is None:
        return
    try:
        await chat_history.append_answer(chat_id, text, meta)
    except Exception as e:                              # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("chat_history.append_failed: %r", e)


def _surface_error(res: dict) -> dict:
    """A generation that FAILED (no model connected, server down) is not an abstention on the
    evidence: its answer becomes the reason, in words the user can act on."""
    if not isinstance(res, dict) or not res.get("error"):
        return res
    err = res["error"]
    msg = err if res.get("model_missing") else humanize_backend_error(RuntimeError(err))
    return {**res, "answer": msg}


def _answer_text(res: dict) -> str:
    """The answer as the chat shows it — an abstain reads as one, not as an empty bubble."""
    if res.get("error"):
        return f"⚠ {res.get('answer') or res['error']}"
    if res.get("abstained"):
        return "No grounded evidence found — abstained instead of guessing."
    return str(res.get("answer") or "")


@router.get("/retrievers")
async def list_retrievers(user: CurrentUser = Depends(require_auth)):
    """Catalog the Chat UI renders: available retrievers, rerankers, profile presets, defaults."""
    from opencortyx.retrieval.adapter import SURFACES
    return {
        "retrievers": [
            {"name": "hipporag", "label": "HippoRAG (PPR)", "desc": "Personalized PageRank from ANY node — full graph activation"},
            {"name": "contextual", "label": "Contextual (Anthropic-style)", "desc": "enriches every result with its parent session context"},
            {"name": "lexical", "label": "Vector (semantic)", "desc": "Qdrant ANN over every attribute of every node"},
            {"name": "bm25", "label": "Keyword (BM25)", "desc": "BM25 keyword search — exact terms, IDs, codes, dates"},
            {"name": "exact", "label": "Exact / B-tree", "desc": "exact B-tree lookup on IDs, labels, terms, and codes"},
            {"name": "fuzzy", "label": "N-gram / Fuzzy", "desc": "fuzzy text matching handling typos and morphological variations"},
            {"name": "metadata", "label": "Metadata Index", "desc": "filters structured JSONB properties and types"},
            {"name": "graph_algo", "label": "Graph Algorithms", "desc": "multi-hop graph algorithms (shortestPath, reachable, degree, neighbors)"},
        ],
        "rerankers": [
            {"name": "cross", "label": "Cross-encoder (bge)", "desc": "fast + strong (needs sentence-transformers)"},
            {"name": "llm", "label": "LLM listwise", "desc": "slower; last-resort / fallback"},
        ],
        # The engine that actually serves /context and /answer, and what it runs. `profiles` is
        # gone with v1: v2 chooses surfaces per QUESTION from the plan rather than from a preset.
        "engine": "retrieval",
        "surfaces": SURFACES,
        # k 0: no block count, the token budget decides (orchestrator.WorkflowConfig.context_blocks)
        "defaults": {"k": 0, "abstain_threshold": 0.30, "rerank": ["cross"]},
    }


@router.post("/context")
async def get_context(payload: ContextQuery, user: CurrentUser = Depends(require_auth)):
    """Return grounded, cited context (or abstain). This is the layer's core endpoint."""
    from opencortyx.retrieval.adapter import build_context_v2
    ctx = await scoped_ctx(user, payload)
    chat_id = await _open_chat(user, payload, ctx)
    try:
        pkg = await build_context_v2(payload.query, ctx)
    except Exception as e:                              # surface a clear reason (esp. model server/embed)
        await _close_chat(chat_id, f"⚠ {humanize_backend_error(e)}", {"error": True})
        raise HTTPException(status_code=503, detail=humanize_backend_error(e))
    await _close_chat(chat_id,
                      "No grounded context above threshold — abstained." if pkg.get("abstained")
                      else str(pkg.get("context") or ""),
                      {"abstained": pkg.get("abstained"), "confidence": pkg.get("confidence"),
                       "retrievers": pkg.get("retrievers"), "citations": pkg.get("citations"),
                       "caveats": pkg.get("caveats"), "trace": pkg.get("citations"),
                       "stage_trace": pkg.get("stage_trace")})
    if not RETRIEVAL_INCLUDE_CITATIONS:
        pkg = {k: v for k, v in pkg.items() if k != "citations"}
    if chat_id is not None:
        pkg = {**pkg, "chat_id": str(chat_id)}
    return pkg


@router.post("/answer")
async def answer(payload: ContextQuery, request: Request, user: CurrentUser = Depends(require_auth)):
    """Generate a grounded answer with citation-verification (flags unsupported sentences)."""
    import time

    from opencortyx.retrieval.answering.answer import answer_query

    start_t = time.perf_counter()
    await enforce_budget(user.workspace_id)                 # cost control: refuse paid LLM work over a hard cap
    ctx = await scoped_ctx(user, payload)
    chat_id = await _open_chat(user, payload, ctx)
    try:
        res = _surface_error(await answer_query(payload.query, ctx))
    except Exception as e:                              # surface a clear reason (esp. model server/embed)
        await _close_chat(chat_id, f"⚠ {humanize_backend_error(e)}", {"error": True})
        raise HTTPException(status_code=503, detail=humanize_backend_error(e))
    await _close_chat(chat_id, _answer_text(res), chat_history.answer_meta(res))
    latency_ms = int((time.perf_counter() - start_t) * 1000)

    # F15: Observability log — latency, trust, token spend, and the trace (contributing nodes)
    trace = res.get("trace") or res.get("citations") or []
    async with V2Session() as db:
        db.add(AuditLog(
            workspace_id=user.workspace_id,
            actor=str(user.user_id),
            action="retrieve/answer",
            latency_ms=latency_ms,
            trust_score=res.get("trust_score", 0.0),
            meta={
                "query": payload.query,
                "abstained": res.get("abstained"),
                "profile": getattr(payload, "profile", None),
                "retrievers": res.get("retrievers"),
                "tokens": {
                    "prompt": res.get("tokens_prompt", 0),
                    "completion": res.get("tokens_completion", 0),
                    "total": res.get("tokens_total", 0),
                    "context": res.get("tokens_context", res.get("tokens_used", 0)),
                },
                "trace": [{"node_id": c.get("node_id"), "type": c.get("type"),
                           "score": c.get("score")} for c in trace],
            },
        ))
        await db.commit()

    await capture_assistant_chat(user, payload.query, res, request, chat_id=chat_id,   # auto-save
                                 title=chat_history.title_from(payload.query))

    if not RETRIEVAL_INCLUDE_CITATIONS and isinstance(res, dict) and res.get("citations"):
        res = dict(res)
        res["citations"] = [{k: v for k, v in c.items() if k != "text"}
                            for c in res["citations"]]
    if chat_id is not None:
        res = {**res, "chat_id": str(chat_id)}
    return res


@router.post("/answer/stream")
async def answer_stream(payload: ContextQuery, request: Request, user: CurrentUser = Depends(require_auth)):
    """SSE variant of /answer: streams the grounded answer token-by-token, then a final `done` event
    carrying citations, trust score, grounding flags and token spend. Same retrieval/CRAG/abstain
    logic as /answer — only the synthesis is streamed. Falls back to a single chunk if the provider
    can't stream, so callers always receive a complete answer."""
    import json as _json
    import time

    from fastapi.responses import StreamingResponse

    from opencortyx.retrieval.answering.answer import answer_query_stream

    await enforce_budget(user.workspace_id)                  # cost control before any paid LLM work
    ctx = await scoped_ctx(user, payload)
    chat_id = await _open_chat(user, payload, ctx)
    start_t = time.perf_counter()

    async def _gen():
        final: dict = {}
        if chat_id is not None:                         # first, so the UI can select the new chat
            yield f"data: {_json.dumps({'type': 'chat', 'chat_id': str(chat_id)})}\n\n"
        try:
            async for ev in answer_query_stream(payload.query, ctx):
                if ev.get("type") == "done":
                    ev = final = _surface_error(ev)
                yield f"data: {_json.dumps(ev, default=str)}\n\n"
        except Exception as e:                          # esp. model server/embed down -> clear, single event
            err = {"type": "done", "answer": humanize_backend_error(e), "abstained": True,
                   "error": str(e), "citations": []}
            await _close_chat(chat_id, f"⚠ {humanize_backend_error(e)}", {"error": True})
            yield f"data: {_json.dumps(err, default=str)}\n\n"
            return
        await _close_chat(chat_id, _answer_text(final), chat_history.answer_meta(final))
        try:                                            # observability log (after the stream finishes)
            latency_ms = int((time.perf_counter() - start_t) * 1000)
            trace = final.get("trace") or final.get("citations") or []
            async with V2Session() as db:
                db.add(AuditLog(
                    workspace_id=user.workspace_id, actor=str(user.user_id), action="retrieve/answer",
                    latency_ms=latency_ms, trust_score=final.get("trust_score", 0.0),
                    meta={"query": payload.query, "abstained": final.get("abstained"),
                          "profile": getattr(payload, "profile", None), "retrievers": final.get("retrievers"), "streamed": True,
                          "tokens": {"prompt": final.get("tokens_prompt", 0),
                                     "completion": final.get("tokens_completion", 0),
                                     "total": final.get("tokens_total", 0),
                                     "context": final.get("tokens_context", final.get("tokens_used", 0))},
                          "trace": [{"node_id": c.get("node_id"), "type": c.get("type"),
                                     "score": c.get("score")} for c in trace]}))
                await db.commit()
            await capture_assistant_chat(user, payload.query, final, request, chat_id=chat_id,
                                         title=chat_history.title_from(payload.query))
        except Exception:
            pass
        yield 'data: {"type":"end"}\n\n'

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


@router.post("/corpus/enrich")
async def corpus_enrich(user: CurrentUser = Depends(require_write)):
    """Build corpus summary + run conflict detection for the org (async)."""
    from opencortyx.tasks.enrich import v2_corpus_enrich
    task = v2_corpus_enrich.delay(str(user.workspace_id))
    return {"task_id": task.id, "status": "dispatched"}
