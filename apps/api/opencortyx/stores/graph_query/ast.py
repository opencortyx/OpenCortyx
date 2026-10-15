"""AST definitions and data models for the OpenCortyx Graph Query primitive."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Union
from pydantic import BaseModel, Field


class Direction(str, Enum):
    OUT = "out"
    IN = "in"
    BOTH = "both"


class FilterOpType(str, Enum):
    EQ = "=="
    NEQ = "!="
    GT = ">"
    LT = "<"
    GTE = ">="
    LTE = "<="
    CONTAINS = "contains"
    IN = "in"


class StepOp(BaseModel):
    op: str


class StartOp(StepOp):
    op: str = "START"
    query: Optional[str] = None
    label: Optional[str] = None
    node_id: Optional[str] = None
    node_ids: Optional[List[str]] = None
    type: Optional[str] = None
    collection_id: Optional[str] = None


class TraverseOp(StepOp):
    op: str = "TRAVERSE"
    rel: Optional[Union[str, List[str]]] = None  # None or "*" means any relation
    direction: Direction = Direction.BOTH
    hops: int = 1
    min_confidence: Optional[float] = None
    min_weight: Optional[float] = None
    target_type: Optional[str] = None


class FilterOp(StepOp):
    op: str = "FILTER"
    field: str  # e.g. "type", "label", "properties.role", "confidence"
    operator: FilterOpType = FilterOpType.EQ
    value: Any


class MatchOp(StepOp):
    op: str = "MATCH"
    rel: Optional[str] = None
    direction: Direction = Direction.BOTH
    target_type: Optional[str] = None
    target_label: Optional[str] = None


class IntersectOp(StepOp):
    op: str = "INTERSECT"
    subquery: Optional[Union[str, List[dict]]] = None


class UnionOp(StepOp):
    op: str = "UNION"
    subquery: Optional[Union[str, List[dict]]] = None


class PathOp(StepOp):
    op: str = "PATH"
    track: bool = True
    max_depth: Optional[int] = None


class LimitOp(StepOp):
    op: str = "LIMIT"
    count: int = 50


class ReturnOp(StepOp):
    op: str = "RETURN"
    fields: List[str] = Field(default_factory=lambda: ["evidence", "nodes", "paths", "edges"])


class GraphQuerySpec(BaseModel):
    steps: List[StepOp] = Field(default_factory=list)
    limit: int = 50
    returns: List[str] = Field(default_factory=lambda: ["count", "nodes"])
    """Default is the CHEAP set. Evidence/paths are opt-in - see the projection note in
    executor.run_pipeline for the measured token cost of each."""


@dataclass
class TraversedStep:
    from_id: str
    edge_id: Optional[str]
    edge_type: str
    to_id: str
    direction: str  # "out" | "in"
    weight: float = 1.0


@dataclass
class GraphPath:
    steps: List[TraversedStep] = field(default_factory=list)
    seed_id: Optional[str] = None

    @property
    def terminal_id(self) -> Optional[str]:
        return self.steps[-1].to_id if self.steps else self.seed_id

    def to_dict(self, node_labels: Optional[dict[str, str]] = None) -> dict:
        labels = node_labels or {}
        nodes_seq = [self.seed_id]
        edges_seq = []
        for s in self.steps:
            nodes_seq.append(s.to_id)
            edges_seq.append({
                "from_id": s.from_id,
                "from_label": labels.get(s.from_id, ""),
                "rel": s.edge_type,
                "direction": s.direction,
                "to_id": s.to_id,
                "to_label": labels.get(s.to_id, ""),
            })
        path_str = f"({labels.get(self.seed_id, self.seed_id)})"
        for s in self.steps:
            arrow = f" -[{s.edge_type}]-> " if s.direction == "out" else f" <-[{s.edge_type}]- "
            path_str += f"{arrow}({labels.get(s.to_id, s.to_id)})"
        node_names = [labels.get(nid, nid) for nid in nodes_seq if nid is not None]
        edge_names = [s.edge_type for s in self.steps]
        return {
            "nodes": node_names,
            "edges": edge_names,
            "readable_path": path_str,
            "seed_id": self.seed_id,
            "terminal_id": self.terminal_id,
            "length": len(self.steps),
            "steps": edges_seq,
        }
