"""Agent memory & session context (§25) — sessions as a graph, not a markdown handoff.

Every write reuses the SAME substrate as the rest of the graph (via the authoring layer → NodeRepo),
so session memory inherits embeddings + hybrid retrieval, entity resolution, bitemporal, governance,
and export. The memory node types are catalog-tagged `memory` in the built-in extractor catalog
(seeder._MEMORY): Session / Turn / Decision / Fact / Preference / Artifact.

Agent surface (MCP) → /v2/memory/* → these functions:
  • capture_exchange — record one live question + answer (the only writer of captured chats;
                       `log_activity` is its entry point for agents and the Assistant)
  • save_session  — persist a session as a subgraph (optionally LLM-distilled from raw turns)
  • remember      — write one durable memory node mid-session
  • recall        — semantic recall of relevant decisions/facts/preferences at session start
  • list_sessions / get_session — find & reload a prior session's context
"""
import re
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from opencortyx.core.db import V2Session
from opencortyx.core.substrate import CONVERSATION_FLAG, NodeRepo, in_scope
from opencortyx.core.inference.embedding import embed
from opencortyx.knowledge import authoring
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

_repo = NodeRepo()

MEMORY_NODE_TYPES = ["Session", "Turn", "Decision", "Fact", "Preference", "Artifact"]
_RECALL_TYPES = ["Decision", "Fact", "Preference", "Session"]

