"""Lightweight rate limiting + request-size guard (no external dependency).

Why this exists: the question endpoints (/answer, /context) each cost real compute, and uploads
fill the disk and the queue. With no limit, a single misbehaving client (or a runaway agent loop) can rack up unbounded
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

# Per-client request budgets (requests / window seconds), one bucket per class of path.
_WINDOW = settings.RATELIMIT_WINDOW_SECONDS
_DEFAULT_MAX = settings.RATELIMIT_DEFAULT_PER_MIN
_MAX_BODY = settings.MAX_REQUEST_BYTES   # 50 MB default
_ENABLED = settings.RATELIMIT_ENABLED

# (class, path substrings, budget, what the 429 calls them), first match wins. Questions are expensive
# per request. An upload is cheap here (the file is stored and queued; the worker does the work at its
# own pace), so uploads have their own, larger budget: sharing the questions' 30 a minute failed every
# file after the 30th of a folder dropped on Add data.
_CLASSES = (
    ("upload", ("/sources/upload", "/json/ingest", "/structured/ingest-file"),
     settings.RATELIMIT_UPLOAD_PER_MIN, "upload"),
    ("heavy", ("/answer", "/context", "/eval/run", "/nl-sql", "/intelligence"),
     settings.RATELIMIT_HEAVY_PER_MIN, "LLM"),
)

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


def _class_of(path: str) -> tuple[str, int, str]:
    for name, paths, limit, label in _CLASSES:
        if any(p in path for p in paths):
            return name, limit, label
    return "default", _DEFAULT_MAX, "general"


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
            cls, limit, label = _class_of(path)
            bucket = f"{_client_id(request)}|{cls}"
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
                                       f"{label} endpoints). Retry in {retry}s."})
            dq.append(now)

        return await call_next(request)
