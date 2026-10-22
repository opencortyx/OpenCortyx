"""L4 — Context optimisation: maximum evidence per token, not maximum retrieved nodes.

    blocks ─► drop exact duplicates ─► merge equivalents ─► reserve per role ─► fill budget

THE TRADE THIS EXISTS TO GET RIGHT

Node count and per-node text are bought with the same currency. A previous configuration drifted
from ~119 tokens per node to ~84 while packing more nodes, and node-identity recall rewarded that
unconditionally — it asks whether the right node arrived, never whether enough of it survived to
answer from. Optimising for block COUNT is therefore optimising the wrong thing.

So the budget is spent on distinct evidence: duplicates removed, equivalents merged, and each role
guaranteed a floor so a single dominant role cannot consume the whole window.

ROLE FLOORS ARE THE ONE RESERVATION, AND THEY ARE ROLE-BASED

A floor per ROLE (what a block contributes) is not a floor per TYPE (what a node is). The
distinction matters: a type floor pins the answer's shape in advance, while a role floor only
guarantees that if relationship evidence exists it is not crowded out by primary evidence.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache
from typing import Iterable, Optional

from opencortyx.retrieval import experiments
from opencortyx.retrieval.contracts import ContextBlock
from opencortyx.retrieval.l4_context.builder import ROLE_ORDER, ROLE_PRIMARY, estimate_tokens, group_by_role, group_provenance
from opencortyx.retrieval.l2_graph.expansion import content_terms

#: Minimum share of the budget each non-empty role may claim before the remainder is allocated by
#: score. Primary gets the largest floor because it is most often the answer.
ROLE_FLOOR = {
    ROLE_PRIMARY: 0.45,
    "relationship": 0.20,
    "temporal": 0.15,
    "supporting": 0.10,
}

ENTITY_FLOOR = 0.30

#: Two blocks whose normalised text is this similar are the same evidence.
MERGE_THRESHOLD = 0.82


def entity_anchored(block, labels: set) -> bool:
    """Is this block evidence ABOUT one of the entities the question named?

    Three signals, cheapest first. `subject` is authoritative — Facts carry it explicitly and it
    is the entity the fact is about, not merely one it mentions. The surface check catches nodes
    that arrived BECAUSE of the entity. The text check is last and deliberately anchored to the
    start of the block, since a name appearing mid-sentence is usually a mention, not a subject.
    """
    if not labels:
        return False
    subj = (block.attributes or {}).get("subject")
    if subj and str(subj).strip().lower() in labels:
        return True
    if any(s in ("entity", "entity_facts") for s in (block.retrieved_by or [])):
        return True
    head = (block.text or "")[:48].lower()
    return any(head.startswith(lbl) for lbl in labels)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _tokens(text: str) -> set[str]:
    return {w for w in _norm(text).split() if len(w) > 2}


#: `_block_text` appends metadata as a trailing " [...]" group — a date for a temporal question,
#: a matched attribute value. Redundancy is a property of the STATEMENT, not of its annotations.
_ANNOTATION = re.compile(r"\s*\[[^\[\]]*\]\s*$")


def _substance(text: str) -> str:
    """A block's own statement, with any appended annotation removed.

    MEASURED: once temporal questions began rendering each block's date, five copies of
    "Joanna values Nate's support" — the same statement from different turns — stopped merging,
    because their appended timestamps differ. Identical text scores 1.00; the same text with
    different dates scores 0.71, under the 0.82 threshold. A timestamp made redundant evidence
    look distinct and it filled the top of the context.
    """
    return _ANNOTATION.sub("", text or "")


@lru_cache(maxsize=8192)
def _statement_tokens(text: str) -> frozenset:
    """The words of a block's statement, computed once per text. Merging compares every block with
    every block kept so far: 578 blocks meant ~167,000 comparisons, each tokenising both texts again,
    3.2 s of a question's 7 s."""
    return frozenset(_tokens(_substance(text)))


def _similarity(a: str, b: str) -> float:
    ta, tb = _statement_tokens(a), _statement_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


CONTAINMENT_THRESHOLD = 0.90

MIN_CONTAINMENT_TOKENS = 4


