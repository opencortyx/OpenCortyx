"""Startup preflight — fail LOUD, not silent.

The #1 production footgun in this stack is silent degradation: a missing embedding model means
every query returns empty vectors and the system abstains on everything; a missing cross-encoder
means reranking silently falls back to a flaky LLM and miscalibrates the abstain gate; a vLLM server
that is up but serving a different `--served-model-name` 404s every call. All three look like "the
product just doesn't work" with no error anywhere.

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

    # 1 — Embedding model present + a live test embed (empty vectors → abstain-on-everything)
    try:
        from opencortyx.core.inference.embedding import embed_batch
        vecs = await embed_batch(["preflight probe"])
        dim = len(vecs[0]) if vecs and vecs[0] else 0
        if dim <= 0:
            issues.append(
                f"EMBEDDING returned an empty vector (provider '{settings.EMBED_PROVIDER}', model "
                f"'{settings.EMBED_MODEL}'). Retrieval will abstain on EVERY query. Fix: check the "
                f"model id is valid for that provider and that there is free memory.")
        elif dim != settings.EMBED_DIMENSIONS:
            issues.append(
                f"EMBEDDING dimension mismatch: model '{settings.EMBED_MODEL}' returns {dim} dims but "
                f"EMBED_DIMENSIONS is {settings.EMBED_DIMENSIONS}. Vector writes will fail or be "
                f"unsearchable. Fix: set EMBED_DIMENSIONS={dim} and RE-INDEX, or change the model back.")
        else:
            log.info("preflight.embedding_ok", dim=dim, model=settings.EMBED_MODEL,
                     provider=settings.EMBED_PROVIDER)
    except Exception as e:
        issues.append(
            f"EMBEDDING failed ({e}). Retrieval will abstain on everything. Fix: with "
            f"EMBED_PROVIDER=fastembed nothing external is needed - check the model id; with an HTTP "
            f"embedding server, check EMBED_BASE_URL is reachable and serving "
            f"'{settings.EMBED_MODEL}'.")

    # 2 — Cross-encoder reranker availability (silent → flaky LLM fallback miscalibrates abstain gate)
    try:
        import importlib.util
        if importlib.util.find_spec("sentence_transformers") is None:
            issues.append(
                "RERANKER: `sentence-transformers` (cross-encoder) is NOT installed — reranking falls "
                "back to a local LLM, which miscalibrates relevance and can abstain on answerable "
                "questions. Fix: `pip install sentence-transformers` (adds the bge cross-encoder), or "
                "accept the slower/weaker LLM fallback knowingly.")
        else:
            log.info("preflight.reranker_ok", reranker="cross-encoder")
    except Exception as e:
        issues.append(f"RERANKER check failed: {e}")

    from opencortyx.core.inference.registry import active_generation_spec
    gen = await active_generation_spec() or {
        "provider": settings.LLM_PROVIDER, "base_url": settings.LLM_BASE_URL,
        "model_id": settings.LLM_MODEL}
    if str(gen.get("provider") or "").lower() in ("vllm", "openai_compatible") and gen.get("base_url"):
        base = str(gen["base_url"]).rstrip("/")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(f"{base}/models")
                r.raise_for_status()
                data = r.json().get("data") or []
            served = {str(d["id"]) for d in data if isinstance(d, dict) and d.get("id")}
            # Every alias this deployment will ask for. FAST_MODEL is only distinct when the split is
            # configured; empty means "same as LLM_MODEL", which is the single-GPU default.
            want = {m for m in (gen.get("model_id"),) if m}
            missing = sorted(want - served)
            if not served:
                issues.append(
                    f"vLLM at {base} is reachable but advertises no models. Fix: check the server "
                    f"finished loading weights (its /health goes ready only after the load).")
            elif missing:
                issues.append(
                    f"vLLM at {base} serves {sorted(served)} but this deployment will request "
                    f"{missing}. EVERY generation call will 404. Fix: set the generation model's Model ID on "
                    f"the Models page to one the server serves (or start vLLM with --served-model-name "
                    f"{sorted(missing)[0]}).")
            else:
                log.info("preflight.generation_ok", model=gen.get("model_id"), served=sorted(served),
                         base_url=base)
        except Exception as e:
            issues.append(
                f"vLLM not reachable at {base} ({e}). Generation and extraction will fail. Fix: "
                f"start it (  docker compose up -d vllm  ); a cold start DOWNLOADS the weights and "
                f"can take several minutes. Check the model's Base URL keeps the /v1 suffix.")

    if issues:
        log.warning("preflight.DEGRADED", count=len(issues))
        for i, msg in enumerate(issues, 1):
            log.warning("preflight.issue", n=f"{i}/{len(issues)}", detail=msg)
    else:
        log.info("preflight.all_ok")

    return {"ok": not issues, "issues": issues}
