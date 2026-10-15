"""Thin async HTTP client to the OpenCortyx v2 API.

Handles per-agent auth: uses OPENCORTYX_TOKEN if set, else mints a dev bearer token from OPENCORTYX_USERNAME
via /api/v2/auth/token (cached; re-minted once on 401). Every MCP tool calls the API through here —
the MCP server holds no business logic of its own.
"""
import json as _json

import httpx

from opencortyx_mcp import auth, config

_token: str | None = config.API_TOKEN or None

_NO_AUTH_MSG = (
    "OpenCortyx MCP is not authenticated — no API key set. Issue one in the web UI "
    "(Connections → Connect), put it in .env as OPENCORTYX_API_KEY=ax_… and run `bin/opencortyx restart mcp` "
    "service (the Connect script does this for you). The legacy OPENCORTYX_USERNAME login no longer "
    "works once real authentication is enabled."
)
_KEY_REJECTED_MSG = (
    "OpenCortyx MCP authentication was REJECTED (401/403) — your OPENCORTYX_API_KEY is missing, "
    "expired, or revoked. Reissue a key in the web UI (Users & Access → API keys / Connections → "
    "Connect), update .env (OPENCORTYX_API_KEY=ax_…) and run `bin/opencortyx restart mcp`."
)

# Real-time, agent-agnostic capture: meaningful tool calls are logged to a rolling session (the API
# gates this behind the 'Capture agent sessions' permission). Maps the API path → a friendly action.
_LOG_LABELS = {
    "/answer": "opencortyx_answer", "/context": "opencortyx_search",
    "/sources/upload": "opencortyx_ingest_document", "/sources/upload_flow": "opencortyx_ingest_document",
    "/memory/remember": "opencortyx_remember", "/feedback": "opencortyx_feedback",
    "/graph/query": "opencortyx_graph_query",
}

# GET tools that carry a real QUESTION — these are conversation, and leaving them out meant an agent
# could interrogate the whole workspace via recall/search and none of it appeared in the transcript.
_LOG_LABELS_GET = {
    "/memory/recall": "opencortyx_recall", "/search": "opencortyx_search_nodes",
}


def _label_for(method: str, path: str):
    if path == "/memory/log-activity":
        return None                              # never log the log call (no recursion)
    m = method.upper()
    if m == "GET":
        return _LOG_LABELS_GET.get(path)
    if m != "POST":
        return None
    if path in _LOG_LABELS:
        return _LOG_LABELS[path]
    if path.startswith("/ingestions/") and path.endswith("/run"):
        return "opencortyx_run_ingestion"
    return None


async def _maybe_log(method: str, path: str, body, result, params=None):
    label = _label_for(method, path)
    if not label:
        return
    try:
        query = ""
        src = body if isinstance(body, dict) else {}
        if not src and isinstance(params, dict):
            src = params                         # GET tools (recall/search_nodes) carry it in the query string
        if src:
            query = str(src.get("query") or src.get("q") or src.get("statement")
                        or src.get("text") or src.get("summary") or "")[:300]
        documents, document_ids = [], []         # documents_retrieved — from the answer/context citations
        if isinstance(result, dict):
            for c in (result.get("citations") or result.get("trace") or [])[:20]:
                # only what the answer rests on (`supports`), when the API says; else every citation
                if isinstance(c, dict) and c.get("supports", True):
                    documents.append(c.get("asset_label") or c.get("label") or c.get("source_ref")
                                     or f"{c.get('type', 'node')}#{str(c.get('node_id', ''))[:8]}")
                    if c.get("node_id"):
                        document_ids.append(str(c["node_id"]))   # linked as Turn -REFERENCES-> node
        payload = {"tool": label, "query": query, "documents": documents,
                   "document_ids": document_ids, "agent": config.AGENT_NAME}
        if isinstance(result, dict) and result.get("answer"):
            payload["output"] = str(result["answer"])[:4000]
        elif not query and not documents:        # non-retrieval tools (ingest/remember/…) → short summary
            out = result if isinstance(result, str) else _json.dumps(result, default=str)
            payload["output"] = (out or "")[:300]
        await _request("POST", "/memory/log-activity", json=payload)
    except Exception as e:
        import sys as _sys
        print(f"opencortyx MCP: FAILED to capture turn for {path}: {e!r}", file=_sys.stderr, flush=True)


