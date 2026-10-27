"""The two things a decomposition asks the generation model: split a question, and read one answer.

Both are short, greedy calls written for a small model on a CPU (the built-in Qwen3-0.6B):

  - the split is a numbered list with `#n` placeholders, never JSON a small model half-closes;
  - the bridge answer is a few words read from a handful of passages, nothing to reason about.

The examples in the split prompt are deliberately NOT about the documents OpenCortyx is tested on.
A prompt that shows the model how to split "the RFC that RFC 9260 obsoletes" teaches it the test,
and says nothing about whether it splits a question about contracts or invoices.

A call that fails, times out or returns something unusable returns None. The caller then searches
the question as it was asked: a decomposition may add evidence, never take the search away.
"""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable, Optional

from opencortyx.retrieval.decomposition.steps import Decomposition, parse_steps

#: (prompt, max_tokens=, temperature=) -> text. The workflow's bound completion callable.
Complete = Callable[..., Awaitable[str]]

#: A CPU model that has not answered in this long is not going to help this question.
CALL_TIMEOUT_S = 30.0

SPLIT_MAX_TOKENS = 120
ANSWER_MAX_TOKENS = 24

#: How much of a sub-question's context the model reads to find the bridge answer. The answer is
#: almost always in the top few blocks (a header line, a title); more text only slows a CPU model.
ANSWER_BLOCKS = 6
ANSWER_BLOCK_CHARS = 700

_SPLIT_PROMPT = """Split the question into the smallest steps a search engine can answer one at a time.
Write each step as a question on its own numbered line. When a step needs the answer to an earlier
step, write #1 or #2 for that answer. Use only names that appear in the question. If the question
is already a single step, write it unchanged as step 1.

Question: Who directed the film that won Best Picture in 1998?
1. Which film won Best Picture in 1998?
2. Who directed #1?

Question: What is the population of the city where Acme Corp has its headquarters?
1. In which city does Acme Corp have its headquarters?
2. What is the population of #1?

Question: Contract C-17 was renewed by a later contract. Who signed that later contract, and when?
1. Which contract renewed contract C-17?
2. Who signed #1?
3. When was #1 signed?

Question: What is the capital of France?
1. What is the capital of France?

Question: {question}
"""

_ANSWER_PROMPT = """Answer the question from the passages only. Reply with the answer alone - a name,
number, date, title or identifier - in a few words. If the passages do not say, reply: unknown

Passages:
{passages}

Question: {question}
Answer:"""

_UNKNOWN = re.compile(r"^\s*(?:unknown|none|n/?a|not (?:stated|given|mentioned|found)|"
                      r"the passages do not)\b", re.I)


async def _call(complete: Complete, prompt: str, max_tokens: int) -> Optional[str]:
    try:
        return await asyncio.wait_for(complete(prompt, max_tokens=max_tokens, temperature=0.0),
                                      timeout=CALL_TIMEOUT_S)
    except Exception:                                  # noqa: BLE001 - no model, timeout, error
        return None


async def split(question: str, complete: Complete) -> Optional[Decomposition]:
    text = await _call(complete, _SPLIT_PROMPT.format(question=question.strip()), SPLIT_MAX_TOKENS)
    if not text:
        return None
    # The model continues the pattern: it may write the next "Question:" itself. Keep the first plan.
    text = re.split(r"\n\s*Question:", text, maxsplit=1)[0]
    return parse_steps(question, text)


def _passages(blocks) -> str:
    out = []
    for i, b in enumerate(blocks[:ANSWER_BLOCKS], start=1):
        body = re.sub(r"\s+", " ", (getattr(b, "text", "") or "")).strip()
        out.append("[%d] %s" % (i, body[:ANSWER_BLOCK_CHARS]))
    return "\n".join(out)


def clean_answer(text: str) -> str:
    """The answer as a search term: first line, no label, no trailing sentence, at most 12 words."""
    line = next((ln for ln in (text or "").splitlines() if ln.strip()), "").strip()
    line = re.sub(r"^\s*(?:answer\s*:\s*)", "", line, flags=re.I).strip(" \"'*`")
    if not line or _UNKNOWN.match(line):
        return ""
    line = re.split(r"(?<=[a-z0-9)])\.\s", line, maxsplit=1)[0].rstrip(" .")
    return " ".join(line.split()[:12])


async def answer(question: str, blocks, complete: Complete) -> str:
    """The bridge: the short answer one sub-question's context gives, to write into the next. Empty
    when the context does not hold it - the chain stops there rather than search for a guess."""
    if not blocks:
        return ""
    text = await _call(complete, _ANSWER_PROMPT.format(passages=_passages(blocks),
                                                       question=question.strip()),
                       ANSWER_MAX_TOKENS)
    return clean_answer(text or "")
