# OpenCortyx — The Context OS: Complete Roadmap & Differentiators

> The single source of truth for *what OpenCortyx is, what's built, and everything that makes it
> category-defining* as a **Context OS / Context Layer for AI** — for developers, non‑technical
> users, enterprises, founders, and researchers.
>
> Legend: ✅ built · 🟡 partial / scaffolded · 🔜 next · 🔭 later · 💎 game‑changing differentiator

> **Status audit — 2026‑06‑21.** Flags reverified against the codebase. Newly **shipped since the last
> pass**: real **auth + RBAC** (admin/editor/viewer, `require_write`, `/users`, API keys) §9 ·
> **Agent‑native MCP** server (20 tools, read **and** governed write‑back) §10 · **Agent memory &
> sessions** (Session/Turn/Decision/Fact graph, `/memory/*`, dashboard, server‑side capture) §25 ·
> **Connectors hub** (saved connections + secret stores + test/browse) §11/§27 · **Model Registry /
> BYO** (`/models` CRUD+test, UI) §15 · **Progress/Runs** page §14 · **Eval cases + run** and
> **feedback** §8 · **Manual curation UI** (glossary, relationships, candidate‑type promotion, review
> inbox for proposals) §13/§10. Still **🔜**: SSO/SAML/SCIM, PII masking, text‑to‑SQL/metrics layer,
> full §23 certification state machine, advanced chunkers/retrievers (HyDE/multi‑query/self‑query),
> scheduled sync/CDC.

---

## 0a. Production retrieval and answer pipeline (built 2026-06-21)

OpenCortyx serves grounded CONTEXT first; the answer endpoint is an optional convenience built on top
of the same context. The pipeline is adaptive, graph-aware, eval-gated, model-agnostic, and fully
controllable from Settings (group "Retrieval") and from the Assistant's retriever panel.

How a request flows (query to context, then optional answer):

1. Query arrives (from an app over REST, an agent over MCP, or the dashboard Assistant).
2. Embed the query.
3. Adaptive routing (Settings: `adaptive_routing`): if the question clearly concerns one entity or
   domain, auto-scope retrieval to that entity's collection so a large unrelated corpus cannot dilute
   a specific answer.
4. Parallel retrieval: hybrid (vector + BM25) + entity graph + hierarchy + hypothetical questions +
   structured catalogue + the hub retriever (Settings: `graph_hub_traversal`), which matches the
   entity a question is about and pulls its connected subtree over any edge type.
5. Fuse with RRF, rank.
6. Rerank cascade (Settings: `default_rerank`): cross-encoder if installed, LLM listwise fallback
   otherwise; or none.
7. Trust filter and abstain decision (low-confidence drop; threshold in Settings).
8. Conflict and freshness caveats, then top-down, token-budgeted assembly.
9. Output: grounded, cited CONTEXT. This is the core deliverable, served by `/api/v2/context` (and the
   `opencortyx_search` MCP tool). Apps and agents reason over it themselves.

Optional answer layer (`/api/v2/answer` and `/answer/stream`, `opencortyx_answer`):

- CRAG relevance gate, advisory when retrieval already returned solid evidence (Settings:
  `crag_min_citations`).
- Grounded generation with strict citation, streamed.
- Grounding verification (Settings: `grounding_mode`): `cosine` (fast) or `entailment` (LLM judge that
  catches wrong-but-on-topic sentences and, in this mode, enforces abstain when not entailed).
- Abstain calibration: a short non-answer is treated as an abstain.
- Per-step model routing (§15): the answer model and a cheaper utility model (relevance gate, judge)
  are each resolved through the Model Registry, so the whole pipeline is model-agnostic (local or API).

Quality is measured, not guessed: a gold suite (`/eval/seed`, `/eval/run`) scores pass rate,
groundedness, and abstain accuracy, with a CI workflow gate. On the 3B demo model the safe default
(cosine) scores about 0.8; entailment mode plus a capable model is the path to green. See
`docs/PRODUCTION-PIPELINE.md` for the full design.

---

## 0. Vision

**A Context OS** is the governed layer between *all of an organization's knowledge* (databases,
lakehouses, documents, SaaS, streams) and *every AI app/agent* that needs grounded, trustworthy,
fresh context. Not a vector store, not a catalog — the **operating system for context**: ingest →
structure → govern → serve, with a **no‑hallucination guarantee**, **agent‑native** access, and
**total flexibility** (any model, any mechanism, any source, self‑hostable).

One substrate unifies what others split across 3 products: **vector search + knowledge graph +
relational catalog** in a single `node`/`edge` store with an ontology.

---

## 1. What's already built ✅

- **Substrate**: `node` + `edge` (FK‑guarded, no ghosts) + `type_registry` ontology; JSONB props (zero migrations); pgvector embeddings.
- **Indexing**: pgvector **HNSW** (configurable `m` / `ef_construction` / `ef_search`) + **BM25** full‑text (GIN).
- **Retrieval**: vector, BM25, entity‑graph, hierarchy (summary→drill), hypothetical‑questions, **structured catalogue**, **GraphRAG community (global)**, **multi‑hop graph path**; **RRF fusion**; per‑query retriever selection + profiles; **parallel retrievers** (configurable concurrency).
- **Reranking**: cross‑encoder (bge) → **LLM listwise** cascade; recency weighting.
- **Trust (no‑hallucination)**: abstain‑on‑low‑confidence, citations, conflict + freshness caveats, sentence‑level grounding verification, **CRAG** relevance gate.
- **Temporal**: **bitemporal as‑of** queries (valid‑time + transaction‑time); conflict detection + resolution (latest‑wins → trust‑weighted), supersede.
- **Extraction**: ontology‑guided entity/claim/relation + **declarative templates** with **all mechanisms** — `unstructured.llm`, `regex`, `dictionary`, `keyword`, `ner` (spaCy→LLM fallback), `seed.literal`; **generic enrichment engine** ("attach anything to any node"), idempotent.
- **Structured**: live SQL introspection → MSO/Property + FK/lineage/derived nodes; **granular column→attribute / node→edge mapping**; per‑source identity, source‑scoped enrichment, per‑stage target tables, column + dropped‑table pruning.
- **Quality/integrity**: idempotent re‑ingestion (checksum + plan signature), **entity resolution** (exact→normalized→embedding merge + aliases), **write‑time ontology validation**.
- **Observability**: per‑answer token spend + contributing‑node trace + latency + trust, audit log; **quarantine/DLQ** for failed extractions.
- **Ops/flex**: embedding + query‑result caching, read‑replica routing, **fast** low‑latency mode, runtime‑tunable knobs in Settings UI, optional DSN encryption at rest.
- **Authoring**: API + **UI** ✅ for custom types, glossary, rules, synonyms, relationships, nodes, edges (curation/curate pages).
- **Auth & access** ✅: first‑run admin setup, password login, **RBAC** (admin/editor/viewer) with `require_write` on mutations, **API keys** (X‑API‑Key) for agents, Users admin page.
- **Agent‑native** ✅: MCP server (20 tools) over `/api/v2` — read + governed write‑back (proposals) + agent memory; one‑command connectors for any MCP client.
- **Agent memory & sessions** ✅: Session/Turn/Decision/Fact/Preference graph, `/memory/*` (recall/remember/save‑session/resume), Conversations dashboard, **consent‑gated server‑side capture**.
- **Connectors hub** ✅: saved connections separate from runs, encrypted secrets (Fernet/env/Azure Key Vault), test/browse/recipe, lake + file formats (Delta/Iceberg/Hudi via DuckDB).
- **Model Registry / BYO** ✅: register/test gen/embed/rerank models (`/models`), per‑extractor prompt+model transparency in Studio.
- **Trust ops** ✅: eval cases + run (`/eval/*`), 👍/👎 feedback (`/feedback`), Progress/Runs page, System Health page, graph export/import (no lock‑in), per‑org cost budget/cap.

