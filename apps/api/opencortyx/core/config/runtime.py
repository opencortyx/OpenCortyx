"""Runtime-tunable performance knobs — editable live from the Settings UI.

Only knobs that can take effect WITHOUT a restart or reindex live here (parallelism,
cache enable/TTL, retrieval defaults). They are read on the hot path through the typed accessors
below and persisted in the `runtime_setting` table so changes survive restarts. Build/infra knobs
(HNSW build params, pool size, replica URL, vector dim) remain env-only — the Settings UI shows
those read-only with a "restart/reindex required" note.
"""
from typing import Any

import opencortyx.core.config.env as cfg
from opencortyx.core.config.env import settings

# key → default; defaults come from env settings so behaviour is unchanged until overridden.
_DEFAULTS = {
    "retrieval_max_parallel": settings.RETRIEVAL_MAX_PARALLEL,
    "query_cache_enabled": settings.QUERY_CACHE_ENABLED,
    "query_cache_ttl": settings.QUERY_CACHE_TTL,
    "embed_cache_enabled": settings.EMBED_CACHE_ENABLED,
    "abstain_threshold": cfg.ABSTAIN_SCORE_THRESHOLD,
    "default_retrieve_k": cfg.DEFAULT_RETRIEVE_K,
    "ingest_concurrency": settings.CELERY_WORKER_CONCURRENCY,
    "allow_local_files": False,   # permission: on-prem connectors may read the server's local filesystem
    "capture_agent_sessions": False,  # permission: log connected agents' OpenCortyx interactions as sessions
    "graph_hub_traversal": True,   # retrieval: pull the subtree of the entity/hub a question is about
    "crag_min_citations": 3,       # retrieval: keep an answer when this many citations were grounded
    "grounding_mode": "cosine",    # retrieval: how answers are verified (cosine similarity or LLM judge)
    "grounding_threshold": 0.55,   # cosine grounding: min answer-sentence↔source similarity to be "grounded"
    "adaptive_routing": True,      # retrieval: auto-scope a query to the matching entity's collection
    "default_rerank": "cross",     # retrieval: default rerank cascade when the caller does not set one
    "context_max_tokens": cfg.CONTEXT_MAX_TOKENS,
    "retrieve_pool_k": cfg.RETRIEVE_POOL_K,  # retrieval: per-retriever fan-out (the wide recall net)
    "rerank_pool_n": cfg.RERANK_POOL_N,      # retrieval: how many fused candidates the cross-encoder scores
    "session_dedup": cfg.SESSION_DEDUP,      # retrieval: collapse retrieved turns to one slot per session
    "business_context_framing": True,  # retrieval: prepend always-on org/business context as a frame
    "capture_assistant_chats": True,   # auto-save your real-time Assistant (web) chats as sessions
    "ai_overviews": True,              # ingest: generate the AI asset overview (exec summary + profile sections + NL description)
    # ── Memory Accuracy (LongMemEval) ─────────────────────────────────────
    "memory_resolution": True,         # ingest: detect contradictions → SUPERSEDES / EVOLVED_FROM
    "contradiction_use_rules": True,   # LAYER 3a: decide numbers/units/money/dates by arithmetic
    "contradiction_use_llm": True,     # LLM judge tier; off = rules + duplicates + entailment only
    "contradiction_support": True,     # LAYER 7: record agreement and reinforce (bounded, independent)
    "contradiction_support_step": 0.1,
    "contradiction_support_cap": 0.95,
    "contradiction_judge_model": "",   # Model Registry ref for the judge; blank = the utility role default
    "contradiction_text_fields": ["text", "statement", "summary", "title", "value"],
    "contradiction_time_fields": ["valid_from", "effective_date", "as_of", "ts", "date", "timestamp"],
    "contradiction_reason_fields": ["reason", "rationale", "because", "note"],
    "contradiction_key_fields": [],    # optional exact-match subject keys, e.g. ["customer_id"]
    "contradiction_types": [],         # [] = compare a statement against others of its own type
    "contradiction_similarity": 0.82,  # min similarity for a candidate comparison
    "contradiction_candidates": 8,     # comparisons per written statement (the cost control)
    "contradiction_hub_degree": 25,    # a holder busier than this is a CATEGORY, not a subject
    "contradiction_auto_apply": 0.75,  # below this confidence a contradiction is proposed, not applied
    "resolution_mode": "latest_wins",  # winner policy: 'latest_wins' | 'trust_weighted'
    "resolution_review": False,        # propose supersessions for human approval instead of auto-applying
    "resolution_trust_boost": 0.5,     # how much a human-validated / user source outweighs others
    "simhash_versioning": True,        # ingest: SimHash LSH near-duplicate doc detection → SUPERSEDES
    "simhash_max_distance": 5,         # max Hamming distance (of 64 bits) to treat docs as versions
    "simhash_version_review": True,    # a version match is PROPOSED for approval, not auto-applied
    "temporal_routing": True,          # retrieval: detect time-intent questions → order by date
    "llm_concurrency": 16,             # ingest: how many extraction LLM calls run in parallel per document
    "extraction_call_groups": [],      # ingest: ordered call groups — [{"name": str, "extractors": [template names]}]
    "extraction_full_coverage": False,
    "extraction_max_chunks": 400,
    "structured_default_mode": "row",       # 'row' (one node per row) | 'catalog' (schema only)
    "structured_default_max_rows": 1000,    # 0 = every row
    "defer_extraction": True,
    "defer_embedding": True,
    "embed_concurrency": 8,
    # Number of nodes/texts embedded per batch. 16-32 for laptops (constant low RAM), 128-256 for GPU/high-end servers (max throughput).
    "embed_batch_size": 32,
}

