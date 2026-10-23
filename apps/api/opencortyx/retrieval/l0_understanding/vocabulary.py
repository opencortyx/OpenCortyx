"""L0 — Node-type and edge-type hypotheses, COMPUTED against the corpus's own vocabulary.

    corpus ─discover─► the types that actually exist, with their counts
                                    │
    question ─its own words─────────┼──► lexical evidence ──► scored hypotheses (or NOTHING)
                                    │
                          rarity (IDF) ─┘  tie-break only

WHY THIS IS NOT A TYPE TABLE

The plan needs to be able to say "this question is about documents" or "this question is about a
skill relation". The obvious way to do that is a map from question words to type names. That map
is precisely the construct this engine exists to eliminate: `_TYPE_ORDER = {"Turn": 1, ...}` with
an implicit default demoted every type nobody had thought to list, and an entire `Fact` layer
became invisible to retrieval without a single error being raised.

So the vocabulary is DISCOVERED — `select type, count(*) group by type` — and scored as TEXT
against the words the question actually used. A type introduced by tomorrow's ingest is scoreable
the moment it exists, with no code change. That is exactly the property a table destroys.

THE SAFETY PROPERTY: NO EVIDENCE MEANS NO OPINION

A hypothesis is emitted only when the question's own words support it. If nothing in the question
resembles any type name, the result is EMPTY — not a ranked list of the corpus's most common
types.

That distinction is not fastidiousness, it is a measured failure. At L2, rank-normalising 64
near-identical relation cosines manufactured scores of 0.97/0.95/0.94 out of noise, and beam
search then walked the top of what was effectively an arbitrary ordering, discovering nothing on
two of three queries. Producing a confident ranking from an absent signal is worse than producing
nothing, because nothing is visibly nothing.

Consequently these scores are BOOSTS, never filters. `plan.node_types` is a set of things the
question gave us reason to prefer; a type missing from it has not been excluded, it has merely
not been argued for. `QueryPlan.type_prior()` enforces that reading by returning a neutral value
for everything it was told nothing about.
"""
from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


from opencortyx.core.substrate import NodeRepo
from opencortyx.retrieval.common.text_norm import (
    content_tokens, humanize, singularize, stem, tokens,
)

MIN_TYPE_EVIDENCE = 0.25

W_RARITY = 0.15

#: How much of a name's content the question accounts for. Matching one token of three is weak
#: and scales down accordingly.
W_COVERAGE = 0.85

W_HEAD = 1.0

#: How long a workspace's vocabulary is used before it is refreshed. Reading it counts every node and
#: every edge by type, which on a thousand-file workspace took 5-13 s and a few hundred MB of the
#: graph service each time; at a minute, one question in fifteen paid that. The counts only weight how
#: rare a type is, so a vocabulary minutes old answers as well, and a stale one is refreshed in the
#: background rather than by the question that finds it stale. New types are seen within this time.
_CACHE_TTL_SECONDS = 900.0

#: Interrogative frames that introduce the answer's own noun.
_HEAD_FRAMES = (
    re.compile(r"\b(?:which|what)\s+(?:of\s+the\s+)?([a-z][\w\-]*)", re.I),
    re.compile(r"\b(?:list|show|find|name)\s+(?:me\s+)?(?:all\s+|the\s+|every\s+)?"
               r"([a-z][\w\-]*)", re.I),
    re.compile(r"\bhow\s+many\s+([a-z][\w\-]*)", re.I),
    re.compile(r"\ball\s+(?:the\s+)?([a-z][\w\-]*)", re.I),
)

#: Head-frame nouns that are grammar rather than subject matter.
_HEAD_NOISE = frozenset({
    "is", "are", "was", "were", "do", "does", "did", "the", "a", "an", "of", "other", "others",
    "kind", "kinds", "type", "types", "sort", "sorts", "one", "ones", "thing", "things", "else",
    "it", "they", "them", "he", "she", "you", "we", "time", "times", "way", "ways", "many",
    "much", "about", "that", "this", "these", "those", "happened", "did",
})


@dataclass
class TypeProfile:
    """What the graph knows about one type name, independent of any question."""
    name: str
    count: int
    rarity: float = 0.0                  # 0..1, higher = more discriminating
    text: str = ""                       # humanized form, e.g. HAS_SKILL -> "has skill"


