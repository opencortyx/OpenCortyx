# OpenCortyx MCP Server

Exposes OpenCortyx as **MCP tools / resources / prompts** for AI agents (Claude Desktop, Cursor, the
MCP Inspector, or any MCP client). It's a thin, well-described adapter over the OpenCortyx v2 API
(`/api/v2`) — one NL question in → grounded, cited context out, over **structured and unstructured**
data alike.

## Tools
| Tool | What it does |
|---|---|
| `opencortyx_answer` | Question → grounded, cited answer with a trust score (abstains if unsure) |
| `opencortyx_search` | Ranked, cited context (passages and table nodes), no synthesis |
| `opencortyx_search_nodes` | Find nodes by text, optionally filtered by type |
| `opencortyx_graph_query` | Multi-stage graph query (`START`, `TRAVERSE`, `FILTER`, `MATCH`, `INTERSECT`, `UNION`, `PATH`, `LIMIT`, `RETURN`) |
| `opencortyx_stats` | Counts of nodes and edges by type |
| `opencortyx_recall` / `opencortyx_remember` | Agent memory across sessions |
| `opencortyx_save_session` / `opencortyx_list_sessions` / `opencortyx_resume` | Session capture and resume |
| `opencortyx_feedback` | Rate an answer and optionally correct it |

Resources: `opencortyx://stats`, `opencortyx://retrievers`, `opencortyx://collections`.
Prompts: `grounded_qa`, `as_of_query`.

---

## Security model

**Who may call this server, and as whom, depends on the transport.** Read this before exposing it.

| | stdio | HTTP (`streamable-http` / `sse`) |
|---|---|---|
| Who can reach it | only the client that spawned the process | anything that can reach the port |
| Inbound credential | none (the environment IS the credential) | **required** — `X-API-Key: ax_…` or `Authorization: Bearer …` |
| API calls run as | the server's configured key | **the caller's own credential** |

Over HTTP:

* **Default deny.** A request without a valid credential gets `401` from an ASGI gate *in front of*
  the MCP app — so `initialize` and `tools/list` are protected too, not just `tools/call`. There is
  nothing to enumerate: missing, malformed and wrong credentials get an identical response.
* **Identity is the caller's.** Each tool's API calls carry the caller's credential, so the API
  enforces *their* role (a `viewer` key cannot write, whatever key this server holds), attributes the
  action to *them*, and honours revocation. That is the point of the design — a shared server key
  makes every agent indistinguishable.
* **Validation is delegated** to `GET /api/v2/auth/me`, so the API stays the single source of truth
  for identity, roles and revocation. This server stores no credential material and never logs a raw
  credential — only an 8-char SHA-256 fingerprint.
* **Results are cached briefly** (`MCP_AUTH_CACHE_TTL`, default 60s) so a hot path isn't one API
  round-trip per call. That TTL is also the worst-case window in which a *revoked* key still works —
  lower it if that matters more than the round-trips.
* **Failed attempts are throttled** per peer (`429` after `MCP_AUTH_FAIL_LIMIT` failures inside
  `MCP_AUTH_FAIL_WINDOW`) to blunt key guessing.
* **`/healthz` is exempt** (container healthchecks, load balancers) and discloses nothing.
* **The compose port is published on `127.0.0.1` only.** This port is a full workspace API surface.
  To expose it deliberately, terminate TLS and authenticate at a reverse proxy and forward to the
  container on the compose network — don't widen the published binding.

`MCP_ALLOW_ANONYMOUS=true` disables the gate and makes every reachable client act with this server's
own key. It prints a loud warning on startup and is only defensible on a loopback-bound port on a
trusted machine.

**Issue a key:** web UI → Users & Access → API keys (role `viewer` = read-only, `editor` = read+write).
The raw key is shown once. Give each agent its own key so you can revoke one without disturbing the rest.

---