# UI rendering schema for the editable knobs (label / kind / bounds / help / group).
SCHEMA: list[dict[str, Any]] = [
    # The two Workspace fields belong to the signed-in user's workspace, not to the install: they are
    # stored per workspace_id (`knowledge.workspace`) and the config route reads and writes them there.
    {"key": "workspace_name", "label": "Workspace name", "kind": "text", "group": "Workspace",
     "scope": "workspace",
     "help": "Your name in Person mode, your team's or organisation's in Team mode. It names the central "
     "node at the top of the graph, which every source and asset hangs from. Blank = a made-up name."},
    {"key": "retrieval_max_parallel", "label": "Parallel retrievers", "kind": "int", "min": 1, "max": 32,
     "group": "Parallelism", "help": "How many retrievers run concurrently per query "
     "(asyncio.gather). Raising this cuts query latency until Postgres or the embedder saturates."},
    {"key": "context_max_tokens", "label": "Answer context budget (tokens)", "kind": "int",
     "min": 500, "max": 128000, "group": "Retrieval",
     "help": "How much retrieved context the answer is built from. This is NOT the model's context "
     "window - that is a server flag shown under Read-only - and it must stay below it or the "
     "prompt is trimmed before the model sees it. More context is not automatically better: on "
     "this corpus retrieval packs 94% of the gold evidence at 6,000 tokens, but the small local "
     "reader answered 78% correct at ~2,500 tokens and only 20% at 6,000, replying 'not in "
     "context' three quarters of the time. Raise it when you move to a larger model."},
    {"key": "default_retrieve_k", "label": "Retrieve top-k", "kind": "int", "min": 1, "max": 100,
     "group": "Latency", "help": "Default candidates pulled per retriever when the query doesn't set k."},
    {"key": "abstain_threshold", "label": "Abstain threshold", "kind": "float", "min": 0.0, "max": 1.0,
     "group": "Quality", "help": "Min relevance before answering; higher = abstains more (fewer hallucinations)."},
    {"key": "query_cache_enabled", "label": "Query cache", "kind": "bool",
     "group": "Caching", "help": "Cache assembled context packages for identical requests."},
    {"key": "query_cache_ttl", "label": "Query cache TTL (s)", "kind": "int", "min": 0, "max": 86400,
     "group": "Caching", "help": "Seconds a cached result stays fresh."},
    {"key": "embed_cache_enabled", "label": "Embedding cache", "kind": "bool",
     "group": "Caching", "help": "Cache query embeddings (skip recompute for repeat queries)."},
    {"key": "ingest_concurrency", "label": "Ingestion concurrency", "kind": "int", "min": 1, "max": 64,
     "group": "Parallelism", "help": "How many ingestion jobs run in parallel (e.g. schemas in one "
     "multi-schema ingestion). Live-resizes the worker pool; also the baseline on worker restart."},
    {"key": "allow_local_files", "label": "Allow local file access", "kind": "bool",
     "group": "Local files", "help": "Permission: let on-prem connectors read files from the server's "
     "local filesystem (the mounted host folder). Off by default for safety — turn on only if you trust "
     "the folders being ingested. Browser bulk-upload does not need this."},
    {"key": "capture_agent_sessions", "label": "Capture agent sessions", "kind": "bool",
     "group": "Agents", "help": "Permission: record what connected agents (Claude, Cursor, any MCP "
     "client) ASK and DO through OpenCortyx — their queries, answers, and writes — as live sessions on "
     "the Conversations page. Off by default. Only the agent's interactions with OpenCortyx are stored, "
     "scoped to this workspace; never the agent's private chat and never sent anywhere external."},
    {"key": "capture_assistant_chats", "label": "Capture Assistant chats", "kind": "bool",
     "group": "Agents", "help": "Auto-save your real-time chats with the in-app Assistant as a rolling "
     "daily session on the Conversations page (each question + grounded answer + the sources it used). "
     "On by default. Stored in this workspace only; queryable by the Assistant and visible in lineage."},
    {"key": "graph_hub_traversal", "label": "Graph hub traversal", "kind": "bool",
     "group": "Retrieval", "help": "Pull the connected subtree of the entity or hub a question is about "
     "(for example a person's aspects, or an org's regions and branches), so entity questions surface "
     "their attached nodes. Recommended on."},
    {"key": "crag_min_citations", "label": "CRAG min citations", "kind": "int", "min": 0, "max": 50,
     "group": "Retrieval", "help": "The relevance gate only abstains when fewer than this many citations "
     "were grounded, so a flaky verdict cannot bury a good retrieval. Higher = stricter (abstains more)."},
    {"key": "grounding_mode", "label": "Grounding verification", "kind": "select",
     "options": ["cosine", "entailment"], "group": "Retrieval",
     "help": "How answers are checked against sources. 'cosine' is fast embedding similarity. "
     "'entailment' uses an LLM judge to catch wrong-but-on-topic sentences (needs a capable model)."},
    {"key": "grounding_threshold", "label": "Grounding similarity", "kind": "float", "min": 0.0, "max": 1.0,
     "group": "Retrieval", "help": "In cosine grounding mode, the minimum similarity between an answer "
     "sentence and a source passage for that sentence to count as grounded. Higher = stricter (the "
     "answer must hug the sources more closely, so more borderline answers abstain). 0.55 is a good start."},
    {"key": "adaptive_routing", "label": "Adaptive auto-scope", "kind": "bool",
     "group": "Retrieval", "help": "When a question clearly concerns one entity or domain, scope "
     "retrieval to that entity's collection automatically, so a large unrelated corpus cannot dilute "
     "the answer. Only applies when the caller did not pass an explicit scope."},
    {"key": "default_rerank", "label": "Default reranker", "kind": "select",
     "options": ["none", "cross", "cross+llm"], "group": "Retrieval",
     "help": "Rerank cascade used when a request does not specify one. 'cross' is a cross-encoder; "
     "'cross+llm' adds an LLM listwise pass (slower, higher precision)."},
    {"key": "business_context_framing", "label": "Business-context framing", "kind": "bool",
     "group": "Retrieval", "help": "Prepend always-on org/business context (mission, domain, standing "
     "assumptions, authored in Manual Curation) to the assembled context as a frame for every answer."},
    {"key": "retrieve_pool_k", "label": "Recall pool size", "kind": "int", "min": 10, "max": 500,
     "group": "Retrieval", "help": "Per-retriever fan-out — the WIDE recall net. Higher = better recall "
     "(a weak embedder is compensated by a bigger net), more to rerank. The final top-k is cut after "
     "reranking, so this doesn't change how many results you see — only how many candidates compete."},
    {"key": "rerank_pool_n", "label": "Rerank pool size", "kind": "int", "min": 10, "max": 500,
     "group": "Retrieval", "help": "How many fused candidates the cross-encoder scores end-to-end. Keep "
     ">= the recall pool so a node the fusion buries deep can still be pulled up by the reranker."},
    {"key": "session_dedup", "label": "Collapse by session", "kind": "bool",
     "group": "Retrieval", "help": "Collapse retrieved turns to one slot per parent session (with a small "
     "bonus per extra matching turn), so near-duplicate turns from one session don't crowd out other "
     "sessions' evidence. The multi-session recall lever."},
    {"key": "mode", "label": "Workspace mode", "kind": "select", "options": ["person", "team"],
     "group": "Workspace", "scope": "workspace",
     "help": "'person': the workspace is you — the central node is a Person and the personal Person "
     "ontology is the default schema. 'team': the workspace is a team or organisation — the central "
     "node is an Organization and the data-catalogue ontology is the default."},
    {"key": "ai_overviews", "label": "AI-generated asset overviews", "kind": "bool",
     "group": "Ingestion", "help": "Generate the AI catalogue overview for each asset at ingest — the "
     "Executive Summary, Business Purpose, NL Description and the profile sections shown on the asset "
     "page (~9 LLM calls per asset). Off = no AI overview is generated at ingest; you can still generate "
     "one on demand from the asset page."},
    {"key": "memory_resolution", "label": "Active memory resolution", "kind": "bool",
     "group": "Memory Accuracy", "help": "When new information contradicts old, detect it and resolve: "
     "a reasoned/time-based change is logged as an EVOLUTION (both kept, newer wins on recency); a flat "
     "contradiction SUPERSEDES the old claim (marked superseded, confidence zeroed) so stale facts can "
     "never resurface. Off = keep every claim as-is."},
    {"key": "contradiction_use_rules", "label": "Check values by rule first", "kind": "bool",
     "group": "Memory Accuracy", "help": "Compare structured values — numbers, units, money, dates, "
     "yes/no — by arithmetic before any model is asked, so '90 days' vs '120 days' is certain and "
     "free. Rules never guess: what they cannot settle falls through to the judge."},
    {"key": "contradiction_support", "label": "Record agreement too", "kind": "bool",
     "group": "Memory Accuracy", "help": "When two statements AGREE, record it and raise confidence a "
     "little. Repeats of the same source do not count — only independent sources corroborate."},
    {"key": "contradiction_support_step", "label": "How much agreement adds", "kind": "float",
     "group": "Memory Accuracy", "min": 0.0, "max": 0.5,
     "help": "Each independent corroboration closes this fraction of the gap to the cap — so "
     "confidence rises fast at first and then barely, and repetition never manufactures certainty."},
    {"key": "contradiction_support_cap", "label": "Most that agreement can reach", "kind": "float",
     "group": "Memory Accuracy", "min": 0.5, "max": 1.0,
     "help": "Corroboration alone never takes a statement above this, so a human-validated fact "
     "always outranks a much-repeated one."},
    {"key": "contradiction_use_llm", "label": "Use the LLM judge", "kind": "bool",
     "group": "Memory Accuracy", "help": "On: pairs the cheap tiers cannot settle go to the model from "
     "your Model Registry (the 'utility' role). Off: only exact/near-duplicate detection and the "
     "entailment model run — deterministic and cheaper, and repeats are still caught."},
    {"key": "contradiction_judge_model", "label": "Judge model (optional)", "kind": "str",
     "group": "Memory Accuracy", "help": "A model name or id from Settings → Models to judge with "
     "instead of the workspace default. Blank = the default utility model."},
    {"key": "contradiction_text_fields", "label": "Where the statement text lives", "kind": "json",
     "group": "Memory Accuracy", "help": "Property names a statement may be stored under, in order. "
     "First one present wins; the node label is the last resort."},
    {"key": "contradiction_time_fields", "label": "Where the date lives", "kind": "json",
     "group": "Memory Accuracy", "help": "Property names carrying when a statement became true. None "
     "present is fine — the date is unknown, and an undated statement never beats a dated one on recency."},
    {"key": "contradiction_reason_fields", "label": "Where a reason lives", "kind": "json",
     "group": "Memory Accuracy", "help": "Property names explaining WHY something changed. A stated "
     "reason makes a disagreement an evolution, so both statements are kept."},
    {"key": "contradiction_key_fields", "label": "Exact-match keys (optional)", "kind": "json",
     "group": "Memory Accuracy", "help": "Property names whose equal values mean the same subject "
     "(e.g. customer_id). Precision only — detection works without them."},
    {"key": "contradiction_types", "label": "Limit to these node types (optional)", "kind": "json",
     "group": "Memory Accuracy", "help": "Blank = compare a statement against others of its own type."},
    {"key": "contradiction_similarity", "label": "How alike counts as the same thing", "kind": "float",
     "min": 0.5, "max": 0.99, "group": "Memory Accuracy", "help": "Minimum similarity for a statement "
     "to be compared at all. Higher = fewer, closer comparisons."},
    {"key": "contradiction_candidates", "label": "Comparisons per statement", "kind": "int",
     "min": 1, "max": 50, "group": "Memory Accuracy", "help": "How many neighbours each new statement "
     "is checked against — the main cost control."},
    {"key": "contradiction_hub_degree", "label": "When a link is too busy to mean anything",
     "kind": "int", "group": "Memory Accuracy", "min": 2, "max": 1000,
     "help": "Statements attached to the same thing are compared. But some things everything is "
     "attached to — a date, a category — and those say nothing about being the same subject. A "
     "connector busier than this is treated as a category and ignored."},
    {"key": "contradiction_auto_apply", "label": "Confidence needed to apply", "kind": "float",
     "min": 0.0, "max": 1.0, "group": "Memory Accuracy", "help": "Below this, a contradiction is "
     "proposed for review instead of applied."},
    {"key": "resolution_mode", "label": "Resolution winner policy", "kind": "select",
     "options": ["latest_wins", "trust_weighted"], "group": "Memory Accuracy",
     "help": "Which claim wins a flat contradiction. 'latest_wins' favours the newer fact; "
     "'trust_weighted' favours the higher-confidence / human-validated source."},
    {"key": "resolution_review", "label": "Review supersessions before applying", "kind": "bool",
     "group": "Memory Accuracy", "help": "When ON, a flat contradiction is PROPOSED (it draws "
     "CONTRADICTS and flags the loser as 'proposed') and waits for you to approve it in the review "
     "queue before the old claim is actually retired. When OFF (default), supersessions apply "
     "automatically. Evolutions (reasoned/time changes) keep both claims either way."},
    {"key": "resolution_trust_boost", "label": "Trust boost (validated sources)", "kind": "float",
     "min": 0.0, "max": 2.0, "group": "Memory Accuracy", "help": "Under 'trust_weighted', how much "
     "extra weight a human-validated or user-entered claim gets over a plain extracted one when "
     "picking the winner. 0 = no boost; 0.5 = default."},
    {"key": "simhash_versioning", "label": "Document version detection", "kind": "bool",
     "group": "Memory Accuracy", "help": "Detect when an uploaded document is a near-duplicate 'v2' of "
     "an existing one (SimHash fingerprint, tenant-scoped) and link them with SUPERSEDES so retrieval "
     "ignores the old version. O(1)-style check — no heavy vector scan."},
    {"key": "simhash_max_distance", "label": "Version match sensitivity", "kind": "int", "min": 0, "max": 16,
     "group": "Memory Accuracy", "help": "Max bit-difference (out of 64) between two document "
     "fingerprints to treat the newer as a version of the older. Lower = stricter (only near-identical "
     "docs match); higher = looser. ~5 catches a typical lightly-edited 'v2'; unrelated docs sit ~25 "
     "bits apart so there is wide margin."},
    {"key": "simhash_version_review", "label": "Review version matches first", "kind": "bool",
     "group": "Memory Accuracy", "help": "When a document looks like a new version of an existing one, "
     "propose it in the review queue instead of retiring the old document straight away. The old "
     "document stays searchable until you approve."},
    {"key": "temporal_routing", "label": "Temporal query routing", "kind": "bool",
     "group": "Memory Accuracy", "help": "Detect time-based questions ('what was the FIRST project I "
     "mentioned?', 'earliest', 'last year') and order results by date instead of pure semantic "
     "similarity, so 'first/last' questions return the right item."},
    # ── Extraction efficiency ─────────────────────────────────────────────
    {"key": "llm_concurrency", "label": "Parallel extraction LLM calls", "kind": "int",
     "min": 1, "max": 32, "group": "Parallelism", "help": "How many extraction LLM calls run concurrently per document. "
     "A batching model server runs concurrent requests together, so raising this speeds ingestion "
     "up roughly linearly until the model server saturates. Accuracy is unaffected — the exact "
     "same prompts run, just in parallel."},
    {"key": "structured_default_mode", "label": "Default table reading mode", "kind": "select",
     "options": ["row", "catalog"], "group": "Structured",
     "help": "How a tabular file with NO pipeline assigned is read. 'row' makes one node per row "
     "(what you usually want when you drop a CSV in); 'catalog' records only the schema. A "
     "pipeline's own Step-1 reading rules always override this."},
    {"key": "structured_default_max_rows", "label": "Default rows sampled per table", "kind": "int",
     "min": 0, "max": 100000000, "group": "Structured",
     "help": "How many rows become nodes when no pipeline says otherwise. 0 = every row. A sample "
     "gives the same schema, edge shapes and retrieval behaviour as the full table at a fraction "
     "of the cost; raise it or set 0 when you need complete data."},
    {"key": "defer_embedding", "label": "Defer embedding to its own queue", "kind": "bool",
     "group": "Extraction",
     "help": "Commit chunks without their vectors and embed them in the background. An embed call "
     "is ~110ms, so embedding inline dominates ingestion latency. Keyword search covers a chunk "
     "immediately; vector search picks it up when its embedding lands, usually seconds later."},
    {"key": "defer_extraction", "label": "Defer extraction to its own queue", "kind": "bool",
     "group": "Extraction",
     "help": "Commit chunks and embeddings first, then run LLM extraction on a separate queue. The "
     "document becomes searchable immediately and one large file stops blocking every other "
     "upload behind it. Turn off only if you need the graph fully built before the run reports done."},
    {"key": "embed_concurrency", "label": "Parallel embedding calls", "kind": "int",
     "min": 1, "max": 64, "group": "Parallelism",
     "help": "How many embeddings are computed at once within a batch. Measured against an HTTP "
     "embedder: 8.3/s at 1, 31.9/s at 4, 64.8/s at 16 — an embed waits on the model server rather "
     "than on local CPU, so concurrency buys close to linear throughput until it saturates."},
    {"key": "embed_batch_size", "label": "Embedding batch size", "kind": "int",
     "min": 8, "max": 512, "group": "Parallelism",
     "help": "Number of nodes/texts embedded per batch pass. Use 16-32 on laptops for bounded O(1) RAM "
     "and 128-256 on GPU / high-end servers for maximum vectorized throughput."},
    {"key": "extraction_full_coverage", "label": "Full coverage — no chunk cap, no call folding",
     "kind": "bool", "group": "Extraction",
     "help": "Every chunk, node and turn reaches every extractor. While on, two settings below are "
     "ignored: the per-document chunk ceiling (which otherwise skips everything past it, silently) "
     "and call groups (so each extractor makes its own call). Parallelism is NOT affected — "
     "extractors still run one after another, and 'Parallel extraction LLM calls' still controls how "
     "many chunks are in flight within the current one. Use this when a corpus must be complete; "
     "leave it off when a very large document must simply finish."},
    {"key": "extraction_max_chunks", "label": "Max extracted chunks per document", "kind": "int",
     "min": 0, "max": 100000, "group": "Extraction",
     "help": "Ceiling on how many chunks of ONE document get LLM extraction. Extraction cost is "
     "chunks x active extractors, so a very large file can otherwise schedule hundreds of "
     "thousands of LLM calls and occupy the worker for hours, delaying every other ingestion. "
     "Chunks past the ceiling are still created and embedded — retrieval still covers the whole "
     "document — only the expensive per-chunk extraction stops. 0 = unlimited."},
    {"key": "extraction_call_groups", "label": "Extraction call groups", "kind": "json",
     "group": "Extraction", "help": "Ordered groups of extractors folded into ONE LLM call per chunk. "
     "Group order = dependency order (a later group runs only after earlier groups' outputs are in the "
     "graph). Extractors not in any group run as their own call. Configured visually on the Extraction "
     "settings page."},
]

