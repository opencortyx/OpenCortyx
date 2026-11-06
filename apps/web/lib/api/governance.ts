/**
 * The API for what the platform keeps about its own conduct: what models cost, how good resolution
 * is (labels and the score), who may read what (policies and groups), and forgetting (deletion jobs,
 * tombstones). One module, so each page imports what it shows and nothing else.
 */
import { apiClient } from "./client";

const get = <T,>(url: string, params?: Record<string, unknown>) =>
  apiClient.get<T>(url, { params }).then((r) => r.data);
const post = <T,>(url: string, body?: unknown) => apiClient.post<T>(url, body).then((r) => r.data);
const del = <T,>(url: string) => apiClient.delete<T>(url).then((r) => r.data);

/** An error from the API, in the words it gave. */
export function apiError(e: any, fallback: string): string {
  const d = e?.response?.data?.detail;
  if (Array.isArray(d)) return d.map((x: any) => x?.msg || String(x)).join("; ");
  return typeof d === "string" && d ? d : fallback;
}

// ── cost ──────────────────────────────────────────────────────────────────────────────────────

export interface UsageRow {
  calls: number; tokens_in: number; tokens_out: number; tokens: number; cost_usd: number;
  measured_share: number | null; cached_calls: number; paid_calls: number;
}
export interface UsageReport {
  days: number; total: UsageRow;
  by_purpose: (UsageRow & { purpose: string })[];
  by_model: (UsageRow & { provider: string | null; model: string | null })[];
  by_day: (UsageRow & { day: string | null })[];
}
export interface RunCost extends UsageRow {
  run_id: string; asset: string; kind: string; status: string; created_at: string | null;
  records: number | null; tokens_per_1k_records: number | null; cost_usd_per_1k_records: number | null;
}
export interface CostEstimate {
  purpose: string; records: number; sample_calls: number; measured_share?: number;
  per_record?: { tokens_in: number; tokens_out: number; cost_usd: number; seconds: number };
  estimate: { tokens: number; cost_usd: number; hours_sequential: number } | null;
  reason?: string;
}

export const costApi = {
  usage: (days = 30) => get<UsageReport>("/api/v2/cost/usage", { days }),
  runs: (limit = 50) => get<{ runs: RunCost[] }>("/api/v2/cost/runs", { limit }).then((r) => r.runs),
  estimate: (purpose: string, records: number) => get<CostEstimate>("/api/v2/cost/estimate", { purpose, records }),
  clearCache: () => del<{ status: string; deleted: number }>("/api/v2/cost/cache"),
};

// ── resolution quality ────────────────────────────────────────────────────────────────────────

export interface Snapshot { label: string | null; type?: string; properties: Record<string, any>; asset_id: string | null }
export interface TypeQuality {
  node_type: string; same_decisions: number; awaiting_review: number;
  labels: number; same: number; different: number; by_origin: Record<string, number>;
}
export interface Score {
  labels: number; pairs_same: number; pairs_different: number;
  true_merges: number; false_merges: number; false_merges_at_write: number;
  missed: number; missed_in_review: number; correct_apart: number; review_pairs: number;
  precision: number | null; recall: number | null; f1: number | null;
  false_merge_examples: { label_id?: string; a: string | null; b: string | null; at_write?: boolean }[];
  missed_examples: { label_id?: string; a: string | null; b: string | null; in_review?: boolean }[];
  rule: Record<string, any> | null; note?: string;
}
export interface Evaluation { node_type: string; current: Score; proposed?: Score; overrides?: Record<string, any> }
export interface AuditItem {
  decision_id: string; node_type: string; score: number; method: string;
  a: Snapshot & { id: string }; b: Snapshot & { id: string };
}
export interface Label {
  id: string; node_type: string; a: Snapshot; b: Snapshot; same: boolean; origin: string;
  note: string | null; created_at: string | null;
}
export interface MergedRecord {
  id: string; type: string; label: string | null; properties: Record<string, any>;
  prov: Record<string, string[]>; collections: string[];
  members: { id: string; label: string | null; source_ref: string | null; properties: Record<string, any> }[];
}
export interface LabelImportItem {
  node_type: string; a: { label: string | null; properties?: Record<string, any>; asset_id?: string | null };
  b: { label: string | null; properties?: Record<string, any>; asset_id?: string | null }; same: boolean; note?: string;
}