## Configuration (env)
| Var | Default | Notes |
|---|---|---|
| `OPENCORTYX_API_URL` | `http://localhost:8000` | OpenCortyx API base (`http://api:8000` in compose) |
| `OPENCORTYX_API_KEY` | — | this server's own key. Used for **stdio**; ignored for authenticated HTTP callers |
| `OPENCORTYX_TOKEN` | — | optional pre-minted bearer (alternative to the key) |
| `MCP_TRANSPORT` | `stdio` | `stdio` \| `sse` \| `streamable-http` |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8001` | for sse/http |
| `MCP_ALLOW_ANONYMOUS` | `false` | **danger** — disables inbound auth on HTTP transports |
| `MCP_AUTH_CACHE_TTL` | `60` | seconds a validated credential is trusted (also the revocation lag) |
| `MCP_AUTH_NEGATIVE_CACHE_TTL` | `30` | seconds a rejected credential is remembered as bad |
| `MCP_AUTH_FAIL_LIMIT` / `MCP_AUTH_FAIL_WINDOW` | `10` / `60` | failed-auth throttle per peer |
| `MCP_LOG_LEVEL` | `info` | uvicorn log level |

## Run

**Prereq:** the OpenCortyx API is up (`bin/opencortyx up` from the repository root).

### Option A — as a Docker service (streamable-HTTP on :8001)
```bash
docker compose up -d --build mcp          # from the repository root
# endpoint: http://localhost:8001/mcp     (NOT /sse — SSE is deprecated in mcp>=1.x)
# health:   http://localhost:8001/healthz
```
Every client must send its own key:
```bash
curl -s -X POST http://localhost:8001/mcp \
  -H "X-API-Key: ax_..." \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

### Option B — locally over stdio (Claude Desktop / Cursor / Inspector)
```bash
cd apps/mcp
pip install -r requirements.lock
OPENCORTYX_API_URL=http://localhost:8000 OPENCORTYX_API_KEY=ax_... python server.py
```

## Connect a client

**Claude Desktop** — its `mcpServers` entries launch a **stdio** process, so they need a `command`.
The `{"type":"http","url":…}` form is Claude Code's config format; Desktop rejects it as invalid.

*Against the running container (no local Python needed):*
```jsonc
{
  "mcpServers": {
    "opencortyx": {
      "command": "docker",
      "args": ["exec", "-i", "-e", "MCP_TRANSPORT=stdio", "opencortyx-mcp-1", "python", "server.py"]
    }
  }
}
```
Convenient locally, but it grants the client Docker exec rights and only works on this machine —
prefer the remote HTTP endpoint (Settings → Connectors) for anything shared.

*Against a local checkout:*
```jsonc
{
  "mcpServers": {
    "opencortyx": {
      "command": "python",
      "args": ["/absolute/path/to/apps/mcp/server.py"],
      "env": {
        "OPENCORTYX_API_URL": "http://localhost:8000",
        "OPENCORTYX_API_KEY": "ax_..."
      }
    }
  }
}
```

**Claude Code** (`.mcp.json` — this client *does* speak HTTP):
```jsonc
{
  "mcpServers": {
    "opencortyx": {
      "type": "http",
      "url": "http://localhost:8001/mcp",
      "headers": { "X-API-Key": "ax_..." }
    }
  }
}
```

**MCP Inspector:**
```bash
cd apps/mcp
OPENCORTYX_API_URL=http://localhost:8000 OPENCORTYX_API_KEY=ax_... \
  npx @modelcontextprotocol/inspector python server.py
```
Call `opencortyx_stats` (no args) to confirm connectivity, then `opencortyx_answer` / `opencortyx_search`.

## Production checklist

1. Keep `MCP_ALLOW_ANONYMOUS` unset (default deny).
2. Terminate **TLS** at a reverse proxy; never carry `ax_…` keys over plaintext off-host.
3. Keep the container port on the compose network; publish only through the proxy.
4. One key **per agent**, least-privilege role (`viewer` unless the agent must write).
5. Lower `MCP_AUTH_CACHE_TTL` if you need revocation to bite faster than 60s.
6. Watch for `401`/`429` bursts in the logs — that's credential probing.
