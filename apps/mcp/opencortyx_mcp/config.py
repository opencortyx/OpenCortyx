"""OpenCortyx MCP server configuration (all via environment)."""
import os


def _env(*names: str, default: str = "") -> str:
    """First non-empty value among the given environment variable names."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


# Where the OpenCortyx v2 API lives. Host/local stdio → http://localhost:8000 ; in compose → http://api:8000
API_URL = _env("OPENCORTYX_API_URL", default="http://localhost:8000").rstrip("/")

API_KEY = _env("OPENCORTYX_API_KEY").strip()
API_TOKEN = _env("OPENCORTYX_TOKEN").strip()
API_USERNAME = _env("OPENCORTYX_USERNAME", default="demo").strip()

# Friendly label for this agent in captured sessions (Conversations page).
AGENT_NAME = _env("OPENCORTYX_AGENT_NAME", default="MCP agent").strip()

# Transport: "stdio" (Claude Desktop / Cursor / Inspector) | "sse" | "streamable-http" (the service).
TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").strip()
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
# Host headers accepted over HTTP (DNS-rebinding protection). Add your proxy hostname when exposing it.
ALLOWED_HOSTS = [h.strip() for h in os.environ.get(
    "MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,[::1]:*,mcp:*").split(",") if h.strip()]
PORT = int(os.environ.get("MCP_PORT", "8001"))

HTTP_TIMEOUT = float(_env("OPENCORTYX_HTTP_TIMEOUT", default="120"))


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


IS_STDIO = TRANSPORT == "stdio"
# Default deny. `MCP_ALLOW_ANONYMOUS=true` is an explicit, loudly-warned escape hatch for a trusted
# local sandbox — never set it on anything reachable beyond localhost.
ALLOW_ANONYMOUS = _flag("MCP_ALLOW_ANONYMOUS", False)
REQUIRE_AUTH = (not IS_STDIO) and (not ALLOW_ANONYMOUS)
# How long a validated credential is trusted before it is re-checked against the API. Short, because
# this is also the maximum window in which a REVOKED key still works.
AUTH_CACHE_TTL = float(os.environ.get("MCP_AUTH_CACHE_TTL", "60"))
AUTH_NEGATIVE_CACHE_TTL = float(os.environ.get("MCP_AUTH_NEGATIVE_CACHE_TTL", "30"))
AUTH_TIMEOUT = float(os.environ.get("MCP_AUTH_TIMEOUT", "10"))
# Failed-attempt throttle per peer (key guessing): LIMIT failures inside WINDOW seconds → 429.
AUTH_FAIL_LIMIT = int(os.environ.get("MCP_AUTH_FAIL_LIMIT", "10"))
AUTH_FAIL_WINDOW = float(os.environ.get("MCP_AUTH_FAIL_WINDOW", "60"))
TRUST_FORWARDED_FOR = _flag("MCP_TRUST_FORWARDED_FOR", False)

LOG_LEVEL = os.environ.get("MCP_LOG_LEVEL", "info").strip().lower()