def _redundancy(a: str, b: str) -> float:
    """How much one statement repeats the other. Symmetric OR containment-based.

    Deliberately says nothing about node TYPE. It asks whether two statements carry the same
    content, which is the property that actually matters - and it therefore works identically on
    a corpus whose types are named differently.
    """
    ta, tb = _statement_tokens(a), _statement_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    jac = inter / len(ta | tb)
    short = min(len(ta), len(tb))
    if short >= MIN_CONTAINMENT_TOKENS:
        contained = inter / short
        if contained >= CONTAINMENT_THRESHOLD:
            return max(jac, contained)
    return jac


SEMANTIC_DUP = 0.93


def _cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def semantic_redundancy(b, kept) -> float:
    """Highest cosine between this block and anything already selected.

    Returns 0.0 when either side has no vector - NOT ASSESSED, never "distinct". A block whose
    redundancy could not be measured keeps its slot; the alternative silently drops evidence for
    lacking an embedding, which is how this system lost an entire Claim layer once already.
    """
    if not getattr(b, "vector", None):
        return 0.0
    best = 0.0
    for k in kept:
        v = getattr(k, "vector", None)
        if v:
            best = max(best, _cosine(b.vector, v))
    return best


def _substance_len(b) -> int:
    return len(_statement_tokens(getattr(b, "text", "") or ""))


def dedupe(blocks: Iterable[ContextBlock]) -> list[ContextBlock]:
    """Remove blocks that repeat a node already present, keeping the higher-scoring one."""
    best: dict[str, ContextBlock] = {}
    for b in blocks:
        key = str(b.node_id)
        cur = best.get(key)
        if cur is None or b.score > cur.score:
            best[key] = b
    return sorted(best.values(), key=lambda b: b.score, reverse=True)


def merge_equivalents(blocks: list[ContextBlock],
                      threshold: float = MERGE_THRESHOLD) -> list[ContextBlock]:
    """Fold near-identical blocks into the strongest one, preserving both provenances.

    Provenance is unioned rather than discarded: the fact that two independent surfaces produced
    equivalent statements is evidence of agreement, and dropping one hides it.
    """
    from opencortyx.retrieval import experiments
    wide = experiments.on("fact_turn_merge")

    kept: list[ContextBlock] = []
    for b in blocks:
        merged = False
        for k in kept:
            score = _redundancy(b.text, k.text) if wide else _similarity(b.text, k.text)
            if wide and score < threshold:
                # Lexical overlap missed it; ask the vectors. This is the Claim-vs-Turn case and
                # the "PS5" vs "PlayStation 5" case, neither of which shares enough tokens.
                sem = semantic_redundancy(b, [k])
                if sem >= SEMANTIC_DUP:
                    score = sem
            if score >= threshold:
                if wide and _substance_len(b) > _substance_len(k):
                    b.retrieved_by = group_provenance(k.retrieved_by + b.retrieved_by)
                    if b.path is None and k.path is not None:
                        b.path = k.path
                    b.score = max(b.score, k.score)
                    kept[kept.index(k)] = b
                    merged = True
                    break
                k.retrieved_by = group_provenance(k.retrieved_by + b.retrieved_by)
                if k.path is None and b.path is not None:
                    k.path = b.path
                merged = True
                break
        if not merged:
            kept.append(b)
    return kept


def compress(text: str, max_chars: int) -> str:
    """Trim a block to fit, at a sentence boundary where possible.

    Cutting mid-sentence produces a fragment the model must guess the end of; cutting at a
    boundary loses information but leaves what remains usable.
    """
    t = (text or "").strip()
    if len(t) <= max_chars or max_chars <= 0:
        return t
    window = t[:max_chars]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    # Accept a sentence boundary that retains at least 40% of the window. A complete sentence at
    # 40% is more useful than a fragment at 100%, which the model must guess the end of.
    if cut >= max_chars * 0.4:
        return window[:cut + 1].strip()
    sp = window.rfind(" ")
    return (window[:sp] if sp > max_chars * 0.6 else window).strip() + "…"


#: How much a block may be lifted for covering ground nothing else covers. Small: novelty breaks
#: ties between comparable evidence, it does not justify preferring weak evidence.
COVERAGE_BONUS = 0.25

ADAPTIVE_TAU = 0.70

ADAPTIVE_MIN_SPREAD = 0.05

#: Never truncate below this. An adaptive cut that empties the window turns a ranking opinion
#: into an abstention, and abstaining is L3's decision on evidence, not L4's on arithmetic.
ADAPTIVE_MIN_BLOCKS = 3