# infra / build-time knobs — read-only in the UI (need restart or reindex)
def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back when unset or malformed."""
    import os
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return int(default)


def readonly_snapshot() -> dict:
    return {
        "llm_max_model_len": settings.LLM_MAX_MODEL_LEN,
        "query_cache_size": settings.QUERY_CACHE_SIZE,
        "embed_cache_size": settings.EMBED_CACHE_SIZE,
        "ingest_worker_pool": _env_int("CELERY_WORKER_CONCURRENCY",
                                       settings.CELERY_WORKER_CONCURRENCY),
        "extraction_worker_pool": _env_int("EXTRACTION_WORKER_CONCURRENCY", 4),
        "embedding_worker_pool": _env_int("EMBEDDING_WORKER_CONCURRENCY", 4),
        "db_pool_size": settings.DB_POOL_SIZE,
        "db_max_overflow": settings.DB_MAX_OVERFLOW,
        "read_replica_configured": bool(settings.DATABASE_READ_URL),
        "vector_dim": settings.EMBED_DIMENSIONS,
        **_compute_snapshot(),
    }


def _compute_snapshot() -> dict:
    """What this container is allowed to use.

    Decided before the process starts — the memory ceiling is a container limit — so not editable
    here. Reported because this is the screen an operator is on when a worker is being killed. Which
    MODELS run is the Model Registry's to say; the config route adds them per workspace.

    Only THIS container's memory is reported. Showing the whole stack would mean handing the API the
    Docker socket, which is root on the host; `scripts/check-memory-budget.py` does that job from
    outside, where the privilege belongs.
    """
    from opencortyx.core.observability import container_memory

    out: dict = {}
    mem = container_memory()
    if mem:
        used, limit = mem["used_bytes"], mem["limit_bytes"]        # both bytes — keep them that way
        out["api_memory_in_use"] = f"{used / 1024 ** 2:.0f} MiB"
        out["api_memory_ceiling"] = f"{limit / 1024 ** 2:.0f} MiB" if limit else "unlimited"
        if limit:
            out["api_memory_headroom"] = f"{100 - (used / limit * 100):.0f}%"
    return out

_overrides: dict = {}     # loaded from DB; empty → use _DEFAULTS


def get(key):
    return _overrides.get(key, _DEFAULTS.get(key))


# typed hot-path accessors
def query_cache_enabled() -> bool: return bool(get("query_cache_enabled"))
def query_cache_ttl() -> int: return int(get("query_cache_ttl"))
def embed_cache_enabled() -> bool: return bool(get("embed_cache_enabled"))
def abstain_threshold() -> float: return float(get("abstain_threshold"))
def ingest_concurrency() -> int: return max(1, int(get("ingest_concurrency")))
def allow_local_files() -> bool: return bool(get("allow_local_files"))
def capture_agent_sessions() -> bool: return bool(get("capture_agent_sessions"))
def graph_hub_traversal() -> bool: return bool(get("graph_hub_traversal"))
def crag_min_citations() -> int: return max(0, int(get("crag_min_citations")))
def grounding_mode() -> str: return str(get("grounding_mode") or "cosine")
def grounding_threshold() -> float: return max(0.0, min(1.0, float(get("grounding_threshold"))))
def adaptive_routing() -> bool: return bool(get("adaptive_routing"))
def default_rerank() -> str: return str(get("default_rerank") or "cross")
def retrieve_pool_k() -> int: return max(1, int(get("retrieve_pool_k")))
def rerank_pool_n() -> int: return max(1, int(get("rerank_pool_n")))
def session_dedup() -> bool: return bool(get("session_dedup"))
def business_context_framing() -> bool: return bool(get("business_context_framing"))
def capture_assistant_chats() -> bool: return bool(get("capture_assistant_chats"))
def ai_overviews() -> bool: return bool(get("ai_overviews"))
def memory_resolution() -> bool: return bool(get("memory_resolution"))
def resolution_mode() -> str: return str(get("resolution_mode") or "latest_wins")
def resolution_review() -> bool: return bool(get("resolution_review"))
def resolution_trust_boost() -> float: return max(0.0, float(get("resolution_trust_boost")))
def simhash_versioning() -> bool: return bool(get("simhash_versioning"))
def simhash_max_distance() -> int: return max(0, int(get("simhash_max_distance")))
def simhash_version_review() -> bool: return bool(get("simhash_version_review"))
def temporal_routing() -> bool: return bool(get("temporal_routing"))
def extraction_full_coverage() -> bool: return bool(get("extraction_full_coverage"))


def llm_concurrency() -> int:
    return max(1, min(32, int(get("llm_concurrency"))))


def extraction_max_chunks() -> int:
    if extraction_full_coverage():
        return 0                                   # 0 = no ceiling: nothing is skipped
    return max(0, int(get("extraction_max_chunks") or 0))


def structured_default_mode() -> str:
    v = str(get("structured_default_mode") or "row").strip().lower()
    return v if v in ("row", "catalog", "auto") else "row"


def structured_default_max_rows() -> int:
    return max(0, int(get("structured_default_max_rows") or 0))


def defer_extraction() -> bool: return bool(get("defer_extraction"))
def defer_embedding() -> bool: return bool(get("defer_embedding"))
def embed_concurrency() -> int: return max(1, min(64, int(get("embed_concurrency") or 1)))
def embed_batch_size() -> int: return max(8, min(512, int(get("embed_batch_size") or 32)))
def extraction_call_groups() -> list:
    """Sanitized ordered call groups: [{"name": str, "extractors": [str, ...]}, ...]."""
    if extraction_full_coverage():
        return []                                  # no folding: each extractor is its own call
    v = get("extraction_call_groups")
    out: list = []
    for g in (v if isinstance(v, list) else []):
        if isinstance(g, dict) and isinstance(g.get("extractors"), list):
            names = [str(x) for x in g["extractors"] if x]
            if names:
                out.append({"name": str(g.get("name") or f"Call {len(out) + 1}"), "extractors": names})
    return out


def current() -> dict:
    return {k: get(k) for k in _DEFAULTS}


def _coerce(key, v):
    spec = next((s for s in SCHEMA if s["key"] == key), None)
    kind = spec["kind"] if spec else "str"
    try:
        if kind == "int":
            return int(v)
        if kind == "float":
            return float(v)
        if kind == "bool":
            return v in (True, "true", "True", 1, "1")
        if kind == "select":
            opts = (spec or {}).get("options") or []
            return v if v in opts else _DEFAULTS.get(key)
        if kind == "json":                     # structured value (list/dict) — parse strings, pass through
            if isinstance(v, (list, dict)):
                return v
            import json as _json
            return _json.loads(v) if isinstance(v, str) and v.strip() else _DEFAULTS.get(key)
    except Exception:
        return _DEFAULTS.get(key)
    return v


_RELOAD_TTL_SECONDS = 5.0
_loaded_at = 0.0


async def load(force: bool = False) -> dict:
    """Load persisted overrides into memory.

    TTL-cached. `init_v2_schema` became a per-process ONCE operation (it does expensive idempotent
    DDL), and this used to be called only from there — which silently made every knob restart-only
    on the worker: changing call groups in Settings had no effect until the container bounced.
    Loading here on a short TTL keeps the knobs genuinely live while keeping the DDL once-only.
    """
    import time as _time
    global _loaded_at
    if not force and _overrides and (_time.monotonic() - _loaded_at) < _RELOAD_TTL_SECONDS:
        return current()
    _loaded_at = _time.monotonic()
    from sqlalchemy import select
    from opencortyx.domain import RuntimeSetting
    from opencortyx.core.db import V2Session
    try:
        async with V2Session() as db:
            rows = (await db.execute(select(RuntimeSetting))).scalars().all()
        _overrides.clear()
        for r in rows:
            if r.key in _DEFAULTS:
                _overrides[r.key] = _coerce(r.key, (r.value or {}).get("v"))
        _apply_side_effects()
    except Exception:
        pass
    return current()


def changes(before: dict, after: dict, keys) -> dict:
    """{key: {"from", "to"}} for the submitted keys whose value actually moved. A settings form
    submits every field; the audit trail and the side effects care about the ones that changed."""
    return {k: {"from": before.get(k), "to": after.get(k)}
            for k in (keys or []) if k in after and before.get(k) != after.get(k)}


class InvalidSetting(ValueError):
    """One or more submitted values break their field's declared kind, range or options."""

    def __init__(self, problems: dict):
        self.problems = problems
        super().__init__("; ".join(f"{k}: {why}" for k, why in problems.items()))


