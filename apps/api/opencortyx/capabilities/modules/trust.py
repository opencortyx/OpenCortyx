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


async def score_relevance(workspace_id, query: str, context: str, util_spec=None, fast: bool = False) -> dict:
    """CRAG relevance evaluation → {trust_score, tokens_in, tokens_out, eval_context}. Fast mode skips
    the LLM round-trip (retrieval is already abstain-gated). Never raises (0.5 on error)."""
    if fast:
        return {"trust_score": 1.0, "tokens_in": 0, "tokens_out": 0, "eval_context": context[:6000]}
    # lazy import to avoid a load-time cycle (answer.py imports this module's owner indirectly)
    from opencortyx.retrieval.answering.answer import _answer_prompt, _DEFAULT_RELEVANCE
    from opencortyx.core.inference.llm import complete
    eval_context = context[:6000]
    tmpl = await _answer_prompt(workspace_id, "builtin:answer.relevance", _DEFAULT_RELEVANCE)
    prompt = tmpl.replace("{context}", eval_context).replace("{query}", query)
    sys = "You only output YES or NO."
    try:
        out = await complete(prompt, system=sys, max_tokens=10, temperature=0.0, model_spec=util_spec)
        return {"trust_score": 1.0 if "yes" in (out or "").lower() else 0.0,
                "tokens_in": _toks(prompt) + _toks(sys), "tokens_out": _toks(out),
                "eval_context": eval_context}
    except Exception:
        return {"trust_score": 0.5, "tokens_in": 0, "tokens_out": 0, "eval_context": eval_context}


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
