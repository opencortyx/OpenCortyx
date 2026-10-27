"""Sub-questions: what a decomposition is, which questions get one, and the split that needs no model.

A decomposition is an ordered list of sub-questions. A sub-question that needs an earlier one's
answer names it `#1`, `#2`: "Who wrote #1?" is asked only once #1 has been answered, with the
answer written in. Sub-questions with no `#n` are independent and run side by side.

    "Who wrote the RFC that was made obsolete by RFC 9260?"
        1. Which RFC was made obsolete by RFC 9260?
        2. Who wrote #1?                       <- asked as "Who wrote RFC 4460?"

    "Which was published first, RFC 9155 or RFC 9410?"
        1. Which was published first, RFC 9155?
        2. Which was published first, RFC 9410?   <- both at once, no model needed

WHICH QUESTIONS. Decomposing costs a model call and a retrieval per step, so it runs only where a
single search is known to fall short: a question that reaches one thing THROUGH another (a
relative clause naming a second subject: "the PEP that PEP 3124 replaces"), and a comparison of
two or more named subjects, where one search tends to fill the context with whichever subject
ranks higher and leave out the other. A plain lookup never pays for it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: More steps than this is a model rambling, not a plan: no question this engine serves needs a
#: chain of five lookups, and each step is a full retrieval.
MAX_STEPS = 4

_REF = re.compile(r"#(\d)\b")


@dataclass
class SubQuestion:
    index: int                                  # 1-based, as the model writes it
    text: str                                   # as planned, with #n placeholders
    depends_on: list[int] = field(default_factory=list)
    asked: str = ""                             # as run: placeholders replaced by answers
    answer: str = ""                            # the bridge answer read from its context

    def resolve(self, answers: dict[int, str]) -> str:
        return _REF.sub(lambda m: answers.get(int(m.group(1)), m.group(0)), self.text)


@dataclass
class Decomposition:
    question: str
    steps: list[SubQuestion]
    source: str                                 # "rules" | "model"

    @property
    def chained(self) -> bool:
        """Whether any step waits on another's answer - the case that needs the model again."""
        return any(s.depends_on for s in self.steps)


# ── which questions ───────────────────────────────────────────────────────────────────────────

#: A thing reached through another: "the PEP that PEP 3124 replaces", "the RFC that was made
#: obsolete by RFC 9260", "the author of the book which ...".
_THROUGH = re.compile(
    r"\bthe\s+(?:\w+\s+){1,3}?(?:that|which|who|whom|whose)\s+\w+", re.I)

#: A second subject named only by its relation to the first: "depends on another PEP. What is the
#: status of that other PEP", "replaced an older RFC".
_OTHER = re.compile(
    r"\b(?:another|the other|that other|an?\s+(?:older|newer|earlier|later|previous|prior)|"
    r"its\s+(?:predecessor|successor|parent|replacement))\b", re.I)

_COMPARE_WORD = re.compile(
    r"\b(?:first|earlier|later|older|newer|more|fewer|less|larger|smaller|bigger|higher|lower|"
    r"longer|shorter|better|worse|faster|slower|cheaper)\b", re.I)
_COMPARE_FRAME = re.compile(r"\b(?:compare|compared|versus|vs\.?|difference between)\b", re.I)

#: A named subject a comparison can be split on: an identifier-shaped name ("RFC 9155", "PEP 8",
#: "ISO 27001", "INV-2041") or a quoted title.
_SUBJECT = re.compile(r"\b[A-Z][A-Za-z]{1,12}[ -]?\d[\w.-]*\b|\"[^\"]{2,80}\"")

_JOIN = r"(?:or|and|vs\.?|versus)"

#: Two subjects joined directly: "RFC 9155 or RFC 9410", '"Q1 plan" and "Q2 plan"'. A join word
#: anywhere else ("PEP Purpose and Guidelines") joins words, not the things compared.
_PAIR = re.compile(r"(?:%s)\s*,?\s*(?i:%s)\s+(?:%s)" % (_SUBJECT.pattern, _JOIN, _SUBJECT.pattern))


