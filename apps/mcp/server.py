"""OpenCortyx MCP server — agent-native access to the Context OS.

Exposes OpenCortyx as MCP **tools / resources / prompts** over the v2 REST API (/api/v2). The mission
contract: one natural-language question in → grounded, cited context out, over BOTH structured and
unstructured data. Read tools are open; ingestion tools dispatch jobs. There is intentionally no
text-to-SQL tool — structured data is served by the same unified retrieval.

Run:
  • stdio (Claude Desktop / Cursor / MCP Inspector):   python server.py
  • HTTP/SSE service:                                   MCP_TRANSPORT=sse python server.py

Auth/tenant: set OPENCORTYX_USERNAME to the SAME username you use in the OpenCortyx web UI so the agent
sees the same org's data (or pass an explicit OPENCORTYX_TOKEN).
"""
import json
from typing import List, Optional, Union

from opencortyx_mcp import auth, client, config

try:                                         # SDK 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer
    _SERVER_KWARGS = {"host": config.HOST, "port": config.PORT}
except ModuleNotFoundError:                  # SDK 2.x
    from mcp.server.mcpserver import MCPServer as _MCPServer
    _SERVER_KWARGS = {}

mcp = _MCPServer("OpenCortyx", **_SERVER_KWARGS)


def _dump(obj) -> str:
    """Serialize a tool result COMPACTLY.

    These payloads are read by a model, not a person, and indentation carries no information to a
    reader that parses JSON - it is whitespace billed at full price. Pretty-printing cost ~70% on
    every response from every tool here (measured: one graph_query hop, 3,191 tok indented vs
    1,884 compact), which matters most for the tools an agent is meant to call repeatedly.
    """
    try:
        return json.dumps(obj, default=str, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(obj)


# ── A. Grounded context & answering (the core) ────────────────────────────────
@mcp.tool()
async def opencortyx_answer(query: str, profile: Optional[str] = None,
                       collections: Optional[List[str]] = None,
                       retrievers: Optional[List[str]] = None,
                       rerank: Optional[List[str]] = None, k: Optional[int] = None,
                       abstain_threshold: Optional[float] = None,
                       as_of: Optional[str] = None, fast: Optional[bool] = None,
                       scope: Optional[str] = None) -> str:
    """Ask a natural-language question; get a GROUNDED, CITED answer with a trust score.

    Runs the full pipeline: retrieve -> pack to the token budget -> relevance-check the context ->
    generate -> verify the answer is grounded. Abstains rather than hallucinating when the evidence
    is weak. This is the expensive path; `opencortyx_search` returns the same packed context WITHOUT
    generating, and `opencortyx_graph_query` traverses the graph for a few hundred tokens.

    ONLY TWO KNOBS DO ANYTHING:
      • `k`            how many context blocks to keep (omit for the full budget — passing a small
                       k is how you get a thin context, not a cheaper one)
      • `collections`  narrow to specific collections

    Every other parameter is accepted for backward compatibility and NOT honoured; the response
    lists them under `unsupported` rather than silently ignoring them. `retrievers` / `rerank` — the
    engine picks surfaces per question from its own plan. `abstain_threshold` — it abstains on a
    feature-based gate instead. `as_of` / `as_of_ingest` — bitemporal filtering is not implemented.
    `fast` — there is no separate fast path. `scope` — not plumbed into the engine, so "knowledge",
    "conversations" and "all" currently return the SAME corpus. Do not rely on it to keep chat
    transcripts out of the evidence."""
    body = {"query": query, "profile": profile, "collection_ids": collections,
            "retrievers": retrievers, "rerank": rerank, "k": k,
            "abstain_threshold": abstain_threshold, "as_of": as_of, "fast": fast,
            "scope": scope}
    return _dump(await client.api_post("/answer", json={k: v for k, v in body.items() if v is not None}))


@mcp.tool()
async def opencortyx_search(query: str, profile: Optional[str] = None,
                       collections: Optional[List[str]] = None,
                       retrievers: Optional[List[str]] = None,
                       rerank: Optional[List[str]] = None, k: Optional[int] = None,
                       scope: Optional[str] = None) -> str:
    """Retrieve the packed CONTEXT for a query — no synthesis. The grounded material to reason over.

    This is `opencortyx_answer` stopped after packing: the same retrieval, fusion, graph expansion,
    ranking, source-turn dedup and budget packing, without the relevance check, the generation or
    the grounding gate. Use it when you want the evidence and will do the reasoning yourself.

    Returns `context` (the packed blocks) plus `tokens_used`, `confidence`, `abstained` and
    `stage_trace`. NOTE `tokens_used` counts block TEXT; the wire format wraps each block in JSON,
    so what you actually receive is roughly 60% larger than that number.

    `k` and `collections` are honoured. `retrievers`, `rerank`, `scope` and the rest are accepted
    but NOT honoured — see the response's `unsupported` field, and the note on opencortyx_answer.
    In particular `scope` does not currently separate ingested data from saved sessions."""
    body = {"query": query, "profile": profile, "collection_ids": collections,
            "retrievers": retrievers, "rerank": rerank, "k": k, "scope": scope}
    return _dump(await client.api_post("/context", json={k: v for k, v in body.items() if v is not None}))


# ── B. Graph & catalog exploration ────────────────────────────────────────────
@mcp.tool()
async def opencortyx_search_nodes(q: str, types: Optional[str] = None, limit: int = 20) -> str:
    """Find nodes by text. `types` is an optional comma-separated filter (e.g. 'Entity,Table,Topic')."""
    params = {"q": q, "limit": limit}
    if types:
        params["types"] = types
    return _dump(await client.api_get("/search", params=params))


@mcp.tool()
async def opencortyx_stats() -> str:
    """Corpus/graph stats — counts by node & edge type, total and embedded nodes. Good for orientation."""
    return _dump(await client.api_get("/stats"))


@mcp.tool()
async def opencortyx_graph_query(
    query: Optional[Union[str, dict]] = None,
    steps: Optional[List[dict]] = None,
    limit: int = 50,
    returns: Optional[List[str]] = None,
    collection_id: Optional[str] = None,
) -> str:
    """Search the knowledge graph cheaply, look at what came back, and query again.

    This is a SEARCH primitive, not an answer path. It is built to be called several times: each
    call is ~30 ms and, used as intended, costs a few hundred tokens — so narrowing over three or
    four queries is far cheaper than one big retrieval. For a written answer call
    `opencortyx_answer` (full pipeline: fusion, expansion, ranking, dedup, budgeted packing).
    Answer synthesis is deliberately not duplicated here.

    THE LOOP — spend tokens only once the set is small:

      1. `opencortyx_stats` once per session for the type vocabulary (node & edge types are real
         names in this graph; do not guess them).
      2. Seed and SIZE it: `RETURN count`. Every response also carries `available_edges` — the
         edge types present on the nodes you are holding right now, with direction and degree.
         Read it instead of guessing the next hop.
      3. Hop with TRAVERSE, using an edge type from `available_edges`. Re-check `count`.
      4. Narrow with FILTER / MATCH / INTERSECT until the count is small.
      5. THEN ask for `evidence`.

    PROJECTION COST (measured, one 140-node hop) — pick deliberately:
        count      13 tok      nodes    ~12 tok/node      edges  ~11 tok/node
        paths     ~25 tok/node evidence ~47 tok/node
    Default is `count` + `nodes` + `available_edges`. `evidence` and `paths` are opt-in because
    on a wide set they cost more than the whole answer pipeline.

    TWO THINGS THAT WILL MISLEAD YOU IF UNREAD:
      • `truncated: true` means START matched more nodes than `limit`, so `count` is a LOWER
        BOUND. Never report a total from a truncated result — raise `limit` or narrow the seed.
      • FILTER `contains` is a literal substring test. For counting or set-completion questions,
        one phrasing under-counts; UNION several phrasings (see the example below).

    PRIMITIVES:
      • START <label|query|id|type>   Seed. `START type="Person" "John"` is far tighter than
                                      `START John`; `query=` matches node TEXT and will pull in
                                      unrelated nodes for short names.
      • TRAVERSE <rel> [direction=both|out|in] [hops=N]
      • FILTER <field> <op> <val>     ops: == != >= <= > < contains in
      • MATCH [rel=X] [target_type=Y] Keep nodes having a matching neighbor.
      • INTERSECT ( <subquery> )      Set intersection — the right tool for "both X and Y".
      • UNION ( <subquery> )          Set union — the right tool for counting across phrasings.
      • PATH / LIMIT <n>
      • RETURN count | nodes | edges | paths | evidence | evidence_blocks

    Narrowing (cheap → expensive):
      START type="Entity" "Nate"
      TRAVERSE MENTIONS direction=in      # available_edges showed MENTIONS in: 140
      FILTER properties.text contains "tournament"
      RETURN evidence                     # 7 nodes, ~550 tok — pay here, not on the hop

    Counting without under-counting — one phrasing is a lower bound, so union them:
      START type="Entity" "Nate"
      TRAVERSE MENTIONS direction=in
      FILTER properties.text contains "tournament"
      UNION (
        START type="Entity" "Nate"
        TRAVERSE MENTIONS direction=in
        FILTER properties.text contains "competition"
      )
      RETURN count, evidence

    Shared attributes between two people:
      START "John"
      TRAVERSE likes
      INTERSECT ( START "Nate" TRAVERSE likes )
      RETURN nodes, paths
    """
    body = {
        "query": query,
        "steps": steps,
        "limit": limit,
        "returns": returns,
        "collection_id": collection_id,
    }
    return _dump(await client.api_post("/graph/query", json={k: v for k, v in body.items() if v is not None}))


# ── D. Agent memory & sessions (§25) — continuity across sessions ─────────────
@mcp.tool()
async def opencortyx_recall(query: str, project: Optional[str] = None, limit: int = 12) -> str:
    """Call at SESSION START. Recall the most relevant past DECISIONS, FACTS, and PREFERENCES for the
    task at hand (semantic + graph retrieval, scoped to your workspace) — so you don't start cold.
    Returns compact memory items with ids you can cite or resume."""
    params = {"q": query, "limit": limit}
    if project:
        params["project"] = project
    return _dump(await client.api_get("/memory/recall", params=params))


@mcp.tool()
async def opencortyx_remember(text: str, kind: str = "fact", session_id: Optional[str] = None,
                         refs: Optional[List[str]] = None) -> str:
    """Write ONE durable memory back mid-session. `kind` = 'decision' | 'fact' | 'preference'.
    Optionally link it to a `session_id` and to `refs` (data node ids it's about)."""
    body = {"text": text, "kind": kind, "session_id": session_id, "refs": refs}
    return _dump(await client.api_post("/memory/remember", json={k: v for k, v in body.items() if v is not None}))


@mcp.tool()
async def opencortyx_save_session(turns: Optional[List[dict]] = None, summary: Optional[str] = None,
                             title: Optional[str] = None, agent: Optional[str] = None,
                             project: Optional[str] = None, session_id: Optional[str] = None,
                             decisions: Optional[List[dict]] = None, facts: Optional[List[str]] = None,
                             preferences: Optional[List[str]] = None, refs: Optional[List[str]] = None,
                             key_points: Optional[List[str]] = None, checkpoints: Optional[List[dict]] = None,
                             highlights: Optional[List[str]] = None, tags: Optional[List[str]] = None,
                             attributes: Optional[dict] = None, distill: bool = False) -> str:
    """Call when the user says "store this session / save our conversation / save the summary". Persist
    YOUR conversation FAITHFULLY — do NOT pre-summarize it. Send the actual turns; each turn becomes its
    OWN node (type Turn) carrying exactly what you have, linked to one Session node:

        turns = [
          {"query": "<the user's question>", "answer": "<your answer>",
           "thinking": "<your reasoning, if available>", "context": "<context/sources you used>"},
          ...
        ]

    Different queries → different Turn nodes; attributes are query · answer · context · thinking, with a
    created date (audit). The Session node groups them, is embedded, shows in the lineage graph, and is
    queryable via the Assistant. Reuse the returned `session_id` to checkpoint the same session
    (idempotent UPSERT — it grows, never duplicates).

    "Save the summary" is just the degenerate case: send ONE turn
    {"query": "summary", "answer": "<the summary text>"}.

    `distill` defaults to False (store verbatim). Set distill=true ONLY if you want OpenCortyx's LLM to
    also derive a summary/decisions/facts on the Session node. Optional Session-level attributes:
    summary, key_points[], checkpoints[{label,detail,at}], highlights[], tags[], attributes{}, refs[]."""
    body = {"summary": summary, "title": title, "agent": agent, "project": project,
            "session_id": session_id, "turns": turns, "decisions": decisions, "facts": facts,
            "preferences": preferences, "refs": refs, "key_points": key_points,
            "checkpoints": checkpoints, "highlights": highlights, "tags": tags,
            "attributes": attributes, "distill": distill}
    return _dump(await client.api_post("/memory/save-session", json={k: v for k, v in body.items() if v is not None}))


@mcp.tool()
async def opencortyx_list_sessions(project: Optional[str] = None, limit: int = 50) -> str:
    """List saved sessions (id, title, summary, status). Reload one with opencortyx_resume."""
    params = {"limit": limit}
    if project:
        params["project"] = project
    return _dump(await client.api_get("/memory/sessions", params=params))


@mcp.tool()
async def opencortyx_resume(session_id: str) -> str:
    """Reload a prior session's full context — summary, decisions, facts, preferences, and turns."""
    return _dump(await client.api_get(f"/memory/sessions/{session_id}"))


# ── E. Feedback (§7) ──────────────────────────────────────────────────────────
@mcp.tool()
async def opencortyx_feedback(query: str, rating: str, correction: Optional[str] = None) -> str:
    """Record feedback on an answer — `rating` = 'up' or 'down', with an optional `correction`.
    Feeds the eval/feedback loop that tracks answer quality over time."""
    body = {"query": query, "rating": rating, "correction": correction}
    return _dump(await client.api_post("/feedback", json={k: v for k, v in body.items() if v is not None}))


# ── Resources (URI-addressable discovery) ─────────────────────────────────────
# Canonical scheme is opencortyx:// (matching the opencortyx_* tool names).
@mcp.resource("opencortyx://stats")
async def res_stats() -> str:
    """Corpus/graph snapshot."""
    return _dump(await client.api_get("/stats"))


@mcp.resource("opencortyx://retrievers")
async def res_retrievers() -> str:
    """Available retrievers / rerankers / profiles."""
    return _dump(await client.api_get("/retrievers"))


@mcp.resource("opencortyx://collections")
async def res_collections() -> str:
    """Collections (scopes) available to this agent."""
    return _dump(await client.api_get("/collections"))


# ── Prompts (reusable templates) ──────────────────────────────────────────────
@mcp.prompt()
def grounded_qa(question: str) -> str:
    """Ask OpenCortyx for a grounded, cited answer (abstain if unsure)."""
    return (f"Use the opencortyx_answer tool to answer the following from OpenCortyx's grounded context, "
            f"and cite the sources it returns. If the evidence is weak, say so rather than guessing.\n\n"
            f"Question: {question}")


@mcp.prompt()
def as_of_query(question: str, date: str) -> str:
    """Time-travel question — answer as the knowledge was valid as of a date."""
    return (f"Use opencortyx_answer with as_of='{date}' so the answer reflects what was true as of that "
            f"date. Cite sources.\n\nQuestion: {question}")


def _banner() -> None:
    """Say exactly how this process is protected — silence here would be the dangerous outcome."""
    if config.IS_STDIO:
        if not (config.API_KEY or config.API_TOKEN):
            print("\n" + "=" * 78
                  + "\n  OpenCortyx MCP: NO API KEY SET — tool calls will fail until you connect."
                  + "\n  Fix: web UI → Connections → Connect (issue a key), then put it in .env"
                  + "\n       as OPENCORTYX_API_KEY=ax_… and restart this service. The Connect script does"
                  + "\n       this for you. (Legacy OPENCORTYX_USERNAME login is disabled once auth is on.)"
                  + "\n" + "=" * 78 + "\n", flush=True)
        return
    if config.REQUIRE_AUTH:
        print(f"opencortyx-mcp: {config.TRANSPORT} on {config.HOST}:{config.PORT} — authentication "
              f"REQUIRED (X-API-Key or Bearer); every call runs as the calling credential.", flush=True)
    else:
        print("\n" + "!" * 78
              + "\n  OpenCortyx MCP: ANONYMOUS ACCESS IS ENABLED (MCP_ALLOW_ANONYMOUS=true)."
              + "\n  Every client that can reach this port has FULL workspace access — including the"
              + "\n  write tools (ingest, run_ingestion, remember) — using this server's own"
              + "\n  key. Only acceptable when the port is bound to localhost on a trusted machine."
              + "\n  Unset MCP_ALLOW_ANONYMOUS before exposing this service to anything else."
              + "\n" + "!" * 78 + "\n", flush=True)


def _http_app():
    """The MCP ASGI app behind the auth gate.

    Pure-ASGI wrapping (not a Starlette mount) so the app's own lifespan — which starts the
    streamable-HTTP session manager — still runs."""
    from mcp.server.transport_security import TransportSecuritySettings
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=config.ALLOWED_HOSTS,
        allowed_origins=[f"http://{h}" for h in config.ALLOWED_HOSTS] + [f"https://{h}" for h in config.ALLOWED_HOSTS],
    )
    factory = mcp.sse_app if config.TRANSPORT == "sse" else mcp.streamable_http_app
    try:
        app = factory(transport_security=security)
    except TypeError:                        # SDK 1.x reads it from the server settings
        mcp.settings.transport_security = security
        app = factory()
    return auth.AuthMiddleware(app, require_auth=config.REQUIRE_AUTH)


if __name__ == "__main__":
    _banner()
    if config.IS_STDIO:
        mcp.run(transport="stdio")
    else:
        import uvicorn
        # Serve the gated app ourselves rather than mcp.run(): that is the only way the credential
        # check sits IN FRONT of every request, including `initialize` and `tools/list`.
        uvicorn.run(_http_app(), host=config.HOST, port=config.PORT,
                    log_level=config.LOG_LEVEL, timeout_graceful_shutdown=5)
