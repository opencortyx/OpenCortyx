"""Graph records and queries shared by every graph store adapter.

A node's type is a property, never a separate table or tag: a Table, an Attribute and a Chunk are
all `Node` records that differ in `type` and `properties`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence

_EDGE_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/edge")


def type_variants(*types: str) -> list[str]:
    """A type name plus the casings it is written under (`Entity`, `entity`)."""
    out: list[str] = []
    for t in types:
        for v in (t, t.lower(), t[:1].upper() + t[1:] if t else t):
            if v and v not in out:
                out.append(v)
    return out


def edge_key(type: str, properties: dict | None) -> str:
    """What distinguishes parallel edges between two nodes: the type, and the `relation` it names
    (dog -RELATES_TO{has_name}-> Max and dog -RELATES_TO{is_a}-> Max are two facts)."""
    relation = (properties or {}).get("relation")
    return f"{type}|{relation}" if relation not in (None, "") else type


def edge_id(source_id, type: str, target_id, properties: dict | None = None) -> uuid.UUID:
    """One relationship between two nodes has one id, so re-writing it is idempotent."""
    return uuid.uuid5(_EDGE_NS, f"{source_id}|{edge_key(type, properties)}|{target_id}")


@dataclass
class Node:
    id: uuid.UUID
    workspace_id: uuid.UUID
    type: str
    label: Optional[str] = None
    entity_type: Optional[str] = None
    properties: dict = field(default_factory=dict)
    collection_id: Optional[uuid.UUID] = None
    source_ref: Optional[uuid.UUID] = None
    confidence: float = 1.0
    level: int = 0
    sensitivity: str = "normal"
    degree: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def base_type(self) -> str:
        """What the node was ingested as, seeing through an anchor retype."""
        return (self.properties or {}).get("__base_type") or self.type


@dataclass
class Edge:
    workspace_id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    type: str
    weight: float = 1.0
    properties: dict = field(default_factory=dict)
    confidence: float = 1.0
    created_at: Optional[datetime] = None
    id: Optional[uuid.UUID] = None

    def __post_init__(self):
        if self.id is None:
            self.id = edge_id(self.source_id, self.type, self.target_id, self.properties)


@dataclass
class NodeQuery:
    """Nodes of one organization. Every field narrows; `None` means no constraint.

    `types` matches what a node was ingested as or is typed as now, in any casing. `exact_types`
    matches the current type only. `props` compares top-level attributes as text (`None` matches a
    missing or empty value); `props_not` excludes matches.
    """
    ids: Optional[Sequence] = None
    types: Optional[Sequence[str]] = None
    exact_types: Optional[Sequence[str]] = None
    exclude_types: Optional[Sequence[str]] = None
    entity_type: Optional[str] = None
    source_refs: Optional[Sequence] = None
    collection_ids: Optional[Sequence] = None
    source_key: Optional[str] = None
    status: Optional[str] = None
    label: Optional[str] = None
    name_key: Optional[str] = None           # `query.normalize_name` of the label
    label_contains: Optional[str] = None
    props: dict[str, Any] = field(default_factory=dict)
    props_not: dict[str, Any] = field(default_factory=dict)
    created_after: Optional[datetime] = None
    order_by: Optional[str] = None          # created_at | -created_at | -updated_at | -degree
    limit: Optional[int] = None


@dataclass
class EdgeQuery:
    """Edges of one organization. `node_ids` matches either endpoint."""
    ids: Optional[Sequence] = None
    source_ids: Optional[Sequence] = None
    target_ids: Optional[Sequence] = None
    node_ids: Optional[Sequence] = None
    types: Optional[Sequence[str]] = None
    exclude_types: Optional[Sequence[str]] = None
    props: dict[str, Any] = field(default_factory=dict)
    limit: Optional[int] = None


@dataclass
class EdgeWrite:
    created: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    refused: list = field(default_factory=list)       # an endpoint does not exist


@dataclass
class Neighbor:
    edge: Edge
    node: Node
    direction: str = "out"


@dataclass
class NeighborGroup:
    """How many of one node's relationships share an edge type, a direction and a far node type —
    one row of the "what is this connected to" summary a hub is opened with."""
    edge_type: str
    direction: str                          # out | in
    node_type: str
    count: int


@dataclass
class Path:
    node_ids: list
    edges: list
