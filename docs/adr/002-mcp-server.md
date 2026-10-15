# ADR-002: Standalone MCP server

## Status
Accepted

## Context
AI agents (Claude, Cursor, VS Code and others) need to search, ask questions, and write memories
without custom integration code for each client.

## Decision
Run the MCP server (`apps/mcp`) as its own service. It is a thin adapter over the public REST API
(`/api/v2`) and exposes `opencortyx_*` tools, resources and prompts.

- Transport: streamable HTTP at `/mcp` (stdio for local clients).
- Authentication: every HTTP client sends its own API key or token, and each tool call runs with
  that caller's role and audit trail. Anonymous access requires `MCP_ALLOW_ANONYMOUS=true`.
- Host headers are restricted to `MCP_ALLOWED_HOSTS` to prevent DNS rebinding.

## Consequences
- Any MCP client works with one JSON configuration.
- The MCP server holds no state and no business logic; tools change without touching the API.
- Every capability an agent uses is also available, and tested, through the REST API.
