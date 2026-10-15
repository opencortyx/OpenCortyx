"""v2 configuration — self-contained (no v1 import).

Phase 1 of the v1→v2 cutover: v2 owns its own settings so it no longer borrows
`opencortyx.config` / `opencortyx.db.models`. Reads the same `.env`, ignores unknown keys.
"""
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_qdrant_url() -> str:
    try:
        import socket
        socket.gethostbyname("qdrant")
        return "http://qdrant:6333"
    except Exception:
        return "http://localhost:6333"


def _default_nebula_host() -> str:
    try:
        import socket
        socket.gethostbyname("nebulagraph")
        return "nebulagraph"
    except Exception:
        return "localhost"


class V2Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = Field(..., description="Async PostgreSQL URL (asyncpg)")
    DATABASE_READ_URL: str = ""        # optional read-replica DSN; reads route here when set
    DB_POOL_SIZE: int = 50
    DB_MAX_OVERFLOW: int = 50
    DB_POOL_RECYCLE: int = 1800
    DB_STATEMENT_TIMEOUT_MS: int = 300_000        # 5 min — cancel the query, free the connection
    DB_IDLE_TX_TIMEOUT_MS: int = 120_000          # 2 min — a transaction nobody is driving
    DB_COMMAND_TIMEOUT_S: float = 330.0           # asyncpg client-side; above statement_timeout so
    #                                               the SERVER wins and we see a clean cancellation

    TASK_SOFT_TIME_LIMIT: int = 1800   # 30 min — task raises and can clean up after itself
    TASK_TIME_LIMIT: int = 2100        # 35 min — process killed; 5 min of headroom to clean up
    RUN_MAX_ATTEMPTS: int = 3          # auto-resume attempts for a run whose worker died (OOM, sleep, update)
    INTEGRITY_AUDIT_SECONDS: int = 600  # how often the write-path invariants are checked (the alarm)

    RETRIEVAL_MAX_PARALLEL: int = 8    # max retrievers run concurrently per query (asyncio.gather)
    STAGE1_SEED_N: int = Field(
        24, validation_alias=AliasChoices("STAGE1_SEED_N", "OPENCORTYX_STAGE1_SEED_N"))
    QUERY_CACHE_ENABLED: bool = True   # cache assembled context packages (per identical request)
    QUERY_CACHE_TTL: int = 300         # seconds a cached answer/context stays fresh
    QUERY_CACHE_SIZE: int = 1024       # max distinct queries cached (LRU)
    EMBED_CACHE_ENABLED: bool = True   # cache query embeddings (skip recompute for repeat queries)
    EMBED_CACHE_SIZE: int = 4096

    # ── Structured ingestion tuning ───────────────────────────────────────
    # Layout strategy (Docling). Not DOCLING_*: Docling reads that env namespace itself. OCR is off by default: the image ships no OCR engine, and a text
    # PDF does not need one. Turn it on only after installing one (e.g. rapidocr), for scans.
    LAYOUT_OCR: bool = False
    LAYOUT_TABLE_STRUCTURE: bool = True      # TableFormer: real row/column grids for tables
    STRUCTURED_SAMPLE_N: int = 50           # rows sampled per table for profiling (null-rate/samples)
    STRUCTURED_MAX_LLM_TABLES: int = 40     # cap tables that get LLM enrichment (cost guard)
    STRUCTURED_MAPPING_ROW_LIMIT: int = 1000  # max rows read per table in granular mapping
    CELERY_WORKER_CONCURRENCY: int = 4       # ingestion worker pool size (parallel ingestion jobs); live-tunable in Settings
    SYNC_TICK_SECONDS: int = 30              # Celery-beat heartbeat for realtime/scheduled ingestion change-detection
    SYNC_HIBERNATE_BACKOFF_SECONDS: int = 120  # how long a failure-hibernated ingestion waits before the tick auto-retries it
    GRAPH_CLUSTER_DEBOUNCE_S: int = 30       # legacy/cluster_tasks.py only (unwired): rebuild debounce
    GRAPH_TREE_DEBOUNCE_S: int = 20          # after a graph change, re-index the browse tree (sources,
    #                                          tags, assets, unconnected parts) this many seconds later.
    GRAPH_ROOT_NAME: str = ""                # seeds Settings → Workspace name (the top of the graph); empty → a name made up
    #                                          from the workspace id, stable across restarts.
    CONNECTOR_SECRET_KEY: str = ""          # Fernet key (base64); if set + cryptography present, DSNs encrypted at rest
    AZURE_KEYVAULT_URL: str = ""            # default Azure Key Vault URL for connector secret references

    # ── Redis / Celery ────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    LLM_PROVIDER: Literal["vllm", "openai", "openai_compatible", "azure", "groq", "together",
                          "anthropic", "huggingface", "mock"] = "vllm"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "opencortyx-llm"
    LLM_MODEL_WEIGHTS: str = ""
    VISION_MODEL: str = "gpt-4o"
    #: Endpoint of the vLLM server (or any OpenAI-compatible server). Defaults to the compose
    #: service so the stack works with no env at all; point it anywhere for a remote/shared GPU box.
    LLM_BASE_URL: str = "http://vllm:8000/v1"

    LLM_MAX_MODEL_LEN: int = Field(
        12288, validation_alias=AliasChoices("LLM_MAX_MODEL_LEN", "LLM_NUM_CTX"))
    LLM_MAX_CONNECTIONS: int = 256      # in-flight HTTP requests the pool allows
    LLM_MAX_KEEPALIVE: int = 128        # connections kept warm between calls (must exceed steady concurrency)
    LLM_TIMEOUT_S: float = 600.0        # a deep vLLM queue is slow, not dead
    LLM_CONNECT_TIMEOUT_S: float = 10.0

    FAST_MODEL_PROVIDER: str = ""
    FAST_MODEL: str = ""
    FAST_MODEL_BASE_URL: str = ""
    # The WEIGHTS behind the fast alias — same rule as LLM_MODEL_WEIGHTS above. Surfaced on the
    # registry row so the Models page stays the single place that answers "which model is running".
    FAST_MODEL_WEIGHTS: str = ""

    EMBED_PROVIDER: Literal["fastembed", "vllm", "openai", "openai_compatible", "azure",
                            "huggingface", "sentence-transformers", "local"] = "fastembed"
    EMBED_MODEL: str = "nomic-ai/nomic-embed-text-v1.5"
    EMBED_DIMENSIONS: int = 768
    EMBED_BASE_URL: str = ""
    EMBED_BATCH_SIZE: int = 64
    # In-process embedder: most characters one ONNX run may hold (texts x longest text).
    FASTEMBED_MAX_BATCH_CHARS: int = 8000

    # ── Stores: NebulaGraph holds the graph, Qdrant the vectors ───────────
    VECTOR_STORE_PROVIDER: Literal["qdrant", "memory"] = "qdrant"
    QDRANT_URL: str = Field(default_factory=_default_qdrant_url)
    QDRANT_API_KEY: str = ""
    QDRANT_COLLECTION: str = "opencortyx"
    QDRANT_TIMEOUT_S: float = 30.0

    GRAPH_STORE_PROVIDER: Literal["nebulagraph", "memory"] = "nebulagraph"
    NEBULA_HOST: str = Field(default_factory=_default_nebula_host, validation_alias=AliasChoices("NEBULA_HOST", "NEBULA_GRAPH_HOST"))
    NEBULA_PORT: int = 9669
    NEBULA_USERNAME: str = Field("root", validation_alias=AliasChoices("NEBULA_USERNAME", "NEBULA_USER"))
    NEBULA_PASSWORD: str = "nebula"
    NEBULA_SPACE: str = "opencortyx"
    NEBULA_TIMEOUT_S: float = 30.0
    NEBULA_MAX_CONN_POOL_SIZE: int = 10

    # ── Auth ──────────────────────────────────────────────────────────────
    JWT_SECRET: str = Field("", description="set in .env")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60
    JWT_REFRESH_EXPIRE_DAYS: int = 14

    # ── Web ───────────────────────────────────────────────────────────────
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:3001"]
    LOG_LEVEL: str = "INFO"
    TESTING: bool = False

    # ── Web-search fallback (optional, trust layer) ───────────────────────
    TAVILY_API_KEY: str = ""

    # ── Request limits (cost + DoS guards on the expensive endpoints) ─────
    MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024
    MAX_REQUEST_BYTES: int = 50 * 1024 * 1024
    RATELIMIT_ENABLED: bool = True
    RATELIMIT_WINDOW_SECONDS: float = 60.0
    RATELIMIT_DEFAULT_PER_MIN: int = 240
    RATELIMIT_HEAVY_PER_MIN: int = 30

    # ── First run ─────────────────────────────────────────────────────────
    #: Empty by design: no shipped credential, so a fresh install goes through the Setup page.
    DEFAULT_ADMIN_PASSWORD: str = ""
    DEFAULT_ADMIN_EMAIL: str = "admin@opencortyx.local"

    # ── URLs advertised to integrations (what a client is told to call) ───
    OPENCORTYX_PUBLIC_API_URL: str = Field(
        "http://localhost:8000",
        validation_alias=AliasChoices("OPENCORTYX_PUBLIC_API_URL", "AXIOM_PUBLIC_API_URL"))
    MCP_PUBLIC_URL: str = "http://localhost:8001"
    #: Where THIS process reaches the MCP server, which is not where clients do: the public URL is a
    #: host-published port, and from inside a container that address is the container itself.
    MCP_INTERNAL_URL: str = "http://mcp:8001"

    # ── Context budget ────────────────────────────────────────────────────
    CONTEXT_HEADROOM_TOKENS: int = 2500
    #: 0 = derive from LLM_MAX_MODEL_LEN minus the headroom. A set value is still capped by it.
    CONTEXT_MAX_TOKENS: int = 0
    RETRIEVAL_WORKFLOW_CACHE: bool = False
    RETRIEVAL_INCLUDE_CITATIONS: bool = False
    #: Blank = use LLM_MODEL. Set to answer with a different model than the one doing extraction.
    GENERATION_MODEL: str = ""

    # ── Grounding verification (per-sentence check against retrieved context) ──
    GROUNDING_MAX_BLOCK_CHARS: int = 6000
    GROUNDING_EMBED_BATCH: int = 4
    GROUNDING_MAX_BLOCKS: int = 60

    # ── Python extractors — read ingest/extraction/pyextractor.py's SECURITY note first ──
    #: Author Python runs IN-PROCESS behind a guard that is not a real sandbox. Safe only because
    #: the extractor author IS the operator. Set to 0 to disable the mechanism entirely.
    OPENCORTYX_ALLOW_PYTHON_EXTRACTORS: bool = True
    OPENCORTYX_PYEXTRACTOR_TIMEOUT: float = 20.0
    OPENCORTYX_PYEXTRACTOR_MODEL_TIMEOUT: float = 180.0   # model-capable extractors call the LLM
    OPENCORTYX_PYEXTRACTOR_LLM_TIMEOUT: float = 120.0
    OPENCORTYX_PYEXTRACTOR_GRAPH_TIMEOUT: float = 60.0

    # ── Local filesystem connector (host path ↔ container mount) ──────────
    LOCAL_FS_HOST_ROOT: str = ""
    LOCAL_FS_MOUNT: str = ""

    # ── Misc tunables ─────────────────────────────────────────────────────
    EXTRACT_NODE_BATCH: int = 100          # nodes per extraction batch on the semistructured path
    CONTRADICTION_NLI_MODEL: str = ""      # blank = the capability's built-in default
    FASTEMBED_THREADS: int = 0             # 0 = let onnxruntime choose
    FASTEMBED_CACHE_DIR: str = ""          # blank = /cache when present, else the library default

    @field_validator("JWT_SECRET")
    @classmethod
    def _require_jwt_secret(cls, v):
        if not v or v in ("changeme", "generate_with_openssl_rand_hex_32"):
            raise ValueError("JWT_SECRET is not set. Run `bin/opencortyx init` or set a random value "
                             "(openssl rand -hex 32) in .env.")
        return v

    # The two validators below print rather than log ON PURPOSE: they run while V2Settings is being
    # constructed, which is before logging is configured, so a logger call here would either be
    # swallowed or would pull observability into config's import. See core/observability.py.
    @field_validator("LLM_PROVIDER", mode="before")
    @classmethod
    def _migrate_llm_provider(cls, v):
        if isinstance(v, str) and v.strip().lower() == "ollama":
            print("WARN: LLM_PROVIDER=ollama is no longer supported - using 'vllm'. Set "
                  "LLM_PROVIDER=vllm and LLM_BASE_URL=<your vLLM>/v1 in .env to silence this. "
                  "NOTE: LLM_MODEL must be vLLM's --served-model-name, not an Ollama tag.")
            return "vllm"
        return v

    @field_validator("EMBED_PROVIDER", mode="before")
    @classmethod
    def _migrate_embed_provider(cls, v):
        if isinstance(v, str) and v.strip().lower() == "ollama":
            print("WARN: EMBED_PROVIDER=ollama is no longer supported - using 'fastembed' "
                  "(in-process, no server, same 768-dim nomic family, no re-index needed).")
            return "fastembed"
        return v

    @field_validator("EMBED_MODEL", mode="before")
    @classmethod
    def _migrate_embed_model(cls, v):
        # The Ollama tag and the fastembed repo id are the SAME weights; only the naming differs.
        # Left as-is, the migrated fastembed provider would be handed a model name it cannot resolve.
        if isinstance(v, str) and v.strip() == "nomic-embed-text":
            return "nomic-ai/nomic-embed-text-v1.5"
        return v