def _validated(key, v, spec: dict | None):
    """The value a field's declaration allows, or ValueError saying why not. The load path stays
    lenient (`_coerce`) so a stored value never stops a process from starting; a WRITE is where a
    wrong value is refused, before every worker picks it up."""
    spec = spec or {}
    kind = spec.get("kind") or "str"
    if kind in ("int", "float"):
        if isinstance(v, bool):
            raise ValueError(f"expected a number, got {v!r}")
        try:
            num = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"expected a number, got {v!r}") from None
        if kind == "int" and not num.is_integer():
            raise ValueError(f"expected a whole number, got {v!r}")
        lo, hi = spec.get("min"), spec.get("max")
        if (lo is not None and num < lo) or (hi is not None and num > hi):
            raise ValueError(f"{v!r} is outside {lo}..{hi}")
        return int(num) if kind == "int" else num
    if kind == "bool":
        if v in (True, "true", "True", 1, "1"):
            return True
        if v in (False, "false", "False", 0, "0"):
            return False
        raise ValueError(f"expected true or false, got {v!r}")
    if kind == "select":
        opts = spec.get("options") or []
        values = [o.get("value") if isinstance(o, dict) else o for o in opts]
        if values and v not in values:
            raise ValueError(f"{v!r} is not one of {values}")
        return v
    if kind == "text":
        if not isinstance(v, str):
            raise ValueError(f"expected text, got {v!r}")
        v = " ".join(v.split())
        if len(v) > 120:
            raise ValueError("at most 120 characters")
        return v
    if kind == "json":
        if isinstance(v, (list, dict)):
            return v
        import json as _json
        try:
            return _json.loads(v)
        except (TypeError, ValueError):
            raise ValueError("expected JSON") from None
    return v


