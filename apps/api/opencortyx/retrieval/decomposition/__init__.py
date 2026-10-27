"""Query decomposition: a question one search cannot answer, asked as several that it can.

    question ──► is_compound? ──no──► the workflow, once (as before)
                     │yes
                     ▼
                 split: comparison of named subjects ─► one step per subject      (rules, no model)
                        anything else                ─► numbered steps with #n    (generation model)
                     │
                     ▼
                 execute: each step through L0-L4, the question as asked beside them,
                          the model reading each bridge answer, one merged context

Why a module of its own, beside L0-L4 rather than inside one of them: every layer works on ONE
question. Decomposition is above them - it decides which questions the layers are asked, runs the
whole workflow per question, and joins the results. L0's `planner.decompose` is a different thing,
kept as it is: it adds extra query vectors to one search and never runs a second one.

In "comparisons" mode (the default) only comparisons are split, which needs no model. In "full"
mode chained questions are split and bridged by the generation model too, which needs a capable
one - see MODES. Any failure along
the way (no model, a timeout, a plan that does not parse) falls back to the single search, and the
question as asked is always searched, so decomposing can add evidence but never lose any.
"""
from __future__ import annotations

from opencortyx.retrieval.contracts import RetrievalResult
from opencortyx.retrieval.decomposition import model
from opencortyx.retrieval.decomposition.executor import Retrieve, execute, merge
from opencortyx.retrieval.decomposition.steps import (
    Decomposition, SubQuestion, is_compound, split_comparison,
)

__all__ = ["Decomposition", "SubQuestion", "decompose", "execute", "is_compound", "merge", "run",
           "split_comparison"]


#: What gets decomposed. "comparisons" needs no model; "full" also asks the generation model to split
#: chained questions, which needs a capable model: Qwen3-0.6B, the built-in one, copied its prompt's
#: examples or echoed the question on every chained question of the 1000-file bench (6-15 s each).
MODES = ("off", "comparisons", "full")


async def decompose(question: str, complete: model.Complete,
                    mode: str = "full") -> Decomposition | None:
    """The plan for `question`, or None when it should be searched as it is."""
    if mode == "off" or not is_compound(question):
        return None
    plan = split_comparison(question)
    if plan is None and mode == "full":
        plan = await model.split(question, complete)
    return plan


async def run(question: str, retrieve: Retrieve, complete: model.Complete,
              budget_tokens: int, max_blocks: int = 0, mode: str = "full") -> RetrievalResult:
    plan = await decompose(question, complete, mode)
    if plan is None:
        return await retrieve(question)
    return await execute(plan, retrieve, complete, budget_tokens, max_blocks)

