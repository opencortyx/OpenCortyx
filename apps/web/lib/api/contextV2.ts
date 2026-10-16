import { getSession } from "next-auth/react";
import { apiClient } from "./client";

// Client for the OpenCortyx Context Layer (mounted at /api/v2). This is the only API surface.
/** One asset a cited node came from, and the chunk that says it. */
export interface CitationSource {
  asset_id: string; label: string; type: string;
  via: string | null;      // the chunk (or record) that links the node to this asset
  snippet: string; score: number;
}

/** Every citation has an `asset_id`, or an `origin` saying why not (curation | template | memory | system). */
export interface Citation {
  node_id: string; type: string; score: number; text?: string;
  asset_id?: string | null; asset_label?: string | null; asset_type?: string | null;
  origin?: string;
  sources?: CitationSource[];
  source_count?: number;
  source_count_capped?: boolean;
  supports?: boolean;      // the answer rests on this one (others were retrieved but not used)
}

export interface ContextResult {
  abstained: boolean;
  reason?: string;
  context: string;
  citations: Citation[];
  caveats?: string[];
  tokens_used?: number;
  confidence: number;
  patterns?: string[];
  chat_id?: string;          // set when the turn was saved to chat history
}

export interface AnswerResult {
  answer: string;
  abstained: boolean;
  grounded: boolean;
  unsupported_sentences?: string[];
  citations: ContextResult["citations"];
  caveats?: string[];
  confidence: number;
  chat_id?: string;
}

export interface RetrievalOpts {
  retrievers?: string[];     // NOT HONOURED by the v2 engine - reported in `unsupported`
  rerank?: string[];         // ["cross"] | ["cross","llm"] | [] (none)
  k?: number;
  abstain_threshold?: number;
  fast?: boolean;            // low-latency / low-VRAM: skip rerank + CRAG eval LLM call
  decompose?: boolean;       // break a complex question into sub-queries
  scope?: "knowledge" | "conversations" | "all";
  chat_id?: string | null;   // record this turn in that chat (the Assistant sidebar)…
  save_chat?: boolean;       // …or in a new one, whose id comes back with the answer
}

// ── chat history (the Assistant sidebar) ──
export interface ChatThread {
  id: string; title: string; message_count: number;
  created_at: string | null; updated_at: string | null;
}
export interface ChatMessageRow {
  id: string; role: "user" | "assistant"; text: string; meta: any; created_at: string | null;
}

export interface RetrieverCatalog {
  retrievers: { name: string; label: string; desc: string }[];
  rerankers: { name: string; label: string; desc: string }[];
  // `profiles` is gone: v2 selects surfaces PER QUESTION from the plan rather than
  // from a named preset. The catalogue now returns `surfaces` describing what runs.
  surfaces?: { name: string; label: string; desc: string }[];
  defaults: { k: number; abstain_threshold: number; rerank: string[] };
}

export interface StatsResult {
  nodes_by_type: Record<string, number>;
  edges_by_type: Record<string, number>;
  embedded_nodes: number;
  total_nodes: number;
  total_edges: number;
}

export interface HealthCheck {
  name: string;
  status: "ok" | "warn" | "down";
  detail: string;
  fix?: string;
}

export interface HealthResult {
  overall: "ok" | "warn" | "down";
  checks: HealthCheck[];
  ts?: string;
}

export interface CostStatus {
  monthly_token_budget: number;
  alert_threshold: number;
  hard_cap: boolean;
  usd_per_1k_tokens: number;
  spent_tokens: number;
  fraction: number;
  alert: boolean;
  over: boolean;
  spent_usd: number | null;
  budget_usd: number | null;
}

export interface BudgetInput {
  monthly_token_budget: number;
  alert_threshold: number;
  hard_cap: boolean;
  usd_per_1k_tokens: number;
}

export interface AssetItem {
  id: string;
  label: string;
  type?: string;
  table?: string;
  domain?: string;
  mime?: string | null;
  description?: string;
  kind?: "document" | "semistructured" | "structured";
  format?: string | null;
  source?: string | null;
  fingerprint?: string | null;
  rows?: number | null;
  columns?: number | null;
  words?: number | null;
  sensitivity?: string;
  updated_at?: string | null;
}

export interface CollectionDeletePreview {
  collection_id: string;
  name: string;
  nodes: number;
  edges: number;
  by_type: { type: string; count: number }[];
}

export interface AssetDeletePreview {
  id: string;
  label: string | null;
  type: string;
  nodes: number;
  edges: number | null;          // null when the asset is too large to count exactly up-front
  by_type: Record<string, number>;
}

export interface AssetDeleteResult extends AssetDeletePreview {
  deleted: boolean;
  edges: number;
  runs: number;                  // Progress runs removed
  manifest_entries: number;      // sync-manifest entries forgotten
}

export interface ProgressStep {
  i: number;
  name: string;
  status: "running" | "completed" | "failed" | "skipped";
  count?: number | null;
}

export interface ProgressRun {
  id: string;
  kind: string;
  asset: string;
  status: "queued" | "running" | "completed" | "failed" | "canceled";
  percent: number;
  current_step: number;
  total_steps: number;
  steps: ProgressStep[];
  error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  result?: Record<string, any>;
  source_ref?: string | null;
}

export interface ProgressSummary {
  total: number;
  queued: number;
  running: number;
  completed: number;
  failed: number;
  canceled: number;
}