@lru_cache
def get_settings() -> "V2Settings":
    # Required fields come from the environment; mypy (no pydantic plugin) cannot see that.
    return V2Settings()  # type: ignore[call-arg]


settings = get_settings()

# ── v2 substrate ─────────────────────────────────────────────────────────
# Defined in `domain.base` (which imports nothing) and re-exported here, because the raw-SQL call
# sites all reach for it as `core.config.V2_SCHEMA`.
from opencortyx.domain.base import V2_SCHEMA   # noqa: E402,F401
VECTOR_DIM = settings.EMBED_DIMENSIONS

# ── Retrieval / trust thresholds (tune against the BEAM eval set) ─────────
MIN_NODE_CONFIDENCE = 0.35       # drop nodes below this
ABSTAIN_SCORE_THRESHOLD = 0.30   # if best fused score < this → "I don't have that"
RERANK_TOP_K = 50                # rerank only the top-K fused candidates

LLM_MAX_MODEL_LEN = settings.LLM_MAX_MODEL_LEN

CONTEXT_HEADROOM_TOKENS = settings.CONTEXT_HEADROOM_TOKENS
_ctx_ceiling = max(1000, LLM_MAX_MODEL_LEN - CONTEXT_HEADROOM_TOKENS)
CONTEXT_MAX_TOKENS = min(settings.CONTEXT_MAX_TOKENS or _ctx_ceiling, _ctx_ceiling)