def adaptive_cut(blocks: list, tau: float = ADAPTIVE_TAU,
                 min_spread: float = ADAPTIVE_MIN_SPREAD,
                 min_blocks: int = ADAPTIVE_MIN_BLOCKS) -> tuple[list, str]:
    """Blocks scoring within `tau` of the best, and a one-line reason for the trace.

    Returns everything unchanged when the distribution does not separate. Reporting WHY it
    declined matters as much as the cut: a truncation that silently did nothing is the failure
    shape this engine has already paid for twice.
    """
    comparable = [b for b in blocks
                  if b.score is not None and math.isfinite(b.score)]
    unassessed = [b for b in blocks if b.score is None]
    degenerate = [b for b in blocks
                  if b.score is not None and not math.isfinite(b.score)]
    if len(blocks) <= min_blocks:
        return blocks, "declined: %d blocks, at or under the floor of %d" % (len(blocks), min_blocks)
    if not comparable:
        return blocks, "declined: no finite scores (%d unassessed, %d non-finite)" % (
            len(unassessed), len(degenerate))
    top = max(b.score for b in comparable)
    low = min(b.score for b in comparable)
    if top - low < min_spread:
        return blocks, "declined: flat scores, spread %.4f < %.2f" % (top - low, min_spread)
    if top <= 0:
        return blocks, "declined: top score %.4f not positive" % top
    floor = tau * top
    kept = [b for b in blocks
            if (b.score is None) or (math.isfinite(b.score) and b.score >= floor)]
    if len([b for b in kept if b.score is not None]) < min_blocks:
        kept = (sorted(comparable, key=lambda b: -b.score)[:min_blocks] + unassessed)
        return kept, ("floor %.4f kept <%d scored; took top %d + %d unassessed"
                      % (floor, min_blocks, min_blocks, len(unassessed)))
    return kept, ("tau=%.2f floor=%.4f top=%.4f kept %d/%d (%d unassessed carried, "
                  "%d non-finite dropped)" % (tau, floor, top, len(kept), len(blocks),
                                              len(unassessed), len(degenerate)))


def optimize(blocks: Iterable[ContextBlock], budget_tokens: int,
             min_block_tokens: int = 12,
             role_floor: Optional[dict] = None,
             plan=None, coverage_bonus: float = COVERAGE_BONUS,
             entity_labels: Optional[set] = None) -> list[ContextBlock]:
    """Select the blocks that fit the budget, maximising distinct evidence.

    Two passes. First each non-empty role fills its reserved share, so no role is starved. Then
    the remaining budget goes to the globally best blocks regardless of role — a reservation
    guarantees a minimum, it does not impose a quota.
    """
    blocks = merge_equivalents(dedupe(blocks))
    if budget_tokens <= 0 or not blocks:
        return []

    if experiments.on("adaptive_truncation"):
        blocks, _why = adaptive_cut(list(blocks))
        if not blocks:
            return []

    floors = dict(ROLE_FLOOR)
    if role_floor:
        floors.update(role_floor)

    grouped = group_by_role(blocks)
    active = [r for r in ROLE_ORDER if grouped.get(r)]
    selected: list[ContextBlock] = []
    used = 0
    taken: set[str] = set()

    # Pass 1 — reserved shares.
    for role in active:
        share = int(budget_tokens * floors.get(role, 0.0))
        spent = 0
        for b in grouped[role]:
            if spent + b.tokens > share:
                continue
            selected.append(b)
            taken.add(str(b.node_id))
            spent += b.tokens
            used += b.tokens

    if entity_labels:
        share = int(budget_tokens * ENTITY_FLOOR)
        spent = 0
        for b in sorted(blocks, key=lambda x: -(x.score or 0.0)):
            if str(b.node_id) in taken:
                continue
            if not entity_anchored(b, entity_labels):
                continue
            if spent + b.tokens > share or used + b.tokens > budget_tokens:
                continue
            selected.append(b)
            taken.add(str(b.node_id))
            spent += b.tokens
            used += b.tokens

    for b in _coverage_ordered(list(blocks), plan, coverage_bonus):
        if str(b.node_id) in taken:
            continue
        if used + b.tokens <= budget_tokens:
            selected.append(b)
            taken.add(str(b.node_id))
            used += b.tokens
            continue
        # Partial fit: include a compressed form only if enough survives to be useful. A
        # three-token stub costs budget and carries nothing.
        remaining = budget_tokens - used
        if remaining >= min_block_tokens:
            b.text = compress(b.text, int(remaining * 3.6))
            b.tokens = estimate_tokens(b.text)
            if b.tokens <= remaining:
                selected.append(b)
                taken.add(str(b.node_id))
                used += b.tokens
        if used >= budget_tokens:
            break

    selected.sort(key=lambda b: (ROLE_ORDER.index(b.role) if b.role in ROLE_ORDER else 99,
                                 -b.score))
    return selected


