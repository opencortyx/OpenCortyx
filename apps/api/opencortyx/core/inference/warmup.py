"""Get the models resident WITHOUT anyone waiting for them.

A first install downloads ~2.7 GB of weights (the generation model lives on the server you connect):

    BAAI/bge-reranker-v2-m3          2.1 GB   first retrieval query
    nomic-embed-text-v1.5            523 MB   first embed
    all-MiniLM-L6-v2                  87 MB

None of that can be made instant — it is a download. What CAN be fixed is who waits for it, and
before this the answer was "whoever acts first":

  * `run_preflight()` performs a live test embed and was awaited in the lifespan, so the API did not
    report healthy until the 523 MB embedder had arrived. On a first install the dashboard sat
    unreachable with nothing saying why.
  * The reranker was never prefetched at all, so the first QUESTION a new user asked paid 2.1 GB
    mid-request — a query that looks hung, on a stack that looks broken.

So warmup runs as a background task: the API is healthy immediately, the UI loads, and the weights
arrive underneath. `status()` reports progress so the dashboard can say "preparing models" instead of
failing silently, and every failure is degraded-not-fatal — a stack with no models still serves
everything that does not need one.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict

import structlog

log = structlog.get_logger()

#: name -> {"state": pending|loading|ready|failed, "seconds": float, "detail": str}
_STATE: Dict[str, Dict[str, Any]] = {}
_TASK: asyncio.Task | None = None


def status() -> Dict[str, Any]:
    """What the models are doing, for /v2/health and the dashboard."""
    done = [k for k, v in _STATE.items() if v["state"] in ("ready", "failed", "n/a")]
    return {
        "models": dict(_STATE),
        "ready": bool(_STATE) and len(done) == len(_STATE),
        "pending": [k for k, v in _STATE.items() if v["state"] in ("pending", "loading")],
    }


async def _step(name: str, coro_factory) -> None:
    _STATE[name] = {"state": "loading", "seconds": 0.0, "detail": ""}
    t0 = time.monotonic()
    try:
        await coro_factory()
        _STATE[name] = {"state": "ready", "seconds": round(time.monotonic() - t0, 1), "detail": ""}
        log.info("opencortyx.warmup_ready", model=name, seconds=_STATE[name]["seconds"])
    except _NotApplicable as e:
        # Distinct from a failure: nothing is wrong, this model is simply not part of this
        # deployment. Logging it as a warning trains people to ignore warmup warnings.
        _STATE[name] = {"state": "n/a", "seconds": round(time.monotonic() - t0, 1),
                        "detail": str(e)[:200]}
        log.info("opencortyx.warmup_not_applicable", model=name, reason=str(e)[:120])
    except Exception as e:                              # noqa: BLE001 — never fatal
        _STATE[name] = {"state": "failed", "seconds": round(time.monotonic() - t0, 1),
                        "detail": str(e)[:200]}
        log.warning("opencortyx.warmup_failed", model=name, error=str(e)[:200])


async def _embedder() -> None:
    from opencortyx.core.inference.embedding import active_embedding_spec, embed_batch
    spec = await active_embedding_spec()
    if not spec:
        raise _NotApplicable("no embedding model registered yet (added when the first workspace is created)")
    vecs = await embed_batch(["warmup"], model_spec=spec)
    if not vecs or not vecs[0]:
        raise RuntimeError("embedder returned an empty vector")


class _NotApplicable(Exception):
    """Not an error: this model has no work to do in this deployment."""


async def _reranker() -> None:
    """The rerankers the Model Registry names as defaults, loaded before the first query uses one.

    A fastembed reranker (ONNX) always runs here. A sentence-transformers one needs the library,
    which the image carries only when built with INSTALL_RERANK=1; without it such a row is skipped,
    exactly as retrieval skips it.
    """
    from opencortyx.core import config as cfg
    if not getattr(cfg, "RERANK_ENABLED", False):
        raise _NotApplicable("reranking is disabled")
    from sqlalchemy import select
    from opencortyx.core.db import V2Session
    from opencortyx.domain import Model
    from opencortyx.core.inference.embedding import cross_encoder_installed, get_cross_encoder
    providers = ["fastembed"]
    if cross_encoder_installed():
        providers += ["sentence-transformers", "sentence_transformers"]
    async with V2Session() as db:
        rows = sorted(set((await db.execute(select(Model.model_id, Model.provider).where(
            Model.role == "rerank", Model.is_default == True,          # noqa: E712
            Model.provider.in_(providers)))).all()))
    if not rows:
        raise _NotApplicable("no reranker this image can run is registered")
    for mid, prov in rows:
        if await asyncio.to_thread(get_cross_encoder, mid, prov) is None:
            raise RuntimeError(f"reranker {mid} unavailable")


async def _run() -> None:
    # Embedder first: everything that writes a node needs it, and it is a quarter the size of the
    # reranker, so the useful half of the stack comes alive four times sooner.
    await _step("embedder", _embedder)
    await _step("reranker", _reranker)


def start() -> None:
    """Fire warmup in the background. Safe to call more than once."""
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    for k in ("embedder", "reranker"):
        _STATE.setdefault(k, {"state": "pending", "seconds": 0.0, "detail": ""})
    try:
        _TASK = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:                                # no loop (sync context) — nothing to do
        log.warning("opencortyx.warmup_no_loop")
