"""Trust capability — the retrieval-time trust score (CRAG relevance) + source-trust boost.

Two distinct "trust" notions, both owned here:
  • Retrieval trust (CRAG) — before answering, a cheap model judges whether the retrieved context
    can actually answer the question (YES/NO → 1.0/0.0). This is the `trust_score` on every answer
    and audit-log row. Extracted from retrieve/answer.py; `answer.py` now calls `score_relevance`.
  • Source trust — how much a claim's origin is trusted when Resolution picks a contradiction winner
    (re-exported from the Resolution policy; the boost knob lives here as it is a trust concept).

Extract-&-wrap: identical behaviour to the previous inline block (incl. the fast-mode short-circuit
and the 0.5-on-error fallback); it just lives in a registered capability now.
"""
from __future__ import annotations

from opencortyx.capabilities.base import Capability, ConfigField


def _toks(s: str) -> int:
    return max(0, len(s or "") // 4)


#: The context is judged in windows of this many characters, at most WINDOWS of them, best one wins:
#: an answer in the fifth passage of a long context is as relevant as one in the first. (Judging only
#: the first 6,000 characters said NO to contexts whose answer sat further down.)
WINDOW_CHARS = 4000
WINDOWS = 4


def _windows(context: str) -> list[str]:
    """Consecutive windows of `context`, cut at line ends (a context block is one line)."""
    out, cur = [], ""
    for line in (context or "").splitlines(keepends=True):
        if cur and len(cur) + len(line) > WINDOW_CHARS:
            out.append(cur)
            cur = ""
            if len(out) == WINDOWS:
                break
        cur += line[:WINDOW_CHARS]
    if cur and len(out) < WINDOWS:
        out.append(cur)
    return out or [""]


async def score_relevance(workspace_id, query: str, context: str, util_spec=None, fast: bool = False) -> dict:
    """CRAG relevance evaluation → {trust_score, tokens_in, tokens_out, eval_context}. Fast mode skips
    the LLM round-trip (retrieval is already abstain-gated). Never raises (0.5 on error).

    The score is graded, not a YES/NO coin: the model's own likelihood of YES against NO
    (core/inference/choice.py), calibrated against the same prompt with no context, for each window
    of the context; the best window is the score. A model that answers only in text (a remote API)
    is asked for the word, as before."""
    import math
    if fast:
        return {"trust_score": 1.0, "tokens_in": 0, "tokens_out": 0, "eval_context": context[:6000]}
    # lazy import to avoid a load-time cycle (answer.py imports this module's owner indirectly)
    from opencortyx.core.inference import choice
    from opencortyx.retrieval.answering.answer import _answer_prompt, _DEFAULT_RELEVANCE
    tmpl = await _answer_prompt(workspace_id, "builtin:answer.relevance", _DEFAULT_RELEVANCE)
    sys = "You only output YES or NO."
    best, best_window, spent = 0.0, "", 0
    try:
        for window in _windows(context):
            prompt = tmpl.replace("{context}", window).replace("{query}", query)
            c = await choice.choose(prompt, ["YES", "NO"], model_spec=util_spec, system=sys,
                                    baseline=tmpl.replace("{context}", "").replace("{query}", query))
            if c is None:
                break
            p = 1.0 / (1.0 + math.exp(-(c.scores[0] - c.scores[1])))   # P(YES) over YES and NO
            spent += _toks(prompt)
            if p > best:
                best, best_window = p, window
            if best >= 0.5:
                break                                   # the context answers it: no need to read further
        return {"trust_score": round(best, 3), "tokens_in": spent + _toks(sys), "tokens_out": 0,
                "eval_context": best_window or context[:6000]}
    except Exception:
        return {"trust_score": 0.5, "tokens_in": 0, "tokens_out": 0, "eval_context": context[:6000]}


class TrustScoring(Capability):
    name = "trust"
    title = "Trust Score"
    group = "Trust"
    description = ("Grade how trustworthy an answer's grounding is (CRAG relevance: does the retrieved "
                   "context actually answer the question?), and how much a source is trusted when "
                   "Resolution picks a contradiction winner. Applies to every datasource at query time.")
    ui_route = "/dashboard/settings?tab=capabilities"

    def config_schema(self):
        return [
            ConfigField("crag_min_citations", "CRAG min citations", "int", default=3, min=0, max=50,
                        help="The relevance gate only abstains when fewer than this many citations were "
                             "grounded, so a flaky YES/NO verdict cannot bury a good retrieval."),
            ConfigField("resolution_trust_boost", "Source-trust boost (validated)", "float",
                        default=0.5, min=0.0, max=2.0,
                        help="Extra trust a human-validated / user-entered claim carries over a plain "
                             "extracted one (used by Resolution's trust-weighted winner policy)."),
        ]

    # RetrievalScorer hook
    async def score(self, query: str, context: str, **kw):
        return await score_relevance(kw.get("workspace_id"), query, context,
                                     util_spec=kw.get("util_spec"), fast=kw.get("fast", False))
