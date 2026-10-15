"use client";

import { useEffect, useState } from "react";
import { contextV2Api, ModelEntry, ModelSpecInput, ActiveModel, DiscoveredModel, ModelRuntime } from "@/lib/api/contextV2";
import {
  Cpu, Plus, Trash2, Pencil, X, CheckCircle2, AlertCircle, Loader2, Star, ShieldCheck, Sparkles, Box, Layers, Brain, Tags, Zap, RefreshCw, HardDriveDownload, Server,
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";

const PROVIDERS = [
  { id: "vllm", label: "vLLM (self-hosted — default)", needsKey: false, base: "" },
  { id: "openai", label: "OpenAI", needsKey: true, base: "" },
  { id: "groq", label: "Groq", needsKey: true, base: "https://api.groq.com/openai/v1" },
  { id: "openai_compatible", label: "OpenAI-compatible (Together, Azure…)", needsKey: true, base: "https://…/v1" },
  { id: "anthropic", label: "Anthropic (Claude)", needsKey: true, base: "" },
  { id: "huggingface", label: "HuggingFace Inference", needsKey: true, base: "" },
  // EMBEDDING providers that run in this process — no server, no endpoint, no VRAM.
  { id: "fastembed", label: "fastembed (in-process ONNX — default embedder)", needsKey: false, base: "" },
  { id: "tei", label: "TEI — HF Text Embeddings Inference (self-hosted)", needsKey: false, base: "" },
  { id: "infinity", label: "Infinity (self-hosted reranker)", needsKey: false, base: "" },
  { id: "cohere", label: "Cohere Rerank", needsKey: true, base: "https://api.cohere.com/v2" },
  { id: "jina", label: "Jina Rerank", needsKey: true, base: "https://api.jina.ai/v1" },
  { id: "sentence-transformers", label: "sentence-transformers (in-process, needs INSTALL_RERANK=1)", needsKey: false, base: "" },
];
// providers that expose a live model list at /v1/models (vLLM's served names, or a hosted catalogue)
const DISCOVERABLE = new Set(["vllm", "openai", "groq", "openai_compatible", "together", "azure"]);
// priority tiers shown as badges (matches the model-stack matrix)
const TIER: Record<string, { label: string; cls: string }> = {
  mandatory:   { label: "Required",    cls: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  recommended: { label: "Recommended", cls: "bg-amber-50 text-amber-700 border-amber-200" },
  useful:      { label: "Useful",      cls: "bg-sky-50 text-sky-700 border-sky-200" },
  optional:    { label: "Optional",    cls: "bg-stone-100 text-stone-600 border-stone-200" },
};

const ROLES = [
  { id: "generation", label: "Generation (LLM)", icon: Sparkles, tier: "mandatory", wired: true,
    purpose: "Answer questions, summarize incidents, generate SAR narratives.", hint: "Used by extractors, chat answers, rerank-LLM." },
  { id: "embedding", label: "Embedding", icon: Layers, tier: "mandatory", wired: true,
    purpose: "Convert text to vectors for semantic search.", hint: "Vectors for search. Changing this needs a re-index." },
  { id: "rerank", label: "Reranker", icon: Box, tier: "mandatory", wired: true,
    purpose: "Re-rank retrieved documents before sending to the LLM.", hint: "Re-orders retrieved candidates." },
  { id: "extraction", label: "Extraction", icon: Tags, tier: "recommended", wired: true,
    purpose: "Run the per-chunk extractors (entities, claims, and your custom extractors).",
    hint: "The high-volume per-chunk calls. A cheaper/faster model here cuts ingest cost; falls back to Generation. Per-extractor model overrides on each Extractor card still win." },
  { id: "utility", label: "Utility / Judge", icon: Brain, tier: "recommended", wired: true,
    purpose: "The relevance check (CRAG) and the grounding / entailment judge that enforce abstention.",
    hint: "Give this a capable model to make strict (entailment) abstention reliable — a weak judge over-rejects. Falls back to Generation." },
];

const EMPTY: ModelSpecInput & { id?: string } = {
  id: "", name: "", role: "generation", provider: "vllm", model_id: "",
  base_url: "", api_key: "", is_default: false,
};

export default function ModelRegistryPage() {
  const [items, setItems] = useState<ModelEntry[]>([]);
  const [active, setActive] = useState<ActiveModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [modal, setModal] = useState(false);
  const [form, setForm] = useState({ ...EMPTY });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState({ text: "", type: "" });
  const [testing, setTesting] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<string, string>>({});
  // live model discovery (what the selected provider serves right now)
  const [discover, setDiscover] = useState<{ loading: boolean; items: DiscoveredModel[]; note?: string | null }>({ loading: false, items: [] });
  // what the model server is ACTUALLY running, and whether the saved rows agree with it
  const [runtime, setRuntime] = useState<ModelRuntime | null>(null);

  const fetchItems = async () => {
    try {
      const [models, act, rt] = await Promise.all([
        contextV2Api.listModels(),
        contextV2Api.activeModels().catch(() => ({ items: [] })),
        contextV2Api.modelRuntime().catch(() => null),
      ]);
      setItems(models?.items || []);
      setActive(act?.items || []);
      setRuntime(rt);
    }
    catch (e) { console.error(e); }
    finally { setLoading(false); }
  };
  useEffect(() => { fetchItems(); }, []);

  const resetPicker = () => setDiscover({ loading: false, items: [] });
  const openAdd = (role = "generation") => { setForm({ ...EMPTY, role }); setMsg({ text: "", type: "" }); resetPicker(); setModal(true); };
  const openEdit = (m: ModelEntry) => {
    setForm({ id: m.id, name: m.name, role: m.role, provider: m.provider, model_id: m.model_id,
      base_url: m.base_url || "", api_key: "", is_default: m.is_default });
    setMsg({ text: "", type: "" }); resetPicker(); setModal(true);
  };

  const runDiscover = async () => {
    if (!DISCOVERABLE.has(form.provider)) { setDiscover({ loading: false, items: [], note: "Enter the model id manually for this provider." }); return; }
    setDiscover({ loading: true, items: [] });
    try {
      const res = await contextV2Api.discoverModels({
        provider: form.provider, base_url: form.base_url || undefined,
        api_key: form.api_key || undefined, model_id: form.id || undefined,
      });
      setDiscover({ loading: false, items: res.items || [], note: res.note });
    } catch (e: any) {
      setDiscover({ loading: false, items: [], note: e?.response?.data?.detail || e?.message || "Couldn't list models." });
    }
  };
  // auto-discover when the modal opens or the provider changes
  useEffect(() => { if (modal) runDiscover(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [modal, form.provider]);

  const save = async () => {
    if (!form.name.trim() || !form.model_id.trim()) { setMsg({ text: "Name and Model ID are required.", type: "error" }); return; }
    setBusy(true); setMsg({ text: "Saving…", type: "info" });
    try {
      const spec: ModelSpecInput = {
        name: form.name, role: form.role, provider: form.provider, model_id: form.model_id,
        base_url: form.base_url || undefined, api_key: form.api_key || undefined, is_default: form.is_default,
      };
      if (form.id) await contextV2Api.updateModel(form.id, spec);
      else await contextV2Api.createModel(spec);
      setModal(false);
      await fetchItems();
    } catch (e: any) {
      setMsg({ text: `Error: ${e?.response?.data?.detail || e?.message || e}`, type: "error" });
    } finally { setBusy(false); }
  };

  const onDelete = async (m: ModelEntry) => {
    if (!confirm(`Delete model "${m.name}"? Extractors using it will fall back to the default/env model.`)) return;
    try { await contextV2Api.deleteModel(m.id); await fetchItems(); }
    catch { alert("Failed to delete."); }
  };

  const onTest = async (m: ModelEntry) => {
    setTesting(m.id); setTestResult((r) => ({ ...r, [m.id]: "" }));
    try {
      const res = await contextV2Api.testModel(m.id);
      const ok = m.role === "embedding" ? `Connected — ${res.dimensions}-dim vectors` : `Connected — replied "${res.sample}"`;
      setTestResult((r) => ({ ...r, [m.id]: `ok:${ok}` }));
    } catch (e: any) {
      setTestResult((r) => ({ ...r, [m.id]: `err:${e?.response?.data?.detail || e?.message || "failed"}` }));
    } finally { setTesting(null); }
  };

  const providerLabel = (p: string) => PROVIDERS.find((x) => x.id === p)?.label || p;
  const needsKey = PROVIDERS.find((x) => x.id === form.provider)?.needsKey;

  const renderSection = (role: (typeof ROLES)[number]) => {
    const rows = items.filter((m) => m.role === role.id);
    const act = active.find((a) => a.role === role.id);
    return (
      <div key={role.id} className="mb-8">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-bold text-stone-700 flex items-center gap-2 uppercase tracking-wide">
            <role.icon size={15} className="text-emerald-500" /> {role.label}
            <span className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-full border normal-case ${TIER[role.tier].cls}`}>{TIER[role.tier].label}</span>
            <span className="text-[11px] font-normal text-stone-400 normal-case">— {role.hint}</span>
          </h2>
          <button onClick={() => openAdd(role.id)} className="text-xs font-semibold text-emerald-600 hover:text-emerald-800 flex items-center gap-1"><Plus size={12} /> Add</button>
        </div>
        {act && (
          <div className="mb-2 flex items-center gap-2 text-xs bg-emerald-50/60 border border-emerald-100 rounded-lg px-3 py-2">
            <Zap size={13} className="text-emerald-500 shrink-0" />
            <span className="font-semibold text-emerald-800">Active now:</span>
            <span className="font-mono text-stone-700 truncate">{act.model_id}</span>
            <span className="text-[10px] font-bold uppercase px-1.5 py-0.5 rounded bg-white border border-stone-200 text-stone-500">{act.provider}</span>
            <span className="text-[10px] font-bold uppercase px-1.5 py-0.5 rounded border" style={{}}>
              {act.source === "registry"
                ? <span className="text-emerald-700">registry default{act.name ? ` · ${act.name}` : ""}</span>
                : <span className="text-stone-500">{act.source === "default" ? "built-in default" : "environment default"}</span>}
            </span>
          </div>
        )}
        {act?.note && (
          <p className="-mt-1 mb-2 flex items-start gap-1.5 text-[11.5px] text-amber-800">
            <AlertCircle size={13} className="shrink-0 mt-px" /> {act.note}
          </p>
        )}
        {rows.length === 0 ? (
          <div className="bg-stone-50 border border-dashed border-stone-200 rounded-xl p-5 text-sm text-stone-400">
            No {role.label.toLowerCase()} model set yet. Add one and mark it default — it&apos;s saved (encrypted) and used for {role.label.toLowerCase()} wherever this capability runs.
          </div>
        ) : (
          <div className="space-y-2">
            {rows.map((m) => {
              const tr = testResult[m.id] || "";
              return (
                <div key={m.id} className="bg-white border border-stone-200 rounded-xl shadow-sm p-4">
                  <div className="flex items-center justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <h3 className="font-bold text-stone-900 truncate">{m.name}</h3>
                        {m.is_default && <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200 flex items-center gap-1"><Star size={10} /> Default</span>}
                        <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded-full bg-stone-100 text-stone-600">{providerLabel(m.provider)}</span>
                      </div>
                      <p className="text-xs text-stone-500 mt-0.5 font-mono truncate">{m.model_id}{m.base_url ? ` · ${m.base_url}` : ""}{m.has_key ? " · 🔑 key set" : ""}</p>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      <Button variant="ghost" size="sm" icon={ShieldCheck} busy={testing === m.id} onClick={() => onTest(m)}>Test</Button>
                      <button onClick={() => openEdit(m)} className="p-1.5 text-stone-400 hover:text-emerald-600 hover:bg-emerald-50 rounded-md"><Pencil size={15} /></button>
                      <button onClick={() => onDelete(m)} className="p-1.5 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-md"><Trash2 size={15} /></button>
                    </div>
                  </div>
                  {tr && (
                    <div className={`mt-2 text-xs flex items-center gap-1.5 ${tr.startsWith("ok:") ? "text-emerald-600" : "text-red-600"}`}>
                      {tr.startsWith("ok:") ? <CheckCircle2 size={13} /> : <AlertCircle size={13} />} {tr.slice(tr.indexOf(":") + 1)}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="max-w-7xl mx-auto pb-20">
      <PageHeader
        title="Models"
        icon={Cpu}
        description="Bring your own models across the full stack — local, hosted API, or tiered. Every model type is equal: whatever you mark as the default for a role is the model that role uses — no code changes, no .env settings. Set it here and it's saved (encrypted) and used wherever that capability runs. Keys are encrypted at rest."
      />

      {/* What the inference server is ACTUALLY running.
          The registry says what this workspace INTENDS to call; this says what is really loaded.
          They can disagree in a way nothing else surfaces: vLLM answers only to its
          --served-model-name, so a healthy, running server will 404 every request made under any
          other name. Without this strip that reads as "the model is broken". */}
      {runtime?.servers?.length ? (
        <div className="mb-6 space-y-3">
          {runtime.servers.map((sv) => {
            const bad = sv.models.filter((m) => m.status !== "ok");
            const tone = !sv.reachable ? "border-red-200 bg-red-50/60"
              : bad.length ? "border-amber-200 bg-amber-50/60"
              : "border-emerald-200 bg-emerald-50/50";
            return (
              <div key={sv.base_url} className={`border rounded-xl p-3.5 ${tone}`}>
                <div className="flex items-center gap-2 flex-wrap">
                  <Server size={15} className="text-stone-500 shrink-0" />
                  <span className="text-sm font-semibold text-stone-800">Model server</span>
                  <span className="font-mono text-xs text-stone-500">{sv.base_url}</span>
                  {sv.is_env_default && (
                    <span className="text-[10px] uppercase tracking-wider font-bold text-stone-400">default</span>
                  )}
                  <span className="ml-auto">
                    {!sv.reachable ? (
                      <span className="text-xs font-semibold text-red-700 flex items-center gap-1">
                        <AlertCircle size={13} /> unreachable
                      </span>
                    ) : bad.length ? (
                      <span className="text-xs font-semibold text-amber-700 flex items-center gap-1">
                        <AlertCircle size={13} /> {bad.length} model id{bad.length > 1 ? "s" : ""} not served
                      </span>
                    ) : (
                      <span className="text-xs font-semibold text-emerald-700 flex items-center gap-1">
                        <CheckCircle2 size={13} /> serving
                      </span>
                    )}
                  </span>
                </div>
                {sv.reachable ? (
                  <p className="text-xs text-stone-600 mt-1.5">
                    Running: {sv.served_models.length
                      ? sv.served_models.map((m) => <span key={m} className="font-mono mr-2">{m}</span>)
                      : <span className="italic">nothing yet — still loading weights</span>}
                  </p>
                ) : (
                  <p className="text-xs text-red-700 mt-1.5">{sv.error}</p>
                )}
                {bad.map((m) => (
                  <p key={m.id} className="text-xs text-amber-800 mt-1.5">
                    <b>{m.role}</b> asks for <span className="font-mono">{m.model_id}</span> — {m.hint}
                  </p>
                ))}
              </div>
            );
          })}
          <p className="text-[11px] text-stone-400">{runtime.note}</p>
        </div>
      ) : null}

      {loading ? (
        <div className="p-8 text-stone-500 animate-pulse">Loading models…</div>
      ) : (
        <>
        {ROLES.map(renderSection)}
        </>
      )}

      {modal && (
        <div className="fixed inset-0 bg-stone-900/40 backdrop-blur-sm z-[60] flex items-center justify-center p-4" onClick={() => !busy && setModal(false)}>
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[92vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <div className="px-6 py-4 border-b border-stone-100 flex items-center justify-between sticky top-0 bg-white">
              <h2 className="font-bold text-stone-900 flex items-center gap-2"><Cpu size={18} className="text-emerald-500" /> {form.id ? "Edit" : "Add"} Model</h2>
              <button onClick={() => setModal(false)} className="p-1.5 text-stone-400 hover:bg-stone-100 rounded-md"><X size={18} /></button>
            </div>
            <div className="p-6 space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <Field label="Name"><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className={inp} placeholder="e.g. GPT-4o (prod)" /></Field>
                <Field label="Role">
                  <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} className={inp}>
                    {ROLES.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
                  </select>
                </Field>
              </div>
              <Field label="Provider">
                <select value={form.provider} onChange={(e) => {
                  const p = PROVIDERS.find((x) => x.id === e.target.value);
                  setForm({ ...form, provider: e.target.value, base_url: form.base_url || (p?.base || "") });
                }} className={inp}>
                  {PROVIDERS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
                </select>
              </Field>
              <Field label="Base URL (optional)"><input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} className={inp + " font-mono"} placeholder="provider endpoint" /></Field>
              {needsKey && (
                <Field label="API Key"><input type="password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} className={inp} placeholder={form.id ? "•••••• (leave blank to keep)" : "sk-…"} /></Field>
              )}

              {/* Live picker — what this provider serves RIGHT NOW, asked of the provider itself.
                  For vLLM these are its --served-model-name(s): the only names it will answer to. */}
              <div className="border border-stone-200 rounded-xl p-3 bg-stone-50/60">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-bold text-stone-500 uppercase tracking-wider flex items-center gap-1.5">
                    <HardDriveDownload size={13} /> Models this server is running
                  </span>
                  <button type="button" onClick={runDiscover} disabled={discover.loading}
                    className="text-xs font-semibold text-emerald-600 hover:text-emerald-800 flex items-center gap-1 disabled:opacity-50">
                    {discover.loading ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />} Refresh
                  </button>
                </div>
                {discover.loading ? (
                  <div className="text-xs text-stone-400 py-2">Listing models…</div>
                ) : discover.items.length > 0 ? (
                  <div className="max-h-40 overflow-y-auto space-y-1 pr-0.5">
                    {discover.items.map((d) => (
                      <button type="button" key={d.id} onClick={() => setForm((f) => ({ ...f, model_id: d.id, name: f.name || d.id }))}
                        className={`w-full text-left px-2.5 py-1.5 rounded-lg text-xs font-mono flex items-center justify-between gap-2 border transition-colors ${form.model_id === d.id ? "bg-emerald-50 border-emerald-300 text-emerald-800" : "bg-white border-stone-200 hover:border-emerald-300 text-stone-700"}`}>
                        <span className="truncate">{d.id}</span>
                        <span className="text-[10px] text-stone-400 shrink-0 flex items-center gap-1">
                          {d.detail || (d.size ? fmtBytes(d.size) : "")}
                          {form.model_id === d.id && <CheckCircle2 size={12} className="text-emerald-500" />}
                        </span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="text-xs text-stone-400 py-1">{discover.note || "No models found."}</div>
                )}

              </div>

              <Field label="Model ID"><input value={form.model_id} onChange={(e) => setForm({ ...form, model_id: e.target.value })} className={inp + " font-mono"} placeholder={form.role === "embedding" ? "nomic-ai/nomic-embed-text-v1.5 / text-embedding-3-small" : "opencortyx-llm (vLLM served name) / gpt-4o / claude-sonnet-5-5"} /></Field>
              {form.provider === "vllm" && (
                <p className="text-[11px] text-stone-400 -mt-1">
                  For vLLM this must be the server&apos;s <span className="font-mono">--served-model-name</span>, not a
                  HuggingFace repo — pick it from the list above. Any other name returns 404 on every call.
                </p>
              )}
              <label className="flex items-center gap-2 text-sm text-stone-700">
                <input type="checkbox" checked={form.is_default} onChange={(e) => setForm({ ...form, is_default: e.target.checked })} className="rounded border-stone-300" />
                Make default for <b>{form.role}</b> (used when an extractor/chat doesn&apos;t pick a specific model)
              </label>
              {msg.text && (
                <div className={`p-3 rounded-lg text-sm font-medium flex items-center gap-2 ${msg.type === "error" ? "bg-red-50 text-red-700 border border-red-100" : "bg-teal-50 text-teal-700 border border-teal-100"}`}>
                  {msg.type === "error" ? <AlertCircle size={16} /> : <Loader2 size={16} className="animate-spin" />} {msg.text}
                </div>
              )}
            </div>
            <div className="px-6 py-4 border-t border-stone-100 flex justify-end gap-3 sticky bottom-0 bg-white">
              <Button variant="secondary" disabled={busy} onClick={() => setModal(false)}>Cancel</Button>
              <Button variant="primary" icon={CheckCircle2} busy={busy} onClick={save}>{form.id ? "Update" : "Save"} Model</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const inp = "w-full border border-stone-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20";
function fmtBytes(n?: number | null) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div><label className="block text-xs font-bold text-stone-500 uppercase tracking-wider mb-1.5">{label}</label>{children}</div>;
}