def evidence_density(blocks: Iterable[ContextBlock]) -> dict:
    """Diagnostics for the trade this module manages.

    `distinct_sources` matters more than `n_blocks`: fifty blocks from three sources is a narrower
    context than twelve blocks from twelve.
    """
    bl = list(blocks)
    if not bl:
        return {"n_blocks": 0, "tokens": 0, "tokens_per_block": 0.0,
                "distinct_sources": 0, "roles": {}}
    tokens = sum(b.tokens for b in bl)
    sources = {tag for b in bl for tag in b.retrieved_by}
    roles: dict[str, int] = {}
    for b in bl:
        roles[b.role] = roles.get(b.role, 0) + 1
    return {
        "n_blocks": len(bl),
        "tokens": tokens,
        "tokens_per_block": round(tokens / len(bl), 1),
        "distinct_sources": len(sources),
        "roles": roles,
    }


def question_terms(plan) -> frozenset:
    """The question's own content words, as comparison stems."""
    if plan is None:
        return frozenset()
    return frozenset(content_terms(getattr(plan, "normalized_query", "")
                                   or getattr(plan, "raw_query", "")))


def evidence_coverage(blocks: Iterable[ContextBlock], plan=None) -> dict:
    """How much of the QUESTION the selected context actually speaks to.

    `evidence_density` answers "how much did we pack". This answers "did we pack the right
    spread" — a different question, and the one a budget decision should be judged on. A window
    filled with fifty blocks that all address one facet is dense and uninformative: the reader
    cannot answer the part of the question nothing covered, and no token count reveals that.

    Two numbers, deliberately separate:
      `term_coverage`   — the share of the question's content words the context speaks to at all.
      `source_spread`   — distinct provenance among the selected blocks, so agreement across
                          surfaces is visible rather than one surface answering to itself.
    """
    bl = list(blocks)
    terms = question_terms(plan)
    if not bl:
        return {"term_coverage": 0.0, "covered": 0, "of": len(terms),
                "uncovered": sorted(terms), "source_spread": 0}

    seen: set = set()
    for b in bl:
        seen |= content_terms(b.text or "")
    covered = terms & seen
    surfaces = {tag.split("(")[0] for b in bl for tag in b.retrieved_by}
    return {
        "term_coverage": round(len(covered) / len(terms), 3) if terms else 0.0,
        "covered": len(covered),
        "of": len(terms),
        "uncovered": sorted(terms - covered),
        "source_spread": len(surfaces),
    }


def _coverage_ordered(blocks: list, plan, bonus: float) -> list:
    """Greedy selection order that prefers a block adding UNCOVERED ground.

    Ordering purely by score fills the window with the strongest evidence for whichever facet the
    retriever liked best, and the facets nothing covered stay uncovered however much budget is
    left. This keeps score as the primary signal and lets novelty break its ties: a block is
    lifted only in proportion to how much of the question it newly speaks to.

    `bonus` is small on purpose. Novelty is a tie-breaker, not a reason to prefer weak evidence —
    the failure in the other direction is a context of loosely-relevant material that covers
    every word of the question and answers none of it.
    """
    terms = question_terms(plan)
    if not terms or bonus <= 0:
        return sorted(blocks, key=lambda b: b.score, reverse=True)

    hits = [(b, content_terms(b.text or "") & terms) for b in blocks]

    covered: set = set()
    ordered: list = []
    while hits:
        best_i, best_v = 0, None
        for i, (b, own) in enumerate(hits):
            value = b.score * (1.0 + bonus * (len(own - covered) / len(terms)))
            if best_v is None or value > best_v:
                best_i, best_v = i, value
        b, own = hits.pop(best_i)
        covered |= own
        ordered.append(b)
        if covered >= terms:
            # Everything the question asked about is spoken to; novelty can lift nothing further,
            # so the rest is plain score order and the quadratic loop can stop.
            ordered.extend(x for x, _ in sorted(hits, key=lambda t: t[0].score, reverse=True))
            break
    return ordered
