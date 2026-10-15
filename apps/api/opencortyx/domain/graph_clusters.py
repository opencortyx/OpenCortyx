"""Legacy: the cluster hierarchy the dashboard graph used to draw (see `legacy/__init__.py`).

The tables stay until a data migration drops them; nothing writes or reads them now.

The raw graph cannot be drawn past a few hundred nodes, so the dashboard shows communities instead:
~40 top-level clusters, each opening into its sub-clusters, down to leaves of real nodes. Every
screen is one parent's children, whatever the graph's size. `legacy/graph_clusters.py` rebuilt
these rows after ingests.

`parent_id` is "" for the top level rather than NULL, so "the children of X" is one equality
predicate on the same index for every level, including the root.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opencortyx.domain.base import V2_SCHEMA, V2Base


class GraphCluster(V2Base):
    """One community. It holds either child clusters or member nodes, never both."""
    __tablename__ = "graph_cluster"
    __table_args__ = (Index("ix_v2_graph_cluster_parent", "workspace_id", "parent_id"),
                      {"schema": V2_SCHEMA})
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    parent_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="community")  # community | bundle | unlinked
    label: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)          # nodes in the whole subtree
    n_children: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    n_members: Mapped[int] = mapped_column(Integer, nullable=False, default=0)     # direct member nodes (leaves)
    internal_edges: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    external_edges: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rep_node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    types: Mapped[dict] = mapped_column(JSONB, default=dict)       # node type → count in the subtree
    preview: Mapped[list] = mapped_column(JSONB, default=list)      # the best-connected member labels


class GraphClusterMember(V2Base):
    """Which leaf cluster a node is in. Label and type are copied so a leaf's members can be listed,
    ranked and searched without a round trip to the graph store."""
    __tablename__ = "graph_cluster_member"
    __table_args__ = (Index("ix_v2_graph_cluster_member_rank", "workspace_id", "cluster_id", "degree"),
                      {"schema": V2_SCHEMA})
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    cluster_id: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    degree: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class GraphClusterLink(V2Base):
    """Edges between two sibling clusters, summed. Stored under the parent they share, which is the
    only level where the two are drawn side by side."""
    __tablename__ = "graph_cluster_link"
    __table_args__ = (Index("ix_v2_graph_cluster_link_parent", "workspace_id", "parent_id"),
                      {"schema": V2_SCHEMA})
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    a_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    b_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    parent_id: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    edges: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    edge_types: Mapped[dict] = mapped_column(JSONB, default=dict)  # relationship type → count


class GraphClusterRun(V2Base):
    """The latest clustering pass of an organization, so the page can say how fresh it is."""
    __tablename__ = "graph_cluster_run"
    __table_args__ = ({"schema": V2_SCHEMA},)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")  # running | ok | error
    nodes: Mapped[int] = mapped_column(Integer, default=0)
    edges: Mapped[int] = mapped_column(Integer, default=0)
    clusters: Mapped[int] = mapped_column(Integer, default=0)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    modularity: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


__all__ = ["GraphCluster", "GraphClusterLink", "GraphClusterMember", "GraphClusterRun"]
