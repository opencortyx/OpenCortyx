"""L0 — Query classification.

Not every question needs every retriever. "What is John's email" wants entity resolution and an
attribute read; running PPR over the graph for it spends latency to add noise. "Which companies
are connected to John through transactions in Germany" is the opposite — lexical recall is nearly
useless and the graph carries the answer.

The previous engine had no classifier. It ran one fixed retriever set per PROFILE, chosen by the
caller before the question was seen, so a lookup and a four-hop reasoning question executed the
identical plan. Two narrow heuristics existed (`is_aggregation`, a temporal cue matcher) and
nothing else.

WHAT THIS CLASSIFIES ON, AND WHAT IT DELIBERATELY DOES NOT

It reads the QUESTION — its interrogative, its verbs, its quantifiers, its comparatives. Those are
properties of language and stable across corpora.

It does NOT consult a table of node types or edge names. A classifier that decides "this is a
relationship query" by matching the literal string `TRANSACTED_WITH` only works on a graph that
happens to use that name, and silently misfires on every other schema. Which relations matter is
established later, in L2, by scoring edges against the query — computed per corpus, never
declared.

Confidence is returned alongside the label. A weak classification widens the plan rather than
narrowing it: the cost of running an extra surface is latency, while the cost of skipping the one
surface that had the answer is an unanswerable question.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from opencortyx.retrieval.contracts import Cardinality, Intent
from opencortyx.retrieval.l0_understanding.constraints import (
    has_date_range,
)

# ── linguistic signals ───────────────────────────────────────────────────────────────────────
# Patterns describe QUESTION SHAPE. None of them name a node type, an edge type or a field.

_AGGREGATION = re.compile(
    r"\b(how many|how much|number of|count of|total|sum of|average|mean|median|"
    r"most|least|fewest|largest|smallest|top \d+|per (each|every)?)\b|"
    r"^\s*count\s+(the|all|every|each)\b", re.I)

_TEMPORAL_CUE = re.compile(
    r"\b(when|since|until|before|after|during|between|earlier|later|recent(ly)?|"
    r"ago|changed|change[ds]?|updated|previous(ly)?|first|last|latest|"
    r"\b(19|20)\d{2}\b|"
    r"(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|jun(e)?|jul(y)?|aug(ust)?|"
    r"sep(tember)?|oct(ober)?|nov(ember)?|dec(ember)?))\b", re.I)

#: Relational language: the question is about a CONNECTION between things, not a thing.
_RELATIONAL = re.compile(
    r"\b(connect(ed|ion)?s?|relat(ed|ionship)s?|link(ed|s)?|associat(ed|ion)s?|"
    r"between|through|via|with whom|who (works?|worked|deals?|dealt|transact\w*)|"
    r"shared|in common|both|together|supplier?s?|counterpart(y|ies)|involv(ed|ing))\b", re.I)

_WHO_FRAME = re.compile(r"^\s*who\s+(did|does|do|will|would|has|have|had)\b", re.I)

#: Chained relational language implies more than one hop.
_MULTI_HOP = re.compile(
    r"\b(through|via|indirect\w*|chain|path|route|"
    r"connected to .+ (through|via|by)|"
    r"(who|what|which) .+ (that|who|which) .+ (that|who|which))\b", re.I)

_EXACT_LOOKUP = re.compile(
    r"^\s*(what|which)\s+(is|are|was|were)\s+(the\s+)?"
    r"(\w+\s+){0,3}(of|for)\b", re.I)

#: An identifier-shaped token: codes, ids, references. Strong evidence of an exact lookup.
_IDENTIFIER = re.compile(r"\b([A-Z]{2,}[-_]?\d{2,}[-\w]*|\b\d{6,}\b)")

#: Asking to be TOLD ABOUT something can only be a profile request. Unambiguous.
_ENTITY_PROFILE_STRONG = re.compile(
    r"^\s*(tell me about|describe|profile of|"
    r"information (on|about)|details? (on|about|of))\b", re.I)

_ENTITY_PROFILE_WEAK = re.compile(
    r"^\s*(who|what)\s+(is|was|are|were)\b", re.I)

_KEYWORD_DOC = re.compile(
    r"\b(document|documents|report|reports|file|files|paper|papers|article|"
    r"mention(ing|s|ed)?|contain(ing|s)?|reference[sd]?|cite[sd]?)\b", re.I)

_SET_ANSWER = re.compile(
    r"\b(which|what) \w*\s*(companies|people|persons|customers|items|things|"
    r"records|documents|entities|accounts|activities|hobbies|interests|"
    r"types|kinds|ways|places|topics)\b|"
    r"\b(list|all|every|each|both|any other|besides|other than|apart from)\b", re.I)


@dataclass
class Classification:
    intent: Intent
    cardinality: Cardinality
    confidence: float
    signals: list[str]

    def __repr__(self) -> str:                                # pragma: no cover - debug aid
        return (f"<{self.intent.value} card={self.cardinality.value} "
                f"conf={self.confidence:.2f} {self.signals}>")


_PLURAL_HEAD = re.compile(r"^\s*(?:what|which)\s+((?:[a-z]+\s+){0,3})", re.I)

#: Endings that look plural but are not: business, status, analysis, series, bonus.
_NOT_PLURAL_SUFFIX = ("ss", "us", "is", "as", "os")

#: Auxiliaries and pronouns that end in s but name nothing.
_NOT_A_NOUN = {"does", "was", "is", "has", "goes", "says", "means", "gets", "his", "hers",
               "its", "this", "thus", "plus", "plans"}


def _asks_for_plural(q: str) -> bool:
    """Does the noun phrase after what/which contain a plural?

    Scans the phrase rather than its first word: "what BOOK RECOMMENDATIONS has Joanna given"
    is a set question whose head noun is the second word, and stopping at the first returns
    "book" and calls it singular.
    """
    m = _PLURAL_HEAD.match(q or "")
    if not m:
        return False
    for w in (m.group(1) or "").lower().split():
        if len(w) < 4 or w in _NOT_A_NOUN:
            continue
        if w.endswith("s") and not w.endswith(_NOT_PLURAL_SUFFIX):
            return True
    return False

def _card(q: str) -> Cardinality:
    if _AGGREGATION.search(q):
        return Cardinality.COUNT
    if _SET_ANSWER.search(q) or _asks_for_plural(q):
        return Cardinality.SET
    if re.match(r"^\s*(is|are|was|were|does|do|did|has|have|had|can|could|would|will)\b", q, re.I):
        return Cardinality.ONE
    if re.match(r"^\s*(who|what|which|where|when)\s+(is|was|are|were)\b", q, re.I):
        return Cardinality.ONE
    return Cardinality.UNKNOWN


def classify(query: str) -> Classification:
    """Label the question. Ties widen the plan rather than resolving arbitrarily."""
    q = (query or "").strip()
    if not q:
        return Classification(Intent.SEMANTIC, Cardinality.UNKNOWN, 0.0, ["empty"])

    signals: list[str] = []
    scores: dict[Intent, float] = {}

    def bump(i: Intent, w: float, sig: str):
        scores[i] = scores.get(i, 0.0) + w
        signals.append(sig)

    if _AGGREGATION.search(q):
        bump(Intent.AGGREGATION, 0.9, "aggregation-language")
    if _IDENTIFIER.search(q):
        bump(Intent.EXACT_LOOKUP, 0.8, "identifier-token")

    attribute_frame = bool(_EXACT_LOOKUP.match(q))
    if attribute_frame:
        bump(Intent.EXACT_LOOKUP, 0.8, "attribute-of-phrasing")
    if _ENTITY_PROFILE_STRONG.match(q):
        bump(Intent.ENTITY, 0.7, "profile-phrasing")
    elif _ENTITY_PROFILE_WEAK.match(q) and not attribute_frame:
        bump(Intent.ENTITY, 0.45, "bare-what-is-phrasing")

    if _KEYWORD_DOC.search(q):
        bump(Intent.KEYWORD, 0.5, "document-language")

    # "between 2023 and 2025" is a time bound; "between John and Acme" is a relation. The word is
    # identical, so the date range is checked before the relational reading is allowed.
    rel = _RELATIONAL.search(q)
    if rel and not (rel.group(0).strip().lower() == "between" and has_date_range(q)):
        bump(Intent.RELATIONSHIP, 0.6, "relational-language")
    elif _WHO_FRAME.match(q):
        bump(Intent.RELATIONSHIP, 0.55, "who-did-frame")
    if _MULTI_HOP.search(q):
        bump(Intent.MULTI_HOP, 0.8, "chained-relation")
    if _TEMPORAL_CUE.search(q):
        # Temporal is usually a CONSTRAINT on another intent rather than the intent itself, so it
        # scores lower than a relation and only wins when nothing else fires.
        bump(Intent.TEMPORAL, 0.55, "temporal-cue")

    if not scores:
        return Classification(Intent.SEMANTIC, _card(q), 0.35, ["no-signal-default-semantic"])

    intent = max(scores.items(), key=lambda kv: kv[1])[0]
    top = scores[intent]
    runner = sorted(scores.values(), reverse=True)
    margin = top - (runner[1] if len(runner) > 1 else 0.0)
    # Confidence blends strength with separation: two intents tied at 0.8 is a weak call even
    # though the winner scored high.
    confidence = max(0.0, min(1.0, 0.5 * min(top, 1.0) + 0.5 * min(margin, 1.0)))
    return Classification(intent, _card(q), confidence, signals)


def is_temporal(query: str) -> bool:
    """Whether time constrains the answer, independent of the winning intent."""
    return bool(_TEMPORAL_CUE.search(query or ""))


def is_relational(query: str) -> bool:
    q = query or ""
    return bool(_RELATIONAL.search(q) or _MULTI_HOP.search(q) or _WHO_FRAME.match(q))