@dataclass
class TypeVocabulary:
    """The types this corpus actually contains. Discovered, never declared.

    An empty vocabulary is a valid, safe state: the planner then emits no type hypotheses and
    behaves exactly as it did before this module existed.
    """
    node_types: dict[str, TypeProfile] = field(default_factory=dict)
    edge_types: dict[str, TypeProfile] = field(default_factory=dict)
    workspace_id: Optional[str] = None
    loaded_at: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.node_types or self.edge_types)

    def summary(self) -> str:
        return f"{len(self.node_types)} node types, {len(self.edge_types)} edge types"


def _profiles(rows) -> dict[str, TypeProfile]:
    """Counts to profiles, with an IDF-shaped rarity. A type covering the whole graph carries no
    information about which part of it answers a question."""
    out: dict[str, TypeProfile] = {}
    total = sum(int(c) for _, c in rows) or 1
    for name, count in rows:
        if not name:
            continue
        c = int(count)
        rarity = math.log(1.0 + total / max(c, 1)) / math.log(1.0 + total)
        out[name] = TypeProfile(name=name, count=c, rarity=max(0.0, min(1.0, rarity)),
                                text=humanize(name))
    return out


_vocab_cache: dict[str, TypeVocabulary] = {}
#: Workspaces whose vocabulary is being refreshed in the background (one refresh at a time each).
_refreshing: set[str] = set()


async def _read_vocabulary(workspace_id: uuid.UUID) -> TypeVocabulary:
    repo = NodeRepo()
    node_rows = list((await repo.count_by_type(workspace_id)).items())
    edge_rows = list((await repo.count_edges_by_type(workspace_id)).items())
    return TypeVocabulary(node_types=_profiles(node_rows), edge_types=_profiles(edge_rows),
                          workspace_id=str(workspace_id), loaded_at=time.monotonic())


async def _refresh(workspace_id: uuid.UUID, key: str) -> None:
    try:
        _vocab_cache[key] = await _read_vocabulary(workspace_id)
    except Exception:                                   # noqa: BLE001 — the stale one keeps serving
        pass
    finally:
        _refreshing.discard(key)


async def load_vocabulary(workspace_id: uuid.UUID, *, use_cache: bool = True) -> TypeVocabulary:
    """The corpus's node and edge type names with counts. The only I/O in L0, so `build_plan`
    stays pure and testable without a store.

    Read once per workspace and process; afterwards a vocabulary older than the TTL is still
    returned while one background task reads a fresh one (stale-while-revalidate)."""
    key = str(workspace_id)
    if use_cache:
        hit = _vocab_cache.get(key)
        if hit is not None:
            if time.monotonic() - hit.loaded_at >= _CACHE_TTL_SECONDS and key not in _refreshing:
                import asyncio
                _refreshing.add(key)
                asyncio.get_running_loop().create_task(_refresh(workspace_id, key))
            return hit
    vocab = await _read_vocabulary(workspace_id)
    if use_cache:
        _vocab_cache[key] = vocab
    return vocab


def clear_vocabulary_cache() -> None:
    """Drop the per-org cache. For tests, and for a forced reload after an ingest."""
    _vocab_cache.clear()
    _refreshing.clear()


# ── question side ────────────────────────────────────────────────────────────────────────────

@dataclass
class _QuestionIndex:
    """The question's words in every form a type name might be written in."""
    surface: set[str]
    singular: set[str]
    stems: set[str]
    phrase: str                          # whole question, lowercased, for contiguous matching
    heads: set[str]                      # stems of the answer's own head noun(s)

    def matches(self, token: str) -> bool:
        if token in self.surface:
            return True
        if singularize(token) in self.singular:
            return True
        s = stem(token)
        return bool(s) and s in self.stems


def answer_head_nouns(query: str) -> list[str]:
    """The noun the question asks to be handed: "which DOCUMENTS mention X" -> `documents`.

    This is about the answer's TYPE, which is why it is separate from
    `entities.detect_attribute_targets` — that finds the FIELD being read off an entity
    ("John's email"), a different question about a different part of the graph.
    """
    q = query or ""
    out: list[str] = []
    for pat in _HEAD_FRAMES:
        for m in pat.finditer(q):
            w = m.group(1).lower().strip()
            if w and w not in _HEAD_NOISE and len(w) > 2:
                out.append(w)
    seen, uniq = set(), []
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq


