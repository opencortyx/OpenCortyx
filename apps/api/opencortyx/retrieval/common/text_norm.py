"""Text primitives shared by every layer that has to compare a NAME against a question.

These live at the package root rather than inside a layer because both ends of the workflow need
them and neither owns them: L0 scores the corpus's type vocabulary against the question, L2 scores
its relation vocabulary against the query embedding, and both must agree on what
`HAS_SKILL` "reads as" before either can compare it to anything.

Everything here is deliberately shallow. These functions exist to let a NAME be compared as TEXT
so that no layer ever needs a lookup table of known types — the rule the whole engine is built
around. A crude stemmer that fails to match an irregular form contributes no evidence, which
merely loses a boost. An aggressive one that matches unrelated words invents evidence, which
corrupts a ranking. The failure directions are not symmetric, so every rule below errs shallow.
"""
from __future__ import annotations

import re

NAME_STOPWORDS = frozenset({
    "has", "have", "had", "is", "are", "was", "were", "be", "been", "of", "on", "in", "at", "to",
    "for", "from", "with", "by", "the", "a", "an", "and", "or", "as", "its", "their",
})


def humanize(name: str) -> str:
    """`EXTRACTED_FROM` -> `extracted from`, `hasAuthor` -> `has author`, `KeyNode` -> `key node`.

    Type and relation names are written for machines. Without this an encoder sees one
    out-of-vocabulary token instead of a phrase, and a lexical comparison against the question
    sees a string that appears nowhere in English.
    """
    r = (name or "").strip()
    if not r:
        return ""
    r = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", r)        # camelCase / PascalCase
    r = re.sub(r"[_\-]+", " ", r)                         # SNAKE_CASE, kebab-case
    return re.sub(r"\s+", " ", r).strip().lower()


def singularize(word: str) -> str:
    """Crude English singular. Enough to match "companies" to "Company" without a lexicon.

    `rstrip("s")` is not enough — it turns "companies" into "companie", which matches nothing.
    Deliberately shallow: an irregular plural simply fails to match and contributes no bonus,
    which is the safe direction. Over-stemming would create false matches between unrelated types.
    """
    w = (word or "").strip().lower()
    if len(w) > 3 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if len(w) > 2 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def stem(word: str) -> str:
    """A morphological key: `allergic`, `allergy` and `allergies` all reduce to `allerg`.

    Singularisation alone cannot connect a question's adjective to a relation's noun, and that
    gap is not hypothetical — the corpus stores `HAS_ALLERGY` while questions ask "what is X
    allergic to". Only a handful of suffixes are stripped, and only from words long enough that
    stripping cannot collapse them onto an unrelated short word.

    The result is a comparison KEY, not a word. It is never shown and never searched for.
    """
    w = singularize(word)
    if len(w) < 5:
        return w
    for suf in ("ically", "ional", "ically", "ation", "ities", "ity", "ical", "ing", "ies",
                "ive", "ed", "al", "ic", "y"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def tokens(text: str) -> list[str]:
    """Word tokens of a phrase, lowercased. Punctuation and digits-only tokens are dropped."""
    return [t for t in re.findall(r"[A-Za-z][A-Za-z0-9']*", (text or "").lower()) if t]


def content_tokens(name: str) -> list[str]:
    """The tokens of a type name that actually identify it, with the connectives removed.

    `HAS_SKILL` -> `["skill"]`. A name that is ENTIRELY connectives (`HAS`, `OF`) keeps its
    tokens rather than becoming empty, because a name with no content tokens would otherwise
    match every question vacuously.
    """
    toks = tokens(humanize(name))
    content = [t for t in toks if t not in NAME_STOPWORDS]
    return content or toks
