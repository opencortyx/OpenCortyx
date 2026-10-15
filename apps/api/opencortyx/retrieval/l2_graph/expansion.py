"""L2 — What the QUESTION says about which nodes are worth walking to.

    plan ──► ExpansionContext ──┬─► accepts(node)   temporal rejection DURING traversal
                                └─► affinity(node)  node- and attribute-awareness

WHY EXPANSION NEEDED MORE THAN EDGE SCORES

Beam search scored a route as the product of its EDGE relevances and nothing else. Two
consequences follow directly from that, and both are visible on any real query:

  1. THE NODE YOU LAND ON CONTRIBUTED NOTHING. A hop to the statement that answers the question
     and a hop to an unrelated node scored identically, provided both were reached over the same
     relation. Every `MENTIONS` edge out of a seed was equally good, so the beam's pruning — the
     step that is supposed to trade breadth for relevance — was choosing between routes it could
     not tell apart.

  2. TIME WAS PARSED AND THEN IGNORED. `plan.temporal` was extracted at L0, filtered in
     `MetadataSurface`, and scored as a feature at L3 — but traversal walked straight through
     out-of-range nodes and spent the fanout budget on them. "What happened to Acme after 2023"
     expanded into 2021 material and then hoped ranking would sort it out.

WHAT THIS IS NOT

It is not a node-type table and not an attribute schema. Everything here is computed against the
QUESTION'S OWN WORDS and the node's own payload: which of the question's terms the node repeats,
whether it carries a field the question asked for, whether its date falls inside a bound the
question stated. A corpus with different types and different fields is handled by the same code
because none of those names appear in it.

TWO RULES THAT KEEP THIS FROM BECOMING A FILTER

  - AN UNKNOWN DATE PASSES. Rejecting a node because its date could not be parsed would delete
    evidence on a technicality. The filter removes what is known to be out of range, never what
    we failed to read.
  - AFFINITY IS A MODULATION, NEVER A VETO. It is bounded below by `NODE_FLOOR`, so a node that
    shares no wording with the question is dampened and still reachable. The answer to "what is
    she allergic to" may be "reptiles and anything with fur", which repeats none of the
    question's content words — a hard gate on lexical overlap would delete exactly that.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from opencortyx.retrieval.contracts import QueryPlan
from opencortyx.retrieval.l0_understanding.constraints import (
    to_iso, within,
)
from opencortyx.retrieval.common.text_norm import singularize, stem

#: Floor on the node term of a path score. A node the question does not resemble is DAMPENED,
#: never excluded — see the module docstring for why a gate here would be wrong.
NODE_FLOOR = 0.55

_DATE_KEYS = ("date_time", "date", "timestamp", "occurred_at", "created_at", "started_at",
              "event_date", "when")

_WORD = re.compile(r"[A-Za-z0-9']+")

#: Question words that appear in every question and describe none of them.
_STOP = frozenset({
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how", "is", "are", "was",
    "were", "do", "does", "did", "the", "a", "an", "and", "or", "of", "for", "to", "from", "in",
    "on", "at", "by", "with", "about", "that", "this", "these", "those", "it", "its", "be",
    "been", "have", "has", "had", "would", "could", "should", "will", "can", "may", "might",
    "there", "their", "them", "they", "he", "she", "his", "her", "you", "your", "we", "our",
})


def content_terms(text: str) -> set[str]:
    """The words of a question that describe what it is about, as comparison stems."""
    out = set()
    for w in _WORD.findall((text or "").lower()):
        if w in _STOP or len(w) < 2:
            continue
        out.add(stem(w))
    return out


def node_date(properties: Optional[dict]) -> Optional[str]:
    """A node's own timestamp as ISO, from whichever key carries it. None when unreadable."""
    if not properties:
        return None
    for key in _DATE_KEYS:
        raw = properties.get(key)
        if isinstance(raw, str) and raw.strip():
            iso = to_iso(raw)
            if iso:
                return iso
    return None


@dataclass
class ExpansionContext:
    """The question's demands on a node, in the form traversal can apply per hop.

    Built once per query and passed into the beam, so the per-node checks are pure string and
    date comparisons with no further I/O — expansion is already the latency-sensitive layer.
    """
    terms: frozenset = frozenset()
    temporal: tuple = ()
    attribute_targets: frozenset = frozenset()
    #: Counters, so a trace can say WHY a hop did not happen. A rejection nobody can see is
    #: indistinguishable from a node that was never reachable.
    rejected_temporal: int = field(default=0, compare=False)

    @classmethod
    def from_plan(cls, plan: Optional[QueryPlan]) -> "ExpansionContext":
        if plan is None:
            return cls()
        targets = set()
        if plan.answer_type_hint:
            targets.add(stem(plan.answer_type_hint))
        return cls(
            terms=frozenset(content_terms(plan.normalized_query or plan.raw_query)),
            temporal=tuple(plan.temporal or ()),
            attribute_targets=frozenset(t for t in targets if t),
        )

    @property
    def active(self) -> bool:
        """Whether the question said anything that could shape expansion at all."""
        return bool(self.terms or self.temporal or self.attribute_targets)

    # ── temporal awareness ───────────────────────────────────────────────────────────────────

    def accepts(self, properties: Optional[dict]) -> bool:
        """Is this node inside every time bound the question stated?

        An unparseable or absent date PASSES. The constraint exists to remove what is known to be
        out of range, not what we failed to read — the opposite policy silently deletes evidence
        whenever a corpus spells its dates unusually.
        """
        if not self.temporal:
            return True
        iso = node_date(properties)
        if not iso:
            return True
        for constraint in self.temporal:
            if not within(constraint, iso):
                return False
        return True

    # ── node and attribute awareness ─────────────────────────────────────────────────────────

    def affinity(self, label: str, properties: Optional[dict]) -> float:
        """How much this node itself answers to the question, in [0, 1].

        Two independent signals, taken at their maximum rather than added, so a node does not
        become confident merely by scoring weakly on both:

          NODE — how many of the question's content words the node's own text repeats.
          ATTRIBUTE — whether the node CARRIES the field the question asked for. "What are
            Joanna's hobbies" should prefer walking to a node with a `hobbies` field over one
            without, and that is a fact about the node's payload, not about its type.
        """
        if not self.active:
            return 1.0

        node_hit = 0.0
        if self.terms:
            text = " ".join(filter(None, [
                label or "",
                str((properties or {}).get("text") or ""),
                str((properties or {}).get("__context") or ""),
            ]))
            if text.strip():
                node_terms = content_terms(text)
                if node_terms:
                    overlap = len(self.terms & node_terms)
                    # Normalised by the QUESTION's length: a long node does not become relevant
                    # by containing many words, only by containing the question's words.
                    node_hit = min(1.0, overlap / max(len(self.terms), 1))

        attr_hit = 0.0
        if self.attribute_targets and properties:
            for key in properties:
                if not isinstance(key, str):
                    continue
                k = stem(singularize(key.lower()))
                if k and k in self.attribute_targets:
                    attr_hit = 1.0
                    break

        return max(node_hit, attr_hit)
