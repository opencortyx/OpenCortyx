"""Stable plugin contracts. New capability = a new Extractor/Pattern/Retriever/Resolver,
never new infrastructure. (design doc §3, §7)"""
import uuid
from dataclasses import dataclass, field
from typing import Protocol, Any, Optional


@dataclass
class ScoredNode:
    node_id: uuid.UUID
    type: str
    score: float
    text: str = ""
    source_ref: Optional[uuid.UUID] = None
    confidence: float = 1.0
    valid_from: Optional[str] = None    # ISO date for temporal/recency ranking
    meta: dict = field(default_factory=dict)


@dataclass
class TokenBudget:
    max_total: int = 8000
    per_layer: dict = field(default_factory=dict)


@dataclass
class RetrieveContext:
    workspace_id: uuid.UUID
    collection_ids: Optional[list] = None     # access scope
    max_sensitivity: str = "normal"
    conversation_id: Optional[uuid.UUID] = None   # scope to one conversation (memory mode)
    # ── per-query overrides (the Chat UI sets these to steer retrieval) ──
    retrievers: Optional[list] = None         # NOT HONOURED in v2 (reported as unsupported)
    rerank: Optional[list] = None             # NOT HONOURED in v2 (reported as unsupported)
    k: Optional[int] = None                   # per-retriever top-k
    abstain_threshold: Optional[float] = None # min fused score before abstaining
    rrf_k: Optional[int] = None               # reciprocal-rank-fusion constant
    # ── bitemporal "as-of" queries ──
    as_of: Optional[str] = None               # valid-time: only facts true as of this ISO date
    as_of_ingest: Optional[str] = None        # transaction-time: only what we KNEW by this ISO date
    # ── low-latency mode: skip rerank + CRAG eval for ms-class retrieval ──
    fast: Optional[bool] = None
    # ── advanced retrieval strategies ──
    decompose: Optional[bool] = None          # search chains/comparisons step by step; None = the setting
    scope: str = "knowledge"
    explain_retriever: bool = False


@dataclass
class ExtractResult:
    nodes: list = field(default_factory=list)   # list[dict]: {type/entity_type,label,properties,...}
    edges: list = field(default_factory=list)   # list[dict]: {source,target,type,properties}


class Extractor(Protocol):
    name: str
    def applies_to(self, source_type: str) -> bool: ...
    async def extract(self, chunk_text: str, ctx: dict) -> ExtractResult: ...


class Pattern(Protocol):
    name: str
    async def build(self, source_id: uuid.UUID, ctx: dict) -> None: ...
    async def retrieve(self, query: str, query_vec: list, budget: TokenBudget,
                       ctx: RetrieveContext) -> list: ...   # -> list[ScoredNode]


class Retriever(Protocol):
    async def retrieve(self, query: str, query_vec: list, ctx: RetrieveContext) -> list: ...


class Resolver(Protocol):
    name: str
    async def resolve(self, params: dict, ctx: RetrieveContext) -> Any: ...