export const qualityApi = {
  types: () => get<{ types: TypeQuality[] }>("/api/v2/resolution/quality").then((r) => r.types),
  evaluate: (node_type: string, overrides?: Record<string, any>) =>
    post<Evaluation>("/api/v2/resolution/quality/evaluate", { node_type, overrides: overrides || null }),
  audit: (node_type?: string, n = 20) =>
    get<{ items: AuditItem[] }>("/api/v2/resolution/audit", { node_type: node_type || undefined, n }).then((r) => r.items),
  label: (a_id: string, b_id: string, same: boolean, apply = true, note?: string) =>
    post<{ status: string }>("/api/v2/resolution/labels", { a_id, b_id, same, apply, note }),
  labels: (node_type?: string, limit = 200) =>
    get<{ items: Label[] }>("/api/v2/resolution/labels", { node_type: node_type || undefined, limit }).then((r) => r.items),
  deleteLabel: (id: string) => del(`/api/v2/resolution/labels/${id}`),
  importLabels: (items: LabelImportItem[], named = false) =>
    post<{ imported: number; skipped: number }>("/api/v2/resolution/labels/import", { items, named }),
  master: (id: string) => get<MergedRecord>(`/api/v2/resolution/masters/${id}`),
  split: (master_id: string, member_id: string) =>
    post<{ status: string; rebuilt: number; removed: number }>(`/api/v2/resolution/masters/${master_id}/split`, { member_id }),
  merge: (a_id: string, b_id: string, note?: string) =>
    post<{ status: string; master_id: string | null }>("/api/v2/resolution/merge", { a_id, b_id, note }),
};

// ── access ────────────────────────────────────────────────────────────────────────────────────

export type Sensitivity = "public" | "normal" | "confidential" | "restricted";
export interface Policy {
  id: string; group: string; collection_id: string | null; collection: string | null;
  max_sensitivity: Sensitivity; permission: string; created_at: string | null;
}
export interface PolicyList {
  enabled: boolean; items: Policy[]; collections: { id: string; name: string }[];
  groups: string[]; sensitivities: Sensitivity[];
}
export interface AccessPreview {
  groups: string[]; collections: string[] | null; every_collection: boolean;
  workspace_wide_records: boolean; max_sensitivity: Sensitivity;
}

export const accessApi = {
  policies: () => get<PolicyList>("/api/v2/access/policies"),
  addPolicy: (group: string, collection_id: string | null, max_sensitivity: Sensitivity) =>
    post<Policy & { access_control_turned_on: boolean }>("/api/v2/access/policies", { group, collection_id, max_sensitivity }),
  deletePolicy: (id: string) => del<{ status: string; access_control_on: boolean }>(`/api/v2/access/policies/${id}`),
  preview: (q: { group?: string; user_id?: string }) => get<AccessPreview>("/api/v2/access/preview", q),
};

// ── forgetting ────────────────────────────────────────────────────────────────────────────────

export interface DeletionJob {
  id: string; kind: "asset" | "subject"; target: Record<string, any>;
  status: "running" | "done" | "incomplete"; attempts: number; error: string | null;
  steps: Record<string, { status: string; count?: number; error?: string; note?: string }>;
  proof: { remaining?: Record<string, number>; kept_documents_mentioning?: { asset_id: string; label: string | null; mentions: string[] }[];
           note?: string; checked_at?: string };
  created_by: string | null; created_at: string | null; finished_at: string | null;
}
export interface SubjectPreview {
  node_id: string; names: string[];
  records: { id: string; label: string | null; type: string; merged: boolean }[];
  linked_records_only_about_it: number;
  documents_mentioning: { asset_id: string; label: string | null; type: string | null; mentions: string[] }[];
  chat_messages: number; labels: number; stores: string[];
}
export interface Tombstone { id: string; kind: string; hash: string; job_id: string | null; created_by: string | null; created_at: string | null }

export const forgetApi = {
  previewSubject: (node_id: string) => get<SubjectPreview>(`/api/v2/forget/subject/${node_id}/preview`),
  forgetSubject: (node_id: string, confirm: string, also_assets: string[]) =>
    post<DeletionJob>("/api/v2/forget/subject", { node_id, confirm, also_assets }),
  jobs: (limit = 50) => get<{ items: DeletionJob[] }>("/api/v2/forget/jobs", { limit }).then((r) => r.items),
  retry: (id: string) => post<DeletionJob>(`/api/v2/forget/jobs/${id}/retry`),
  tombstones: () => get<{ items: Tombstone[] }>("/api/v2/forget/tombstones").then((r) => r.items),
  lift: (id: string) => del(`/api/v2/forget/tombstones/${id}`),
};

/** "12,345" / "1.2M" for token counts. */
export function tokens(n: number | null | undefined): string {
  const v = Number(n || 0);
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  return v.toLocaleString();
}

/** "$0.0123" with enough digits for small amounts; "—" when no price is set. */
export function usd(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n === 0) return "$0";
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

export function pct(n: number | null | undefined): string {
  return n === null || n === undefined ? "—" : `${Math.round(n * 1000) / 10}%`;
}