def subjects(question: str) -> list[str]:
    out: list[str] = []
    for m in _SUBJECT.finditer(question or ""):
        s = m.group(0).rstrip(".")
        if s not in out:
            out.append(s)
    return out


def is_comparison(question: str) -> bool:
    q = question or ""
    if len(subjects(q)) < 2:
        return False
    return bool(_COMPARE_FRAME.search(q) or (_PAIR.search(q) and _COMPARE_WORD.search(q)))


def is_compound(question: str) -> bool:
    """Whether a question is worth decomposing. Cheap and textual: it runs on every question."""
    q = question or ""
    return bool(is_comparison(q) or _OTHER.search(q) or _THROUGH.search(q))


# ── the split that needs no model ─────────────────────────────────────────────────────────────

def split_comparison(question: str) -> Decomposition | None:
    """One sub-question per compared subject: the question with the OTHER subjects taken out, along
    with the "or"/"and" that joined them. Independent steps, so they run at once and no answer has
    to be read in between."""
    if not is_comparison(question):
        return None
    names = subjects(question)[:MAX_STEPS]
    steps = []
    for i, keep in enumerate(names, start=1):
        text = question
        for other in names:
            if other == keep:
                continue
            o = re.escape(other)
            text = re.sub(r",?\s*\b%s\s+%s|%s\s*,?\s*%s\b\s*" % (_JOIN, o, o, _JOIN), "", text,
                          count=1, flags=re.I)
            text = re.sub(r"\s*,?\s*%s\b" % o, "", text, count=1)
        text = re.sub(r"\s+([?.,])", r"\1", re.sub(r"\s{2,}", " ", text)).strip()
        steps.append(SubQuestion(index=i, text=text))
    if len({s.text for s in steps}) < 2:
        return None
    return Decomposition(question=question, steps=steps, source="rules")


# ── reading a model's plan ────────────────────────────────────────────────────────────────────

_LINE = re.compile(r"^\s*(?:step\s*)?(\d)\s*[.):-]\s*(.+?)\s*$", re.I)

#: Words that start a question or a sentence; capitalised without being names.
_NOT_NAMES = {"what", "which", "who", "whom", "whose", "when", "where", "why", "how", "is", "are",
              "was", "were", "does", "do", "did", "the", "a", "an", "in", "of", "i", "and", "or",
              "find", "give", "list", "name"}


def _names(text: str) -> set[str]:
    """Numbers and capitalised words: what a step must not invent."""
    words = re.findall(r"\b(?:[A-Z][\w-]*|\d[\w.-]*)\b", _REF.sub(" ", text))
    return {w.lower().rstrip(".") for w in words if w.lower() not in _NOT_NAMES}


def parse_steps(question: str, text: str) -> Decomposition | None:
    """The model's numbered lines, or None when they are not a usable plan.

    Not usable: fewer than two steps, more than MAX_STEPS, a `#n` that points at itself or forward,
    every step a copy of the question, or a step naming something the question does not. That last
    one is the failure a small model makes most - asked to split "the PEP that PEP 3124 replaces"
    it writes "Which PEP does PEP 246 replace?", and a step that invents the answer it was meant to
    find sends the next search after the wrong document."""
    steps: list[SubQuestion] = []
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        idx, body = int(m.group(1)), m.group(2).strip().strip('"')
        if idx != len(steps) + 1 or len(body.split()) < 3:
            return None
        deps = sorted({int(r) for r in _REF.findall(body)})
        if any(d >= idx or d < 1 for d in deps):
            return None
        steps.append(SubQuestion(index=idx, text=body, depends_on=deps))
    if not 2 <= len(steps) <= MAX_STEPS:
        return None
    q = question.strip().lower().rstrip("?. ")
    if all(s.text.lower().rstrip("?. ") == q for s in steps):
        return None
    known = question.lower()
    for s in steps:
        if any(n not in known for n in _names(s.text)):
            return None
    return Decomposition(question=question, steps=steps, source="model")
