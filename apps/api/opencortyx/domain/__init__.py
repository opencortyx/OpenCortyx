"""Domain layer — what an OpenCortyx row IS, with no idea how it is stored or served.

Everything here is definitions: the declarative base, the control-plane tables and the ontology.
Nothing in this package imports `core`, `ingest`, `retrieve`, `api` or `tasks`, and nothing here
opens a database session. That one rule is what keeps the dependency graph pointing downward —
`core` can depend on the SHAPE of a row without the row depending on `core`.

Code that queries these tables lives in the layer that owns the use case, not here.
"""
from opencortyx.domain.base import V2_SCHEMA, V2Base, uuid_pk
from opencortyx.domain.control import (
    SENSITIVITY_ORDER, ApiKey, AuditLog, Collection, Connector, CostBudget, ExtractionTemplate,
    IngestRun, Ingestion, Model, Pipeline, Policy, RuntimeSetting, SchemaMapping, User, Workspace,
)
from opencortyx.domain.ontology import (
    ASSET_TYPES, TypeRegistry, base_type_of, is_asset_node,
)
from opencortyx.domain.graph_clusters import (
    GraphCluster, GraphClusterLink, GraphClusterMember, GraphClusterRun,
)
from opencortyx.domain.graph_tree import (
    GraphAsset, GraphAssetTag, GraphRoot, GraphTagRequest, GraphTreeRun,
)
from opencortyx.domain.resolution import ResolutionDecision, ResolutionKey
from opencortyx.domain.chat import ChatMessage, ChatThread

__all__ = [
    "V2_SCHEMA", "V2Base", "uuid_pk",
    "SENSITIVITY_ORDER", "ApiKey", "AuditLog", "Collection", "Connector", "CostBudget",
    "ExtractionTemplate", "IngestRun", "Ingestion", "Model", "Pipeline", "Policy",
    "RuntimeSetting", "SchemaMapping", "User", "Workspace",
    "ASSET_TYPES", "TypeRegistry", "base_type_of", "is_asset_node",
    "GraphCluster", "GraphClusterLink", "GraphClusterMember", "GraphClusterRun",
    "GraphAsset", "GraphAssetTag", "GraphRoot", "GraphTagRequest", "GraphTreeRun",
    "ResolutionDecision", "ResolutionKey",
    "ChatMessage", "ChatThread",
]
