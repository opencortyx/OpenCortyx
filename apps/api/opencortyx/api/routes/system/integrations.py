"""Routes: connection details for MCP clients."""
from fastapi import APIRouter, Depends

from opencortyx.core.security.auth import CurrentUser, require_auth
from opencortyx.core.config import settings

router = APIRouter()


@router.get("/integrations/mcp")
async def integrations_mcp(user: CurrentUser = Depends(require_auth)):
    """Return ready-to-paste MCP connection configs for AI agents / IDEs (Claude Code, Antigravity),
    each carrying a freshly-minted bearer token scoped to THIS user's org — so 'Connect' wires the
    agent to the same data the user sees. (The web app can't write a user's local IDE config, so we
    hand back copy/paste configs + a CLI command.)"""

    from opencortyx.core.security.auth import create_access_token
    token = create_access_token(user.user_id, user.workspace_id, role="admin")
    api_url = settings.OPENCORTYX_PUBLIC_API_URL.rstrip("/")
    mcp_url = settings.MCP_PUBLIC_URL.rstrip("/")
    sse_url = f"{mcp_url}/sse"
    auth_header = {"Authorization": f"Bearer {token}"}
    claude_code = {
        "transport": "sse", "url": sse_url,
        "cli": f'claude mcp add --transport sse opencortyx {sse_url} '
               f'--header "Authorization: Bearer {token}"',
        "json_block": {"mcpServers": {"opencortyx": {
            "type": "sse", "url": sse_url, "headers": auth_header}}},
        "stdio_fallback": {"mcpServers": {"opencortyx": {
            "command": "python", "args": ["server.py"],
            "env": {"OPENCORTYX_API_URL": api_url, "OPENCORTYX_TOKEN": token, "MCP_TRANSPORT": "stdio"}}}},
    }
    antigravity = {
        "transport": "sse", "url": sse_url,
        "json_block": {"mcpServers": {"opencortyx": {
            "serverUrl": sse_url, "headers": auth_header}}},
    }
    return {"api_url": api_url, "mcp_url": mcp_url, "sse_url": sse_url, "token": token,
            "clients": {"claude_code": claude_code, "antigravity": antigravity}}
