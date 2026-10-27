"""Shared data model for the graph-native retrieval workflow.

Every layer speaks these types and nothing else. The point of putting them in one place is that
a candidate's EVIDENCE travels with it from the moment it is discovered to the moment it is
rendered into context — where it came from, what matched, at what rank, over which path. The
previous engine carried a bare `List[ScoredNode]`, so by the time a node reached ranking there was
no way to ask "why is this here?", and no way for a reranker to prefer a node that three
independent surfaces agreed on over one that a single retriever guessed at.

Two design rules run through all of it, both of them the direct product of measured failures:

1. NOTHING IS KEYED ON A HARDCODED TYPE OR EDGE TABLE. The previous engine ranked by
   `_TYPE_ORDER = {"Turn": 1, ... }` with an implicit default of 5, and expanded the graph only
   across a fixed `_PROVENANCE_EDGES` set. A `Fact` node type absent from that dict was silently
   demoted below every turn and never surfaced; an edge type absent from that set could never
   carry relevance. Both are represented here as SCORES computed against the query, never as
   membership in a literal.

2. A SCORE ALWAYS CARRIES ITS SCALE. Raw BM25, cosine, PPR mass and fuzzy ratios are not
   comparable, and averaging them silently lets whichever retriever has the widest range decide
   the ranking. `Evidence` therefore records the raw score, the rank within that retriever, and
   the scale it was measured on, so fusion can rank-fuse and the reranker can use calibrated
   features rather than a number of unknown provenance.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ── query understanding ──────────────────────────────────────────────────────────────────────

class Intent(str, Enum):
    """What KIND of question this is. Drives which strategies the planner activates.

    Deliberately a small, closed set: every member must imply a materially different retrieval
    plan. A taxonomy with twenty intents that all run the same retrievers is decoration.
    """
    EXACT_LOOKUP = "exact_lookup"          # "what is John's email"      → resolve + attribute read
    KEYWORD = "keyword"                    # "documents mentioning X"    → lexical leads
    SEMANTIC = "semantic"                  # "information about Y risk"  → dense leads
    ENTITY = "entity"                      # "tell me about JP Morgan"   → resolve + neighbourhood
    RELATIONSHIP = "relationship"          # "who works with X"          → graph leads
    MULTI_HOP = "multi_hop"                # "connected to X through Y"  → beam search dominates
    TEMPORAL = "temporal"                  # "what changed since 2023"   → time constraints bind
    AGGREGATION = "aggregation"            # "how many per company"      → count, do not stuff


class Cardinality(str, Enum):
    """How many things a correct answer names. Governs how hard the context builder should work
    to be COMPLETE rather than merely correct: a `SET` answer that returns four of five items is
    wrong in a way a `ONE` answer cannot be."""
    ONE = "one"
    SET = "set"
    COUNT = "count"
    UNKNOWN = "unknown"


@dataclass
class EntityMention:
    """A span of the question that names something in the graph, before resolution."""
    text: str
    start: int = -1
    end: int = -1
    hint_type: Optional[str] = None        # a TYPE NAME read off the question, never a fixed enum


@dataclass
class ResolvedEntity:
    """A mention bound to a canonical node, with the evidence for that binding.

    `matched_by` exists so downstream stages can tell an exact identifier hit from a fuzzy
    name guess. Treating those as equally certain is how a query about one company retrieves a
    different company with a similar description.
    """
    mention: EntityMention
    node_id: Optional[uuid.UUID] = None
    label: str = ""
    confidence: float = 0.0
    matched_by: list[str] = field(default_factory=list)   # exact | alias | fuzzy | dense | keyword
    alternatives: list[tuple[uuid.UUID, str, float]] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.node_id is not None


@dataclass
class TemporalConstraint:
    """A time bound read off the question. `relation` is free text from the question's own words
    ("after", "before", "between", "on", "since") rather than an enum, so an unusual phrasing
    degrades to an unused constraint instead of a parse error."""
    relation: str
    start: Optional[str] = None            # ISO date
    end: Optional[str] = None              # ISO date
    raw: str = ""


@dataclass
class AttributeConstraint:
    """A structured filter the question implies: `country = Germany`, `amount > 1000000`.

    The attribute NAME is whatever the question and the corpus agree on — discovered, never
    drawn from a fixed schema. `op` is a comparison, not a type.
    """
    attr: str
    op: str                                # eq | ne | gt | gte | lt | lte | in | contains
    value: Any
    raw: str = ""


@dataclass
class ExecutionPolicy:
    """Which retrieval surfaces actually RUN for a question, and why the others do not.

    Distinct from `QueryPlan.strategies`, which decides how loudly a surface that ran gets to
    vote. Both are needed: the weight floor guarantees a wrong plan costs precision rather than
    recall, but it can only make that guarantee about surfaces that executed. Without this, every
    surface ran on every question and the classification governed nothing it was meant to govern.

    `skipped` carries a REASON per surface. A retriever that is silently absent is
    indistinguishable in a trace from one that ran and found nothing, and telling those apart is
    the whole point of measuring layers independently.
    """
    run: set[str] = field(default_factory=set)
    skipped: dict[str, str] = field(default_factory=dict)
    widen_below: int = 0                   # fused-candidate count under which skipped surfaces run
    reason: str = ""
    widened: bool = False                  # set by the orchestrator if the safety net fired

    def allows(self, surface: str) -> bool:
        return surface in self.run


@dataclass
class QueryPlan:
    """The structured form of the question. This is the contract L0 produces and L1–L4 consume.

    A plan is a set of HYPOTHESES, not commitments: `strategies` names the retrieval surfaces
    worth spending on, and every one of them still has to earn its candidates. A plan that
    activates nothing is valid and means "fall back to broad recall" — it must never mean
    "retrieve nothing".
    """
    raw_query: str
    normalized_query: str = ""
    intent: Intent = Intent.SEMANTIC
    cardinality: Cardinality = Cardinality.UNKNOWN
    mentions: list[EntityMention] = field(default_factory=list)
    entities: list[ResolvedEntity] = field(default_factory=list)
    temporal: list[TemporalConstraint] = field(default_factory=list)
    attributes: list[AttributeConstraint] = field(default_factory=list)
    sub_queries: list[str] = field(default_factory=list)
    answer_type_hint: str = ""
    node_types: dict[str, float] = field(default_factory=dict)
    edge_types: dict[str, float] = field(default_factory=dict)
    max_hops: int = 2
    strategies: dict[str, float] = field(default_factory=dict)   # surface -> weight in [0,1]
    #: Which surfaces RUN. Weight and execution are separate decisions — see `ExecutionPolicy`.
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    confidence: float = 0.0                                      # the classifier's, kept for trace
    notes: list[str] = field(default_factory=list)               # human-readable plan trace

    def wants(self, surface: str, threshold: float = 0.0) -> bool:
        return self.strategies.get(surface, 0.0) > threshold

    def type_prior(self, node_type: str) -> float:
        """How much this question argues for a node of this type. Neutral 0.0 when unargued.

        Deliberately not a multiplier and deliberately never negative: a type the plan says
        nothing about must rank exactly as it would have without a plan. The previous engine's
        `_TYPE_ORDER.get(t, 5)` did the opposite — an unlisted type was assigned the WORST rank
        by default, and a whole extracted layer disappeared from retrieval because of it.
        """
        return self.node_types.get(node_type, 0.0)

    def edge_prior(self, edge_type: str) -> float:
        """As `type_prior`, for a relation name. Unargued relations are 0.0, never excluded."""
        return self.edge_types.get(edge_type, 0.0)


# ── candidates and their evidence ────────────────────────────────────────────────────────────

@dataclass
class Evidence:
    """ONE retriever's reason for proposing a candidate.

    `raw_score` is on `scale` and is NOT comparable across surfaces. `rank` is, which is why
    fusion works on rank. `detail` names the specific surface that fired — for attribute
    embeddings that is the attribute, so "found via description_embedding" survives to the
    reranker instead of collapsing into "found via dense".
    """
    surface: str                           # node_embedding | attr_embedding | bm25 | fuzzy | ...
    rank: int
    raw_score: float
    scale: str = "unknown"                 # cosine | bm25 | ratio | ppr | rrf | constant
    detail: str = ""                       # e.g. the attribute name, the edge type walked
    matched_text: str = ""


@dataclass
class PathStep:
    """One hop of a traversal. Kept so the PATH can be evidence, not just its endpoint."""
    from_id: uuid.UUID
    edge_type: str
    to_id: uuid.UUID
    direction: str                         # out | in
    edge_score: float = 0.0                # query-relevance of this edge, COMPUTED not looked up
    node_score: Optional[float] = None


@dataclass
class Path:
    """A traversal from a seed to a candidate, with the score of the whole route.

    A node reached over a highly query-relevant relation is better evidence than the same node
    reached over an incidental one, and a question about a RELATIONSHIP is answered by the path
    itself. The previous engine discarded routes entirely and kept only endpoints.
    """
    steps: list[PathStep] = field(default_factory=list)
    seed_id: Optional[uuid.UUID] = None
    score: float = 0.0

    @property
    def length(self) -> int:
        return len(self.steps)

    @property
    def terminal_id(self) -> Optional[uuid.UUID]:
        return self.steps[-1].to_id if self.steps else self.seed_id

    def as_tuple(self) -> tuple:
        out: list[Any] = [str(self.seed_id)]
        for s in self.steps:
            out += [s.edge_type, str(s.to_id)]
        return tuple(out)


@dataclass
class Candidate:
    """A node proposed by one or more surfaces, with everything known about why.

    `evidence` accumulates rather than overwrites: a node found by three independent surfaces
    carries three entries and is measurably more trustworthy than one found by a single guess.
    """
    node_id: uuid.UUID
    node_type: str = ""
    label: str = ""
    text: str = ""
    properties: dict = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)
    fusion_score: float = 0.0
    rerank_score: Optional[float] = None
    features: dict[str, float] = field(default_factory=dict)

    @property
    def surfaces(self) -> set[str]:
        return {e.surface for e in self.evidence}

    def best(self, scale: str) -> float:
        """Best raw score among evidence measured on `scale`. Never mixes scales."""
        vals = [e.raw_score for e in self.evidence if e.scale == scale]
        return max(vals) if vals else 0.0

    def best_rank(self, surface: Optional[str] = None) -> Optional[int]:
        ranks = [e.rank for e in self.evidence if surface is None or e.surface == surface]
        return min(ranks) if ranks else None

    def add(self, ev: Evidence) -> None:
        self.evidence.append(ev)

    @property
    def final_score(self) -> float:
        return self.rerank_score if self.rerank_score is not None else self.fusion_score


@dataclass
class CandidateGraph:
    """Nodes, the edges actually traversed between them, and the paths that produced them.

    This is the object L3 reranks over. Ranking a flat list cannot express "these two candidates
    are the same evidence" or "this one is only here because it sits on a strong path", and both
    of those change what belongs in the final context.
    """
    candidates: dict[uuid.UUID, Candidate] = field(default_factory=dict)
    edges: list[PathStep] = field(default_factory=list)
    seeds: list[uuid.UUID] = field(default_factory=list)

    def upsert(self, cand: Candidate) -> Candidate:
        cur = self.candidates.get(cand.node_id)
        if cur is None:
            self.candidates[cand.node_id] = cand
            return cand
        cur.evidence.extend(cand.evidence)
        cur.paths.extend(cand.paths)
        if len(cand.text or "") > len(cur.text or ""):
            cur.text = cand.text
        cur.label = cur.label or cand.label
        cur.node_type = cur.node_type or cand.node_type
        if cand.properties and not cur.properties:
            cur.properties = cand.properties
        return cur

    def ranked(self) -> list[Candidate]:
        return sorted(self.candidates.values(), key=lambda c: c.final_score, reverse=True)

    def __len__(self) -> int:
        return len(self.candidates)


    def adjacency(self) -> dict:
        """Map each candidate to the retrieved candidates it is joined to, direction-agnostic.

        Restricted to endpoints that are themselves candidates. An edge leaving the retrieved set
        says nothing about how well a candidate sits inside the subgraph that answers the
        question, and that is the only thing being asked here.
        """
        adj: dict = {nid: set() for nid in self.candidates}
        for step in self.edges:
            a, b = step.from_id, step.to_id
            if a in adj and b in adj and a != b:
                adj[a].add(b)
                adj[b].add(a)
        return adj

    def neighbours(self, node_id: uuid.UUID) -> set:
        return self.adjacency().get(node_id, set())

    def dangling_edges(self) -> list:
        """Steps with an endpoint that is not a candidate.

        Expected rather than an error: a walk passes THROUGH nodes it does not retain. Exposed so
        an integrity check can tell "passed through" apart from "lost the node it landed on".
        """
        return [s for s in self.edges
                if s.from_id not in self.candidates or s.to_id not in self.candidates]


# ── output ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class ContextBlock:
    """One rendered piece of context, with the provenance that lets an answer be explained.

    `role` describes what this block CONTRIBUTES (primary / relationship / temporal / supporting),
    which is derived from how the block was retrieved, not from the node's type.
    """
    node_id: uuid.UUID
    role: str
    text: str
    node_type: str = ""
    label: str = ""
    relations: list = field(default_factory=list)
    retrieved_by: list[str] = field(default_factory=list)
    score: float = 0.0
    path: Optional[tuple] = None
    attributes: dict = field(default_factory=dict)
    tokens: int = 0
    vector: Optional[list] = None


@dataclass
class LayerTrace:
    """Per-layer measurement. Each layer is evaluated independently, so each layer reports."""
    layer: str
    stage: str
    detail: str = ""
    n_in: int = 0
    n_out: int = 0
    ms: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class RetrievalResult:
    plan: Optional[QueryPlan] = None
    graph: Optional[CandidateGraph] = None
    blocks: list[ContextBlock] = field(default_factory=list)
    trace: list[LayerTrace] = field(default_factory=list)
    tokens: int = 0
    abstained: bool = False
    paths: list = field(default_factory=list)
    aggregation: Optional[Any] = None
    #: The sub-questions a decomposed question was searched as (retrieval/decomposition), in order:
    #: index, question, asked (with earlier answers written in), answer, blocks, ran. Empty when the
    #: question was searched as it is.
    steps: list = field(default_factory=list)

    def block_ids(self) -> list[str]:
        return [str(b.node_id) for b in self.blocks]