---

## 2. Chunking strategies (the full menu)

| Strategy | Status | What / why |
|---|---|---|
| Fixed‑size + overlap | ✅ | Baseline; token/char windows with overlap. |
| Recursive splitting | 🔜 | Split on structure (¶ → sentence → token) preserving boundaries. |
| **Semantic chunking** | 🔜 | Embedding‑similarity boundary detection (split where meaning shifts). |
| Sentence / paragraph | 🔜 | NLP sentence segmentation. |
| Sliding window | 🔜 | Overlapping windows for recall. |
| **Hierarchical / parent‑child** | 🔜 | Small chunks for match, big parents for context (small‑to‑big). |
| Document‑structure‑aware | 🔜 | Markdown headers, HTML, PDF sections/layout. |
| **Layout/table/figure‑aware** | 🔭 | Detect tables, figures, captions (PDF layout models). |
| **Proposition / dense‑X** | 🔭 | Atomic factual statements as units (highest precision). |
| **Late chunking** | 🔭 | Embed whole doc, then chunk the embeddings (keeps global context). |
| **Contextual retrieval headers** | 🔜 | Prepend doc/section context to each chunk (Anthropic technique). |
| **Agentic chunking** | 🔭 | LLM decides boundaries by topic. |
| Code‑aware (AST) | 🔭 | Split by function/class for codebases. |
| Row/record chunking | ✅ | Structured rows/records as units. |
| Multimodal chunking | 🔭 | Images, audio segments, video keyframes. |

→ **Pluggable chunker registry** (like the retriever registry) so any strategy is config‑selectable per source/collection.

---

## 3. Indexing strategies (the full menu)

| Index | Status | When |
|---|---|---|
| **HNSW** (graph ANN) | ✅ | Default; fast, high recall ≤10⁷. |
| Flat / exact | 🔜 | Small collections, perfect recall. |
| IVF / IVF‑PQ | 🔭 | 10⁸+ with compression. |
| **Product / scalar / binary quantization** | 🔭 | 4–32× memory cut for billions. |
| **DiskANN** | 🔭 | Billions on SSD, low RAM. |
| **Multi‑vector / ColBERT (late interaction)** | 🟡 stub | Token‑level matching; strong on long/precise queries. |
| **Sparse / SPLADE** | 🟡 stub | Learned sparse; exact‑term strength. |
| BM25 inverted (full‑text) | ✅ | Keywords, IDs, codes, dates. |
| **Hybrid (dense+sparse)** | ✅ (RRF) | Best of both, fused. |
| **Hierarchical / RAPTOR tree** | 🟡 (summaries) | Multi‑level summary index for global Qs. |
| **Knowledge‑graph index** | ✅ | Entities + relations + communities. |
| Metadata / filtered index | 🟡 | Pre‑filter pushdown (org/collection/type/time). |
| Time‑partitioned index | 🟡 (as‑of) | Bitemporal slices. |
| **Sharded / distributed index** | 🔭 | Scatter‑gather for trillions. |
| Semantic cache index | ✅ (query cache) | Reuse near‑duplicate queries. |

---

## 4. Embedding strategies

| Strategy | Status |
|---|---|
| Dense (fastembed nomic / vLLM / OpenAI) | ✅ |
| **BYO / HuggingFace / local models** | 🔜 💎 |
| **Per‑task / per‑type embeddings** (different model per extraction) | 🔜 |
| Instruction‑tuned (query vs doc) | 🔜 |
| Multilingual | 🔜 |
| Matryoshka (variable dim, cheap recall→precise) | 🔭 |
| Fine‑tuned domain embeddings | 🔭 |
| Sparse (SPLADE) / Multi‑vector (ColBERT) | 🟡 |
| Multimodal (CLIP / image / audio) | 🔭 |
| Quantized (int8 / binary) | 🔭 |
| Re‑embed / re‑index job on model change | 🔜 (required for the above) |

---

## 5. Retrieval strategies

| Strategy | Status |
|---|---|
| Dense vector | ✅ |
| BM25 / keyword | ✅ |
| **Hybrid + RRF fusion** | ✅ |
| Entity‑centric graph | ✅ |
| Hierarchy (summary → drill) | ✅ |
| Hypothetical questions | ✅ |
| **GraphRAG global (community summaries)** | ✅ |
| **Multi‑hop graph path** | ✅ |
| Structured catalogue | ✅ |
| Memory (working/long‑term) | ✅ (agent memory + sessions §25) |
| As‑of / time‑travel | ✅ |
| **HyDE** (hypothetical doc embeddings) | 🔜 |
| **Multi‑query / query expansion** | 🔜 |
| **Sub‑question decomposition** | 🔜 |
| **Step‑back prompting** | 🔭 |
| **Self‑query** (LLM extracts metadata filters) | 🔜 |
| **Parent‑document / sentence‑window / auto‑merging** | 🔜 |
| **Agentic / iterative (Self‑RAG, CRAG, ReAct loops)** | 🟡 (CRAG gate) 💎 |
| **Text‑to‑SQL retrieval** (NL → SQL over the catalogue) | 🔜 💎 |
| Adaptive routing (pick strategy by query type) | 🔜 |
| Recency / freshness weighted | ✅ |
| Late interaction / SPLADE | 🟡 |

---

## 6. Reranking strategies

| Strategy | Status |
|---|---|
| Cross‑encoder (bge) | ✅ |
| LLM listwise | ✅ |
| RRF fusion | ✅ |
| **MMR (diversity / dedup)** | 🔜 |
| **Cohere / Voyage / Jina rerank (API)** | 🔜 |
| **ColBERT late‑interaction rerank** | 🔭 |
| Recency / freshness boost | ✅ |
| **Source‑trust / authority weighting** | ✅ (in conflict) |
| **Business‑rule rerank** (boost certified/owned assets) | 🔜 |
| **Personalized rerank** (by user / role) | 🔭 |
| Learned‑to‑rank (LTR) from feedback | 🔭 💎 |

> All of the above are exposed as **config‑selectable plugins** per query/profile — the user picks
> retrievers, rerankers, models, chunkers, and thresholds, end‑to‑end.

---

## 6.5 💎 Unified Task + DAG architecture (the north star)

**Principle:** *every* built-in extractor is an individual, composable **task**. A task takes the
output of **any prior step (any nodes)** and emits new nodes/edges. The pipeline is a **DAG of
tasks**. This is **identical for documents and structured** — one engine, one task model.

A **Task** = `{ name, accepts(input node types), run(input_node_pairs, config) → output_pairs + edges }`.

| Task | Domain | Status |
|---|---|---|
| chunk, summarize(scope: chunk/document/rollup), topics, keywords, entities, claims | document | ✅ DAG (chain over any step) |
| custom declarative template (any mechanism) over any node type | both | ✅ generic engine |
| introspect (→MSO/Property), business_semantics, segments, computations, data_quality, lineage, business_rules | structured | 🔜 still flag-based; convert to tasks |

