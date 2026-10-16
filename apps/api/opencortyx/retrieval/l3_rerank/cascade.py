"""Reranker cascade — cheap→expensive, pluggable.

  Stage 0  (recall)    : bi-encoder (vector) + BM25 → RRF   [done in the planner]
  Stage 1  (precision) : cross-encoder (bge-reranker), or an HTTP reranker   [this module]

The cross-encoder needs `sentence-transformers` (heavy/torch). If it isn't installed, the
rerank step returns None and the candidates keep their fused order — nothing breaks.
"""
import math

from opencortyx.core.config import RECENCY_WEIGHT
from opencortyx.core import http
from opencortyx.core.inference.embedding import get_cross_encoder
from opencortyx.retrieval.common.temporal import recency_score

def _recency(c) -> float:
    return recency_score(c.meta.get("valid_from") or c.valid_from) * RECENCY_WEIGHT


#: A reranker reached over HTTP: a server you run (registered as openai_compatible with its address)
#: or the HuggingFace Inference API.
HTTP_RERANK_PROVIDERS = frozenset({"openai_compatible", "huggingface", "hf"})


async def http_rerank(query: str, candidates: list, top_n: int, spec: dict):
    """Score `candidates` with a remote reranker. Returns a reordered list, or None to fall through.

    Speaks the TWO request shapes rerank servers use rather than picking one:
      * POST {base}/rerank  {"query", "texts"}      →  [{index, score}]
      * POST {base}/rerank  {"query", "documents"}  →  {results:[{index, relevance_score}]}
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
    # `texts` for one shape, `documents` + `model` for the other — sending both keys lets one
    # request satisfy either server instead of probing for the flavour first.
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
    # The registry's reranker or none: one that will not load is not swapped for another model.
    model = get_cross_encoder(model_id)
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
