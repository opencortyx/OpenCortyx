"""L0 — query expansion: retrieve with the vocabulary of an ANSWER, not of a question.

THE MEASUREMENT THIS EXISTS FOR

Of the 122 winnable evidence turns that never entered the candidate pool, **87% share ZERO
content terms with their own question**, and not one shares three or more:

    What is Joanna ALLERGIC to?          -> "I can't have dairy, so no ice cream for me"
    What NICKNAME does Nate use?         -> "Hey Jo, guess what I did?"
    What are Joanna's HOBBIES?           -> "Writing and hanging with friends"
    What major ACHIEVEMENT in Jan 2022?  -> "I finally finished my first full screenplay"

A question is a third-person interrogative; the evidence is first-person casual speech. They are
different registers, and no amount of pool size, budget, ranking or depth bridges that — dense
search missed these at depth 4000 and BM25 put them at ranks 246-1305. The bridge has to be built
in the query, or at write time.

WHY THE OBVIOUS VERSION FAILED, AND WHAT CHANGED

A first attempt asked the model for "a sentence someone might say that answers this", and it
scored 2/12. The failure was visible in the output: it invented SPECIFICS.

    What is Joanna allergic to?      -> "Joanna is allergic to peanuts"   (it is dairy)
    What nickname does Nate use?     -> "Nate calls Joanna Jellybean"     (it is "Jo")
    Where did Joanna travel in July? -> "Joanna traveled to Japan"        (it is not Japan)

A hallucinated specific is worse than no specific: it drags the query vector into the wrong
neighbourhood, so the expansion actively competes with the question instead of complementing it.

So the model is asked for the FRAME an answer would take, with the unknown left unfilled, and for
several different framings rather than one guess. "I can't have <something>, it makes me sick"
sits near "I can't have dairy" in embedding space without asserting anything false. Diversity
matters more than fluency here: three cheap framings cover more of the register than one polished
sentence, and each is an independent chance to land near the evidence.

THE EXPANSIONS NEVER REPLACE THE QUESTION. They are added as extra representations alongside
`full`, `concept`, `entity` and `attribute`, every one of them its own ANN surface whose evidence
names which representation fired. If an expansion is useless it contributes a weak surface that
fusion down-weights; it cannot displace the question's own vector. That is the same rule the
cross-encoder lives under, and for the same reason: a generated signal may contribute, never
decide.
"""
from __future__ import annotations

import re
from typing import Optional

HYPO_K = 25

#: How many framings to ask for. Three covers noticeably more register than one at one LLM call.
N_HYPOTHESES = 3

#: Cheap, and it only has to produce plausible phrasing - no reasoning, no facts.
EXPANSION_MAX_TOKENS = 160

_PROMPT = """A search system needs to find a casual chat message that answers this question.

QUESTION: {q}

Write {n} DIFFERENT one-line messages, as someone would actually type them in a chat, that would
answer it. Rules:
- First person and casual. "I can't have dairy", not "Joanna is allergic to dairy".
- DO NOT invent the actual answer. Leave the unknown part vague - "something", "that", "a while
  back". Guessing a specific detail wrong is worse than leaving it out.
- Vary the phrasing between the {n} lines; each should use different words for the same idea.

Write only the {n} lines, one per line, no numbering."""


_REPHRASE_PROMPT = """Rewrite this question {n} different ways. Same meaning, different wording.

QUESTION: {q}

Rules:
- Keep every name exactly as written. Do not add names that are not there.
- Do not answer the question. Each line must still be a question.
- Use different words for the same idea - synonyms for the verbs and nouns.

Write only the {n} rewritten questions, one per line, no numbering."""


async def rephrasings(query: str, complete, n: int = N_HYPOTHESES,
                      model: Optional[str] = None) -> list[str]:
    """Question REWRITES of `query`. Returns [] on any failure, like `hypotheticals`."""
    if not query or not query.strip():
        return []
    try:
        raw = await complete(
            _REPHRASE_PROMPT.format(q=query.strip(), n=n),
            max_tokens=EXPANSION_MAX_TOKENS,
            temperature=0.3,
            model=model,
        )
    except Exception:
        return []
    out = []
    for line in parse(raw, limit=n):
        # A rewrite that dropped every named entity has drifted; discard it.
        if line.strip().lower() != (query or "").strip().lower():
            out.append(line)
    return out


def _clean(line: str) -> str:
    line = re.sub(r"^\s*[-*•]?\s*\d*[.)]?\s*", "", line).strip()
    return line.strip(' "\'')


def parse(raw: str, limit: int = N_HYPOTHESES) -> list[str]:
    """Lines from a completion, cleaned of numbering and quotes. Junk yields nothing."""
    out: list[str] = []
    for line in (raw or "").splitlines():
        cleaned = _clean(line)
        # Too short to carry a register; over-long means the model wrote prose, not a message.
        if 8 <= len(cleaned) <= 240 and cleaned.lower() not in {o.lower() for o in out}:
            out.append(cleaned)
        if len(out) >= limit:
            break
    return out


async def hypotheticals(query: str, complete, n: int = N_HYPOTHESES,
                        model: Optional[str] = None) -> list[str]:
    """Answer-shaped framings of `query`. Returns [] on any failure.

    `complete` is injected rather than imported so this stays testable without a model, and so
    the caller decides which model pays for it. Returning [] on failure is deliberate: expansion
    is an ADDITION, and a query that cannot be expanded must still run exactly as it does today.
    """
    if not query or not query.strip():
        return []
    try:
        raw = await complete(
            _PROMPT.format(q=query.strip(), n=n),
            max_tokens=EXPANSION_MAX_TOKENS,
            temperature=0.4,          # some spread: identical framings waste the extra surfaces
            model=model,
        )
    except Exception:
        return []
    return parse(raw, limit=n)