async def update(values: dict, specs: list[dict] | None = None) -> dict:
    """Persist + apply editable knobs. Ignores unknown/infra keys; raises InvalidSetting, writing
    nothing, when any known key breaks its declaration. `specs` adds declarations for keys a
    capability owns (its own schema), which the global SCHEMA does not describe."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from opencortyx.domain import RuntimeSetting
    from opencortyx.core.db import V2Session
    declared = {s["key"]: s for s in SCHEMA}
    declared.update({s["key"]: s for s in (specs or [])})
    clean, problems = {}, {}
    for k, v in (values or {}).items():
        if k not in _DEFAULTS or v is None:
            continue
        try:
            clean[k] = _validated(k, v, declared.get(k))
        except ValueError as exc:
            problems[k] = str(exc)
    if problems:
        raise InvalidSetting(problems)
    if clean:
        async with V2Session() as db:
            for k, v in clean.items():
                stmt = pg_insert(RuntimeSetting).values(key=k, value={"v": v}).on_conflict_do_update(
                    index_elements=["key"], set_={"value": {"v": v}})
                await db.execute(stmt)
            await db.commit()
        _overrides.update(clean)
        _apply_side_effects()
        if "ingest_concurrency" in clean:        # live-resize the running worker pool (non-blocking)
            import threading
            threading.Thread(target=_resize_worker_pool, args=(int(clean["ingest_concurrency"]),),
                             daemon=True).start()
    return current()


def _resize_worker_pool(target: int):
    """Best-effort LIVE resize of the running Celery ingestion worker pool to `target` processes
    (grow/shrink via remote control). Broadcasts over the broker so it works cross-process (API →
    worker). Never raises; runs in a thread so it can't block the request. Also re-applied as the
    baseline on worker restart via CELERY_WORKER_CONCURRENCY."""
    try:
        from opencortyx.core.worker.celery_app import celery_app
        stats = celery_app.control.inspect(timeout=1.5).stats() or {}
        for worker, st in stats.items():
            cur = (st.get("pool") or {}).get("max-concurrency")
            if not cur:
                continue
            delta = int(target) - int(cur)
            if delta > 0:
                celery_app.control.pool_grow(delta, destination=[worker])
            elif delta < 0:
                celery_app.control.pool_shrink(-delta, destination=[worker])
    except Exception:
        pass


def _apply_side_effects():
    """Keep the live query cache in sync with the current TTL / enabled flag.

    The v2 engine has NO query cache, so the Settings toggles for it currently govern nothing.
    That is stated rather than left as a `try/except: pass` around a vanished import - a settings
    control that silently governs nothing is the same failure as a retrieval option accepted and
    ignored, and this one would have looked like it still worked.
    """
    return