# Fallback if the editable builtin:mem.summarizer template hasn't been seeded yet.
_DEFAULT_SUMMARIZER = (
    "You distill an AI working session into durable memory.\n"
    "From the transcript below, extract: a 2-3 sentence SUMMARY of what was done, the DECISIONS "
    "made (with rationale), and durable FACTS / preferences learned.\n\n"
    "Transcript:\n{transcript}\n\n"
    'Return ONLY JSON: {"summary":"...","decisions":[{"title":"...","rationale":"..."}],'
    '"facts":["..."],"preferences":["..."]}'
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_uuid(workspace_id, session_id):
    """Stable Session node id from a caller-supplied key, so resume/checkpoint UPSERT the same node.
    A real UUID is used as-is; any other string is hashed (per-org) into a deterministic UUID."""
    if not session_id:
        return uuid.uuid4()
    try:
        return uuid.UUID(str(session_id))
    except (ValueError, AttributeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, f"session:{workspace_id}:{session_id}")


def _parse_json(raw) -> dict:
    """Tolerant JSON parse for LLM output (strips code fences, finds the outermost object)."""
    if isinstance(raw, dict):
        return raw
    s = str(raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s).rstrip("`").strip()
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
    return {}


async def _summarizer_prompt(workspace_id) -> str:
    """The editable session-summarizer prompt (builtin:mem.summarizer) — no hidden prompts.
    Falls back to the default if the catalog row isn't present."""
    try:
        from opencortyx.domain.control import ExtractionTemplate
        async with V2Session() as db:
            t = (await db.execute(select(ExtractionTemplate).where(
                ExtractionTemplate.workspace_id == workspace_id,
                ExtractionTemplate.name == "builtin:mem.summarizer"))).scalar_one_or_none()
        p = (t.mechanism or {}).get("prompt") if t else None
        return p or _DEFAULT_SUMMARIZER
    except Exception:
        return _DEFAULT_SUMMARIZER


async def _distill(workspace_id, turns) -> dict:
    """Compress a raw transcript into summary + decisions + facts + preferences via the LLM."""
    from opencortyx.core.inference.llm import complete
    transcript = "\n".join(
        f"{(t.get('role') if isinstance(t, dict) else 'user')}: "
        f"{(t.get('text') if isinstance(t, dict) else str(t))}"
        for t in (turns or []))[:8000]
    if not transcript.strip():
        return {}
    prompt = (await _summarizer_prompt(workspace_id)).replace("{transcript}", transcript)
    try:
        raw = await complete(prompt, system="You distill AI sessions into durable memory. Return only JSON.",
                             max_tokens=700, temperature=0.1)
        data = _parse_json(raw)
    except Exception:
        data = {}
    return {
        "summary": data.get("summary"),
        "decisions": data.get("decisions") or [],
        "facts": data.get("facts") or [],
        "preferences": data.get("preferences") or [],
    }


async def save_session(workspace_id, *, title=None, agent=None, project=None, participants=None,
                       summary=None, turns=None, decisions=None, facts=None, preferences=None,
                       refs=None, session_id=None, author=None, distill=None,
                       key_points=None, checkpoints=None, highlights=None, tags=None,
                       attributes=None) -> dict:
    """Persist a session as a memory subgraph. If raw `turns` are given without explicit
    decisions/facts (and distill isn't disabled), the session is LLM-distilled first. Idempotent per
    `session_id` (UPSERT): on re-save, the session's existing turns/decisions/facts/prefs are replaced
    (no duplication) — so a live per-turn checkpoint keeps ONE clean, growing session. Pass
    distill=False (live checkpoints) to skip the LLM call.

    Rich session attributes live ON the Session node so the whole conversation (not just turns) is one
    queryable, lineage-visible node:
      • key_points  : list[str]                      — the bullet summary of the chat
      • checkpoints : list[{label, detail, at}]      — named milestones / "what we built so far"
      • highlights  : list[str]                      — artifacts produced (files, features, decisions)
      • tags        : list[str]
      • attributes  : dict                           — ANYTHING else, free-form, from the session's POV
    All of these are folded into the node's embedding text, so the Assistant can answer over them."""
    sid = _session_uuid(workspace_id, session_id)

    if distill is not False and turns and not (summary and (decisions or facts)):
        distilled = await _distill(workspace_id, turns)
        summary = summary or distilled.get("summary")
        decisions = decisions or distilled.get("decisions")
        facts = facts or distilled.get("facts")
        preferences = preferences or distilled.get("preferences")

    # merge prior props forward (upsert_node REPLACES properties) so re-saves accumulate and
    # started_at / earlier attributes are never lost
    existing = await _repo.get(workspace_id, sid)
    prior = (existing.properties or {}) if existing else {}
    is_new = existing is None
    # A session capture is already recording (the chat's own session) owns its turns: they carry
    # who asked, the documents used and REFERENCES edges, which a client's {role, text} list cannot
    # rebuild. Saving such a session adds what saving is for — the summary and what was decided or
    # learned — and leaves its turns alone. `turns` still feed the distillation above.
    keep_turns = bool(existing) and str(prior.get("captured", "")).lower() == "true"

    def _take(new, key, default):
        return new if new is not None else prior.get(key, default)

    props = {**prior}
    props.update({
        "title": title or prior.get("title") or "Session",
        "agent": agent or prior.get("agent"),
        "project": project or prior.get("project"),
        "participants": participants or prior.get("participants") or [],
        "status": "saved",
        "summary": summary or prior.get("summary") or "",
        "key_points": _take(key_points, "key_points", []),
        "checkpoints": _take(checkpoints, "checkpoints", []),
        "highlights": _take(highlights, "highlights", []),
        "tags": _take(tags, "tags", []),
        "started_at": prior.get("started_at") or _now(),
        "ended_at": _now(),
    })
    if attributes:                                    # free-form session-perspective attributes
        props["attributes"] = {**(prior.get("attributes") or {}), **attributes}

    # rich embedding text: title + summary + key points + checkpoints + highlights → so the
    # Assistant can answer "what did we build / decide in the session about X?"
    _cps = [(f"{c.get('label','')}: {c.get('detail','')}".strip(": ") if isinstance(c, dict) else str(c))
            for c in (props.get("checkpoints") or [])]
    text_parts = [props["title"], props["summary"], *map(str, props.get("key_points") or []),
                  *_cps, *map(str, props.get("highlights") or [])]
    props["text"] = "\n".join(p for p in text_parts if p).strip() or (props["title"])

    await authoring.create_node(workspace_id, type="Session", label=props["title"],
                                properties=props, node_id=sid, author=author)

    # UPSERT: drop the session's prior child nodes so re-saves don't duplicate turns/facts/etc.
    if not is_new:
        turns_in = [] if keep_turns else await _repo.edges(workspace_id, target_ids=[sid], types=["PART_OF"])
        learned = await _repo.edges(workspace_id, source_ids=[sid], types=["DECIDED", "LEARNED", "PREFERS"])
        await _repo.delete_nodes(workspace_id, [e.source_id for e in turns_in] + [e.target_id for e in learned])

    counts = {"turns": 0, "decisions": 0, "facts": 0, "preferences": 0}

    def _clip(v, n):
        return (str(v)[:n] if v not in (None, "") else None)

    prev = None
    for i, t in enumerate([] if keep_turns else (turns or [])):
        if isinstance(t, dict):
            role = t.get("role") or "agent"
            q = _clip(t.get("query"), 4000)
            a = _clip(t.get("answer"), 8000)
            context = _clip(t.get("context"), 8000)
            thinking = _clip(t.get("thinking"), 8000)
            text = t.get("text") or "\n".join(p for p in [
                (f"Q: {q}" if q else None), (f"A: {a}" if a else None),
                (f"Thinking: {thinking}" if thinking else None),
                (f"Context: {context}" if context else None)] if p)
        else:
            role, text = "user", str(t)
            q = a = context = thinking = None
        if not (text and text.strip()) and not (q or a):
            continue
        label = (q or a or text or f"{role} #{i}")[:80]
        tid = await authoring.create_node(
            workspace_id, type="Turn", label=label,
            properties={"role": role, "ordinal": i, "ts": _now(),
                        "query": q, "answer": a, "context": context, "thinking": thinking,
                        "text": text or (q or a or "")}, author=author)
        await authoring.create_edge(workspace_id, source_id=tid, target_id=sid, type="PART_OF", author=author)
        if prev is not None:
            await authoring.create_edge(workspace_id, source_id=prev, target_id=tid, type="FOLLOWS", author=author)
        prev = tid
        counts["turns"] += 1

    written: list = []
    for d in (decisions or []):
        td = (d.get("title") if isinstance(d, dict) else str(d)) or "Decision"
        rationale = (d.get("rationale") if isinstance(d, dict) else "") or ""
        alts = (d.get("alternatives") if isinstance(d, dict) else None) or []
        did = await authoring.create_node(
            workspace_id, type="Decision", label=td[:120],
            properties={"title": td, "rationale": rationale, "alternatives": alts,
                        "text": f"{td}: {rationale}".strip(": "), "ts": _now()},
            author=author, embed="sync")
        await authoring.create_edge(workspace_id, source_id=sid, target_id=did, type="DECIDED", author=author)
        written.append(did)
        counts["decisions"] += 1

    for f in (facts or []):
        stmt = (f.get("statement") if isinstance(f, dict) else str(f)) or ""
        if not stmt.strip():
            continue
        fid = await authoring.create_node(
            workspace_id, type="Fact", label=stmt[:120],
            properties={"statement": stmt, "text": stmt, "ts": _now()},
            author=author, embed="sync")
        await authoring.create_edge(workspace_id, source_id=sid, target_id=fid, type="LEARNED", author=author)
        written.append(fid)
        counts["facts"] += 1

    for p in (preferences or []):
        stmt = (p.get("statement") if isinstance(p, dict) else str(p)) or ""
        if not stmt.strip():
            continue
        scope = (p.get("scope") if isinstance(p, dict) else None) or "session"
        pid = await authoring.create_node(
            workspace_id, type="Preference", label=stmt[:120],
            properties={"statement": stmt, "text": stmt, "scope": scope},
            author=author, embed="sync")
        await authoring.create_edge(workspace_id, source_id=sid, target_id=pid, type="PREFERS", author=author)
        written.append(pid)
        counts["preferences"] += 1

    for r in (refs or []):                 # link the session to the data nodes it used / produced
        try:
            await authoring.create_edge(workspace_id, source_id=sid, target_id=uuid.UUID(str(r)),
                                        type="REFERENCES", author=author)
        except (ValueError, AttributeError):
            continue

    # continuity: link a brand-new session to the most recent prior one in the same project
    if is_new and project:
        try:
            previous = [n for n in await _repo.find(workspace_id, exact_types=["Session"], props={"project": project},
                                                    order_by="-created_at", limit=2) if n.id != sid]
            if previous:
                await authoring.create_edge(workspace_id, source_id=sid, target_id=previous[0].id,
                                            type="CONTINUES", author=author)
        except Exception:
            pass

    await _check_contradictions(workspace_id, written, author)
    return {"session_id": str(sid), "summary": summary or "", **counts}


#: Caps on one captured exchange. A turn is a record of what was asked and used, not an archive.
MAX_DOCUMENTS = 20
MAX_REFERENCES = 20


def session_key(*, chat_id=None, prefix: str, actor_id, day: str | None = None) -> str:
    """Which Session an exchange belongs to.

    A chat thread is a conversation, so its exchanges share one Session whose id IS the thread id —
    the Save button and the chat sidebar then name the same thing. A caller with no thread (an MCP
    agent, an API call without `save_chat`) gets one rolling Session per caller per day, as before.
    """
    if chat_id:
        return str(chat_id)
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{prefix}:{actor_id}:{day}"


def _clean_ids(values) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for v in values or []:
        try:
            u = v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
        except (ValueError, AttributeError, TypeError):
            continue
        if u not in out:
            out.append(u)
    return out


async def capture_exchange(workspace_id, *, who, key: str, tool: str, query=None, output=None,
                           documents=None, node_ids=None, title=None, project="agent-activity",
                           via="MCP") -> dict:
    """Record one question and its answer in the graph. THE writer of captured conversation.

    There used to be three: the answer path kept graph turns per chat thread for follow-ups, this
    module kept a rolling per-day session per caller, and the Save button wrote a third session for
    the same chat. Each held a different subset of the truth, none linked to another, and all of
    them floated unconnected in the graph view. Everything that records a conversation now comes
    through here, and produces one shape:

        (AppUser) -STARTED-> (Session) -HANDLED_BY-> (AppAgent)
        (Turn) -PART_OF-> (Session)      one Turn per exchange, ordinal 0, 1, 2 …
        (Turn) -FOLLOWS-> (Turn)         the previous exchange to this one
        (Turn) -REFERENCES-> (node)      every node the answer was built from that still exists

    REFERENCES is what ties a conversation to the knowledge it used: a chat about a report sits
    under that report in the graph instead of beside the whole workspace.

    `who` is `actors.Participants`. `key` is `session_key(...)`. `documents` are display labels (kept
    on the turn for the Sessions page); `node_ids` are what gets linked. Best-effort by contract of
    every caller: capture must never cost anyone their answer, so callers wrap this.
    """
    from opencortyx.knowledge import actors

    sid = _session_uuid(workspace_id, key)
    now = _now()
    documents = [str(d) for d in (documents or []) if d][:MAX_DOCUMENTS]
    agent_name = who.agent.name
    user_name = who.user.name if who.user else None

    # The Session. upsert REPLACES properties, so prior ones are merged forward: a re-capture must not
    # lose `started_at`, or a summary the Save button distilled onto the same session.
    existing = await _repo.get(workspace_id, sid)
    prior = (existing.properties or {}) if existing else {}
    day = now[:10]
    session_title = (prior.get("title") if existing else None) or title or f"{agent_name} · {day}"
    props = {**prior,
             "title": session_title, "agent": agent_name, "agent_id": who.agent.id,
             "user": user_name, "user_id": who.user.id if who.user else None,
             "actor_type": who.actor_type, "actor_id": who.actor_id,
             "project": prior.get("project") or project, "via": via, "captured": True,
             "status": prior.get("status") if prior.get("status") == "saved" else "live",
             "summary": prior.get("summary") or f"Live capture of {agent_name} activity via {via}",
             "started_at": prior.get("started_at") or now, "ended_at": now,
             "text": prior.get("text") or session_title,
             # DECLARE it: retrieval scopes on this, so a hand-authored node is never mistaken for chat
             CONVERSATION_FLAG: True}
    await authoring.create_node(workspace_id, type="Session", label=session_title, properties=props,
                                node_id=sid, embed="async" if existing is None else "off")
    await actors.link_session(workspace_id, sid, who, _repo)

    # The Turn, numbered after the ones already in this session.
    prior_turns = await _repo.edges(workspace_id, target_ids=[sid], types=["PART_OF"])
    previous = await _repo.get_many(workspace_id, [e.source_id for e in prior_turns]) if prior_turns else []
    previous = [n for n in previous if n.type == "Turn"]
    ordinal = len(previous)
    last = max(previous, key=lambda n: (int((n.properties or {}).get("ordinal") or 0), str(n.created_at)),
               default=None)

    text = (query or tool) + (f"\n↳ {', '.join(documents[:8])}" if documents else "")
    if output:                                  # capture the answer too (Q + A), so the turn is recallable
        text = (f"Q: {query}\nA: {str(output)[:600]}" if query else f"{tool}: {str(output)[:300]}")
        if documents:
            text += f"\n↳ {', '.join(documents[:8])}"
    tid = await authoring.create_node(
        workspace_id, type="Turn", label=(query or tool)[:80],
        properties={"role": "agent", "tool": tool, "ts": now, "ordinal": ordinal,
                    "agent": agent_name, "user": user_name,
                    "actor_type": who.actor_type, "actor_id": who.actor_id,
                    "query": query, "query_to_axiom": query,
                    "answer": (str(output)[:4000] if output else None),
                    "documents_retrieved": documents, "text": text, CONVERSATION_FLAG: True})

    edges = [{"source_id": tid, "target_id": sid, "type": "PART_OF"}]
    if last is not None:
        edges.append({"source_id": last.id, "target_id": tid, "type": "FOLLOWS"})

    # Only nodes that exist, and never another conversation's memory: a turn references knowledge.
    wanted = [i for i in _clean_ids(node_ids) if i not in (sid, tid)][:MAX_REFERENCES]
    found = [n for n in await _repo.get_many(workspace_id, wanted)
             if str((n.properties or {}).get(CONVERSATION_FLAG, "")).lower() != "true"] if wanted else []
    edges += [{"source_id": tid, "target_id": n.id, "type": "REFERENCES"} for n in found]
    await _repo.add_edges(workspace_id=workspace_id, edges=edges)

    return {"captured": True, "session_id": str(sid), "turn_id": str(tid), "ordinal": ordinal,
            "documents_retrieved": documents, "references": [str(n.id) for n in found]}


async def log_activity(workspace_id, *, actor_id, actor_type="agent", tool, query=None,
                       documents=None, document_ids=None, output=None, agent=None,
                       project="agent-activity", via="MCP", key_prefix="agent",
                       chat_id=None, title=None) -> dict:
    """Capture ONE request *coming to OpenCortyx* — from a connected agent (`/memory/log-activity`)
    or the in-app Assistant (`capture_assistant_chat`). Resolves who is behind the credential, picks
    the session (`session_key`), and hands both to `capture_exchange`. No LLM."""
    from opencortyx.knowledge import actors
    who = await actors.resolve(workspace_id, actor_id=actor_id, actor_type=actor_type, agent_name=agent)
    key = session_key(chat_id=chat_id, prefix=key_prefix, actor_id=actor_id)
    return await capture_exchange(workspace_id, who=who, key=key, tool=tool, query=query, output=output,
                                  documents=documents, node_ids=document_ids, title=title,
                                  project=project, via=via)


async def remember(workspace_id, *, text, kind="fact", session_id=None, refs=None,
                   scope="global", author=None) -> dict:
    """Write a single durable memory node (decision | fact | preference) mid-session."""
    typ = {"decision": "Decision", "fact": "Fact", "preference": "Preference"}.get(
        (kind or "fact").lower(), "Fact")
    nid = await authoring.create_node(
        workspace_id, type=typ, label=(text or "")[:120],
        properties={"text": text, "statement": text, "ts": _now(), "scope": scope,
                    CONVERSATION_FLAG: True},
        author=author, embed="sync")           # searchable at once — see the note in save_session
    if session_id:
        sid = _session_uuid(workspace_id, session_id)
        edge = {"Decision": "DECIDED", "Fact": "LEARNED", "Preference": "PREFERS"}[typ]
        await authoring.create_edge(workspace_id, source_id=sid, target_id=nid, type=edge, author=author)
    for r in (refs or []):
        try:
            await authoring.create_edge(workspace_id, source_id=nid, target_id=uuid.UUID(str(r)),
                                        type="REFERENCES", author=author)
        except (ValueError, AttributeError):
            continue
    await _check_contradictions(workspace_id, [nid], author)
    return {"id": str(nid), "type": typ}


async def _check_contradictions(workspace_id, node_ids, author=None) -> None:
    """Hand freshly written memories to the contradiction engine.

    This is the write-path hook the engine exists for: a memory used to be written straight to the
    graph, so two contradictory facts both sat there at full confidence and both came back from
    recall. Best-effort by contract — a judge outage must never cost the caller their memory."""
    try:
        from opencortyx.capabilities.modules.contradiction import resolve_many
        await resolve_many(workspace_id, [str(n) for n in node_ids if n])
    except Exception as e:                                 # noqa: BLE001
        log.warning("memory.contradiction_check_skipped", error=f"{type(e).__name__}: {e}")


async def recall(workspace_id, *, query, project=None, limit=12) -> dict:
    """Semantic recall of the most relevant memories for a new task (session start)."""
    try:
        qvec = await embed(query) if query else None
    except Exception:
        qvec = None
    items = []
    if qvec is not None:
        rows = await _repo.vector_search(workspace_id=workspace_id, query_vec=qvec,
                                         types=_RECALL_TYPES, k=limit,
                                         scope="conversations")
        for n, score in rows:
            p = n.properties or {}
            if project and p.get("project") and p.get("project") != project:
                continue
            items.append({"id": str(n.id), "type": n.type, "label": n.label,
                          "text": (p.get("text") or p.get("statement") or p.get("summary") or "")[:1000],
                          "score": round(float(score), 4), "ts": p.get("ts") or p.get("ended_at")})
    else:                                  # no embedding backend → most-recent memories
        rows = [n for n in await _repo.find(workspace_id, exact_types=list(_RECALL_TYPES), order_by="-created_at")
                if in_scope(n, "conversations")][:limit]
        for n in rows:
            p = n.properties or {}
            items.append({"id": str(n.id), "type": n.type, "label": n.label,
                          "text": (p.get("text") or p.get("statement") or p.get("summary") or "")[:1000],
                          "ts": p.get("ts") or p.get("ended_at")})
    return {"items": items, "count": len(items)}


async def list_sessions(workspace_id, *, limit=50, project=None) -> dict:
    rows = [n for n in await _repo.find(workspace_id, exact_types=["Session"], order_by="-updated_at")
            if in_scope(n, "conversations")][:limit]
    out = []
    for n in rows:
        p = n.properties or {}
        if project and p.get("project") and p.get("project") != project:
            continue
        out.append({"id": str(n.id), "title": n.label, "agent": p.get("agent"),
                    "project": p.get("project"), "status": p.get("status"),
                    "summary": (p.get("summary") or "")[:500],
                    "started_at": p.get("started_at"), "ended_at": p.get("ended_at"),
                    "updated_at": n.updated_at.isoformat() if n.updated_at else None})
    return {"items": out, "count": len(out)}


async def get_session(workspace_id, session_id) -> dict:
    """Reload a prior session's full context — summary + decisions/facts/preferences + turns."""
    sid = _session_uuid(workspace_id, session_id)
    s = await _repo.get(workspace_id, sid)
    if s is None:
        return {"status": "not_found"}
    out_edges = await _repo.neighbors(workspace_id, [sid], direction="out", limit=500)
    in_edges = await _repo.neighbors(workspace_id, [sid], direction="in", limit=1000)
    sp = s.properties or {}
    related = {"decisions": [], "facts": [], "preferences": [], "artifacts": [], "references": []}
    related_sessions = []
    bucket_of = {"DECIDED": "decisions", "LEARNED": "facts", "PREFERS": "preferences",
                 "PRODUCED": "artifacts", "REFERENCES": "references"}
    for e, n in out_edges:
        if e.type == "CONTINUES" and n.type == "Session":
            related_sessions.append({"id": str(n.id), "title": n.label, "dir": "continues"})
            continue
        bucket = bucket_of.get(e.type)
        if not bucket:
            continue
        np = n.properties or {}
        related[bucket].append({"id": str(n.id), "type": n.type, "label": n.label,
                                "text": (np.get("text") or np.get("statement") or "")[:600]})
    turns = []
    for e, n in in_edges:
        if e.type == "PART_OF" and n.type == "Turn":
            np = n.properties or {}
            turns.append({"id": str(n.id), "role": np.get("role"),
                          "text": (np.get("text") or "")[:2000], "ordinal": np.get("ordinal"),
                          "ts": np.get("ts"), "tool": np.get("tool"), "agent": np.get("agent"),
                          "user": np.get("user"),
                          "query": np.get("query") or np.get("query_to_axiom"),
                          "query_to_axiom": np.get("query_to_axiom"),
                          "answer": np.get("answer"), "context": np.get("context"),
                          "thinking": np.get("thinking"),
                          "created_at": n.created_at.isoformat() if n.created_at else None,
                          "documents_retrieved": np.get("documents_retrieved") or []})
        elif e.type == "CONTINUES" and n.type == "Session":
            related_sessions.append({"id": str(n.id), "title": n.label, "dir": "continued_by"})
    turns.sort(key=lambda x: str(x.get("ordinal") if x.get("ordinal") is not None else (x.get("ts") or "")))
    return {"status": "ok",
            "session": {"id": str(s.id), "title": s.label, "agent": sp.get("agent"),
                        "project": sp.get("project"), "summary": sp.get("summary"),
                        "status": sp.get("status"), "started_at": sp.get("started_at"),
                        "ended_at": sp.get("ended_at"),
                        "key_points": sp.get("key_points") or [],
                        "checkpoints": sp.get("checkpoints") or [],
                        "highlights": sp.get("highlights") or [],
                        "tags": sp.get("tags") or [],
                        "attributes": sp.get("attributes") or {}},
            "turns": turns, "related_sessions": related_sessions, **related}
