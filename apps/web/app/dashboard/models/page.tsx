"use client";

import { useEffect, useState } from "react";
import { contextV2Api, ModelEntry, ModelSpecInput, ActiveModel, DiscoveredModel, ModelRuntime } from "@/lib/api/contextV2";
import {
  Cpu, Plus, Trash2, Pencil, X, CheckCircle2, AlertCircle, Loader2, Star, ShieldCheck, Sparkles, Box, Layers, Brain, Tags, Zap, RefreshCw, HardDriveDownload, Server,
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import ModelDownload, { DOWNLOADABLE } from "./ModelDownload";

// A server you run yourself is "openai_compatible" + its address: Ollama, LM Studio, vLLM, ... all
// speak that protocol. Inside Docker, a server on this computer is host.docker.internal.
const PROVIDERS = [
  { id: "openai_compatible", label: "OpenAI-compatible server (Ollama, LM Studio, vLLM, …)", needsKey: false, base: "http://host.docker.internal:11434/v1" },
  { id: "openai", label: "OpenAI", needsKey: true, base: "" },
  { id: "anthropic", label: "Anthropic (Claude)", needsKey: true, base: "" },
  { id: "huggingface", label: "HuggingFace Inference (needs a HF token)", needsKey: true, base: "" },
  { id: "groq", label: "Groq", needsKey: true, base: "https://api.groq.com/openai/v1" },
  // Run inside OpenCortyx itself — no server, no address.
  { id: "transformers", label: "Hugging Face model in OpenCortyx (CPU, download first)", needsKey: false, base: "" },
  { id: "fastembed", label: "fastembed (in OpenCortyx, ONNX on CPU: embedding, rerank)", needsKey: false, base: "" },
  { id: "sentence-transformers", label: "sentence-transformers (in-process, needs INSTALL_RERANK=1)", needsKey: false, base: "" },
  { id: "spacy", label: "spaCy pipeline in OpenCortyx (entities: downloaded on first use)", needsKey: false, base: "" },
];
// providers that expose a live model list at /v1/models; for `transformers`, what is downloaded;
// for `fastembed`, every model it can run for the role
const DISCOVERABLE = new Set(["openai", "groq", "openai_compatible", "together", "azure", "huggingface", "transformers", "fastembed"]);
// providers with no address and no key: they run inside OpenCortyx
const IN_PROCESS = new Set(["fastembed", "sentence-transformers", "transformers", "spacy"]);
// priority tiers shown as badges (matches the model-stack matrix)
const TIER: Record<string, { label: string; cls: string }> = {
  mandatory:   { label: "Required",    cls: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  recommended: { label: "Recommended", cls: "bg-amber-50 text-amber-700 border-amber-200" },
  useful:      { label: "Useful",      cls: "bg-sky-50 text-sky-700 border-sky-200" },
  optional:    { label: "Optional",    cls: "bg-stone-100 text-stone-600 border-stone-200" },
};

const ROLES = [
  { id: "generation", label: "Generation (LLM)", icon: Sparkles, tier: "mandatory", wired: true,
    purpose: "Answer questions, summarize incidents, generate SAR narratives.", hint: "Used by chat answers, and by extraction and utility unless they have their own." },
  { id: "embedding", label: "Embedding", icon: Layers, tier: "mandatory", wired: true,
    purpose: "Convert text to vectors for semantic search.", hint: "Vectors for search. Changing this needs a re-index." },
  { id: "rerank", label: "Reranker", icon: Box, tier: "optional", wired: true,
    purpose: "Re-rank retrieved documents before sending to the LLM.", hint: "Re-orders retrieved candidates. Without it, retrieval still ranks by its other signals." },
  { id: "extraction", label: "Extraction", icon: Tags, tier: "recommended", wired: true,
    purpose: "Run the per-chunk extractors (entities, claims, and your custom extractors).",
    hint: "The high-volume per-chunk calls. A cheaper/faster model here cuts ingest cost; with none set, the Generation model is used. Per-extractor model overrides on each Extractor card still win." },
  { id: "entities", label: "Entities (in-process)", icon: Tags, tier: "useful", wired: true,
    purpose: "Read entities and how sentences connect them, from documents and free-text fields - no LLM.",
    hint: "A spaCy pipeline from Hugging Face (Model ID like spacy/en_core_web_sm, provider spacy), run inside OpenCortyx. Used when \"Extract entities and relationships\" is ticked in Add data." },
  { id: "utility", label: "Utility / Judge", icon: Brain, tier: "recommended", wired: true,
    purpose: "The relevance check (CRAG) and the grounding / entailment judge that enforce abstention.",
    hint: "Give this a capable model to make strict (entailment) abstention reliable — a weak judge over-rejects. With none set, the Generation model is used." },
];

const EMPTY: ModelSpecInput & { id?: string } = {
  id: "", name: "", role: "generation", provider: "openai_compatible", model_id: "",
  base_url: "http://host.docker.internal:11434/v1", api_key: "", is_default: true,
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
  // a download for the form's Model ID is running: the form must not close or save under it
  const [downloading, setDownloading] = useState(false);

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
  // Embedding and reranking run in OpenCortyx (fastembed) unless the user picks a server.
  const openAdd = (role = "generation") => {
    const local = role === "embedding" || role === "rerank";
    setForm({ ...EMPTY, role, ...(local ? { provider: "fastembed", base_url: "" } : {}) });
    setMsg({ text: "", type: "" }); resetPicker(); setModal(true);
  };
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
        api_key: form.api_key || undefined, model_id: form.id || undefined, role: form.role,
      });
      setDiscover({ loading: false, items: res.items || [], note: res.note });
    } catch (e: any) {
      setDiscover({ loading: false, items: [], note: e?.response?.data?.detail || e?.message || "Couldn't list models." });
    }
  };
  // auto-discover when the modal opens or the provider changes
  useEffect(() => { if (modal) runDiscover(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [modal, form.provider, form.role]);

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
    if (!confirm(`Delete model "${m.name}"? Whatever uses it will have no model until you choose another.`)) return;
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
        {act && act.source === "none" && (
          <div className="mb-2 flex items-center gap-2 text-xs bg-amber-50/70 border border-amber-200 rounded-lg px-3 py-2 text-amber-800">
            <AlertCircle size={13} className="shrink-0" />
            <span className="font-semibold">Not connected.</span>
            <span>Add a model for this role and mark it default.</span>
          </div>
        )}
        {act && act.source === "registry" && (
          <div className="mb-2 flex items-center gap-2 text-xs bg-emerald-50/60 border border-emerald-100 rounded-lg px-3 py-2">
            <Zap size={13} className="text-emerald-500 shrink-0" />
            <span className="font-semibold text-emerald-800">Active now:</span>
            <span className="font-mono text-stone-700 truncate">{act.model_id}</span>
            <span className="text-[10px] font-bold uppercase px-1.5 py-0.5 rounded bg-white border border-stone-200 text-stone-500">{act.provider}</span>
            <span className="text-[10px] font-bold uppercase px-1.5 py-0.5 rounded border text-emerald-700">
              {act.shared_from ? `uses the ${act.shared_from} model` : `default${act.name ? ` · ${act.name}` : ""}`}
            </span>
          </div>
        )}
        {act?.source === "registry" && act.note && (
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
                      {(m as any).warning && <p className="text-xs text-amber-700 mt-1">{(m as any).warning}</p>}
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

      {/* What each connected model server is ACTUALLY serving.
          The registry says what this workspace INTENDS to call; this says what is really there.
          A server answers 404 to a model name it does not serve, which without this strip reads
          as "the model is broken". */}
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
                  {sv.is_generation && (
                    <span className="text-[10px] uppercase tracking-wider font-bold text-stone-400">generation</span>
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
                      : <span className="italic">no models yet — pull or load one on the server</span>}
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
        <div className="fixed inset-0 bg-stone-900/40 backdrop-blur-sm z-[60] flex items-center justify-center p-4" onClick={() => !busy && !downloading && setModal(false)}>
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[92vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <div className="px-6 py-4 border-b border-stone-100 flex items-center justify-between sticky top-0 bg-white">
              <h2 className="font-bold text-stone-900 flex items-center gap-2"><Cpu size={18} className="text-emerald-500" /> {form.id ? "Edit" : "Add"} Model</h2>
              <button aria-label="Close" disabled={downloading} onClick={() => setModal(false)} className="p-1.5 text-stone-400 hover:bg-stone-100 rounded-md disabled:opacity-40"><X size={18} /></button>
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
                  setForm({ ...form, provider: e.target.value, base_url: p?.base || "" });
                }} className={inp}>
                  {PROVIDERS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
                </select>
              </Field>
              {!IN_PROCESS.has(form.provider) && (
                <Field label={form.provider === "openai_compatible" ? "Server address" : "Base URL (optional)"}>
                  <input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} className={inp + " font-mono"}
                    placeholder={form.provider === "openai_compatible" ? "http://host.docker.internal:11434/v1" : "provider endpoint"} />
                </Field>
              )}
              {form.provider === "openai_compatible" && (
                <p className="text-[11px] text-stone-400 -mt-2">
                  A server on this computer is <span className="font-mono">host.docker.internal</span>, not <span className="font-mono">localhost</span>.
                  Ollama: <span className="font-mono">:11434/v1</span> · LM Studio: <span className="font-mono">:1234/v1</span>.
                </p>
              )}
              {(needsKey || form.provider === "openai_compatible") && (
                <Field label={needsKey ? "API Key" : "API Key (if the server needs one)"}><input type="password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} className={inp} placeholder={form.id ? "•••••• (leave blank to keep)" : needsKey ? "sk-… / hf_…" : "optional"} /></Field>
              )}

              {/* Live picker — what this provider serves RIGHT NOW, asked of the provider itself:
                  the only model names it will answer to. */}
              <div className="border border-stone-200 rounded-xl p-3 bg-stone-50/60">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-bold text-stone-500 uppercase tracking-wider flex items-center gap-1.5">
                    <HardDriveDownload size={13} /> {form.provider === "transformers" ? "Downloaded to OpenCortyx" : form.provider === "fastembed" ? "Models fastembed can run" : "Models this server is running"}
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
                          {[d.detail, d.size ? fmtBytes(d.size) : "", d.downloaded ? "downloaded" : ""].filter(Boolean).join(" · ")}
                          {form.model_id === d.id && <CheckCircle2 size={12} className="text-emerald-500" />}
                        </span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="text-xs text-stone-400 py-1">{discover.note || "No models found."}</div>
                )}

              </div>

              <Field label="Model ID"><input value={form.model_id} onChange={(e) => setForm({ ...form, model_id: e.target.value })} className={inp + " font-mono"} placeholder={form.role === "embedding" ? "nomic-ai/nomic-embed-text-v1.5 / text-embedding-3-small" : form.role === "rerank" ? "Xenova/ms-marco-MiniLM-L-6-v2" : form.provider === "transformers" ? "Qwen/Qwen2.5-0.5B-Instruct" : "qwen2.5:1.5b (Ollama) / Qwen/Qwen2.5-1.5B-Instruct / gpt-4o"} /></Field>
              {/* Not on this machine yet: download it here, into OpenCortyx or into Ollama. */}
              {DOWNLOADABLE.has(form.provider) && !discover.loading && (
                <ModelDownload provider={form.provider} role={form.role} baseUrl={form.base_url || ""} modelId={form.model_id}
                  available={discover.items} onBusy={setDownloading}
                  onDone={(id) => { if (id) setForm((f) => ({ ...f, model_id: id, name: f.name || id.split("/").pop() || id })); runDiscover(); }} />
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
              <Button variant="secondary" disabled={busy || downloading} onClick={() => setModal(false)}>Cancel</Button>
              <Button variant="primary" icon={CheckCircle2} busy={busy} disabled={downloading} onClick={save}>{form.id ? "Update" : "Save"} Model</Button>
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
