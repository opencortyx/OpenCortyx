"""Routes: agent memory: sessions, decisions, facts and preferences."""
from typing import Optional

from fastapi import APIRouter, Depends

from opencortyx.api.schemas.knowledge.memory import ActivitySpec, RememberSpec, SaveSessionSpec
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.knowledge import memory

router = APIRouter()


@router.post("/memory/save-session")
async def memory_save_session(spec: SaveSessionSpec, user: CurrentUser = Depends(require_write)):
    """Persist a working session as a memory subgraph (Session/Turn/Decision/Fact/Preference)."""
    return await memory.save_session(
        user.workspace_id, session_id=spec.session_id, title=spec.title, agent=spec.agent,
        project=spec.project, participants=spec.participants, summary=spec.summary,
        turns=spec.turns, decisions=spec.decisions, facts=spec.facts,
        preferences=spec.preferences, refs=spec.refs, author=user.user_id, distill=spec.distill,
        key_points=spec.key_points, checkpoints=spec.checkpoints, highlights=spec.highlights,
        tags=spec.tags, attributes=spec.attributes)


@router.post("/memory/log-activity")
async def memory_log_activity(spec: ActivitySpec, user: CurrentUser = Depends(require_write)):
    """Agent-agnostic, real-time capture of a request *coming to OpenCortyx*: append one interaction to a
    rolling per-agent/day session — { timestamp · user · agent · query_to_axiom · documents_retrieved }.
    Gated by the 'Capture agent sessions' permission (Settings → Agents) — a no-op when disabled."""
    from opencortyx.core.config import runtime as rc
    if not rc.capture_agent_sessions():
        return {"captured": False, "reason": "disabled"}
    return await memory.log_activity(user.workspace_id, actor_id=user.user_id, actor_type=user.actor_type,
                                     tool=spec.tool, query=spec.query, documents=spec.documents,
                                     document_ids=spec.document_ids, output=spec.output,
                                     agent=spec.agent)


@router.post("/memory/remember")
async def memory_remember(spec: RememberSpec, user: CurrentUser = Depends(require_write)):
    """Write a single durable memory node mid-session (decision | fact | preference)."""
    return await memory.remember(user.workspace_id, text=spec.text, kind=spec.kind,
                                 session_id=spec.session_id, refs=spec.refs, scope=spec.scope,
                                 author=user.user_id)


@router.get("/memory/recall")
async def memory_recall(q: str = "", project: Optional[str] = None, limit: int = 12,
                        user: CurrentUser = Depends(require_auth)):
    """Recall the most relevant memories for a new task (semantic + graph, scoped)."""
    return await memory.recall(user.workspace_id, query=q, project=project, limit=limit)


@router.get("/memory/sessions")
async def memory_sessions(limit: int = 50, project: Optional[str] = None,
                          user: CurrentUser = Depends(require_auth)):
    """List saved sessions (newest first)."""
    return await memory.list_sessions(user.workspace_id, limit=limit, project=project)


@router.get("/memory/sessions/{session_id}")
async def memory_get_session(session_id: str, user: CurrentUser = Depends(require_auth)):
    """Reload one session's full context — summary, decisions, facts, preferences, turns."""
    return await memory.get_session(user.workspace_id, session_id)