export interface Pipeline {
  id: string;
  name: string;
  description: string;
  kind: "document" | "structured" | "semistructured";
  stages: any[];
  stage_count: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export type MappingParadigm = "structured" | "semistructured" | "unstructured";

export interface SchemaMappingSummary {
  targets?: string[];
  nodes?: number;
  edges?: number;
}

export interface SchemaMappingRecord {
  id: string;
  name: string;
  paradigm: MappingParadigm;
  source_type: string;
  payload: any;                 // paradigm-specific body (semi: {structures,auto_accept,rules,...})
  summary: SchemaMappingSummary;
  status: "active" | "draft" | "paused" | "auto";
  created_at?: string | null;
  updated_at?: string | null;
}

/** A file type ingestion accepts, and the pipeline route that will read it. */
export interface IngestFormat { ext: string; name: string; decoder?: string | null; }
export interface IngestRoute {
  route: "document" | "semistructured" | "structured";
  label: string;
  reads: string;
  count: number;
  accept: string;                 // ready for an <input type="file" accept=…>
  formats: IngestFormat[];
}
export interface IngestFormats { routes: IngestRoute[]; total: number; }

/** One shape of names, used for every side of a conformance answer. */
export interface ShapeNames { nodes: string[]; edges: string[]; }

/**
 * Declared vs produced, by EXACT name, in both directions.
 *  missing  — declared and not produced: the rule isn't honoured (or the data has no such content).
 *  extra    — produced and never declared. An authoritative mapping should have none.
 *  platform — produced by the platform regardless of any mapping (Date links, provenance, reading
 *             order). Explained, never a violation.
 */
export interface MappingConformance {
  id: string;
  name: string;
  paradigm: MappingParadigm;
  asset_id: string;
  strict: boolean;
  conformant: boolean;
  declared: ShapeNames;
  produced: ShapeNames;
  missing: ShapeNames;
  extra: ShapeNames;
  platform: ShapeNames;
}

export interface SchemaMappingInput {
  name: string;
  paradigm: MappingParadigm;
  source_type?: string;
  payload?: any;
  summary?: SchemaMappingSummary;
  status?: string;
}

export interface SecretRef {
  provider: "inline" | "env" | "azure_keyvault";
  value?: string;
  var?: string;
  vault_url?: string;
  secret_name?: string;
}

export interface ConnectorSpec {
  name: string;
  kind?: string;   // postgres | mysql | sqlite | mssql | oracle | snowflake | bigquery | duckdb | … (any SQLAlchemy dialect)
  host?: string;
  port?: number;
  database?: string;
  username?: string;
  schema_name?: string;
  secret?: SecretRef;
  connection_string?: string;
  // data lake (s3/gcs/adls storage) + table format (delta/iceberg/hudi)
  options?: Record<string, any>;
  lake_connector_id?: string;
  directory?: string;
  query_engine_id?: string;
}

export interface Connector {
  id: string;
  name: string;
  kind: string;
  host?: string | null;
  port?: number | null;
  database?: string | null;
  username?: string | null;
  schema?: string | null;
  secret_provider?: string | null;
  has_connection_string?: boolean;
  created_at?: string | null;
}

export interface BrowseTable {
  name: string;
  type: "table" | "view";
  columns: string[];
}
export interface BrowseSchema {
  schema: string;
  tables: BrowseTable[];
}

export interface SchemaSelection {
  schema: string;
  tables: string[];
  views: string[];
}

export interface Ingestion {
  id: string;
  name: string;
  kind: string;
  connector_id?: string | null;
  pipeline_id?: string | null;
  selections: SchemaSelection[];
  object_count: number;
  schemas: string[];
  directories?: string[];
  formats?: string[];
  enrich: boolean;
  schedule?: string | null;
  // "one_time" = a device upload: it ran once and can't be re-run (the files aren't on the server)
  mode?: "on_demand" | "scheduled" | "realtime" | "one_time";
  source?: "device_upload" | "connector" | null;
  files?: string[];                        // one_time: the uploaded file names
  run_ids?: string[];                      // the runs behind this ingestion
  run_status?: Record<string, number>;     // one_time: {completed: 3, failed: 1, …}
  paused?: boolean;
  hibernated?: boolean;
  mappings?: { connector_id: string | null; pipeline_id: string | null; directories?: string[]; formats?: string[] }[];
  failures?: { at: string; category: string; message: string; run_id?: string | null }[];
  failure_count?: number;
  sync_summary?: { tracked: number };
  status: string;
  last_run_id?: string | null;
  last_run_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export type IngestionKind = "structured" | "unstructured" | "semistructured";

export interface IngestionSpec {
  name: string;
  connector_id?: string;
  pipeline_id?: string;
  selections?: SchemaSelection[];
  enrich?: boolean;
  schedule?: string;
  mode?: "on_demand" | "scheduled" | "realtime";
  paused?: boolean;
  kind?: IngestionKind;
  directories?: string[];
  formats?: string[];
  mappings?: { connector_id: string | null; pipeline_id: string | null; directories?: string[]; formats?: string[] }[];
}

// ── Manual-curation Resources (glossary / synonym / rule / business context) ──
export interface GlossaryInput {
  term: string; definition: string; targets?: string[]; applies_to?: string;
  scope?: string; status?: string; owner?: string; source?: string;
  valid_from?: string; valid_until?: string; examples?: string[];
}
export interface SynonymInput {
  term: string; targets?: string[]; target_id?: string; kind?: string;
  scope?: string; status?: string; owner?: string; source?: string;
}
export interface RuleInput {
  title: string; content: string; targets?: string[]; applies_to?: string;
  severity?: string; kind?: string; scope?: string; status?: string;
  owner?: string; source?: string; valid_from?: string; valid_until?: string;
}
export interface TypeResolution {
  identify_by: string[]; compare: string[]; ignore: string[]; enabled: boolean;
  relations?: string[]; scorer: "exact" | "similar"; auto: number; review: number;
}
export interface BusinessContextInput {
  title: string; content: string; scope?: string; collection_id?: string;
  always_on?: boolean; kind?: string; owner?: string; source?: string;
}
export interface BusinessContextEntry {
  id: string; title: string; content: string; scope: string;
  always_on: boolean; collection_id?: string | null;
}

export interface ModelEntry {
  id: string;
  name: string;
  role: string;   // generation | embedding | rerank (wired) + extended registry roles
  provider: string;
  model_id: string;
  base_url?: string | null;
  params: Record<string, any>;
  is_default: boolean;
  has_key: boolean;
  created_at?: string | null;
}

// The model in effect per role (GET /models/active): a registry row, or none. There is no other source.
export interface ActiveModel {
  role: string;   // any registry role — the default set for it is what that role uses
  source: "registry" | "none";
  shared_from?: string;   // extraction/utility with no row of their own use the generation model
  provider: string | null;
  model_id: string | null;
  base_url?: string | null;
  name?: string | null;
  has_key: boolean;
  note?: string;
}

export interface ModelSpecInput {
  name: string;
  role: string;
  provider: string;
  model_id: string;
  base_url?: string;
  api_key?: string;
  params?: Record<string, any>;
  is_default?: boolean;
}

// One model a provider serves right now — an entry of the server's /v1/models list.
export interface DiscoveredModel {
  id: string; size?: number | null; detail?: string | null;
  dim?: number | null;          // fastembed embedders: the vector size
  downloaded?: boolean;         // fastembed: already in the model cache
}
export interface ModelDiscovery { items: DiscoveredModel[]; source: string; provider: string; note?: string | null; }

// One element of a document as Docling reads it (POST /authoring/layout).
export interface LayoutElement {
  id: string; label: string; type: string; text: string;
  level: number | null; page: number; parent_id: string | null;
}
export interface LayoutReading {
  filename: string; engine: string; fallback_reason: string | null; doc_id: string;
  pages_read: number; elements: LayoutElement[];
}

// A Hugging Face model, before it is downloaded (POST /models/hf/info).
export interface HFModelInfo {
  repo: string; task: string | null; bytes: number; gguf: boolean; gated: boolean;
  runs_in_opencortyx: boolean; why_not: string | null; memory_bytes: number;
}
// A download into OpenCortyx's model cache (GET /models/hf/download).
export interface HFDownloadStatus {
  repo?: string; model?: string; status: "none" | "downloading" | "done" | "failed";
  done_bytes: number; total_bytes: number | null; error: string | null;
}
// One line of an Ollama pull's progress, as Ollama reports it.
export interface OllamaPullEvent { status?: string; model?: string; total?: number; completed?: number; error?: string; }

export interface RuntimeModelRow {
  id: string; name: string; role: string; model_id: string; weights?: string | null;
  status: "ok" | "mismatch" | "unreachable"; hint: string;
}
export interface RuntimeServer {
  base_url: string; reachable: boolean; error?: string | null;
  served_models: string[]; models: RuntimeModelRow[]; is_generation: boolean;
}
export interface ModelRuntime { servers: RuntimeServer[]; note: string; }

export interface AppUser {
  id: string; email: string; role: "admin" | "editor" | "viewer"; status: string; created_at?: string | null;
}
export interface ApiKeyView { id: string; app: string; role: string; created_at?: string | null; }

export const SYSTEM_DEFAULT_PIPELINES_LIST: Pipeline[] = [
  {
    id: "00000000-0000-4000-8000-000000000001",
    name: "Default Pipeline 1: Universal SQL Ingestion Engine",
    description: "System default pipeline for structured SQL ingestion. Automatically maps Tables to Table Nodes, PKs to Identities, and Foreign Keys to Edges.",
    kind: "structured",
    is_system_default: true,
    stages: [{ id: "stage_0", type: "base_schema", action: "base_schema", config: { mode: "auto", auto_mode: "row", auto_detect: true, auto_accept: true } }]
  } as any,
  {
    id: "00000000-0000-4000-8000-000000000002",
    name: "Default Pipeline 2: Universal JSON Ingestion Engine",
    description: "System default pipeline for semi-structured JSON payloads. Uses prose density heuristics to find the text in each record.",
    kind: "semistructured",
    is_system_default: true,
    stages: [
      { id: "stage_0", type: "json_structures", action: "json_structures", config: { auto_accept: true, auto_text_min_chars: 40 } },
    ]
  } as any,
  {
    id: "00000000-0000-4000-8000-000000000003",
    name: "Default Pipeline 3: Universal Document Ingestion Engine",
    description: "System default pipeline for unstructured PDF/Docx files. Layout AST chunking.",
    kind: "document",
    is_system_default: true,
    stages: [
      { id: "stage_0", type: "chunking", action: "chunking", config: { auto_accept: true, strategy: "layout" } },
    ]
  } as any,
];


// ── Authoring: drafted mappings, pipelines and extractors ──────────────────
/**
 * A proposal the server derived from real data, with its reasons attached.
 *
 * `payload` is the exact request body `saveEndpoint` accepts — that is the contract, and it is
 * why saving a draft is one POST with no re-wrapping on this side. Editing a draft means editing
 * `payload` and posting it; there is no separate "accept draft" endpoint to keep in step.
 */
export type DraftKind = "mapping" | "pipeline" | "extractor";
export type DraftConfidence = "high" | "medium" | "low";

export interface DraftEvidence {
  claim: string;          // "body → prose that gets read and extracted from"
  basis: string;          // "str of 412+ characters: 'Northwind Supplies shall…'"
  path?: string;
}

export interface Draft {
  kind: DraftKind;
  name: string;
  payload: Record<string, any>;
  drafter: string;
  confidence: DraftConfidence;
  summary: string;
  evidence: DraftEvidence[];
  warnings: string[];
  save_endpoint: string;
  model: string;
}

export interface DraftResponse {
  drafts: Draft[];
  sample: { filename?: string; route?: string; format?: string; records?: number;
            columns?: string[]; rows?: number; text_chars?: number;
            truncated?: boolean; warnings?: string[] };
  available: { name: string; kind: string; description: string }[];
}

/** One thing worth doing about a finished run, with everything needed to do it. */
export interface OutcomeStep {
  action: "ask" | "refine_mapping" | "extract_more" | "open_asset" | "retry" | "replace";
  label: string;
  reason: string;
  href?: string;
  payload?: Record<string, any>;
}

export interface OutcomeTypeCount {
  type: string;
  count: number;
  examples: string[];
}

export interface IngestOutcome {
  run_id: string;
  status: string;
  asset: string;
  kind: string;
  found: OutcomeTypeCount[];        // entities — what the user was looking for
  structural: OutcomeTypeCount[];   // Source/Chunk/… — how it works, not what it found
  total_nodes: number;
  duration_seconds: number | null;
  error: string;
  next_steps: OutcomeStep[];
  reported: Record<string, any>;
  notes: string[];
  /** The run finished but work it queued has not (a document's deferred extraction). */
  pending?: boolean;
}

/** One decision of the resolution engine, as the Review queue shows it. */
export type ResolutionDecision = {
  id: string; relation: "SAME" | "SUPPORTS" | "EXTENDS" | "CONTRADICTS"; node_type: string;
  subject: string; subject_label?: string | null; target?: string | null; target_label?: string | null;
  attribute?: string | null; evidence: Record<string, any>; needs_review: boolean;
  confidence: number; method: string; source_id?: string | null; source_label?: string | null;
  created_at?: string | null;
};

export const contextV2Api = {
  resetDatabase: () => apiClient.delete("/api/v2/reset").then((r) => r.data),
  // Clear the task QUEUE: drop queued broker messages + revoke running/scheduled tasks (admin).
  purgeQueue: () => apiClient.post("/api/v2/queue/purge").then((r) => r.data),
  // Un-wedge the worker: kill its currently-executing tasks (queued work survives and resumes).
  // hard=true additionally shuts it down so Docker's restart policy revives it with fresh state.
  resetWorker: (hard = false) =>
    apiClient.post(`/api/v2/worker/reset?hard=${hard}`).then((r) => r.data),

  // ── auth / users / api keys (RBAC) ──────────────────────────────────────
  authNeedsSetup: () => apiClient.get<{ needs_setup: boolean }>("/api/v2/auth/needs-setup").then((r) => r.data),
  authSetup: (email: string, password: string, adopt_demo = true,
              workspace: { mode?: "person" | "team"; name?: string } = {}) =>
    apiClient.post("/api/v2/auth/setup", { email, password, adopt_demo, ...workspace }).then((r) => r.data),
  authMe: () => apiClient.get<{ user_id: string; workspace_id: string; role: string }>("/api/v2/auth/me").then((r) => r.data),
  listUsers: () => apiClient.get<{ items: AppUser[] }>("/api/v2/users").then((r) => r.data),
  createUser: (email: string, password: string, role: string) =>
    apiClient.post<AppUser>("/api/v2/users", { email, password, role }).then((r) => r.data),
  updateUser: (id: string, patch: { role?: string; status?: string; password?: string }) =>
    apiClient.patch(`/api/v2/users/${id}`, patch).then((r) => r.data),
  deleteUser: (id: string) => apiClient.delete(`/api/v2/users/${id}`).then((r) => r.data),
  listApiKeys: () => apiClient.get<{ items: ApiKeyView[] }>("/api/v2/apikeys").then((r) => r.data),
  createApiKey: (app: string, role: string) =>
    apiClient.post<{ id: string; app: string; role: string; key: string }>("/api/v2/apikeys", { app, role }).then((r) => r.data),
  revokeApiKey: (id: string) => apiClient.delete(`/api/v2/apikeys/${id}`).then((r) => r.data),

  // ── integrations: one-click MCP connect for AI agents / IDEs (Claude Code, Antigravity) ──
  integrationsMcp: () => apiClient.get("/api/v2/integrations/mcp").then((r) => r.data),
  // ── agent memory & sessions (§25) ──
  recallMemory: (q: string, limit = 12) =>
    apiClient.get("/api/v2/memory/recall", { params: { q, limit } }).then((r) => r.data),
  // Save the current chat as durable, searchable context. Pass back session_id to checkpoint
  // (idempotent upsert) the same conversation instead of creating a duplicate.
  saveSession: (spec: {
    session_id?: string | null; title?: string; agent?: string; summary?: string;
    turns?: { role: string; text: string }[]; refs?: string[];
  }) => apiClient.post<{ session_id: string; summary?: string }>("/api/v2/memory/save-session", spec).then((r) => r.data),
  listSessions: (limit = 50) =>
    apiClient.get("/api/v2/memory/sessions", { params: { limit } }).then((r) => r.data),
  memorySession: (id: string) =>
    apiClient.get(`/api/v2/memory/sessions/${id}`).then((r) => r.data),
  // ── chat history (Assistant sidebar) ──
  listChats: (q = "", limit = 50, offset = 0) =>
    apiClient.get<{ items: ChatThread[]; total: number }>("/api/v2/chats", { params: { q, limit, offset } })
      .then((r) => r.data),
  getChat: (id: string) =>
    apiClient.get<ChatThread & { messages: ChatMessageRow[] }>(`/api/v2/chats/${id}`).then((r) => r.data),
  renameChat: (id: string, title: string) =>
    apiClient.patch<ChatThread>(`/api/v2/chats/${id}`, { title }).then((r) => r.data),
  deleteChat: (id: string) => apiClient.delete(`/api/v2/chats/${id}`).then((r) => r.data),
  // ── resolution decisions (Review → Auto-resolution / Conflicts) ──
  resolutionDecisions: (params: { needs_review?: boolean; node_id?: string; limit?: number } = {}) =>
    apiClient.get<{ items: ResolutionDecision[] }>("/api/v2/resolution/decisions", { params }).then((r) => r.data.items),
  decideConflict: (id: string, choice: "keep" | "use_new") =>
    apiClient.post(`/api/v2/resolution/decisions/${id}/decide`, { choice }).then((r) => r.data),
  // ── eval harness + feedback loop (§7) ──
  evalCases: () => apiClient.get("/api/v2/eval/cases").then((r) => r.data),
  addEvalCase: (question: string, expect_keywords?: string[], should_abstain?: boolean) =>
    apiClient.post("/api/v2/eval/cases", { question, expect_keywords, should_abstain }).then((r) => r.data),
  deleteEvalCase: (id: string) => apiClient.delete(`/api/v2/eval/cases/${id}`).then((r) => r.data),
  runEval: () => apiClient.post("/api/v2/eval/run").then((r) => r.data),
  runEvalCase: (id: string) => apiClient.post(`/api/v2/eval/run-case/${id}`).then((r) => r.data),
  feedback: () => apiClient.get("/api/v2/feedback").then((r) => r.data),
  sendFeedback: (query: string, rating: string, correction?: string) =>
    apiClient.post("/api/v2/feedback", { query, rating, correction }).then((r) => r.data),
  
  // ── mapping ────────────────────────────────────────────────────────────
  introspectSql: (payload: { connection_string?: string; connector_id?: string; schema_name?: string }) =>
    apiClient.post("/api/v2/structured/introspect-sql", payload).then((r) => r.data),
  // per-extractor attribute catalog (enable/disable attributes per stage in the structured builder)
  structuredExtractorAttrs: () =>
    apiClient.get<{ attributes: Record<string, { key: string; label: string; default: boolean }[]> }>(
      "/api/v2/structured/extractors").then((r) => r.data),
  getTemplates: () => apiClient.get("/api/v2/templates").then((r) => r.data),

  uploadDocument: (file: File, features?: string[], nodes?: string[], relationships?: string[], batchId?: string,
                   anchors?: any[], assetPath?: string, tags?: string[]) => {
    const fd = new FormData();
    fd.append("file", file);
    if (features?.length) fd.append("features", features.join(","));
    if (nodes?.length) fd.append("nodes", nodes.join(","));
    if (relationships?.length) fd.append("relationships", JSON.stringify(relationships));
    if (batchId) fd.append("batch_id", batchId);
    if (anchors?.length) fd.append("anchors", JSON.stringify(anchors));
    if (assetPath) fd.append("asset_path", assetPath);
    if (tags?.length) fd.append("tags", JSON.stringify(tags));
    return apiClient.post("/api/v2/sources/upload", fd, {
      headers: { "Content-Type": "multipart/form-data" },
    }).then((r) => r.data);
  },

  /** Upload a file from an auto-detect source: the server detects its kind and uses that kind's pipeline. */
  uploadAutoDetected: (file: File, pipelines: Record<string, string>, batchId?: string,
                       anchors?: any[], assetPath?: string, tags?: string[]) => {
    const fd = new FormData();
    fd.append("file", file);
    const chosen = Object.fromEntries(Object.entries(pipelines || {}).filter(([, id]) => id));
    if (Object.keys(chosen).length) fd.append("pipeline_ids", JSON.stringify(chosen));
    if (batchId) fd.append("batch_id", batchId);
    if (anchors?.length) fd.append("anchors", JSON.stringify(anchors));
    if (assetPath) fd.append("asset_path", assetPath);
    if (tags?.length) fd.append("tags", JSON.stringify(tags));
    return apiClient.post("/api/v2/sources/upload", fd, {
      headers: { "Content-Type": "multipart/form-data" },
    }).then((r) => r.data);
  },

  verifyJsonAgainstMappings: (file: File, mappingIds: string[]) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("options", JSON.stringify({ mapping_ids: mappingIds }));
    fd.append("batch_id", `verify:${[...mappingIds].sort().join(",")}`);
    return apiClient.post("/api/v2/json/ingest", fd, {
      headers: { "Content-Type": "multipart/form-data" },
    }).then((r) => r.data);
  },
  // `parse` carries the file's dialect (delimiter / header / encoding …) so the ingestion reads the
  // file exactly the way the preview did — otherwise the author verifies one thing and ingests another.
  verifyTabularAgainstMappings: (file: File, mappingIds: string[], parse?: Record<string, any>) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("overrides", JSON.stringify({ mapping_ids: mappingIds, ...(parse || {}) }));
    fd.append("batch_id", `verify:${[...mappingIds].sort().join(",")}`);
    return apiClient.post("/api/v2/structured/ingest-file", fd, {
      headers: { "Content-Type": "multipart/form-data" },
    }).then((r) => r.data);
  },