**Target:** one `TaskRegistry` + one `run_dag(stages)` executor used by **both** document and
structured ingestion. Each stage reads its Input Source (a prior task's output or the base nodes),
runs, and passes output downstream — so "summary on top of summaries," "topics on top of segments,"
"a custom task on top of MSOs" are all the same mechanism.

**Staged plan (low-risk):**
1. 🔜 Wrap each structured generator as a Task (input node-pairs → output pairs), keeping current behaviour.
2. 🔜 Add a structured DAG executor honoring per-stage Input Source (mirrors the document `_run_dag`).
3. 🔜 Merge document + structured executors into one `TaskRegistry`/`run_dag`.
4. 🔭 Surface the DAG visually (the builder becomes a true node-graph editor).

---

## 7. Extraction & enrichment ✅ (keep extending)

- Declarative templates: `source → mechanism → emit`; mechanisms: LLM, regex, dictionary, keyword, NER, seed.literal, structured.sql 🔜.
- Generic enrichment engine: attach attributes/nodes/edges to **any** node type, idempotent, ontology‑validated.
- Built‑in catalog of all 22 node + ~34 edge types (deterministic = view‑only; LLM = runnable).
- 🔜 cross‑table mapping edges (join keys), agentic extraction, multimodal extraction (image/table/audio).

---

## 8. Trust, quality & evaluation 💎

- ✅ Abstain, citations, caveats, grounding verification, CRAG.
- 🟡 **Evaluation harness** — gold Q→A cases + on‑demand run shipped (`/eval/cases`, `/eval/run`, dashboard); 🔜 recall@k/groundedness/abstain‑calibration metrics + **regression gate on every change**.
- 🟡 **Feedback loop** — 👍/👎 + corrections captured (`/feedback`); 🔜 wire it into ranking / auto‑learn.
- 🔜 **Completion / data‑health score** per asset (completeness + freshness + usage + certification).
- 🔜 Column‑level **lineage** + impact analysis; certification badges; freshness SLAs; drift/anomaly alerts.

---

## 9. Governance, security & compliance 💎 (enterprise unlock)

- 🟡 Collections + policies + sensitivity lattice (scaffolded).
- ✅ **RBAC** (admin/editor/viewer) with `require_write`/`require_admin` on mutations + admin endpoints; ✅ **API keys** (issue/list/revoke, X‑API‑Key) for agents. 🔜 groups, **SSO/SAML + SCIM**, per‑collection/per‑type permissions, **rate limiting**.
- 🔜 **PII detection + classification + masking/redaction at retrieval**.
- 🔜 **Policy engine** (ABAC, purpose‑based), **data contracts**, **multi‑level approval + certification workflows** for any asset → see **§23**.
- 🔜 GDPR/CCPA: delete‑by‑subject, compliance reports; ✅ audit log; 🔭 data residency / multi‑region.
- ✅ Optional secret encryption at rest.

---

## 10. Agent‑native & MCP 💎 (the category wedge)

- ✅ **MCP server (read)** — `axiom_answer` / `axiom_search` / `axiom_get_node` / `axiom_catalog` / `axiom_stats` + ingestion/extractor tools (20 total) over `/api/v2`. 🔜 text‑to‑SQL tool.
- ✅ **MCP write‑back** — `axiom_propose` → Draft proposal → **Review Inbox** approve/reject (`/proposals/*`) → governed merge (no silent writes).
- ✅ **Agent memory** — working + long‑term per agent/session via `axiom_recall` / `axiom_remember` / `axiom_save_session` / `axiom_resume` (§25), plus consent‑gated real‑time capture at the MCP layer.
- 🔜 **Universal capture (API‑layer)** — move session capture from the MCP layer down to the API so it *also* records agents that call the REST API directly (not just via MCP), and **auto‑distinguishes each client by its own API key** (today: MCP‑layer, one shared key + label).
- 🔜 **Streaming answers (SSE)**, context budgeting/compression, **"context packs"** agents subscribe to.
- ✅ Per‑agent scoping via API key (org‑scoped); returned citations + reasoning path. 🔜 finer per‑agent RBAC.

---

## 11. Connectivity & freshness (your "Connectors hub")

- ✅ **Dedicated Connectors section** — saved *connection* (encrypted credential) separate from *ingestion run*; test/browse/recipe; table/dir picker. (See §27 for the engine/format matrix.)
- 🔜 Connectors: warehouses (Snowflake, BigQuery, Redshift, **Databricks**), **lakehouse formats (Delta, Iceberg, Hudi)** via their catalogs, object storage (S3/GCS/Azure), **SaaS** (Salesforce, Notion, Confluence, Slack, GDrive, SharePoint), **streaming** (Kafka), generic JDBC/REST.
- 🔜 **Custom table / column / directory selection**; custom document & unstructured selection.
- 🔜 **Scheduled sync + CDC + incremental**; schema‑drift handling; TTL/staleness; **dbt/catalog metadata import** (pull existing descriptions + lineage).
- 🔜 Push API + webhooks/events.

### 11.1 🔜 Run scheduling — one‑time, recurring, and real‑time

Today a run is triggered manually (UI/API/MCP). Make *when* a run fires first‑class, configurable per pipeline + connector:
- **One‑time** — run once, now or at a chosen future timestamp.
- **Recurring / scheduled** — any interval (every N minutes/hours, daily, weekly, cron expression); per‑run timezone; backfill window.
- **Real‑time / continuous** — event‑driven (webhook/push API, connector CDC stream, Kafka topic, S3/object‑store notification, file‑watch) so changes flow in as they happen with no polling.
- Controls: enable/pause, max concurrency, retry/backoff, catch‑up vs skip‑missed, and a **Schedules** view on the Runs page showing next‑fire, last‑status, and history. Each fire reuses the saved **Pipeline** (§6.5) + **Connector** (§11) — schedule is orthogonal to *what* runs.

### 11.2 🔜 Bring‑your‑own store — pluggable persistence backends (write to *your* databases)

Today OpenCortyx persists the graph (nodes/edges), vectors, and metadata in *its* Postgres + pgvector. Make the **storage layer pluggable** so an org can point OpenCortyx at infrastructure it already runs and have it write there instead of (or in addition to) ours:
- **Relational / metadata** → external **PostgreSQL** (or other SQL) you already operate.
- **Vectors** → an existing **vector DB** (pgvector, Qdrant, Weaviate, Milvus, pgvector‑on‑your‑Postgres, Pinecone).
- **Graph** → a native **graph DB** (Neo4j, Memgraph, Apache AGE) for relationship‑heavy workloads.
- Configured like Connectors (saved, encrypted credentials, test/health); a **storage profile** is selected per org (and, later, per collection). Repository interfaces (`NodeRepo`, vector search, neighbors) get backend adapters so the pipeline code is unchanged. Goal: **no lock‑in on the storage side** — your data lives in your stores, OpenCortyx provides the intelligence layer over them.

---

## 12. Structured intelligence 💎

- 🔜 **Text‑to‑SQL** over the catalogue (NL question → governed SQL using MSO/Property/Computation).
- 🔜 **Semantic / metrics layer** — define a KPI once (`Computation`), reuse everywhere; governed metrics.
- 🔭 MDM‑lite golden records + cross‑source entity linking.

---

## 13. Human‑in‑the‑loop curation (your manual‑UI ask)

- ✅ **Manual editing UI** (no LLM): glossary terms, synonyms, **relationships** (`/glossary`, `/relationships`) via curation/curate pages.
- ✅ **Rich Resources** (2026‑06): glossary / synonym / rule can **attach to any node(s)** (not just "assets") — multi‑target — with optional **scope, status (draft/approved/deprecated), owner/steward, source/authority, validity dates, examples**; **synonym alias‑kind** (acronym / abbreviation / former‑name / variant / translation — a deprecated name can warn rather than silently resolve) and **rule kind + severity** (must/should/info). Advanced fields are collapsed by default (no friction).
- ✅ **Business / Org context** (2026‑06): org/collection‑level narrative (mission, domain, standing assumptions). **Always‑on** items are prepended to every answer's context as a frame and shown in the trace — toggle in Settings → Retrieval (`business_context_framing`). Distinct from per‑asset `ContextRule`: it frames, it isn't only retrieved on match.
- 🟡 Inline asset‑page editing, glossary manager, visual relationship builder; ✅ **candidate‑type promotion** (Review Inbox → Ontology).
- 🔜 Collaboration: comments, ownership/stewardship, change requests, notifications → **full multi‑level review + certification in §23**.

---

## 14. Observability & ops

- ✅ Token/cost + trace + latency + trust per answer; audit; quarantine/DLQ.
- ✅ **Progress / Runs page** — `IngestRun` job states, per‑stage steps, cancel/purge, orphan reconcile. 🔜 **Schedules** tab (one‑time / recurring / real‑time triggers — see §11.1).
- 🔜 Usage analytics + cost dashboards; tracing across retrieval→rerank→answer.
- 🔜 **Cost‑aware model routing** (cheap model for easy, big for hard); budgets/quotas.

---

## 15. Model flexibility / BYO 💎

- ✅ **Model Registry — every tier registerable** (`/models` CRUD+test, UI). Wired into the live pipeline: **generation, embedding, reranker**. Registerable + saved (encrypted) now, consumed as each capability ships: **reasoning, vision, OCR, speech‑to‑text, text‑to‑SQL, graph‑embedding, classification, entity‑extraction**. Mix self-hosted (vLLM), hosted API (OpenAI/Anthropic), and tiered models freely; per‑extractor model+prompt transparency in Studio. 🔜 automatic per‑task routing across the registered tiers.
  - *Note: the registry stores model **references** — provider, model id, base URL, and an encrypted API key — not weights. Local weights stay where the runtime keeps them (vLLM's HuggingFace cache in the shared `model_cache` volume); OpenCortyx only points to them, so nothing model‑related is duplicated into our DB.*
- ✅ Provider abstraction (vLLM ✅ default and the only local runtime; OpenAI/Anthropic/Groq/Azure/HF via the registry, all sharing one OpenAI-wire dispatcher) → 🔜 Bedrock adapter.
- ✅ Privacy‑first: fully self‑hostable, no data leaves the VPC.

---

## 16. Multimodal 🔭

- Images (OCR + captioning + CLIP), tables/figures, audio (ASR), video (keyframes) → nodes with embeddings; multimodal retrieval.

---

## 17. Platform, scale & DX

- 🔜 **SDKs** (Python/JS) + **GraphQL** alongside REST; plugin SDK (custom connectors/extractors/retrievers/chunkers).
- 🔭 Distributed / sharded substrate + ANN (trillions); tiered hot/cold storage.
- 🔭 SaaS + self‑host + VPC; backup/restore/DR; multi‑region.
- 🔜 Query **playground / notebook**; discovery/search UX; 🔭 **data marketplace** + recommendations.

---

## 18. 💎 The game‑changing differentiators (why we win)

1. **Provable no‑hallucination** — trust layer + grounding + abstain + **eval harness** (measurable, not marketing).
2. **One substrate** — vector + graph + relational unified; no stitching 3 tools.
3. **Agent‑native Context OS** — MCP read **and governed write**, memory, agent tools, streaming.
4. **Bitemporal time‑travel** — "what did we know / what was true on date X."
5. **Total flexibility** — any chunker, index, retriever, reranker, embedder, LLM (incl. local/HF), any extraction mechanism — all declarative + config‑selectable.
6. **Self‑improving** — feedback → auto‑tune ranking; auto‑eval regression gates.
7. **Governance built‑in** — RBAC, PII masking, lineage, data contracts, compliance, audit.
8. **Declarative no‑code studio** — non‑technical users define types, glossary, rules, extractions, pipelines.
9. **Conflict‑aware knowledge** — detects + resolves contradictions; supersede; "what changed."
10. **Universal connectivity + freshness** — DBs, lakehouse, SaaS, streams + CDC.
11. **Structured intelligence** — text‑to‑SQL + semantic/metrics layer over a governed catalogue.
12. **Privacy‑first + self‑hostable** — runs in your VPC; bring your own models.
13. **Explainable** — every answer cites sources + shows the reasoning/retrieval path + confidence + caveats.
14. **Completion/health scoring** — every asset gets a score + concrete steps to improve.

---

## 19. Value by persona

| Persona | What wins them |
|---|---|
| **Developers** | SDKs, REST/GraphQL/MCP, plugin SDK, local/HF models, self‑host, declarative config. |
| **Non‑technical / common people** | No‑code Studio, NL search + chat, manual curation UI, completion scores. |
| **Enterprises** | RBAC/SSO, PII masking, lineage, data contracts, audit/compliance, scale, certification, SLAs. |
| **Founders / startups** | Fast setup, SaaS or self‑host, cost‑aware routing, BYO‑model, marketplace. |
| **Researchers** | Eval harness, model/strategy flexibility, reproducibility, experimentation. |

---

## 20. Prioritized roadmap (phased)

**Phase A — Enterprise & agent wedge (highest leverage)**
1. ✅ MCP server (read + governed write) + agent tools 💎
2. 🟡 Evaluation harness + feedback loop 💎 (cases/run + 👍/👎 shipped; metrics + regression gate next)
3. 🟡 RBAC ✅ + API keys ✅ · 🔜 SSO/SCIM + PII masking + rate limiting 💎
4. 🟡 Connectors hub ✅ + lakehouse (Delta/Iceberg/Hudi) ✅ + table/column select ✅ · 🔜 scheduled/CDC sync

**Phase B — Intelligence & UX**
5. 🔜 Text‑to‑SQL + semantic/metrics layer 💎
6. 🔜 Manual curation UI (descriptions/glossary/relationships) + Progress page + Completion score
7. 🔜 **Human governance: multi‑level review → validate → certify, role/ownership, review inbox + badges (§23)** 💎
8. 🔜 Model flexibility / BYO‑HF + per‑task routing + re‑index job
9. 🔜 Pluggable chunker registry (semantic, hierarchical, contextual headers) + more retrieval strategies (HyDE, multi‑query, self‑query, parent‑doc, agentic)

**Phase C — Depth & scale**
10. 🔭 Column‑level lineage + certification SLA/escalation + e‑signature + drift‑triggered re‑certification (extends §23)
11. 🔭 Advanced indexes (ColBERT, SPLADE, quantization, DiskANN) + distributed/sharded scale
12. 🔭 Multimodal (image/table/audio/video)
13. 🔭 Marketplace + collaboration + multi‑region + DR

---

DAG / section improvements worth doing later: a visual node-graph editor (drag stages, branch one step into several), per-stage model selection (which LLM/embedder), dry-run preview (what a stage would emit), saved/reusable pipelines, and per-stage cost/time estimates. But those are

## 21. Data Catalog & Active Metadata (we already catalog — now make it *active*)

Cataloging isn't a future feature — it's **already core**: `MSO` (tables), `Property` (columns),
`Segment`, `Computation` (metrics), `Lineage`, `DataQuality`, `Absence`, plus glossary, synonyms,
relationships, FK/JOIN edges, and live SQL introspection. That's a real catalog **today**.

The difference vs a classic catalog (Atlan/Collibra/Alation): theirs is **passive metadata for
humans**; ours is **active metadata that serves AI**. The upgrades that complete the pillar:

- ✅ Structured catalogue + introspection + derived business nodes + granular mapping.
- 🔜 **Auto-curation** — LLM-drafted column/table descriptions, glossary terms, synonyms, domains, with **one-click human review** (kills the manual-curation burden that leaves catalogs 20% covered and stale).
- 🔜 **Completion / health score** per asset + a "fix-it" worklist (coverage, freshness, ownership, certification) — coverage rises automatically.
- 🔜 **Semantic lineage + impact analysis** (column-level), fed *into retrieval* (not just drawn for humans).
- 🔜 **Governance enforced at query time** — RBAC + PII masking + sensitivity actually applied when AI answers, not just documented in a policy page.
- 🔜 **Text-to-SQL + semantic/metrics layer** — ask the catalogue in English; governed, reusable KPIs.

---

## 22. 💎 How we top Glean & Atlan — by solving pain, not by spending

We don't out-spend them. We win by being the thing **neither is**: an **open, self-hostable Context
OS that unifies catalog + grounding + governance and serves it to the AI/agents you build.** The
unification *is* the moat — competitors split structured vs unstructured, human vs AI, catalog vs RAG.

### Beat **Atlan** (passive catalog for humans → active catalog for AI)

| Atlan pain (what users complain about) | Our feature | Value delivered |
|---|---|---|
| Catalog **describes** data but can't **answer** with it or feed agents | **Active catalog serves grounded context** (structured retriever + text-to-SQL) | "Your metadata now answers questions and powers agents" |
| Manual curation → low coverage, stale descriptions | **Auto-curation + human review + completion score** | Coverage climbs by itself; stays fresh |
| **Structured-only** | **Unified structured + unstructured + graph** | One catalog for tables *and* docs *and* relationships |
| Governance is **documented**, not enforced on AI | **RBAC + PII masking + sensitivity enforced at answer time** | Policy is actually applied, not a PDF |
| No grounded answering / no NL→SQL | **Text-to-SQL + semantic metrics layer** | Ask the warehouse in English, governed |
| Lineage is technical, for humans | **Semantic lineage + impact, fed into retrieval** | Lineage improves answers, not just diagrams |
| Cloud-only SaaS | **Open-source + self-host in your VPC** | Regulated/privacy-sensitive orgs can finally adopt |

### Beat **Glean** (closed assistant → open agent-native platform)

| Glean pain | Our feature | Value delivered |
|---|---|---|
| **Closed cloud — data leaves the VPC** | **Self-host + BYO-model + open** | Wins regulated, privacy-first enterprises outright |
| It's an **assistant**, not a platform — can't build your own agents on it | **Agent-native MCP (read + governed write) + context API** | You build *your* agents on it; it's infrastructure, not one chat |
| **Unstructured-first** — weak on databases | **Structured catalog + text-to-SQL** | Grounds your databases, not just documents |
| **Black-box** retrieval/ranking/model | **Fully selectable retrievers / rerankers / chunkers / embedders / LLMs** | Total control + tunability + transparency |
| Hallucination control is **opaque** | **Eval harness + abstain + citations + grounding verification** | Provable, runnable no-hallucination — not a claim |
| Per-seat assistant pricing | **Context layer + API powering many apps** | One layer, unlimited apps/agents |
| No structured governance/catalog | **Catalog + governance unified** | One system instead of Glean **+** a catalog |

### The one-line wedge
> **Atlan catalogs for humans. Glean answers from documents. We do both — and serve governed,
> grounded context to the agents you build, in your own VPC, with provable no-hallucination.**

### The "win" features, in priority order (value-first)
1. 🔜 **Auto-curation + completion score** — directly kills Atlan's #1 pain (coverage/staleness). Fast, high-visibility win.
2. 🔜 **Text-to-SQL + semantic layer** — "ask your databases in English," which neither does well.
3. 🔜 **Governance enforced at answer time** (RBAC + PII masking) — turns governance from docs into enforcement.
4. ✅ **Agent-native MCP (read + governed write)** — the platform play that Glean (assistant) can't match (20 tools + memory).
5. 🔜 **Eval harness + provable grounding** — the trust differentiator, demoable in a POC.
6. ✅/🔜 **Open + self-host + BYO-model** — the structural advantage both lack; lead every regulated deal with it.
7. ✅ **Unified structured + unstructured + graph** — already true; make it the headline.

> Principle: **win one painful job completely** (e.g. "auto-curated, governed, AI-answerable catalog
> over your own databases, self-hosted") before broadening. Depth on a real pain beats breadth.

---

## 23. 💎 Human governance: multi‑level review, validation & certification

**Why.** Everything we automate (extraction, enrichment, derived nodes) is a *draft until a human
trusts it*. Enterprises won't let AI answer from — or agents act on — un‑vetted data. This is the
**trust control plane**: humans comment, approve through configurable gates, validate, and finally
**certify ("Verified")** any asset. It turns OpenCortyx from "AI that extracts" into "AI **people
own and stand behind**," and it directly feeds retrieval (certified > approved > draft). Applies to
**both structured and unstructured** — because every asset is the same `node`, governance attaches
uniformly (a `Property`/`MSO`/`Computation` is reviewed exactly like a `Summary`/`Topic`/`Claim`).

### 23.1 The review lifecycle (state machine)
A reviewable asset (or a proposed *change* to one) moves through configurable states:

```
 Draft ──submit──▶ In Review ──(gate 1..N approvals)──▶ Approved ──validate──▶ Validated ──certify──▶ Certified ✅
   ▲                   │  ▲                                                                                │
   └──── Changes ◀─────┘  └──── Rejected ───────────────────────────────────────────────────────────────┘
        Requested                                                            Certificate can expire / be revoked → back to Approved/Draft
```

- **Comment** — anyone with access threads comments/annotations on an asset or a specific attribute/cell (no state change).
- **Approve** — a reviewer signs off one **gate**; a stage can require *N approvals* or *specific roles* (multi‑level).
- **Validate** — a deeper check (data correctness, lineage, query‑tested) by a senior role; gate before certification.
- **Certify / Verify** — a **high‑authority steward/owner** issues a signed **Certificate** once all prior gates clear.
- **Reject / Request changes** — sends it back with required edits; **Revoke / Expire** — pulls a certificate (drift, staleness, new source version).

### 23.2 Roles, ownership & authority (assignable to people *and* teams)
Pluggable roles mapped onto RBAC groups (§9); each asset/collection/domain has **owners** and **assignees**:

| Role | Comment | Approve (gate) | Validate | Certify | Typical ownership |
|---|---|---|---|---|---|
| **Viewer / Consumer** | ✅ | — | — | — | none |
| **Analyst** | ✅ | ✅ (gate 1) | — | — | reports, metrics usage |
| **Data Engineer** | ✅ | ✅ | ✅ (technical) | — | tables/pipelines (`MSO`/`Property`/lineage) |
| **Data Scientist** | ✅ | ✅ | ✅ (semantic) | — | features, `Computation`/`Segment` |
| **Data Steward / Owner** | ✅ | ✅ | ✅ | ✅ **(issues certificate)** | domain/collection certification |
| **Admin** | ✅ | ✅ | ✅ | ✅ | policy, gate config, role grants |

- **Ownership levels** — `owner` (accountable, can certify within scope) vs `steward` (curates) vs `assignee` (a task to do) vs `subscriber` (notified). Assignable at **asset, collection, type, or domain** scope, to an **individual or a team**.
- **Authority ladder** — certification requires a role at/above the asset's `certify_authority`; higher tiers can override/revoke lower decisions (with audit).
- **Separation of duties** — optionally the author can't approve their own asset; certifier ≠ sole approver.

### 23.3 Configurable approval policy (per domain / type / sensitivity)
- **Gates** = ordered list of `{name, required_roles, min_approvals, optional}` — e.g. *restricted* `Computation` needs `engineer ✚ scientist ✚ steward`; a `Topic` needs 1 analyst.
- Policy selected by **asset type + collection + sensitivity** (reuses the sensitivity lattice).
- **Auto‑routing** — on ingest/enrich, new/changed assets are auto‑assigned to the owning team's **review inbox**; SLA timers + escalation.
- **Bulk review** — approve/validate by table, by source, by extractor, or by saved filter (curation at scale).

### 23.4 The Certificate (the artifact)
- A signed, versioned record: `{asset_id, asset_version, scope, level, issued_by, issued_at, expires_at, evidence[]}` where evidence links the comments/approvals/validations and the **plan‑signature** the asset was built with.
- **Badge** surfaced everywhere the asset appears (catalogue, graph, chat citations): `Verified ✅` / `Approved` / `In review` / `Draft`.
- **Re‑certification triggers** — source re‑ingest (checksum/version change), pipeline change (plan‑sig), schema drift, or TTL → certificate auto‑expires and the asset re‑enters review.

### 23.5 Feeds the rest of the system (not a side feature)
- **Retrieval‑aware trust** — certified content ranks above approved above draft; **filter `certified_only`** for high‑stakes answers/agents; abstain when only un‑vetted context is found.
- **Governed agent write‑back (§10)** — agent‑proposed facts land as **Draft** and must clear the same gates before merge → safe MCP write.
- **Quarantine bridge** — a rescued quarantine item re‑enters as Draft; **completion/health score (§21)** counts certification coverage.
- **Audit** — every comment/approval/validation/certificate/revoke is an `AuditLog` event (who/when/why) → compliance reports.

### 23.6 Build map
- **Data model** — `Review` (per asset+version, status), `ReviewGate`/`Approval` (role, decision, actor), `Comment` (threaded, optional attribute anchor), `Certificate` (signed, expiry), `Assignment`/`Ownership` (subject→role→scope), `ReviewPolicy` (gates by type/collection/sensitivity). All in `axiom_v2` control plane beside `node`/`edge` — **never** graph nodes.
- **API** — submit/transition endpoints, comment CRUD, assign/own, policy CRUD, certificate issue/revoke, `GET /reviews?inbox=me`, asset‑status badge on existing node views.
- **UI** — (1) **Review Inbox** ("assigned to me / my team", SLA, bulk actions); (2) **Asset Review panel** (status timeline, comments, approve/validate/certify buttons gated by role); (3) **Certificate badge** on catalogue/graph/chat; (4) **Governance dashboard** (coverage %, pending by team, expiring certs, ownership gaps); (5) ownership/role admin.
- **Status**: 🟡 **partial** — a **Review Inbox** UI already triages agent‑proposed facts (approve/reject, `/proposals/*`) and ontology candidate‑types; 🔜 the full asset `Review`/`Certificate`/`ReviewGate`/`Ownership` model + multi‑level state machine + badges (Phase A/B); 🔭 SLA/escalation, e‑signature, separation‑of‑duties, attestation reports.

> One‑liner: **every asset has an owner, a review trail, and a certificate — for tables and documents alike — so AI only answers from data a human has verified.**

---

## 24. 💎 Pain‑point coverage — built for **humans AND agents**

Every classic data‑catalog failure mode, mapped to OpenCortyx's answer — and crucially, **what a human
gets vs. what an agent gets**, because they hit the **same governed substrate** (no second system, no
drift). Humans use the **dashboard**; agents use the **MCP server / context API**; both read/write the
exact same `node`/`edge` graph under the same scope and governance.

| Catalog pain | What a **human** gets | What an **AI agent** gets | Status |
|---|---|---|---|
| **Search & discovery** (scattered, irrelevant) | Catalogue search + Data Assistant chat over tables *and* docs | `axiom_search` / `axiom_answer` — one call, both modalities | ✅ hybrid+RRF+rerank · 🔜 relevance polish |
| **Trust & certification** (which to trust?) | Trust score + freshness/conflict caveats; **certified ✅ badge** | trust score + **abstains** + citations (no hallucination) | ✅ trust/abstain/citations · 🔜 certification (§23) |
| **Ownership & roles** (ghost towns) | Owners/stewards, review inbox, "who do I ask?" | only answers from data its scope allows | 🔜 §23 |
| **Metadata quality** (stale, manual) | LLM‑drafted descriptions/glossary + **1‑click review** + health score | that same metadata *is* its context | 🔜 §21 (✅ extraction engine that drafts it) |
| **Lineage & impact** ("what breaks if…") | Lineage graph + impact view | `axiom_traverse` for impact/feature lineage | ✅ table/FK lineage · 🔜 column‑level |
| **Access control & secrets** | RBAC + collections/sensitivity; **Key Vault / env secrets** | per‑agent scope + PII masking at answer time | ✅ RBAC + API‑key scope + secret stores · 🔜 SSO + PII masking |
| **Freshness & observability** | Freshness/quality on the **same** asset view + Progress tab | trust + `as_of` time‑travel in every answer | ✅ audit/cost/latency/Progress + bitemporal · 🔜 freshness SLAs/CDC |
| **Explainability** (what does this field mean?) | Glossary/ontology definitions inline | **entity disambiguation** via the graph (one "customer") | ✅ ontology + citations · 🔜 glossary UI |
| **Governance & compliance** | Review→certify workflow + full audit trail | **governed write‑back** — proposals, never silent merges | ✅ audit · 🔜 §9/§23 |
| **Collaboration & culture** | Comments, ownership, low‑friction curation | agents *contribute* vetted facts back | 🔜 §23 |
| **Scale & performance** | Fast search at scale | same, via API | ✅ HNSW + parallel + cache (cluster) · 🔭 distributed |
| **Cost** | Open‑source, **self‑host in your VPC**, BYO‑model | no per‑seat tax — one layer powers many agents | ✅ |
| **Integration** (portal‑first, weak APIs) | Connectors hub, no‑URI ingestion | **API‑first MCP** (the agent surface itself) | ✅ connectors + MCP · 🔜 more connectors |
| **Onboarding & change** | `docker compose up` → schema auto‑creates | zero‑config: point an MCP client at it | ✅ self‑host quickstart |

**The point:** competitors make humans a catalog **or** make an assistant for agents — and the two
drift apart. OpenCortyx is **one substrate serving both**: a human certifies an asset in the dashboard,
and the next agent answer automatically prefers it (and can be forced to *only* use certified data).
Metadata a human curates becomes an agent's grounding; a fact an agent proposes enters a human's
review queue. **Same data, same governance, same trust — for people and machines.**

> Design rule to keep it simple: **never build a feature only for humans or only for agents.** If it
> can't be expressed as a node/edge + a tool, it doesn't belong here.

---

## 25. 💎 Agent memory & session context — *as a graph, not a markdown handoff* ✅ (core shipped)

**The real unsolved problem in agentic work isn't retrieval — it's continuity.** Every AI session
starts cold. Today people keep context alive with brittle hacks, and *all of them fail the same way:*

| How people do it today | Why it breaks |
|---|---|
| **Manual handoff doc** — summarize at session end, paste at session start (3–5×/day) | The doc degrades when it hits the context limit; **decisions get silently lost**. |
| **A dedicated "context‑keeper" agent** | Rebuilt from scratch every project; no shared substrate; no governance. |
| **Folders + markdown** (Obsidian / Notion / plain `.md`) | Manual upkeep = friction; **falls apart within a week**. |
| **SharePoint / wiki** | Corporate teams sharing AI context in a system never designed for it; stale, unsearchable by agents. |
| **Nothing — re‑explain from scratch every time** | Surprisingly common. People have *given up on continuity.* |

The common root cause: **context lives in documents.** Documents have a fixed window, no identity,
no dedup, no governance, and no machine‑native write path. The moment context outgrows the page, it
rots.

### 25.1 The OpenCortyx answer: context **is** the graph

> **We already replaced markdown files with a graph for *data*. Do the same for *conversations*.**

When a session ends, the agent **writes its context back into the same governed `node`/`edge` graph** —
no markdown, no second system. When a session starts, the agent **recalls** the relevant slice by
semantic + graph retrieval (not by re‑reading a whole doc). Because it's the same substrate as the
rest of OpenCortyx, session memory inherits everything we already built: embeddings + hybrid retrieval,
entity resolution (the same decision mentioned in three sessions **merges into one node**), bitemporal
`as_of` time‑travel, scope/sensitivity governance, trust/citations, and **graph export/backup (no
lock‑in)**.

**Why the graph wins over a handoff doc — point by point:**
- **No context‑limit decay.** Recall fetches only the *relevant* memories for the new task, not the
  whole history — so it never overflows the window.
- **Nothing gets lost.** Every decision/fact is its own node with provenance; superseded facts are
  versioned (bitemporal), not overwritten.
- **Dedup & disambiguation.** Entity resolution collapses "the auth refactor" mentioned across
  sessions into one node — the memory gets *sharper* over time instead of duplicating.
- **Shareable by design.** A session graph is just nodes/edges under a collection + scope → share it
  with a teammate, another agent, or a whole team (the SharePoint use‑case, done right).
- **Cross‑session, cross‑agent, cross‑project.** Memory isn't trapped in one chat or one tool; any
  MCP client reads/writes the same layer.
- **Governed write‑back.** Agents *propose* memories; they enter the same review/trust flow (§23) —
  never silent, unverifiable merges.

### 25.2 Data model (reuses the substrate — no new engine)

New built‑in node types (catalog templates, tagged `memory`):

- **`Session`** — one continuous working context. Props: title, agent, participants, project/collection,
  started/ended, summary, status.
- **`Turn`** *(optional, configurable retention)* — a message/exchange. Props: role, text, ordinal, ts.
- **`Decision`** — a choice made + rationale + alternatives considered. *The thing handoff docs lose.*
- **`Fact` / `Insight`** — a durable learned statement (e.g. "the worker needs a restart after edits").
- **`Preference`** — a user/working preference ("always use PowerShell syntax on Windows").
- **`Artifact`** — a produced output reference (PR, file, query, doc) tied to the session.

Edges: `PART_OF` (Turn→Session), `FOLLOWS` (Turn→Turn), `DECIDED` / `LEARNED` (Session→Decision/Fact),
`REFERENCES` (any→data node — link a decision to the actual table/doc it was about), `DERIVED_FROM`
(Fact→source), `SHARED_WITH` (Session→User/Team), `CONTINUES` (Session→Session).

Retention/PII: per‑collection policy — store full turns, or only distilled Decisions/Facts (an LLM
**summarizer extractor** runs at checkpoint to compress a transcript into durable memory nodes, so the
graph stays small and high‑signal). PII masking + scope apply exactly as elsewhere.

### 25.3 MCP tools (the agent surface) + dashboard

New MCP tools (thin, over `/api/v2`, same pattern as the existing 13):
- **`axiom_recall(query?, project?, limit?)`** — call at **session start**: returns the relevant
  Decisions/Facts/Preferences/open‑threads as compact context (semantic + graph retrieval, scoped).
- **`axiom_remember(text|decision|fact, refs?, scope?)`** — write a single memory node back mid‑session.
- **`axiom_save_session(summary, turns?, decisions?, refs?)`** / **`axiom_checkpoint()`** — at
  session end (or interval): distill + persist the session as a subgraph.
- **`axiom_list_sessions` / `axiom_resume(session_id)`** — find and reload a prior session's context.

Dashboard: a **"Memory / Sessions"** view — browse sessions, see the decision/fact timeline, search
memory, share a session with a teammate/team, edit or certify a memory, set retention. Same graph the
agents use (design rule: *never build for only humans or only agents*).

### 25.4 Build map (phased)

1. ✅ **Module + node types** — `memory`‑category built‑in templates (Session/Turn/Decision/Fact/
   Preference) + edges (PART_OF/FOLLOWS/…); auto‑seeded with the catalog.
2. ✅ **Writeback API + summarizer** — `/v2/memory/*` (save‑session/recall/remember/list/resume +
   `log-activity`); LLM distill of `turns` → Decisions/Facts.
3. ✅ **MCP tools** — `axiom_recall` / `axiom_remember` / `axiom_save_session` / `axiom_list_sessions` /
   `axiom_resume` (live in the 20‑tool MCP server).
4. ✅ **Dashboard Memory view** — Conversations page: session list + decision/fact/turn timeline.
   🔜 share/certify/retention controls in the UI.
5. 🟡 **Sharing & governance** — scope via API key/collection ✅; `SHARED_WITH` + routing proposed
   memories through review (§23) 🔜. **Consent‑gated server‑side capture** ✅ (Settings → Agents).
6. 🔜 **Recall quality** — recency + importance weighting, "open threads / next steps" surfacing,
   cross‑session dedup via entity resolution.

> **The pitch in one line:** *Stop pasting markdown summaries between sessions. Your AI's memory
> becomes a shared, governed graph that dedups, never overflows the context window, time‑travels, and
> exports with zero lock‑in — the same Context OS that already holds your data now holds your context.*

---

## 26. Local‑first, no lock‑in & cost control (ops promises)

Cross‑cutting commitments that make the above safe to adopt — several already shipped:

| Promise | What it means | Status |
|---|---|---|
| **Local LLM = free** | vLLM BYO‑model path: no per‑token/per‑seat tax; runs fully offline. | ✅ (Model Registry + vLLM default) |
| **Low‑VRAM mode** | "Fast" toggle skips rerank+CRAG; small models (llama3.2:1b) run on 4 GB GPUs. | ✅ |
| **System Health** | One screen checks API/worker/DB/vLLM (reachable *and* serving the requested model) + live embed, with plain‑English fixes. | ✅ |
| **Full graph export / backup (no lock‑in)** | `GET /v2/export` → one portable JSON (nodes+edges+ontology+templates+pipelines, embeddings incl.); `POST /v2/import` (merge/replace, idempotent). Settings UI Export/Restore. Secrets never exported. | ✅ (JSON · Parquet 🔭) |
| **One‑click / desktop deploy** | One-command install and CLI (`install.sh` / `install.ps1` / `bin/opencortyx up`): check Docker → compose up → wait healthy → open dashboard. | ✅ scripts · 🔜 native installer (Electron/Tauri) |
| **Cost controls** | Per‑org monthly **token budget + alerts + hard cap** (refuses paid LLM when over; local stays free), live spend meter + $ estimate, LLM‑table cost guard on ingest. | ✅ (budget/cap/alerts + guard · model‑tier routing via Model Registry default) |

---

## 27. 💎 Connect anything — every DBMS, data lake & table format, securely

OpenCortyx's connector layer is **SQLAlchemy-URI-generic**: introspection, browse and ingestion go
through one driver-agnostic path, so *any* source with a SQLAlchemy dialect works the moment its
driver is present. The UI now exposes the famous engines (each pre-fills a URI template), plus an
"Other — any SQLAlchemy URI" escape hatch. Drivers for **Postgres / MySQL / SQLite** are bundled;
others are a one-line install (Test reports a missing driver clearly).

| Category | Targets | Status |
|---|---|---|
| **Relational DBMS** | PostgreSQL ✅, MySQL/MariaDB ✅, SQLite ✅, Oracle ✅, CockroachDB ✅, ClickHouse ✅, SQL Server | ✅ pg/mysql/sqlite/oracle/cockroach/clickhouse bundled · 🔜 SQL Server (pyodbc/pymssql) |
| **Cloud warehouses / lakes** | Trino·Presto ✅, Snowflake, BigQuery, Redshift, Databricks/Delta, Athena (S3) | ✅ Trino bundled · 🟡 others URI-wired · 🔜 bundle heavy SDKs + auth helpers |
| **Table / file formats** | **Delta Lake, Apache Iceberg, Apache Hudi** — compositional: a **storage** connector (S3/GCS/ADLS/local) + a **format** connector (references the lake + directory + a **DuckDB** query engine) | ✅ built + live-verified (local Hudi/parquet) · ✅ Delta/Iceberg via DuckDB extensions · 🔜 cloud-creds sample test |
| **Unstructured files in a lake** | PDF/docx/txt/… under an S3/GCS/ADLS/local directory → ingested as **documents** (parse→chunk→extract) via a `files` connector (fsspec read) | ✅ built + live-verified (local) |
| **Selection at ingest** | DataHub-style schema → table/view picker (multi-schema, parallel) | ✅ (works for any dialect that exposes schemas/tables) |

Build steps: bundle the common dialect drivers as optional extras (snowflake-sqlalchemy,
sqlalchemy-bigquery, sqlalchemy-redshift, databricks-sql, trino, clickhouse-sqlalchemy,
**duckdb-engine**); per-source auth helpers (BigQuery service-account JSON, Snowflake key-pair,
Databricks token, AWS IAM); a DuckDB file/lake connector that attaches Parquet/Iceberg/Delta and
exposes them as tables to the same introspection path.

### 27.1 Secure credentials — on-prem, never in the clear

Credentials stay **inside your OpenCortyx** and are never stored or logged in plaintext:
- ✅ **Encrypted at rest** (Fernet `enc:` inline), **env-var refs**, and **Azure Key Vault** (lazy SDK).
  `_connector_view` returns only `has_connection_string` / `secret_provider` — never the secret or DSN.
- ✅ Secrets resolve **in-worker** at run time (`resolve_connector_dsn`), so the DSN is never passed
  over the broker.
- 🔜 **On-prem secret stores**: HashiCorp Vault, AWS/GCP Secrets Manager (same `resolve_secret` switch).
- ✅ **Cloud lake auth (DuckDB-verified)**: Azure — service principal (+secret or +certificate), SAS,
  managed identity; AWS S3 — access key, or credential chain / **STS AssumeRole** (IAM); GCS — HMAC key.
  Storage connectors capture account name / region / bucket-container (the bucket/container is in the
  format connector's directory path). ❌ GCP service-account-JSON / Workload Identity is **not**
  supported by DuckDB's GCS reader — use a GCS HMAC key, or read GCP lakes via Trino/Spark.
- 🔜 **Secure tunnel**: SSH-tunnel / bastion and private-link options so a self-hosted OpenCortyx can
  reach a DB that isn't publicly exposed, with the tunnel credentials themselves held in the secret
  store. (The "secure tunnel only, credentials on-prem" model.)
- 🔜 Per-connector **least-privilege** guidance (read-only role) + connection allow-lists.

> Principle: a connector stores a **connection, not a credential** — the credential lives in a secret
> store and is resolved at run time, on the box that owns it.

---

*Living document — update status flags (✅/🟡/🔜/🔭) as features land. Companion docs:
`architecture.md`, `models.md`, `getting-started.md`.*


"Vision drift detection" — the architecture supports it (feedback loops, eval harness), but there's no explicit shipped feature yet. I've suggested reframing slightly.
"Personal mode for individuals" — the engine fully supports it, but the simplified UX (Add · Ask · Memory · Connect AI) isn't built yet. This is a frontend task, not an engine gap.


🚀 OpenCortyx — The Pitch
The Problem We're Solving
Look around. Data is fragmented everywhere.

Your notes are in one place. Your documents in another. Your databases somewhere else. Your AI conversations disappear the moment you close the tab. And when you try to make sense of it all — when you feed it to an AI and ask a question — what do you get? Half-truths. Hallucinations. Answers that sound right but feel wrong, because the AI doesn't understand you.

Here's the thing nobody talks about:

Data needs ownership. Data needs care. Data should tell the truth.

Right now, it doesn't. Your information is scattered, orphaned, and context-less. Nobody is looking after it. Nobody is asking: "Does this data still reflect what I believe? Does it align with where I'm going?"

And that's the real crisis — not that we have too little data, but that our data has lost its connection to our vision.

Why This Matters — For Everyone With a Vision
If you're a business, you should be able to look at your information and immediately spot: "This is wrong. This piece of data is deviating from our business vision." Your data should be a mirror of your strategy — not a junk drawer.

If you're a founder, the same thing. Your scattered notes, investor decks, product docs, customer conversations — they should all reflect your vision. When they drift, you should know. When your vision evolves, your context should evolve with it.

If you're a researcher, a student, a creator — anyone with a purpose — your information should serve that purpose. Not just sit in folders. Not just get dumped into ChatGPT and hope for the best.

Every piece of data should be seen through the eyes of the person or business it belongs to.

What Everyone Else Is Doing (And Why It Fails)
Today, people try to solve this with brute force:

Hardcoding massive prompts
Iterating endlessly to get better recall and precision
Dumping everything into vector databases and praying the AI figures it out
It's like giving someone a library with no index, no categories, no librarian — and saying "find me the answer." Of course they'll get it wrong.

They're extracting randomly. Pulling out whatever the algorithm grabs, with no understanding of what matters to you.

What OpenCortyx Does Differently
OpenCortyx flips the entire approach:

Instead of randomly extracting things, we extract what the business needs. We enrich information with what people need for their vision.

Here's the shift:

You tell OpenCortyx your vision — what your business cares about, what information matters to you, what your world looks like.
OpenCortyx unifies everything — your documents, databases, conversations, notes — into one place, giving every piece of data an identity, traceability, and citations.
It enriches your data through the lens of your vision — not generic extraction, but purposeful enrichment aligned with what you actually need.
Then it serves that context to AI — and because the context is already aligned, truthful, and traceable, the AI gets it right. Every time.
The result?

High recall, high precision — not through prompt tricks, but because the information is already right.
Fewer tokens — because you're not flooding the AI with noise, only signal.
Complete auditability — every answer traces back to a source, with citations.
Vision tracking — by tracing conversations and usage, OpenCortyx can identify when your vision changes and rearrange itself accordingly.
The Core Insight
If a human understands the context, a brilliant AI agent can identify it too.

That's the key. We're not asking AI to magically understand your business from raw data. We're saying: humans define the vision, humans define what information matters — and OpenCortyx bridges that understanding to the AI.

It's more than just a retrieval tool. It's a context engine that thinks the way you think.

Who It's For
You Are...	OpenCortyx Gives You...
A visionary founder	An AI that finally understands your business the way you do — and evolves as your vision evolves.
A business / organization	A mirror that shows where your data deviates from your strategy — and fixes it.
A researcher	Grounded answers over your papers, with provenance, citations, and time-travel.
A student	A study buddy that cites your actual notes — and never makes things up.
A creator / individual	A private, personal memory for your AI — aligned with your worldview.
One Line
"OpenCortyx doesn't just give AI your data. It gives AI your truth."

Under the Hood (For the Curious)
For those who want the technical depth — here's what powers the above:

Unified Substrate: One store (Postgres + pgvector) replaces your vector DB + graph DB + data catalog. No more stitching three products together.
Ontology-Guided Extraction: A declarative ontology — your business vocabulary — drives what gets extracted. Not generic NER. Your concepts, your rules.
Provenance & Citations: Every piece of derived knowledge carries a source reference, confidence score, and citation chain. No citation = not served.
Abstain Over Hallucinate: If there's no grounded context above the confidence threshold, OpenCortyx says "I don't have that" instead of making something up.
Hybrid Retrieval: Vector + BM25 + entity-graph + hierarchy + GraphRAG community + multi-hop, fused with RRF and a rerank cascade.
Token Efficiency: Top-down retrieval (summaries first), sentence-level strips, dedup, confidence-ranked truncation.
Self-Hostable & Model-Agnostic: Runs on your machine. Bring your own LLM. Your data never leaves your environment.
MCP Server: 20 tools registered — connect Claude, Cursor, or any AI tool instantly.
The Vision
Most tools ask: "How do we make AI smarter?"

We ask: "How do we make the data worthy of AI?"

Fix the data. Align it with your truth. Give it care, identity, and purpose. The AI will follow.

OpenCortyx — give your AI a memory it can be trusted with.
