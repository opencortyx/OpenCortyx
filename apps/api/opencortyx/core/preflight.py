"""Startup preflight — fail LOUD, not silent.

The #1 production footgun in this stack is silent degradation: a missing embedding model means
every query returns empty vectors and the system abstains on everything; a missing cross-encoder
means reranking silently falls back to a flaky LLM and miscalibrates the abstain gate. Both look like
"the product just doesn't work" with no error anywhere.

This module runs once at startup and emits clear, actionable warnings for each degraded dependency.
It NEVER raises — the app still boots (so an operator can open the dashboard and read the health
page) — but the logs make the problem obvious instead of invisible. Mirrors the deeper per-request
checks in GET /api/v2/health.
"""
import structlog

log = structlog.get_logger()


async def run_preflight() -> dict:
    """Probe the critical dependencies. Returns a summary dict and logs warnings for anything degraded.
    Best-effort: any probe that errors is reported as a warning, never propagated."""
    from opencortyx.core.config import settings

    issues: list[str] = []

    # 1 — The registered embedding model and a live test embed (empty vectors → abstain-on-everything).
    # Models come from the registry only; before the first workspace exists there is none yet.
    from opencortyx.core.inference.embedding import active_embedding_spec, embed_batch
    spec = await active_embedding_spec()
    if not spec:
        log.info("preflight.embedding_pending",
                 detail="no embedding model registered yet - one is added when the first workspace is created")
    else:
        name = spec.get("model_id")
        try:
            vecs = await embed_batch(["preflight probe"], model_spec=spec)
            dim = len(vecs[0]) if vecs and vecs[0] else 0
            if dim <= 0:
                issues.append(
                    f"EMBEDDING returned an empty vector (provider '{spec.get('provider')}', model "
                    f"'{name}'). Retrieval will abstain on EVERY query. Fix: check the model on the "
                    f"Models page and that there is free memory.")
            elif dim != settings.EMBED_DIMENSIONS:
                issues.append(
                    f"EMBEDDING dimension mismatch: model '{name}' returns {dim} dims but "
                    f"EMBED_DIMENSIONS is {settings.EMBED_DIMENSIONS}. Vector writes will fail or be "
                    f"unsearchable. Fix: set EMBED_DIMENSIONS={dim} and RE-INDEX, or change the model back.")
            else:
                log.info("preflight.embedding_ok", dim=dim, model=name, provider=spec.get("provider"))
        except Exception as e:
            issues.append(f"EMBEDDING failed ({e}). Retrieval will abstain on everything. Fix: check "
                          f"the embedding model '{name}' on the Models page.")

    # 2 — Cross-encoder reranker availability (silent → flaky LLM fallback miscalibrates abstain gate)
    try:
        import importlib.util
        # either library runs a cross-encoder: fastembed the built-in one, sentence-transformers a
        # model added on the Models page
        if not any(importlib.util.find_spec(m) for m in ("fastembed", "sentence_transformers")):
            issues.append(
                "RERANKER: no cross-encoder library (fastembed or sentence-transformers) is installed — "
                "reranking falls back to a local LLM, which miscalibrates relevance and can abstain on "
                "answerable questions. Fix: rebuild the image, which carries fastembed.")
        else:
            log.info("preflight.reranker_ok", reranker="cross-encoder")
    except Exception as e:
        issues.append(f"RERANKER check failed: {e}")

    # Generation is per workspace (each connects its own server on the Models page), so there is no
    # deployment-wide model to probe here; the health page checks the signed-in workspace's.

    if issues:
        log.warning("preflight.DEGRADED", count=len(issues))
        for i, msg in enumerate(issues, 1):
            log.warning("preflight.issue", n=f"{i}/{len(issues)}", detail=msg)
    else:
        log.info("preflight.all_ok")

    return {"ok": not issues, "issues": issues}
