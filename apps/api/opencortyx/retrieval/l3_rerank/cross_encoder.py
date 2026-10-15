"""L3 — the learned relevance signal: a cross-encoder scoring (question, text) pairs.

    top-N by fusion ──► BGE cross-encoder ──► one NAMED FEATURE among fourteen
                             │
                       unavailable / too slow / outside the budget
                             │
                             └──► the feature is ABSENT, and everything else still ranks

WHY IT IS A FEATURE AND NOT A RANKING

A cross-encoder reads the question and the passage together, so it separates "owns a Nintendo
Switch" from "discusses Nintendo" in a way no bi-encoder can. Measured here on the real model
(`BAAI/bge-reranker-v2-m3`): 0.995 for the passage that answers the question, 0.000016 for an
unrelated one. That is a far sharper discriminator than any hand-weighted feature.

It is also the component with the worst track record in this system. Two failures are on record
and both came from letting it DECIDE rather than CONTRIBUTE:

  - it buried in-corpus answers, and the fix was `max(rerank, cosine)` — a patch that exists
    precisely because a single flaky number had been allowed to overwrite a good one.
  - it cost 568M parameters of CPU inference per query in a latency investigation. Measured here
    at ~80ms per pair: a pool of 30 is ~2.4 seconds.

So it enters as `cross_encoder`, one weighted feature beside thirteen others. A wrong score costs
a share of the ranking, never the answer, and no `max()` patch is needed because nothing was
overwritten in the first place.

ABSENT IS NOT ZERO

Only the top `pool` candidates are scored, because the cost is real. Candidates outside that
budget have the feature ABSENT — `score_features` then leaves its weight out of the normalising
mass entirely. Scoring the top 30 and reading "absent" as "worst" would make being outside a COST
bound into a demotion, which is the conflation this codebase already paid for once when a
cross-encoder's pool size silently capped how much evidence could survive.

OFF BY DEFAULT

At ~2.4s for a 30-candidate pool this is not free, and the engine ranks perfectly well without
it. It is opt-in per query via `WorkflowConfig.cross_encoder`.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

#: Text handed to the model per candidate. Long passages are truncated by the model anyway, and
#: a shorter pair is markedly faster.
MAX_TEXT_CHARS = 900

#: How many candidates are scored. A COST bound, and it must never be read as a recall bound:
#: everything outside it keeps every other feature and is still ranked.
DEFAULT_POOL = 30


@dataclass
class CrossEncoderResult:
    scores: dict                      # node_id -> score in [0,1]
    model: str = ""
    ms: float = 0.0
    scored: int = 0
    error: str = ""

    @property
    def available(self) -> bool:
        return bool(self.scores)


def _load(model_id: Optional[str] = None):
    """The shared loader. Reuses the existing engine's cache so a model is not held twice.

    A ~2GB model resident in one process and again in another is not a caching subtlety, it is
    an out-of-memory error on a machine already running an embedder and an LLM.
    """
    from opencortyx.core.inference.embedding import get_cross_encoder
    return get_cross_encoder(model_id)


def _pair_text(cand) -> str:
    text = (cand.text or "").strip() or (cand.label or "").strip()
    return text[:MAX_TEXT_CHARS]


async def score(query: str, candidates: list, *, pool: int = DEFAULT_POOL,
                model_id: Optional[str] = None) -> CrossEncoderResult:
    """Score the top `pool` candidates against the question.

    Returns an empty result when the model is unavailable — not an exception and not zeros. The
    caller then omits the feature, and every other signal ranks exactly as it did before.
    """
    t0 = time.perf_counter()
    subject = [c for c in (candidates or [])[:pool] if _pair_text(c)]
    if not subject:
        return CrossEncoderResult(scores={}, error="no candidate carried text")

    model = _load(model_id)
    if model is None:
        return CrossEncoderResult(
            scores={}, error="cross-encoder unavailable (sentence-transformers or model missing)")

    pairs = [(query, _pair_text(c)) for c in subject]
    try:
        # `predict` is synchronous and CPU-bound; a thread keeps it off the event loop so the
        # rest of the request is not blocked behind ~2.4s of inference.
        raw = await asyncio.to_thread(model.predict, pairs)
    except Exception as exc:                       # a failed model must degrade, not raise
        return CrossEncoderResult(scores={}, error=f"{type(exc).__name__}: {exc}")

    scores = {}
    for cand, value in zip(subject, raw):
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        # bge-reranker-v2-m3 emits [0,1] already; clamping keeps a differently-calibrated model
        # from handing a feature a value outside the range every other feature obeys.
        scores[cand.node_id] = max(0.0, min(1.0, v))

    return CrossEncoderResult(scores=scores, model=model_id or "default",
                              ms=round((time.perf_counter() - t0) * 1000, 1),
                              scored=len(scores))


def apply(result: CrossEncoderResult, candidates: list) -> int:
    """Attach the scores as the `cross_encoder` feature. Returns how many were annotated.

    Candidates the model did not see are left ALONE — the key stays absent, which is what makes
    "not measured" distinguishable from "measured as irrelevant".
    """
    if not result.available:
        return 0
    n = 0
    for cand in candidates:
        value = result.scores.get(cand.node_id)
        if value is not None:
            cand.features["cross_encoder"] = value
            n += 1
    return n