  ingestJson: (file: File, options?: Record<string, any>, batchId?: string, tags?: string[]) => {
    const fd = new FormData();
    fd.append("file", file);
    if (options) fd.append("options", JSON.stringify(options));
    if (batchId) fd.append("batch_id", batchId);
    if (tags?.length) fd.append("tags", JSON.stringify(tags));
    return apiClient.post("/api/v2/json/ingest", fd, {
      headers: { "Content-Type": "multipart/form-data" },
    }).then((r) => r.data);
  },

  // Inline JSON ingestion: the browser parses + slices the file, then sends the (smaller) JSON as a
  // body — so a huge file never has to be uploaded whole. Same walker + options as ingestJson.
  ingestJsonInline: (data: any, source_label: string, options?: Record<string, any>, batchId?: string) =>
    apiClient.post("/api/v2/json/ingest_inline",
      { data, source_label, options: options || {}, batch_id: batchId })
      .then((r) => r.data),

  uploadDocumentWithPipeline: (file: File, pipelinePayload: any, batchId?: string, tags?: string[]) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("pipeline_config", JSON.stringify(pipelinePayload));
    if (batchId) fd.append("batch_id", batchId);
    if (tags?.length) fd.append("tags", JSON.stringify(tags));
    return apiClient.post("/api/v2/sources/upload_flow", fd, {
      headers: { "Content-Type": "multipart/form-data" },
    }).then((r) => r.data);
  },
  ingestSql: (payload: { connection_string?: string; connector_id?: string; schema_name?: string; enrich?: boolean; table_mappings?: Record<string, string>; pipeline?: any[] }) =>
    apiClient.post("/api/v2/structured/ingest-sql", payload).then((r) => r.data),

  // Tabular FILE (CSV/TSV/Parquet/Excel) via DuckDB → structured pipeline.
  // previewTabularFile: auto-detect shape (no ingest) → {detected, columns, sample_rows, row_count}.
  previewTabularFile: (file: File, overrides?: Record<string, any>) => {
    const fd = new FormData();
    fd.append("file", file);
    if (overrides) fd.append("overrides", JSON.stringify(overrides));
    return apiClient.post("/api/v2/structured/preview-file", fd, {
      headers: { "Content-Type": "multipart/form-data" },
    }).then((r) => r.data);
  },
  ingestTabularFile: (file: File, pipelineId?: string, overrides?: Record<string, any>, collectionId?: string, batchId?: string) => {
    const fd = new FormData();
    fd.append("file", file);
    if (pipelineId) fd.append("pipeline_id", pipelineId);
    if (overrides) fd.append("overrides", JSON.stringify(overrides));
    if (collectionId) fd.append("collection_id", collectionId);
    if (batchId) fd.append("batch_id", batchId);
    return apiClient.post("/api/v2/structured/ingest-file", fd, {
      headers: { "Content-Type": "multipart/form-data" },
    }).then((r) => r.data);
  },
  // DOCUMENT files (pdf/docx/txt/json/…) that live in a file/lake connector — the pool a mapping can be
  // verified against without uploading anything to the browser. `exts` filters (e.g. "json").
  listConnectorDocumentFiles: (connectorId: string, path = "", exts = "") =>
    apiClient.get<{ path: string; files: { name: string; path: string }[] }>(
      `/api/v2/connectors/${connectorId}/document-files?path=${encodeURIComponent(path)}`
      + (exts ? `&exts=${encodeURIComponent(exts)}` : "")).then((r) => r.data),
  // tabular files that live in a file/lake connector (server-side, streamed by DuckDB — not uploaded).
  listConnectorTabularFiles: (connectorId: string, path = "") =>
    apiClient.get<{ path: string; files: { name: string; path: string }[] }>(
      `/api/v2/connectors/${connectorId}/tabular-files?path=${encodeURIComponent(path)}`).then((r) => r.data),
  ingestConnectorTabularFiles: (connectorId: string, paths: string[], pipelineId?: string, collectionId?: string,
                                directories?: string[], anchors?: any[]) =>
    apiClient.post("/api/v2/structured/ingest-connector-files",
      { connector_id: connectorId, paths, directories, pipeline_id: pipelineId, collection_id: collectionId,
        // Each table anchors on the file's own path, so one rule set covers a whole folder tree.
        anchors: anchors?.length ? anchors : undefined }).then((r) => r.data),
  enrichNative: () => apiClient.post("/api/v2/structured/enrich-native").then((r) => r.data),

  // ── saved pipelines (authored in the Pipelines dashboard, assigned at ingest) ──
  listPipelines: (kind?: "document" | "structured" | "semistructured") =>
    apiClient.get<{ items: Pipeline[] }>(
      `/api/v2/pipelines${kind ? `?kind=${kind}` : ""}`
    )
      .then((r) => {
        const fetched = r.data?.items || [];
        const defaults = SYSTEM_DEFAULT_PIPELINES_LIST.filter((p) => !kind || p.kind === kind);
        const fetchedIds = new Set(fetched.map((x) => x.id));
        const merged = [...defaults.filter((d) => !fetchedIds.has(d.id)), ...fetched];
        return { items: merged };
      })
      .catch(() => {
        const defaults = SYSTEM_DEFAULT_PIPELINES_LIST.filter((p) => !kind || p.kind === kind);
        return { items: defaults };
      }),
  getPipeline: (id: string) => {
    if (id === "00000000-0000-4000-8000-000000000001" || id === "default-pipeline-sql") {
      return Promise.resolve(SYSTEM_DEFAULT_PIPELINES_LIST[0]);
    }
    if (id === "00000000-0000-4000-8000-000000000002" || id === "default-pipeline-json") {
      return Promise.resolve(SYSTEM_DEFAULT_PIPELINES_LIST[1]);
    }
    if (id === "00000000-0000-4000-8000-000000000003" || id === "default-pipeline-doc") {
      return Promise.resolve(SYSTEM_DEFAULT_PIPELINES_LIST[2]);
    }
    return apiClient.get<Pipeline>(`/api/v2/pipelines/${id}`).then((r) => r.data);
  },
  createPipeline: (spec: { name: string; description?: string; kind: string; stages: any[] }) =>
    apiClient.post<Pipeline>("/api/v2/pipelines", spec).then((r) => r.data),
  updatePipeline: (id: string, patch: { name?: string; description?: string; kind?: string; stages?: any[] }) => {
    if (id.startsWith("00000000-0000-4000-8000-") || id.startsWith("default-pipeline-")) {
      return apiClient.post<Pipeline>("/api/v2/pipelines", {
        name: patch.name || "Customized Pipeline",
        description: patch.description,
        kind: patch.kind || "structured",
        stages: patch.stages || []
      }).then((r) => r.data);
    }
    return apiClient.put<Pipeline>(`/api/v2/pipelines/${id}`, patch).then((r) => r.data);
  },
  deletePipeline: (id: string) => {
    if (id.startsWith("default-pipeline-")) return Promise.resolve({ status: "ok" });
    return apiClient.delete(`/api/v2/pipelines/${id}`).then((r) => r.data);
  },

  // Every file type ingestion accepts, grouped by the pipeline that handles it. Served from the SAME
  // registry that routes an upload (ingest/formats.py), so the list shown can never drift from reality.
  ingestFormats: () =>
    apiClient.get<IngestFormats>("/api/v2/ingest/formats").then((r) => r.data),

  listSchemaMappings: (paradigm?: MappingParadigm, light?: boolean) => {
    const qs = [paradigm ? `paradigm=${paradigm}` : "", light ? "light=1" : ""].filter(Boolean).join("&");
    return apiClient.get<{ items: SchemaMappingRecord[] }>(
      `/api/v2/schema-mappings${qs ? `?${qs}` : ""}`).then((r) => r.data);
  },
  getSchemaMapping: (id: string) =>
    apiClient.get<SchemaMappingRecord>(`/api/v2/schema-mappings/${id}`).then((r) => r.data),
  createSchemaMapping: (spec: SchemaMappingInput) =>
    apiClient.post<SchemaMappingRecord>("/api/v2/schema-mappings", spec).then((r) => r.data),
  updateSchemaMapping: (id: string, patch: Partial<SchemaMappingInput>) =>
    apiClient.put<SchemaMappingRecord>(`/api/v2/schema-mappings/${id}`, patch).then((r) => r.data),
  // which pipelines bind this mapping — shown before deleting so a silent break can't sneak through
  schemaMappingUsage: (id: string) =>
    apiClient.get<{ id: string; name: string; paradigm: MappingParadigm; pipelines: { id: string; name: string; kind: string }[] }>(
      `/api/v2/schema-mappings/${id}/usage`).then((r) => r.data),
  deleteSchemaMapping: (id: string) =>
    apiClient.delete<{ status: string; deleted: number; name: string }>(
      `/api/v2/schema-mappings/${id}`).then((r) => r.data),
  schemaMappingConformance: (id: string, assetId: string) =>
    apiClient.get<MappingConformance>(
      `/api/v2/schema-mappings/${id}/conformance?asset_id=${assetId}`).then((r) => r.data),

  // ── model registry (bring-your-own models for generation/embedding/rerank) ──
  listModels: (role?: string) =>
    apiClient.get<{ items: ModelEntry[] }>(`/api/v2/models${role ? `?role=${role}` : ""}`).then((r) => r.data),
  activeModels: () =>
    apiClient.get<{ items: ActiveModel[] }>("/api/v2/models/active").then((r) => r.data),
  createModel: (spec: ModelSpecInput) =>
    apiClient.post<ModelEntry>("/api/v2/models", spec).then((r) => r.data),
  updateModel: (id: string, spec: ModelSpecInput) =>
    apiClient.put<ModelEntry>(`/api/v2/models/${id}`, spec).then((r) => r.data),
  deleteModel: (id: string) =>
    apiClient.delete(`/api/v2/models/${id}`).then((r) => r.data),
  testModel: (id: string) =>
    apiClient.post<{ ok: boolean; role: string; sample?: string; dimensions?: number }>(
      `/api/v2/models/${id}/test`).then((r) => r.data),
  // live-list what a provider serves (the server's / a hosted provider's /v1/models catalogue)
  discoverModels: (body: { provider: string; base_url?: string; api_key?: string; model_id?: string; role?: string }) =>
    apiClient.post<ModelDiscovery>("/api/v2/models/discover", body).then((r) => r.data),
  modelRuntime: () =>
    apiClient.get<ModelRuntime>("/api/v2/models/runtime").then((r) => r.data),
  // Hugging Face models run in OpenCortyx itself (CPU): look one up, download it, list/delete them.
  hfModelInfo: (repo: string, token?: string) =>
    apiClient.post<HFModelInfo>("/api/v2/models/hf/info", { repo, token: token || undefined }).then((r) => r.data),
  hfDownload: (repo: string, token?: string) =>
    apiClient.post<HFDownloadStatus>("/api/v2/models/hf/download", { repo, token: token || undefined }).then((r) => r.data),
  hfDownloadStatus: (repo: string) =>
    apiClient.get<HFDownloadStatus>("/api/v2/models/hf/download", { params: { repo } }).then((r) => r.data),
  // fastembed (ONNX, CPU) embedders and rerankers: download one ahead of its first use.
  fastembedDownload: (model: string, role: string) =>
    apiClient.post<HFDownloadStatus>("/api/v2/models/fastembed/download", { model, role }).then((r) => r.data),
  fastembedDownloadStatus: (model: string, role: string) =>
    apiClient.get<HFDownloadStatus>("/api/v2/models/fastembed/download", { params: { model, role } }).then((r) => r.data),
  hfLocalModels: () =>
    apiClient.get<{ items: DiscoveredModel[] }>("/api/v2/models/hf/local").then((r) => r.data),
  hfDeleteLocal: (repo: string) =>
    apiClient.delete("/api/v2/models/hf/local", { params: { repo } }).then((r) => r.data),
  // Have an Ollama server pull a model (a HF GGUF repo or an Ollama name); progress per NDJSON line.
  ollamaPull: async (baseUrl: string, model: string, onEvent: (e: OllamaPullEvent) => void) => {
    const session = (await getSession()) as any;
    const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/v2/models/ollama/pull`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(session?.accessToken ? { Authorization: `Bearer ${session.accessToken}` } : {}),
      },
      body: JSON.stringify({ base_url: baseUrl, model }),
    });
    if (!res.ok || !res.body) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json())?.detail || detail; } catch { /* keep status */ }
      throw new Error(detail);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (line) { try { onEvent(JSON.parse(line)); } catch { /* skip a partial line */ } }
      }
    }
  },

  // ── ingestion progress (the Progress tab) ──────────────────────────────
  listProgress: (limit = 100) =>
    apiClient.get<{ summary: ProgressSummary; items: ProgressRun[] }>(
      `/api/v2/progress?limit=${limit}`).then((r) => r.data),
  getProgressRun: (id: string) =>
    apiClient.get<ProgressRun>(`/api/v2/progress/${id}`).then((r) => r.data),
  deleteProgressRun: (id: string, purge = true) =>
    apiClient.delete(`/api/v2/progress/${id}?purge=${purge}`).then((r) => r.data),
  clearProgress: (status?: string) =>
    apiClient.delete(`/api/v2/progress${status ? `?status=${status}` : ""}`).then((r) => r.data),

  // ── connectors (saved data sources, DataHub-style) ─────────────────────
  connectors: () => apiClient.get<{ connectors: Connector[] }>("/api/v2/connectors").then((r) => r.data),
  getConnector: (id: string) => apiClient.get<Connector>(`/api/v2/connectors/${id}`).then((r) => r.data),
  createConnector: (spec: ConnectorSpec) =>
    apiClient.post<Connector>("/api/v2/connectors", spec).then((r) => r.data),
  updateConnector: (id: string, spec: ConnectorSpec) =>
    apiClient.put<Connector>(`/api/v2/connectors/${id}`, spec).then((r) => r.data),
  deleteConnector: (id: string) =>
    apiClient.delete(`/api/v2/connectors/${id}`).then((r) => r.data),
  testConnector: (id: string) =>
    apiClient.post<{ ok: boolean; tables: number }>(`/api/v2/connectors/${id}/test`).then((r) => r.data),
  connectorFromRecipe: (recipe: string, name?: string) =>
    apiClient.post<Connector>("/api/v2/connectors/from-recipe", { recipe, name }).then((r) => r.data),
  connectorRecipe: (id: string) =>
    apiClient.get<{ recipe: string }>(`/api/v2/connectors/${id}/recipe`).then((r) => r.data),
  browseConnector: (id: string, schema?: string) =>
    apiClient.get<{ schemas: BrowseSchema[] }>(
      `/api/v2/connectors/${id}/browse${schema ? `?schema=${encodeURIComponent(schema)}` : ""}`).then((r) => r.data),
  // folder picker for document ingestion: list sub-folders of a path in a data lake / on-prem connector
  listConnectorDirs: (id: string, path = "") =>
    apiClient.get<{ path: string; dirs: string[] }>(
      `/api/v2/connectors/${id}/dirs?path=${encodeURIComponent(path)}`).then((r) => r.data),

  // ── ingestions (saved, re-runnable jobs) ───────────────────────────────
  listIngestions: () => apiClient.get<{ items: Ingestion[] }>("/api/v2/ingestions").then((r) => r.data),
  getIngestion: (id: string) => apiClient.get<Ingestion>(`/api/v2/ingestions/${id}`).then((r) => r.data),
  createIngestion: (spec: IngestionSpec) =>
    apiClient.post<Ingestion>("/api/v2/ingestions", spec).then((r) => r.data),
  updateIngestion: (id: string, patch: Partial<IngestionSpec>) =>
    apiClient.put<Ingestion>(`/api/v2/ingestions/${id}`, patch).then((r) => r.data),
  deleteIngestion: (id: string) =>
    apiClient.delete(`/api/v2/ingestions/${id}`).then((r) => r.data),
  runIngestion: (id: string) =>
    apiClient.post<{ task_id: string; run_id: string }>(`/api/v2/ingestions/${id}/run`).then((r) => r.data),
  // the runs behind one ingestion (every file of a one-time upload / a connector job's history)
  ingestionRuns: (id: string) =>
    apiClient.get<{ items: ProgressRun[] }>(`/api/v2/ingestions/${id}/runs`).then((r) => r.data),
  syncIngestion: (id: string) =>
    apiClient.post<{ status: string; run_ids: string[]; task_ids: string[] }>(`/api/v2/ingestions/${id}/sync`).then((r) => r.data),
  pauseIngestion: (id: string) =>
    apiClient.post<{ status: string; id: string }>(`/api/v2/ingestions/${id}/pause`).then((r) => r.data),
  resumeIngestion: (id: string) =>
    apiClient.post<{ status: string; id: string; run_id?: string | null }>(`/api/v2/ingestions/${id}/resume`).then((r) => r.data),

  // ── the product: grounded context / answer ─────────────────────────────
  // `opts` lets the Chat UI steer retrieval: which retrievers, reranker stages, k, abstain threshold.
  getContext: (query: string, collectionIds?: string[], opts?: RetrievalOpts) =>
    apiClient
      .post<ContextResult>("/api/v2/context", { query, collection_ids: collectionIds, ...opts })
      .then((r) => r.data),
  answer: (query: string, collectionIds?: string[], opts?: RetrievalOpts) =>
    apiClient
      .post<AnswerResult>("/api/v2/answer", { query, collection_ids: collectionIds, ...opts })
      .then((r) => r.data),

  // streaming answer (SSE): tokens arrive as they're generated; the final `done` event carries
  // citations / trust / grounding / token spend. Falls back gracefully server-side if a model can't stream.
  answerStream: async (
    query: string,
    handlers: {
      onToken?: (t: string) => void; onMeta?: (m: any) => void; onDone?: (d: any) => void;
      onChat?: (chatId: string) => void;   // the chat this turn was saved in (sent first)
    },
    collectionIds?: string[],
    opts?: RetrievalOpts,
  ) => {
    const session = (await getSession()) as any;
    const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/v2/answer/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(session?.accessToken ? { Authorization: `Bearer ${session.accessToken}` } : {}),
      },
      body: JSON.stringify({ query, collection_ids: collectionIds, ...opts }),
    });
    if (!res.ok || !res.body) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json())?.detail || detail; } catch { /* keep status */ }
      throw new Error(detail);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (!raw.startsWith("data:")) continue;
        const json = raw.slice(5).trim();
        if (!json) continue;
        let ev: any;
        try { ev = JSON.parse(json); } catch { continue; }
        if (ev.type === "token") handlers.onToken?.(ev.text || "");
        else if (ev.type === "meta") handlers.onMeta?.(ev);
        else if (ev.type === "done") handlers.onDone?.(ev);
        else if (ev.type === "chat") handlers.onChat?.(ev.chat_id);
      }
    }
  },

  // comments & collaboration (per asset)
  listComments: (nodeId: string) =>
    apiClient.get<{ items: any[] }>(`/api/v2/assets/${nodeId}/comments`).then((r) => r.data),
  addComment: (nodeId: string, text: string) =>
    apiClient.post(`/api/v2/assets/${nodeId}/comments`, { text }).then((r) => r.data),
  deleteComment: (commentId: string) =>
    apiClient.delete(`/api/v2/comments/${commentId}`).then((r) => r.data),

  // catalog of available retrievers / rerankers / profiles for the Chat config panel
  retrieversCatalog: () => apiClient.get<RetrieverCatalog>("/api/v2/retrievers").then((r) => r.data),
  corpusEnrich: () => apiClient.post("/api/v2/corpus/enrich").then((r) => r.data),

  // ── system health ──────────────────────────────────────────────────────
  health: () =>
    apiClient
      .get<HealthResult>("/api/v2/health")
      .then((r) => r.data),

  // ── export / backup (no lock-in) ────────────────────────────────────────
  exportGraph: (vectors: boolean = true) =>
    apiClient
      .get<Record<string, any>>(`/api/v2/export?vectors=${vectors}`)
      .then((r) => r.data),
  importGraph: (data: Record<string, any>, mode: "merge" | "replace" = "merge") =>
    apiClient
      .post<{ status: string; mode: string; imported: Record<string, number> }>(
        "/api/v2/import",
        { data, mode }
      )
      .then((r) => r.data),

  // ── cost controls ───────────────────────────────────────────────────────
  cost: () => apiClient.get<CostStatus>("/api/v2/cost").then((r) => r.data),
  setBudget: (spec: BudgetInput) =>
    apiClient.put<{ status: string }>("/api/v2/cost/budget", spec).then((r) => r.data),

  // ── catalogue / dashboard ──────────────────────────────────────────────
  stats: () => apiClient.get<StatsResult>("/api/v2/stats").then((r) => r.data),
  // `total` is the real asset count and is NOT capped by `limit` — render from `items`, but always
  // report counts from `total`, or the page size ends up being displayed as the number of assets.
  listStructured: (limit = 200) =>
    apiClient.get<{ total: number; items: AssetItem[] }>(`/api/v2/structured?limit=${limit}`).then((r) => r.data),
  // light=true → minimal per-node properties (identity flags only) for the dashboard overview;
  // full node properties come from getNode() when a node is opened.
  graph: (limit = 0, light = false) =>
    apiClient.get(`/api/v2/graph?limit=${limit}&light=${light}`).then((r) => r.data),
  // focus=true skips time-bookkeeping edges (CREATED_ON/…) that otherwise bridge the whole workspace
  // through the shared Date nodes — use it for "what came out of THIS source" views.
  // Every asset a node came from, ranked for the question, a page at a time (a citation's "+ N more").
  nodeSources: (id: string, q = "", offset = 0, limit = 25) =>
    apiClient.get<{ items: CitationSource[]; total: number; capped: boolean }>(
      `/api/v2/nodes/${id}/sources`, { params: { q, offset, limit } }).then((r) => r.data),
  getNode: (id: string, hops: number = 1, focus = false) =>
    apiClient.get(`/api/v2/nodes/${id}?hops=${hops}${focus ? "&focus=true" : ""}`).then((r) => r.data),

  // What a set of anchor rules WOULD match, without ingesting. Pure path matching server-side, so
  // a mistyped glob is visible while typing rather than after a run has written the wrong edges.
  previewAnchors: (anchors: any[], paths: string[]) =>
    apiClient.post("/api/v2/ingestions/anchors/preview", { anchors, paths }).then((r) => r.data),

  searchNodes: (q: string, types?: string, limit = 20) =>
    apiClient.get("/api/v2/search", { params: { q, types, limit } }).then((r) => r.data),

  // ── ontology (custom node/edge types + curation) ───────────────────────
  ontology: () => apiClient.get("/api/v2/ontology").then((r) => r.data),
  /** What each node type ACTUALLY carries in this org's graph, sampled from the nodes.
   *  Distinct from `ontology()`, which returns the DECLARED types — a mapping is free to disagree
   *  with the declared schema, and an open-vocabulary extractor declares nothing at all, so this is
   *  the only source that can tell a pipeline author which attributes really exist to read. */
  observedNodeAttributes: () =>
    apiClient.get("/api/v2/node-types/attributes").then((r) => r.data),
  candidates: () => apiClient.get("/api/v2/ontology/candidates").then((r) => r.data),
  registerType: (spec: {
    name: string;
    kind: "node" | "edge" | "entity";
    description: string;
    allowed_source_types?: string[];
    allowed_target_types?: string[];
    fields?: { name: string; type?: string; description?: string }[];
    domain?: string;
  }) => apiClient.post("/api/v2/ontology/types", spec).then((r) => r.data),
  promoteType: (name: string, kind: string) =>
    apiClient
      .post(`/api/v2/ontology/promote?name=${encodeURIComponent(name)}&kind=${kind}`)
      .then((r) => r.data),
  materializeOntology: (template: "person" | "organization") =>
    apiClient.post(`/api/v2/ontology/materialize?template=${template}`).then((r) => r.data),

  // ── collections (scoping) ──────────────────────────────────────────────
  collections: () => apiClient.get("/api/v2/collections").then((r) => r.data),
  previewCollectionDelete: (id: string) =>
    apiClient
      .get<CollectionDeletePreview>(`/api/v2/collections/${id}/delete-preview`)
      .then((r) => r.data),
  deleteCollection: (id: string) =>
    apiClient.delete<{ status: string; deleted: boolean }>(`/api/v2/collections/${id}`).then((r) => r.data),

  // ── authoring: Custom & Flexible OpenCortyx (add your own knowledge) ─────────
  editType: (name: string, kind: string, description: string) =>
    apiClient.patch(`/api/v2/ontology/types?name=${encodeURIComponent(name)}&kind=${kind}&description=${encodeURIComponent(description)}`).then((r) => r.data),
  deleteType: (name: string, kind: string) =>
    apiClient.delete(`/api/v2/ontology/types?name=${encodeURIComponent(name)}&kind=${kind}`).then((r) => r.data),
  search: (q: string, types?: string) =>
    apiClient.get<{ results: { id: string; label: string; type: string }[] }>(
      `/api/v2/search?q=${encodeURIComponent(q)}${types ? `&types=${types}` : ""}`).then((r) => r.data),
  addGlossary: (spec: GlossaryInput) =>
    apiClient.post("/api/v2/glossary", spec).then((r) => r.data),
  addRule: (spec: RuleInput) =>
    apiClient.post("/api/v2/rules", spec).then((r) => r.data),
  // `/business-context`, not `/context`: the latter is the retrieval endpoint, and posting a
  // business-context spec at it came back "Field required: query" — the two routes shared a path.
  addBusinessContext: (spec: BusinessContextInput) =>
    apiClient.post("/api/v2/business-context", spec).then((r) => r.data),
  listBusinessContext: () =>
    apiClient.get<{ items: BusinessContextEntry[] }>("/api/v2/business-context").then((r) => r.data),
  updateTypeStatus: (name: string, kind: string, status: string) => 
    apiClient.put(`/api/v2/types/${name}/status`, { kind, status }).then(r => r.data),

  // ── Declarative Extractions (Stored in PostgreSQL) ──────────────────────
  getExtractionTemplates: () => apiClient.get("/api/v2/extraction-templates").then(r => r.data.templates),
  /**
   * What ingestion itself creates, per paradigm: {document|semistructured|structured} →
   * {label, configured_in, nodes:[{type, role, attributes, measured}], edges:[…]}.
   *
   * Rendered on the Step-1 / base-schema card. These used to be extractor cards; they aren't
   * extractors (they MAKE the nodes an extractor runs over), and showing them in the extractor list
   * duplicated what the schema mapping already declares.
   */
  getReadingShapes: () => apiClient.get("/api/v2/reading-shapes").then(r => r.data.shapes),
  /**
   * The node types + attributes a pipeline stage can choose as its INPUT.
   *
   * One question, asked the same way in all three builders: "which nodes does this extractor read,
   * and through which attributes?". The vocabulary comes from the schema mapping bound to the
   * ingestion when there is one, and from the paradigm's default shape when there isn't — so a user
   * who never opened the mapping editor still gets a sensible list instead of an empty box.
   *
   * `mapped`  — node types the bound mapping declares, with their attributes
   * `fallback`— the default shape (document: the Step-1 chunk node; structured: Table/Attribute)
   * `emitted` — what earlier stages produce, so a stage can chain onto a previous one's output
   */
  stageInputVocab: (opts: {
    mapped?: { node_type: string; attrs: string[] }[];
    fallback?: { node_type: string; attrs: string[] }[];
    emitted?: { node_type: string; attrs: string[]; from_stage?: string; from_extractor?: string }[];
  }) => {
    const merged = new Map<string, { node_type: string; attrs: string[]; origin: string;
                                     from_stage?: string; from_extractor?: string }>();
    const add = (list: any[] | undefined, origin: string) => {
      (list || []).forEach((n) => {
        if (!n?.node_type) return;
        const prev = merged.get(n.node_type);
        const attrs = Array.from(new Set([...(prev?.attrs || []), ...(n.attrs || [])])).filter(Boolean);
        merged.set(n.node_type, {
          node_type: n.node_type, attrs, origin: prev?.origin || origin,
          from_stage: prev?.from_stage ?? n.from_stage,
          from_extractor: prev?.from_extractor ?? n.from_extractor,
        });
      });
    };
    add(opts.mapped, "mapping");        // the user's own vocabulary wins the origin label
    add(opts.fallback, "default");
    add(opts.emitted, "step");
    return Array.from(merged.values());
  },

  /**
   * The extractors a PIPELINE STAGE may be built from, split into built-in and custom.
   *
   * Only `executable` templates qualify: a loader (the chunker, SQL introspection, the JSON walker)
   * describes something ingestion produces on its own and cannot be "run" over nodes, so offering it
   * as a stage produced a step that silently did nothing. `exclude` drops the ones a builder already
   * offers as a fixed shortcut, so the same extractor never appears twice in one menu.
   */
  stageableTemplates: (templates: any[], category: string, exclude: string[] = []) => {
    const skip = new Set(exclude);
    const usable = (templates || []).filter((t: any) =>
      t?.executable && !skip.has(t.name) &&
      ((t.category || "unstructured") === category));
    const mech = (t: any) => {
      const ref = String(t.mechanism?.ref || "");
      if (ref === "unstructured.python") return "python";
      if (ref === "unstructured.attributes") return "per-attribute";
      if (ref === "seed.literal") return "constant";
      if (ref === "unstructured.keyword") return "keyword";
      if (ref === "unstructured.regex") return "regex";
      if (ref === "unstructured.ner") return "ner";
      if (ref === "unstructured.dictionary") return "dictionary";
      return "llm";
    };
    const label = (t: any) => `${String(t.name).replace(/^builtin:/, "")} · ${mech(t)}`;
    return {
      builtin: usable.filter((t: any) => String(t.name).startsWith("builtin:"))
        .map((t: any) => ({ ...t, label: label(t) }))
        .sort((a: any, b: any) => a.label.localeCompare(b.label)),
      custom: usable.filter((t: any) => !String(t.name).startsWith("builtin:"))
        .map((t: any) => ({ ...t, label: label(t) }))
        .sort((a: any, b: any) => a.label.localeCompare(b.label)),
    };
  },
  createExtractionTemplate: (spec: { name: string; source: any; mechanism: any; emit: any }) => 
    apiClient.post("/api/v2/extraction-templates", spec).then(r => r.data),
  deleteExtractionTemplate: (id: string) =>
    apiClient.delete(`/api/v2/extraction-templates/${id}`).then(r => r.data),
  // Live preview — dry-run an UNSAVED extractor over sample text; returns the nodes/edges/
  // attributes it would emit (each with mechanism, model, weight, value). No DB writes.
  previewExtractionTemplate: (spec: { name?: string; source?: any; mechanism: any; emit: any; sample_text: string }) =>
    apiClient.post("/api/v2/extraction-templates/preview", spec).then(r => r.data),
  // Enable/disable an extractor — disabled templates are skipped by the engine everywhere.
  setExtractorStatus: (id: string, status: "active" | "disabled") =>
    apiClient.patch(`/api/v2/extraction-templates/${id}`, { status }).then(r => r.data),
  // Run an executable enrichment template over the existing graph (attach attrs/nodes/edges).
  runExtractionTemplate: (id: string) =>
    apiClient.post(`/api/v2/extraction-templates/${id}/run`).then(r => r.data),
  runAllExtractors: () =>
    apiClient.post(`/api/v2/extraction/run-all`).then(r => r.data),
  // Memory-resolution review queue (proposed supersessions awaiting approval)
  getResolutionProposals: () =>
    apiClient.get(`/api/v2/resolution/proposals`).then(r => r.data.proposals as any[]),
  approveResolutionProposal: (id: string) =>
    apiClient.post(`/api/v2/resolution/proposals/${id}/approve`).then(r => r.data),
  rejectResolutionProposal: (id: string) =>
    apiClient.post(`/api/v2/resolution/proposals/${id}/reject`).then(r => r.data),
  // How a node type is resolved (same-thing matching across assets), for any type: a mapping's
  // record types have no extractor whose Resolution section could hold it.
  getTypeResolution: (name: string) =>
    apiClient.get<{ type: string; resolution: TypeResolution | null; attributes: string[]; sampled: number }>(
      `/api/v2/resolution/types/${encodeURIComponent(name)}`).then((r) => r.data),
  setTypeResolution: (name: string, spec: TypeResolution) =>
    apiClient.put(`/api/v2/resolution/types/${encodeURIComponent(name)}`, spec).then((r) => r.data),
  resolveTypeAgain: (name: string) =>
    apiClient.post<{ status: string; assets: number; capped: boolean }>(
      `/api/v2/resolution/types/${encodeURIComponent(name)}/run`).then((r) => r.data),
  addSynonym: (spec: SynonymInput) =>
    apiClient.post("/api/v2/synonyms", spec).then((r) => r.data),
  addRelationship: (source_id: string, target_id: string, relation: string) =>
    apiClient.post("/api/v2/relationships", { source_id, target_id, relation }).then((r) => r.data),
  createNode: (type: string, label: string, properties?: any) =>
    apiClient.post("/api/v2/nodes", { type, label, properties }).then((r) => r.data),
  createEdge: (source_id: string, target_id: string, type: string, properties?: any) =>
    apiClient.post("/api/v2/edges", { source_id, target_id, type, properties }).then((r) => r.data),
  deleteEdge: (edge_id: string) =>
    apiClient.delete(`/api/v2/edges/${edge_id}`).then((r) => r.data),
  // Manual curation (§13): edit a node's label/properties, or delete a node.
  updateNode: (id: string, patch: { type: string; label?: string; properties?: any }) =>
    apiClient.patch(`/api/v2/nodes/${id}`, { type: patch.type, label: patch.label ?? "", properties: patch.properties }).then((r) => r.data),
  deleteNode: (id: string) =>
    apiClient.delete(`/api/v2/nodes/${id}`).then((r) => r.data),
  // Permanent asset deletion: the asset + everything derived from it (nodes, edges, progress runs,
  // sync-manifest entry). `previewAssetDelete` is the dry-run that feeds the confirm dialog.
  previewAssetDelete: (id: string) =>
    apiClient
      .get<AssetDeletePreview>(`/api/v2/assets/${id}/delete-preview`)
      .then((r) => r.data),
  deleteAsset: (id: string) =>
    apiClient.delete<AssetDeleteResult>(`/api/v2/assets/${id}`).then((r) => r.data),
  seedDomain: (domain: string, n_tables = 6) =>
    apiClient.post("/api/v2/structured/seed-domain", { domain, n_tables }).then((r) => r.data),

  // ── custom extraction templates (define what to extract) ───────────────
  extractionTemplates: () =>
    apiClient.get<{ templates: any[] }>("/api/v2/extraction-templates").then((r) => r.data),
  createTemplate: (spec: { name: string; instruction: string; output_schema: any[]; target_type: string }) =>
    apiClient.post("/api/v2/extraction-templates", spec).then((r) => r.data),
  deleteTemplate: (tid: string) =>
    apiClient.delete(`/api/v2/extraction-templates/${tid}`).then((r) => r.data),

  // ── Observability & KPI ────────────────────────────────────────────────
  auditLogs: (limit = 100) => 
    apiClient.get(`/api/v2/audit?limit=${limit}`).then((r) => r.data),
  computeKpis: () => 
    apiClient.post("/api/v2/stats/compute").then((r) => r.data),

  // ── Quarantine ─────────────────────────────────────────────────────────
  /** Writes the substrate recorded instead of dropping (unresolved edges, unmatched records…). */
  getWriteQuarantine: () =>
    apiClient.get("/api/v2/quarantine/writes").then((r) => r.data),
  getQuarantine: () => 
    apiClient.get("/api/v2/quarantine").then((r) => r.data),
  dismissQuarantine: (node_id: string) => 
    apiClient.delete(`/api/v2/quarantine/${node_id}`).then((r) => r.data),
  retryQuarantine: (node_id: string, json_data: string) =>
    apiClient.post(`/api/v2/quarantine/${node_id}/retry`, { json_data }).then((r) => r.data),
  rerunQuarantine: (node_id: string) =>
    apiClient.post(`/api/v2/quarantine/${node_id}/rerun`).then((r) => r.data),

  // ── Performance & scaling config (runtime-tunable knobs) ────────────────
  getConfig: () => apiClient.get("/api/v2/config").then((r) => r.data),
  updateConfig: (values: Record<string, any>) =>
    apiClient.put("/api/v2/config", { values }).then((r) => r.data),
  // Generate (or regenerate) the AI catalogue overview for ONE asset on demand.
  generateOverview: (id: string) =>
    apiClient.post(`/api/v2/assets/${id}/generate-overview`).then((r) => r.data),
  // Persist user-edited overview sections (exec summary / business purpose / NL description).
  editOverview: (id: string, patch: { exec_summary?: string; business_purpose?: string; nl_description?: string }) =>
    apiClient.put(`/api/v2/assets/${id}/overview`, patch).then((r) => r.data),

  // ── Authoring (draft a mapping / pipeline / extractor from real data) ──
  /** Every registered drafter, so the UI can offer "read this another way" without a hard-coded list. */
  listDrafters: () =>
    apiClient.get<{ items: { name: string; kind: string; description: string }[] }>(
      "/api/v2/authoring/drafters").then((r) => r.data),

  /** Draft from a file. Reads and samples it — never ingests, so it is safe to call on every pick. */
  // How Docling reads a document (PDF, Word, ...): every element in reading order. Saves nothing.
  readLayout: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return apiClient.post<LayoutReading>("/api/v2/authoring/layout", fd, {
      headers: { "Content-Type": "multipart/form-data" },
    }).then((r) => r.data);
  },
  draftFromFile: (file: File, kind: DraftKind,
                  opts?: { goal?: string; drafter?: string; useModel?: boolean;
                           context?: Record<string, any> }) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("kind", kind);
    if (opts?.goal) fd.append("goal", opts.goal);
    if (opts?.drafter) fd.append("drafter", opts.drafter);
    if (opts?.useModel === false) fd.append("use_model", "false");
    if (opts?.context) fd.append("context", JSON.stringify(opts.context));
    return apiClient.post<DraftResponse>("/api/v2/authoring/draft/upload", fd, {
      headers: { "Content-Type": "multipart/form-data" },
    }).then((r) => r.data);
  },

  /** Draft from a goal sentence, or from content already in hand. */
  draft: (spec: { kind: DraftKind; goal?: string; content?: string; filename?: string;
                  drafter?: string; use_model?: boolean; context?: Record<string, any> }) =>
    apiClient.post<DraftResponse>("/api/v2/authoring/draft", spec).then((r) => r.data),

  /** Re-run an edited draft against a sample without saving it. */
  previewDraft: (kind: DraftKind, payload: Record<string, any>, sampleText: string) =>
    apiClient.post("/api/v2/authoring/draft/preview",
                   { kind, payload, sample_text: sampleText }).then((r) => r.data),

  /**
   * Save a draft by POSTing its payload to the endpoint it names.
   *
   * Deliberately generic: the draft carries its own destination, so this does not grow a
   * kind → URL table that can drift from what the server actually offers.
   */
  saveDraft: (draft: Draft) =>
    apiClient.post(draft.save_endpoint, draft.payload).then((r) => r.data),

  /**
   * Add one file, letting the server route it. Optionally through a pipeline.
   *
   * The plain `/sources/upload` endpoint, which already detects whether a file is records, a
   * table or prose and sends it to the engine that reads that. Distinct from `uploadDocument`
   * (which forces the document path) and `uploadDocumentWithPipeline` (which posts an inline
   * pipeline body rather than naming a saved one).
   */
  addSource: (file: File, opts?: { pipelineId?: string; collectionId?: string; batchId?: string;
                                   /** a pipeline per file kind ("document" | "structured" | "semistructured");
                                    *  the server detects the file's kind and uses that one, else its default */
                                   pipelineIds?: Record<string, string>;
                                   /** read it again even if this exact file is already in */
                                   replace?: boolean; tags?: string[] }) => {
    const fd = new FormData();
    fd.append("file", file);
    const perKind = Object.fromEntries(Object.entries(opts?.pipelineIds || {}).filter(([, id]) => id));
    if (Object.keys(perKind).length) fd.append("pipeline_ids", JSON.stringify(perKind));
    if (opts?.replace) fd.append("replace", "true");
    if (opts?.tags?.length) fd.append("tags", JSON.stringify(opts.tags));
    if (opts?.pipelineId) fd.append("pipeline_id", opts.pipelineId);
    if (opts?.collectionId) fd.append("collection_id", opts.collectionId);
    if (opts?.batchId) fd.append("batch_id", opts.batchId);
    return apiClient.post<{ task_id: string; run_id: string | null; filename: string;
                            routing: Record<string, any> }>(
      "/api/v2/sources/upload", fd, { headers: { "Content-Type": "multipart/form-data" } },
    ).then((r) => r.data);
  },

  /** What a run produced, and what is worth doing about it. Safe to poll while it is running. */
  getOutcome: (runId: string) =>
    apiClient.get<IngestOutcome>(`/api/v2/progress/${runId}/outcome`).then((r) => r.data),

  // ── Capability platform (Settings → Capabilities) ──────────────────────
  getCapabilities: () => apiClient.get("/api/v2/capabilities").then((r) => r.data),
  updateCapability: (name: string, values: Record<string, any>) =>
    apiClient.put(`/api/v2/capabilities/${name}`, { values }).then((r) => r.data),
};
