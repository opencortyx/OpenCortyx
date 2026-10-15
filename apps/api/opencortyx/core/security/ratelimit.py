"""Lightweight rate limiting + request-size guard (no external dependency).

Why this exists: the LLM/embedding endpoints (/answer, /context, /sources/upload) each cost real
compute. With no limit, a single misbehaving client (or a runaway agent loop) can rack up unbounded
cost and starve everyone else — a cost-and-DoS hole. This middleware applies a per-client sliding
window and rejects oversized bodies with a clear 413/429.

Scope/limits are deliberately conservative defaults, overridable via env. It's an in-process limiter
(per API worker) — fine for a first release / single-node deploy; swap in Redis-backed limiting when
you scale horizontally and need a shared counter.
"""
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from opencortyx.core.config import settings

# Per-client request budgets (requests / window seconds). "heavy" = LLM/embedding/upload paths.
_WINDOW = settings.RATELIMIT_WINDOW_SECONDS
_HEAVY_MAX = settings.RATELIMIT_HEAVY_PER_MIN
_DEFAULT_MAX = settings.RATELIMIT_DEFAULT_PER_MIN
_MAX_BODY = settings.MAX_REQUEST_BYTES   # 50 MB default
_ENABLED = settings.RATELIMIT_ENABLED

# Paths whose work is expensive (substring match on the request path).
_HEAVY = ("/answer", "/context", "/sources/upload", "/eval/run", "/nl-sql", "/intelligence")

_hits: dict[str, deque] = defaultdict(deque)


def _client_id(request) -> str:
    # Prefer the API key / bearer subject so one workspace can't be impersonated by IP spoofing;
    # fall back to the peer IP (respecting a single proxy hop).
    key = request.headers.get("x-api-key")
    if key:
        return f"key:{key[:16]}"
    auth = request.headers.get("authorization", "")
    if auth:
        return f"tok:{auth[-16:]}"
    fwd = request.headers.get("x-forwarded-for", "")
    ip = (fwd.split(",")[0].strip() if fwd else None) or (request.client.host if request.client else "?")
    return f"ip:{ip}"


def _is_heavy(path: str) -> bool:
    return any(h in path for h in _HEAVY)


#: A JSON-escaped or raw NUL. No store accepts it (PostgreSQL text rejects 0x00), so a body holding one
#: failed deep inside a write with a 500; refused here, it is the caller's 422.
_NUL = (b"\\u0000", b"\x00")


async def _has_nul(request) -> bool:
    if "json" not in (request.headers.get("content-type") or ""):
        return False
    body = await request.body()                    # cached by Starlette; the route reads it again
    return any(n in body for n in _NUL)


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method in ("POST", "PUT", "PATCH") and await _has_nul(request):
            return JSONResponse(status_code=422,
                                content={"detail": "Text may not contain the NUL character (\\u0000)."})
        if not _ENABLED or request.method == "OPTIONS":
            return await call_next(request)

        # 1 — reject oversized bodies up front (cheap; uses the declared Content-Length)
        clen = request.headers.get("content-length")
        if clen:
            try:
                if int(clen) > _MAX_BODY:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"Request body too large (> {_MAX_BODY // (1024 * 1024)} MB). "
                                           "Split the upload or raise MAX_REQUEST_BYTES."})
            except ValueError:
                pass

        # 2 — sliding-window rate limit per client + path class
        path = request.url.path
        if path.startswith("/api"):
            heavy = _is_heavy(path)
            limit = _HEAVY_MAX if heavy else _DEFAULT_MAX
            bucket = f"{_client_id(request)}|{'heavy' if heavy else 'default'}"
            now = time.monotonic()
            dq = _hits[bucket]
            cutoff = now - _WINDOW
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= limit:
                retry = max(1, int(_WINDOW - (now - dq[0])))
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry)},
                    content={"detail": f"Rate limit exceeded ({limit} requests / {int(_WINDOW)}s for "
                                       f"{'LLM/upload' if heavy else 'general'} endpoints). Retry in {retry}s."})
            dq.append(now)

        return await call_next(request)