RETRIEVAL_WORKFLOW_CACHE = settings.RETRIEVAL_WORKFLOW_CACHE

RETRIEVAL_INCLUDE_CITATIONS = settings.RETRIEVAL_INCLUDE_CITATIONS

DEFAULT_RETRIEVE_K = 12

RETRIEVE_POOL_K = 100            # per-retriever fan-out (recall net; final top-k is cut later)
RERANK_POOL_N   = 120            # fused candidates the cross-encoder scores end-to-end
SESSION_DEDUP   = True

# ── Reranking cascade (Stage 0 recall = vector+BM25 fuse; Stage 1 cross-encoder; Stage 2 LLM) ──
RERANK_ENABLED = True
RERANK_TOP_N = 30                # candidates the cross-encoder scores (Stage 1)
LLM_RERANK_TOP_N = 8             # LLM only reranks the top few (Stage 2 — slow, last resort)
CROSS_ENCODER_MODEL = "BAAI/bge-reranker-v2-m3"   # stronger cross-encoder (distinguishes "owns X" from
# "discusses X" where bge-reranker-base was fooled by topical density). No re-indexing — scores (query,
# text) live at query time. Downloads ~2GB into the container on first use. Needs `sentence-transformers`.

GENERATION_MODEL = settings.GENERATION_MODEL or settings.LLM_MODEL
PROFILE_RERANK = {
    "fast":       ["cross"],
    "docs_qa":    ["cross"],
    "memory":     ["cross"],
    "structured": ["cross"],
    "research":   ["cross", "llm"],
    "precise":    ["cross", "llm"],
    "default":    ["cross"],
}

# ── Temporal / knowledge-update ──────────────────────────────────────────
RECENCY_WEIGHT = 0.15            # how much newer facts are boosted in ranking

# ── Working memory + scratchpad (LIGHT method) ───────────────────────────
WORKING_MEMORY_TURNS = 8        # recent turns always included for a conversation-scoped query
SCRATCHPAD_EVERY = 20          # rebuild the running scratchpad every N messages during load

# ── Ontology extraction mode: 'open' | 'guided' | 'hybrid' ───────────────
ONTOLOGY_MODE = "hybrid"

OPENAI_COMPATIBLE_PROVIDERS = frozenset({
    "vllm", "openai", "openai_compatible", "azure", "groq", "together", "deepinfra", "fireworks",
})

#: Providers that need an explicit base_url to be usable at all (no public default endpoint).
SELF_HOSTED_PROVIDERS = frozenset({"vllm", "openai_compatible"})

LOCAL_FREE_PROVIDERS = frozenset({
    "vllm", "openai_compatible", "fastembed", "onnx", "sentence-transformers",
    "sentence_transformers", "local", "st", "tei", "infinity",
})
