"""The rest of the platform tree, registered as capability descriptors.

The user's model is a tree of capabilities, each owning its data and exposing a stable API. The
five knowledge-quality capabilities (Versioning, Contradiction Detection, Resolution, Trust,
Abstain) are fully implemented in their own modules. The remaining tree nodes already exist as
mature subsystems in the codebase; here we register a THIN adapter descriptor for each so the whole
tree is visible, uniform, and pluggable — each links to the page where it is configured. This keeps
the "extract & wrap" contract (no rewrite of working subsystems) while making the tree real.

An SDK plugin adds a node the same way: subclass Capability and `registry.register(...)`.
"""
from __future__ import annotations

from opencortyx.capabilities.base import Capability


class _Adapter(Capability):
    """A tree node backed by an existing subsystem — identity + a deep link to its config page."""
    def __init__(self, name, title, group, description, ui_route=None):
        self.name = name
        self.title = title
        self.group = group
        self.description = description
        self.ui_route = ui_route


# order roughly follows the platform tree in the design
ADAPTERS = [
    _Adapter("ingestion", "Ingestion", "Ingestion",
             "Bring data in from files, connectors, SQL and streams; sync + scheduling.",
             "/dashboard/ingest"),
    _Adapter("extraction", "Extraction", "Extraction",
             "Turn raw content into nodes/edges via built-in, custom and coded extractors, folded "
             "into efficient LLM call groups.",
             "/dashboard/settings?tab=extraction"),
    _Adapter("ontology", "Ontology", "Ontology",
             "The node/edge/attribute type system (Person & Team ontologies) that shapes the graph.",
             "/dashboard/schema"),
    _Adapter("resolution", "Resolution", "Resolution",
             "After each asset is ingested, its nodes are matched to the organization's graph by the "
             "identity each type declares: same, supports, extends or conflicts, every decision recorded.",
             "/dashboard/review"),
    _Adapter("governance", "Governance", "Governance",
             "What resolution applied automatically and why, conflicts and versions awaiting a decision, "
             "and quarantine.",
             "/dashboard/review"),
    _Adapter("retrieval", "Retrieval", "Retrieval",
             "Hybrid retrieval, reranking, adaptive routing, graph traversal and temporal routing.",
             "/dashboard/settings?tab=performance"),
    _Adapter("reasoning", "Reasoning", "Reasoning",
             "Grounded answer generation with chain-of-thought and query decomposition.",
             "/dashboard/chat"),
    _Adapter("memory", "Memory", "Memory",
             "Session capture, summarisation and recall — conversations become part of the graph.",
             "/dashboard/sessions"),
    _Adapter("analytics", "Analytics", "Analytics",
             "Evaluation, KPIs and graph metrics over the knowledge base.",
             "/dashboard/eval"),
    _Adapter("agents", "Agents", "Agents",
             "Agent-native MCP access and captured agent sessions.",
             "/dashboard/settings?tab=agents"),
    _Adapter("api", "API", "API",
             "Connectors, model registry and the /api/v2 surface external clients call.",
             "/dashboard/connectors"),
    _Adapter("ui", "UI", "UI",
             "The dashboard surfaces that make every capability observable and configurable.",
             None),
]
