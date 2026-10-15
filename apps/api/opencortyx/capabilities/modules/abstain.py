"""Abstain capability — the no-hallucination guarantee: refuse rather than guess.

Owns the abstain CONFIG (relevance floor, grounding mode/threshold, CRAG citation gate) and
re-exports the proven gate primitives. The gates themselves are enforced inline in
retrieve/answer.py + retrieve/planner.py (multi-stage: retrieval relevance floor → CRAG rejection →
non-answer detection → grounding gate); this capability makes them a single named, configurable
concern that already applies to every datasource (retrieval is unified).

Extract-&-wrap: the critical `filter_and_decide` relevance floor — max(rerank, cosine) so a
miscalibrated reranker can never bury a confidently-retrieved passage — is re-exported unchanged.
"""
from __future__ import annotations

from opencortyx.capabilities.base import Capability, ConfigField

# stable re-export of the relevance-floor primitive
from opencortyx.retrieval.answering.trust import filter_and_decide

__all__ = ["AbstainGate", "filter_and_decide"]


class AbstainGate(Capability):
    name = "abstain"
    title = "Abstain (no-hallucination)"
    group = "Trust"
    description = ("Refuse to answer when the knowledge base does not actually support an answer: a "
                   "relevance floor on retrieval, a CRAG relevance gate, non-answer detection, and a "
                   "grounding gate (every answer sentence must be supported by the sources). Applies "
                   "to every datasource at query time.")
    ui_route = "/dashboard/settings?tab=capabilities"

    def config_schema(self):
        return [
            ConfigField("abstain_threshold", "Abstain threshold", "float", default=0.3,
                        min=0.0, max=1.0,
                        help="Minimum retrieval relevance before answering; higher = abstains more "
                             "(fewer hallucinations). Uses max(rerank, cosine) so a flaky reranker "
                             "cannot bury a confidently-retrieved passage."),
            ConfigField("grounding_mode", "Grounding verification", "select", default="cosine",
                        options=["cosine", "entailment"],
                        help="How answers are checked against sources. 'cosine' is fast embedding "
                             "similarity; 'entailment' uses an LLM judge to catch wrong-but-on-topic "
                             "sentences (needs a capable model)."),
            ConfigField("grounding_threshold", "Grounding similarity", "float", default=0.55,
                        min=0.0, max=1.0,
                        help="In cosine mode, minimum answer-sentence↔source similarity for a sentence "
                             "to count as grounded. Higher = stricter (more borderline answers abstain)."),
            ConfigField("crag_min_citations", "CRAG min citations", "int", default=3, min=0, max=50,
                        help="The CRAG relevance gate only abstains when fewer than this many "
                             "citations were grounded (shared with Trust)."),
        ]

    # AnswerGate marker — the concrete gates run inline in answer.py; this lists them for the UI.
    def gate_names(self) -> list[str]:
        return ["retrieval_floor", "crag_rejection", "non_answer", "grounding"]