async def _ensure_token(http: httpx.AsyncClient) -> str:
    if _token:
        return _token
    raise RuntimeError(_NO_AUTH_MSG)


async def _request(method: str, path: str, *, params=None, json=None, data=None, files=None):
    """Call /api/v2{path} as the CALLER. Returns parsed JSON (or text). Raises
    httpx.HTTPStatusError with the server detail on failure.

    Identity, in priority order:
      1. the credential of the caller currently being served (HTTP transports — already validated at
         the gate). This is what makes the API enforce THEIR role and attribute the action to THEM;
         a viewer's key cannot write here just because the server holds an admin key.
      2. this server's own configured key/token (stdio, where the client owns the process, or an
         explicitly anonymous local HTTP server).
    """
    url = f"{config.API_URL}/api/v2{path}"
    caller_cred = auth.current_credential()
    try:
        return await _do_request(method, path, url, caller_cred,
                                 params=params, json=json, data=data, files=files)
    except httpx.TimeoutException:
        raise RuntimeError(
            f"The OpenCortyx API did not respond within {config.HTTP_TIMEOUT:.0f}s for {method} {path}. "
            f"The first reranked query after a restart is slow while the models load — retry, or raise "
            f"OPENCORTYX_HTTP_TIMEOUT if this is normal for your hardware.") from None
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = (e.response.json() or {}).get("detail", "")
        except Exception:
            detail = (e.response.text or "")[:300]
        raise RuntimeError(f"OpenCortyx API returned {e.response.status_code} for {method} {path}"
                           + (f": {detail}" if detail else "")) from None
    except httpx.RequestError as e:
        raise RuntimeError(f"Could not reach the OpenCortyx API at {config.API_URL} "
                           f"({type(e).__name__}: {e or 'no detail'}). Is it running?") from None


async def _do_request(method: str, path: str, url: str, caller_cred, *,
                      params=None, json=None, data=None, files=None):
    global _token
    _mine = {"X-OpenCortyx-Capture": "client"} if _label_for(method, path) else {}
    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as http:
        if caller_cred:
            r = await http.request(method, url, params=params, json=json, data=data, files=files,
                                   headers={**auth._auth_headers(caller_cred), **_mine})
            if r.status_code in (401, 403):
                detail = ""
                try:
                    detail = (r.json() or {}).get("detail", "")
                except Exception:
                    detail = (r.text or "")[:200]
                raise RuntimeError(
                    f"Refused for your credential ({r.status_code}): {detail or 'not permitted'}. "
                    f"Your API key's role may not allow this action.")
            r.raise_for_status()
        # Preferred: a workspace API key (X-API-Key) — no token minting, no 401 re-mint dance.
        elif config.API_KEY:
            r = await http.request(method, url, params=params, json=json, data=data, files=files,
                                   headers={"X-API-Key": config.API_KEY, **_mine})
            if r.status_code in (401, 403):       # key missing/expired/revoked → actionable guidance
                raise RuntimeError(_KEY_REJECTED_MSG)
            r.raise_for_status()
        else:
            token = await _ensure_token(http)
            async def _do(tok):
                return await http.request(method, url, params=params, json=json, data=data, files=files,
                                          headers={"Authorization": f"Bearer {tok}", **_mine})
            r = await _do(token)
            if r.status_code == 401:                  # token expired → mint a fresh one, retry once
                _token = None
                r = await _do(await _ensure_token(http))
            r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        result = r.json() if ctype.startswith("application/json") else r.text
    await _maybe_log(method, path, json, result, params)   # real-time capture (best-effort, gated server-side)
    return result


async def api_get(path: str, params: dict | None = None):
    return await _request("GET", path, params=params)


async def api_post(path: str, json: dict | None = None, data: dict | None = None, files: dict | None = None):
    return await _request("POST", path, json=json, data=data, files=files)
