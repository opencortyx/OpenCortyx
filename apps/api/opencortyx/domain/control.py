"""Control-plane tables — relational rows BESIDE the graph, never graph nodes.

Holds collections (scoping), policies (access), audit, api keys, connectors, pipelines, models,
ingestions and their runs. These live in the same isolated `opencortyx_v2` schema as the ontology
but are plain tables — they never pollute node/edge.

Definitions only. Anything that QUERIES these tables belongs in the application layer (see
`opencortyx.core.security.scope.resolve_scope`), so that `core` can depend on the shape of a row without
depending on the code that reads it.
"""
import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, func, Integer, Float
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opencortyx.domain.base import V2_SCHEMA, V2Base, uuid_pk as _pk

# sensitivity lattice — higher number = more restricted
SENSITIVITY_ORDER = {"public": 0, "normal": 1, "confidential": 2, "restricted": 3}


class Collection(V2Base):
    __tablename__ = "collection"
    __table_args__ = ({"schema": V2_SCHEMA},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Policy(V2Base):
    __tablename__ = "policy"
    __table_args__ = ({"schema": V2_SCHEMA},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    group: Mapped[str] = mapped_column(String(120), nullable=False)        # role/team key
    collection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # None = all collections
    max_sensitivity: Mapped[str] = mapped_column(String(20), default="normal")
    permission: Mapped[str] = mapped_column(String(20), default="read")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(V2Base):
    __tablename__ = "api_key"
    __table_args__ = ({"schema": V2_SCHEMA},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    app: Mapped[str] = mapped_column(String(120), default="default")
    hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The app_user who issued the key — the person an agent's sessions are attributed to. NULL for
    #: keys issued before this was recorded: their owner is unknown, and is never guessed.
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class User(V2Base):
    """A real human account (replaces dev-mode username auth). One workspace (org) per install;
    the first registered user is the admin who owns the workspace and registers everyone else."""
    __tablename__ = "app_user"
    __table_args__ = ({"schema": V2_SCHEMA},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="viewer")    # admin | editor | viewer
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | disabled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Workspace(V2Base):
    """What an `workspace_id` IS, for the people in it: its name and its mode. One row per workspace.

    `mode` is what the workspace stands for — `person` (someone's own knowledge) or `team` (a team or
    an organization) — and decides the type of its central graph node (`knowledge.workspace`), whose
    id is the workspace_id itself. Both used to be install-wide runtime settings, which every workspace on
    an install shared."""
    __tablename__ = "workspace"
    __table_args__ = ({"schema": V2_SCHEMA},)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="person")
    #: Where this workspace's data is written: `delta` (Delta tables, with only knowledge in the
    #: graph - docs/knowledge-layer-implementation.md; every new workspace) or `graph` (chunk and row
    #: nodes: a workspace from before 0.2.0, until it is reset and re-ingested).
    data_store: Mapped[str] = mapped_column(String(10), nullable=False, default="delta",
                                            server_default="delta")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuditLog(V2Base):
    __tablename__ = "audit_log"
    __table_args__ = ({"schema": V2_SCHEMA, "extend_existing": True},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)              # Execution latency
    trust_score: Mapped[float | None] = mapped_column(Float)             # Confidence/Trust score (e.g. from CRAG)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Connector(V2Base):
    """A saved data source (SQL DSN, etc.) so native ingestion can reference it by id.

    NOTE: `config` holds the connection string; encrypt at rest before production use.
    """
    __tablename__ = "connector"
    __table_args__ = ({"schema": V2_SCHEMA},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="sql")
    config: Mapped[dict] = mapped_column(JSONB, default=dict)   # {"connection_string": "...", "schema": "..."}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExtractionTemplate(V2Base):
    """A user-defined extraction recipe — the heart of 'extract whatever you need'.

    `instruction` = what to pull; `output_schema` = the data points (fields); `target_type` = the
    node type each extracted record becomes. The extractor runs every active template on each chunk
    at ingest, so anyone can extract domain-specific data points without code.
    """
    __tablename__ = "extraction_template"
    __table_args__ = ({"schema": V2_SCHEMA},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[dict] = mapped_column(JSONB, default=dict)       # { select: "Chunk", where: {} }
    mechanism: Mapped[dict] = mapped_column(JSONB, default=dict)    # { ref: "unstructured.llm", prompt: "..." }
    emit: Mapped[dict] = mapped_column(JSONB, default=dict)         # { nodes: [], edges: [], attributes: [] }
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | disabled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Pipeline(V2Base):
    """A SAVED, reusable ingestion pipeline (the ordered DAG of stages) — authored in the Pipelines
    dashboard, then ASSIGNED to a file/SQL source at ingestion time. Decouples pipeline design from
    ingestion. `kind` filters which builder/extractors apply (document = unstructured-tagged tasks,
    structured = structured-tagged tasks). `stages` is the same JSON the DAG executors consume."""
    __tablename__ = "pipeline"
    __table_args__ = ({"schema": V2_SCHEMA},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="document")     # document | structured
    stages: Mapped[list] = mapped_column(JSONB, default=list)             # ordered DAG stages
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SchemaMapping(V2Base):
    """A SAVED, reusable schema→graph mapping — the authoritative source-shape definition, authored in
    the Schema-Mapping engines (Structured / Semi-Structured / Unstructured) and CONSUMED by a pipeline
    at ingest (a pipeline references one by `mapping_id` instead of carrying an inline copy). This is
    the piece that lets 'define a source's shape' happen once and be reused across pipelines.

    `payload` is the paradigm-specific mapping body (the SAME JSON the ingest machinery already reads):
      • semistructured → {"structures": [...], "auto_accept": bool, "rules": [...], "id_patterns": [...]}
        (identical to the pipeline's `json_structures` stage config → compile_walk_spec)
      • structured     → {"table_mappings": {...}, ...} (→ apply_custom_mappings)
      • unstructured   → {"sections": [...], ...} (documents have no fixed schema; usually empty/auto)
    `summary` is a small denormalized view for the hub table (targets / node & edge counts), refreshed
    on save. `status`: active | draft | paused | auto (auto = discovered by auto-accept at ingest)."""
    __tablename__ = "schema_mapping"
    __table_args__ = ({"schema": V2_SCHEMA},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    paradigm: Mapped[str] = mapped_column(String(20), default="semistructured")  # structured|semistructured|unstructured
    source_type: Mapped[str] = mapped_column(String(120), default="")            # descriptive, e.g. "JSON", "PostgreSQL", "PDF"
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)                    # paradigm-specific mapping body
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)                    # {targets:[...], nodes:int, edges:int}
    status: Mapped[str] = mapped_column(String(20), default="active")            # active | draft | paused | auto
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Ingestion(V2Base):
    """A SAVED, re-runnable ingestion job (DataHub-style). Binds a `connector` (the connection) to a
    SELECTION of what to ingest (schema + tables/views) and a `pipeline`. Persisted so it can be
    triggered manually anytime, listed/edited (CRUD), and (future) scheduled for recurring/realtime
    sync. Each run dispatches a Celery task and surfaces on the Progress tab."""
    __tablename__ = "ingestion"
    __table_args__ = ({"schema": V2_SCHEMA},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="structured")   # structured (connector-based)
    connector_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # None → default full ingest
    config: Mapped[dict] = mapped_column(JSONB, default=dict)             # {schema, tables:[], views:[], enrich}
    schedule: Mapped[str | None] = mapped_column(String(120))            # cron-ish; FUTURE (no executor yet)
    status: Mapped[str] = mapped_column(String(20), default="idle")       # idle | running | completed | failed
    last_run_id: Mapped[str | None] = mapped_column(String(80))           # last IngestRun id (Progress)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Model(V2Base):
    """A registered model — bring-your-own for generation / embedding / reranking. Used anywhere a
    model is needed (built-in & custom extraction prompts, retrieval, rerank). `api_key` is encrypted
    at rest via core.security.secrets. One `is_default` per role is what the workspace uses for that role."""
    __tablename__ = "model"
    __table_args__ = ({"schema": V2_SCHEMA},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)            # friendly label
    role: Mapped[str] = mapped_column(String(20), default="generation")       # generation | embedding | rerank
    provider: Mapped[str] = mapped_column(String(40), default="openai_compatible")  # openai_compatible|openai|anthropic|huggingface|groq|fastembed|sentence-transformers
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)       # what the API is CALLED with: the server's model name, or gpt-4o / claude-… / a HF repo
    base_url: Mapped[str | None] = mapped_column(String(300))                # the server address (required for openai_compatible); NULL = the hosted provider's own
    api_key: Mapped[str | None] = mapped_column(Text)                         # encrypted secret (enc: prefix)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)                 # {temperature, dimensions, …}
    is_default: Mapped[bool] = mapped_column(default=False)                   # the org default for its role
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class IngestRun(V2Base):
    """One row per ingestion of an asset (a file or a SQL source) — powers the Progress tab.

    The DAG executors update it step-by-step (status, %, per-stage `steps`) so the UI can show live
    progress; it also lets us queue, cancel, and audit/clear runs. Celery already serialises work
    across the worker's concurrency, so extra runs naturally sit as `queued` until a slot frees up.
    """
    __tablename__ = "ingest_run"
    __table_args__ = ({"schema": V2_SCHEMA},)
    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="document")     # document | structured
    asset: Mapped[str] = mapped_column(String(500), default="")           # filename or table/source name
    source_ref: Mapped[str | None] = mapped_column(String(300))          # source_id (doc) / source_key (structured)
    task_id: Mapped[str | None] = mapped_column(String(80))               # Celery task id (for revoke)
    status: Mapped[str] = mapped_column(String(20), default="queued")     # queued|running|completed|failed|canceled
    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    steps: Mapped[list] = mapped_column(JSONB, default=list)              # [{i,name,status,count}]
    result: Mapped[dict] = mapped_column(JSONB, default=dict)             # final totals
    error: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    retry_payload: Mapped[dict | None] = mapped_column(JSONB)
    batch_id: Mapped[str | None] = mapped_column(String(80))              # files dropped together
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeSetting(V2Base):
    """Global, runtime-tunable knobs (parallelism, caching, permissions) editable from the Settings UI.
    Process-wide (not per-org); persisted so changes survive restarts. Infra/build-time knobs
    (HNSW build params, pool size, replica URL) stay env-only and are shown read-only in the UI."""
    __tablename__ = "runtime_setting"
    __table_args__ = ({"schema": V2_SCHEMA},)
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)         # {"v": <value>}
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CostBudget(V2Base):
    """Per-org LLM cost controls. Spend is measured in TOKENS (provider-neutral; local LLMs are free
    so this is mostly a guardrail for paid APIs). `monthly_token_budget`=0 means unlimited. When usage
    crosses `alert_threshold` (fraction, e.g. 0.8) the UI/API flag an alert; if `hard_cap` is on and
    the budget is exceeded, paid LLM answer calls are refused (a clear 429), local models keep working.
    `usd_per_1k_tokens` is an optional display rate to estimate $ from tokens."""
    __tablename__ = "cost_budget"
    __table_args__ = ({"schema": V2_SCHEMA},)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    monthly_token_budget: Mapped[int] = mapped_column(Integer, default=0)     # 0 = unlimited
    alert_threshold: Mapped[float] = mapped_column(Float, default=0.8)        # fraction of budget
    hard_cap: Mapped[bool] = mapped_column(default=False)                     # block paid calls when over
    usd_per_1k_tokens: Mapped[float] = mapped_column(Float, default=0.0)      # optional $ estimate rate
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())



__all__ = ["SENSITIVITY_ORDER", "Collection", "Policy", "ApiKey", "User", "AuditLog", "Connector",
           "ExtractionTemplate", "Pipeline", "SchemaMapping", "Ingestion", "Model", "IngestRun",
           "RuntimeSetting", "CostBudget"]
