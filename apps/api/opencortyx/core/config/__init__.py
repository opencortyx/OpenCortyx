"""Configuration: the static settings and the live-tunable knobs, in one place.

Two mechanisms, deliberately kept apart and deliberately kept adjacent:

    env.py        `V2Settings` — read once from the environment at start-up. Anything that must be
                  fixed before the process serves traffic: DSNs, pool sizes, timeouts, provider
                  choices. Changing one means restarting.
    runtime.py    knobs an operator edits live from the Settings UI, stored in the database. Read
                  through `runtime` so a change takes effect without a deploy.

If you are adding a value, the question is which of those two it is. A third mechanism — reading
`os.environ` directly at the point of use — is not one of them, however tempting: it is untyped,
undocumented in `.env.example`, and invisible to the Settings UI.

`from ...core.config import settings` is unchanged; this was one module until the runtime knobs
outgrew it.
"""
from opencortyx.core.config import runtime
from opencortyx.core.config.env import (
    lru_cache,
    Literal,
    AliasChoices,
    Field,
    field_validator,
    BaseSettings,
    SettingsConfigDict,
    V2Settings,
    get_settings,
    settings,
    V2_SCHEMA,
    VECTOR_DIM,
    MIN_NODE_CONFIDENCE,
    ABSTAIN_SCORE_THRESHOLD,
    RERANK_TOP_K,
    LLM_MAX_MODEL_LEN,
    CONTEXT_HEADROOM_TOKENS,
    CONTEXT_MAX_TOKENS,
    RETRIEVAL_WORKFLOW_CACHE,
    RETRIEVAL_INCLUDE_CITATIONS,
    DEFAULT_RETRIEVE_K,
    RETRIEVE_POOL_K,
    RERANK_POOL_N,
    SESSION_DEDUP,
    RERANK_ENABLED,
    RERANK_TOP_N,
    LLM_RERANK_TOP_N,
    CROSS_ENCODER_MODEL,
    BUILTIN_EMBED_PROVIDER,
    BUILTIN_EMBED_MODEL,
    BUILTIN_RERANK_PROVIDER,
    BUILTIN_RERANK_MODEL,
    BUILTIN_ENTITIES_MODEL,
    BUILTIN_GENERATION_PROVIDER,
    BUILTIN_GENERATION_MODEL,
    SPARSE_MODEL,
    PROFILE_RERANK,
    RECENCY_WEIGHT,
    WORKING_MEMORY_TURNS,
    SCRATCHPAD_EVERY,
    ONTOLOGY_MODE,
    OPENAI_COMPATIBLE_PROVIDERS,
    SELF_HOSTED_PROVIDERS,
    LOCAL_LLM_PROVIDERS,
    LOCAL_FREE_PROVIDERS,
)

__all__ = [
    "runtime",
    'lru_cache', 'Literal', 'AliasChoices', 'Field', 'field_validator', 'BaseSettings', 'SettingsConfigDict', 'V2Settings', 'get_settings', 'settings', 'V2_SCHEMA', 'VECTOR_DIM', 'MIN_NODE_CONFIDENCE', 'ABSTAIN_SCORE_THRESHOLD', 'RERANK_TOP_K', 'LLM_MAX_MODEL_LEN', 'CONTEXT_HEADROOM_TOKENS', 'CONTEXT_MAX_TOKENS', 'RETRIEVAL_WORKFLOW_CACHE', 'RETRIEVAL_INCLUDE_CITATIONS', 'DEFAULT_RETRIEVE_K', 'RETRIEVE_POOL_K', 'RERANK_POOL_N', 'SESSION_DEDUP', 'RERANK_ENABLED', 'RERANK_TOP_N', 'LLM_RERANK_TOP_N', 'CROSS_ENCODER_MODEL', 'BUILTIN_EMBED_PROVIDER', 'BUILTIN_EMBED_MODEL', 'BUILTIN_RERANK_PROVIDER', 'BUILTIN_RERANK_MODEL', 'BUILTIN_ENTITIES_MODEL', 'BUILTIN_GENERATION_PROVIDER', 'BUILTIN_GENERATION_MODEL', 'SPARSE_MODEL', 'PROFILE_RERANK', 'RECENCY_WEIGHT', 'WORKING_MEMORY_TURNS', 'SCRATCHPAD_EVERY', 'ONTOLOGY_MODE', 'OPENAI_COMPATIBLE_PROVIDERS', 'SELF_HOSTED_PROVIDERS', 'LOCAL_LLM_PROVIDERS', 'LOCAL_FREE_PROVIDERS',
]
