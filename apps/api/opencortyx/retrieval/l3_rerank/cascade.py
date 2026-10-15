"""Reranker cascade — cheap→expensive, pluggable.

  Stage 0  (recall)    : bi-encoder (vector) + BM25 → RRF   [done in the planner]
  Stage 1  (precision) : cross-encoder (bge-reranker)        [this module]
  Stage 2  (last resort): LLM listwise on the top few         [this module]

The cross-encoder needs `sentence-transformers` (heavy/torch). If it isn't installed the
cascade GRACEFULLY falls back to the LLM reranker — nothing breaks, just slower.
"""
import json
import math

from opencortyx.core.inference.llm import complete
from opencortyx.core.config import (
    RECENCY_WEIGHT, CROSS_ENCODER_MODEL)
from opencortyx.core import http
from opencortyx.core.inference.embedding import get_cross_encoder
from opencortyx.retrieval.common.temporal import recency_score

def _recency(c) -> float:
    return recency_score(c.meta.get("valid_from") or c.valid_from) * RECENCY_WEIGHT


HTTP_RERANK_PROVIDERS = frozenset({
    "tei", "huggingface", "hf", "cohere", "jina", "voyage",
    "vllm", "openai_compatible", "infinity",
})


async def http_rerank(query: str, candidates: list, top_n: int, spec: dict):
    """Score `candidates` with a remote reranker. Returns a reordered list, or None to fall through.

    Speaks the TWO shapes that exist in the wild rather than picking one and excluding half the
    ecosystem:
      * TEI / Infinity  →  POST {base}/rerank  {"query", "texts"}      →  [{index, score}]
      * Cohere / Jina   →  POST {base}/rerank  {"query", "documents"}  →  {results:[{index, relevance_score}]}
    The response is detected, not configured, because a user pasting a base_url should not also have
    to know which dialect their server speaks.
    """
    base = (spec.get("base_url") or "").rstrip("/")
    if not base or not candidates:
        return None
    head, tail = candidates[:top_n], candidates[top_n:]
    texts = [(c.text or "")[:512] for c in head]
    url = base if base.endswith("/rerank") else f"{base}/rerank"
    headers = {}
    if spec.get("api_key"):
        headers["Authorization"] = f"Bearer {spec['api_key']}"
    model_id = spec.get("model_id")
    # `texts` for TEI, `documents` + `model` for Cohere/Jina — sending both keys lets one request
    # satisfy either server instead of probing for the flavour first.
    payload = {"query": query, "texts": texts, "documents": texts, "top_n": len(texts)}
    if model_id:
        payload["model"] = model_id
    try:
        r = await http.get_client().post(url, json=payload, headers=headers, timeout=20.0)
        r.raise_for_status()
        data = r.json()
    except Exception:                       # noqa: BLE001 — unreachable reranker must not fail a query
        return None
    rows = data.get("results") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        return None
    scored = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        i = row.get("index")
        sc = row.get("score", row.get("relevance_score"))
        if isinstance(i, int) and 0 <= i < len(head) and sc is not None:
            scored.append((i, float(sc)))
    if not scored:
        return None
    for i, sc in scored:
        head[i].score = float(sc) + _recency(head[i])
        head[i].meta["rerank_by"] = "http"
    order = [head[i] for i, _ in sorted(scored, key=lambda t: -t[1])]
    # Anything the server did not score keeps its place AFTER the scored set rather than vanishing.
    rest = [c for n, c in enumerate(head) if n not in {i for i, _ in scored}]
    return order + rest + tail


async def cross_encoder_rerank(query: str, candidates: list, top_n: int, model_id: str | None = None):
    """Stage 1. Returns reordered list, or None if the model isn't available."""
    model = get_cross_encoder(model_id)
    if model is None and model_id and model_id != CROSS_ENCODER_MODEL:
        # a registry-resolved reranker that won't load must NEVER silently disable reranking —
        # fall back to the known-good config default (bge-reranker-v2-m3).
        model = get_cross_encoder(CROSS_ENCODER_MODEL)
    if model is None:
        return None
    head, tail = candidates[:top_n], candidates[top_n:]
    pairs = [(query, (c.text or "")[:512]) for c in head]
    try:
        import anyio
        scores = await anyio.to_thread.run_sync(lambda: model.predict(pairs))
    except Exception:
        try:
            scores = model.predict(pairs)
        except Exception:
            return None
    for c, s in zip(head, scores):
        c.meta["rerank"] = 1.0 / (1.0 + math.exp(-float(s)))   # logit → 0..1
        c.meta["rerank_by"] = "cross"
    head.sort(key=lambda c: (c.meta.get("rerank", c.score) + _recency(c), c.score), reverse=True)
    return head + tail


async def llm_rerank(query: str, candidates: list, top_n: int = 30) -> list:
    """Stage 2 (and the fallback). LLM listwise scoring of the top-N."""
    if not candidates:
        return candidates
    head, tail = candidates[:top_n], candidates[top_n:]
    listing = "\n".join(f"[{i}] ({c.type}) {(c.text or '')[:240]}" for i, c in enumerate(head))
    prompt = (
        f"Question: {query}\n\nPassages:\n{listing}\n\n"
        "Score each passage 0.0-1.0 for how directly it helps answer the question. "
        'Return ONLY JSON: {"scores": {"0": 0.0, "1": 0.0, ...}}')
    try:
        raw = await complete(prompt, system="You are a precise reranking judge.",
                             max_tokens=600, json_mode=True, temperature=0.0)
        scores = (json.loads(raw) if raw and raw.strip().startswith("{") else {}).get("scores", {})
        for i, c in enumerate(head):
            v = scores.get(str(i))
            if v is not None:
                try:
                    c.meta["rerank"] = max(0.0, min(1.0, float(v)))
                    c.meta["rerank_by"] = "llm"
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    head.sort(key=lambda c: (c.meta.get("rerank", c.score) + _recency(c), c.score), reverse=True)
    return head + tail