def _index(query: str) -> _QuestionIndex:
    toks = tokens(query)
    return _QuestionIndex(
        surface=set(toks),
        singular={singularize(t) for t in toks},
        stems={stem(t) for t in toks if stem(t)},
        phrase=" " + " ".join(toks) + " ",
        heads={stem(h) for h in answer_head_nouns(query) if stem(h)},
    )


def score_type(profile: TypeProfile, qi: _QuestionIndex, *, head_applies: bool = False,
               ) -> tuple[float, str]:
    """How much the question's own words argue for this type name. Returns `(score, why)`.

    Three independent signals, taken at their MAXIMUM rather than summed. A name that appears
    verbatim in the question is not made more certain by also overlapping token-wise, and adding
    the two would let an agreeable-looking name outrank genuinely stronger evidence.
    """
    human = profile.text or humanize(profile.name)
    if not human:
        return 0.0, ""

    why: list[str] = []
    best = 0.0

    # 1. The whole name, contiguous, in the question: "created on", "next turn", "has skill".
    if f" {human} " in qi.phrase:
        best = 1.0
        why.append("phrase")

    content = content_tokens(profile.name)
    if content:
        hit = [t for t in content if qi.matches(t)]
        if hit:
            coverage = len(hit) / len(content)
            best = max(best, W_COVERAGE * coverage)
            why.append(f"tokens={'+'.join(hit)} {len(hit)}/{len(content)}")

    # 3. The answer's head noun. Node types only.
    if head_applies and qi.heads:
        for t in content:
            if stem(t) in qi.heads:
                best = max(best, W_HEAD)
                why.append(f"head={t}")
                break

    if best <= 0.0:
        return 0.0, ""

    # Rarity separates equally-supported types. Added rather than multiplied, and capped low
    # enough that it can never lift an unsupported type over MIN_TYPE_EVIDENCE by itself.
    score = min(1.0, best + W_RARITY * profile.rarity)
    return round(score, 4), f"{','.join(why)} rarity={profile.rarity:.2f} n={profile.count}"


def match_type(noun: str, profiles: Optional[dict], *,
               min_score: float = MIN_TYPE_EVIDENCE) -> Optional[tuple[str, float]]:
    """The corpus type a single word names, if any: `"turns"` -> `("Turn", 0.9)`.

    Used where the question has already told us a word is a type — "how many TURNS per SESSION"
    — and only the binding to the corpus's spelling is missing. Same rule as everywhere else:
    matched as text against what the graph actually holds, so `None` means this corpus has no
    such type rather than that the word is unknown to some list.
    """
    if not noun or not profiles:
        return None
    qi = _index(noun)
    best: Optional[tuple[str, float]] = None
    for name, prof in profiles.items():
        s, _ = score_type(prof, qi, head_applies=False)
        if s >= min_score and (best is None or s > best[1]):
            best = (name, s)
    return best


def score_vocabulary(query: str, vocab: Optional[TypeVocabulary],
                     ) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """Score every discovered type against the question.

    Returns `(node_type_scores, edge_type_scores, notes)`, each ordered strongest first. Types
    the question said nothing about are ABSENT, not zero — absence is how "no opinion" is
    expressed, and a caller that read a missing key as a zero score would silently turn a boost
    into a penalty, which is the original `_TYPE_ORDER` bug wearing different clothes.
    """
    if not vocab:
        return {}, {}, []

    qi = _index(query)
    notes: list[str] = []
    nodes: dict[str, float] = {}
    edges: dict[str, float] = {}

    for name, prof in (vocab.node_types or {}).items():
        s, why = score_type(prof, qi, head_applies=True)
        if s >= MIN_TYPE_EVIDENCE:
            nodes[name] = s
            notes.append(f"node:{name}={s:.2f} ({why})")
    for name, prof in (vocab.edge_types or {}).items():
        s, why = score_type(prof, qi, head_applies=False)
        if s >= MIN_TYPE_EVIDENCE:
            edges[name] = s
            notes.append(f"edge:{name}={s:.2f} ({why})")

    nodes = dict(sorted(nodes.items(), key=lambda kv: -kv[1]))
    edges = dict(sorted(edges.items(), key=lambda kv: -kv[1]))
    return nodes, edges, notes
