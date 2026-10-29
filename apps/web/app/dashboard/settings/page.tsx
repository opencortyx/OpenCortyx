"use client";

import { useEffect, useRef, useState } from "react";
import { contextV2Api } from "@/lib/api/contextV2";
import { Shield, Clock, Server, AlertCircle, Save, SlidersHorizontal, Lock, Download, Upload, HardDriveDownload, Layers, Calculator, Network, Workflow, Settings2, Boxes, ExternalLink, RefreshCw, Cpu, MemoryStick, Scissors } from "lucide-react";
import Link from "next/link";
import PageHeader from "@/components/layout/PageHeader";
import { FULL_UI, GENERAL_SETTINGS, HIDDEN_SETTINGS_TABS, SETTING_COPY, SHOW_OPERATOR_PANELS } from "@/lib/simple";
import Button from "@/components/ui/Button";

export default function SettingsPage() {
  const [logs, setLogs] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [computing, setComputing] = useState(false);

  // Performance & scaling config
  const [cfgSchema, setCfgSchema] = useState<any[]>([]);
  const [cfgValues, setCfgValues] = useState<Record<string, any>>({});
  const [cfgReadonly, setCfgReadonly] = useState<Record<string, any>>({});
  const [cfgSaving, setCfgSaving] = useState(false);
  // Rebuilding a built-in ontology onto the graph (Person / Company). `null` = idle.
  const [ontoBusy, setOntoBusy] = useState<"person" | "organization" | null>(null);
  const [ontoMsg, setOntoMsg] = useState("");
  const [cfgMsg, setCfgMsg] = useState("");

  // Backup / export
  const [backupBusy, setBackupBusy] = useState(false);
  const [backupMsg, setBackupMsg] = useState("");
  const [importMode, setImportMode] = useState<"merge" | "replace">("merge");
  const fileRef = useRef<HTMLInputElement>(null);

  // Sub-tabs (clean "data hub" layout)
  const [tab, setTab] = useState<"performance" | "parallelism" | "chunking" | "capabilities" | "extraction" | "agents" | "backup" | "audit" | "danger">("performance");

  // Capabilities tab — the platform-tree capability modules (schema-driven)
  const [caps, setCaps] = useState<any[]>([]);
  const [capVals, setCapVals] = useState<Record<string, Record<string, any>>>({});
  const [capSaving, setCapSaving] = useState("");
  const [capMsg, setCapMsg] = useState("");

  // Extraction tab — every available extractor (built-in / custom / relationship) + selection
  const [extractors, setExtractors] = useState<any[]>([]);
  const [selExt, setSelExt] = useState<Record<string, boolean>>({});
  const [extSaving, setExtSaving] = useState(false);
  const [extMsg, setExtMsg] = useState("");
  const [calcChunkChars, setCalcChunkChars] = useState(2000);
  const [calcDocChars, setCalcDocChars] = useState(200000);
  const [calcCtxTokens, setCalcCtxTokens] = useState(8192);

  useEffect(() => {
    loadLogs();
    loadConfig();
    loadExtractors();
    loadCapabilities();
    // deep-link: /dashboard/settings?tab=capabilities opens that sub-tab (capability ui_route links)
    try {
      const t = new URLSearchParams(window.location.search).get("tab");
      if (t) setTab(t as typeof tab);
    } catch { /* ignore */ }
  }, []);

  const loadCapabilities = async () => {
    try {
      const res = await contextV2Api.getCapabilities();
      const list = res.capabilities || [];
      setCaps(list);
      // json fields (e.g. compare-passes) are edited as pretty-printed text
      const vals: Record<string, Record<string, any>> = {};
      for (const c of list) {
        vals[c.name] = { ...c.values };
        for (const f of c.schema || []) {
          if (f.kind === "json") vals[c.name][f.key] = JSON.stringify(c.values?.[f.key] ?? null, null, 2);
        }
      }
      setCapVals(vals);
    } catch {
      // ignore
    }
  };

  const handleSaveCapability = async (name: string) => {
    setCapSaving(name);
    setCapMsg("");
    try {
      const cap = caps.find((c) => c.name === name);
      const payload: Record<string, any> = { ...(capVals[name] || {}) };
      for (const f of cap?.schema || []) {           // parse json-kind fields before sending
        if (f.kind === "json" && typeof payload[f.key] === "string") {
          try { payload[f.key] = JSON.parse(payload[f.key]); }
          catch { setCapMsg(`Failed: ${f.label} is not valid JSON.`); setCapSaving(""); return; }
        }
      }
      await contextV2Api.updateCapability(name, payload);
      await loadCapabilities();
      await loadConfig(); // capability values share the runtime_config store — keep Performance tab in sync
      setCapMsg(`Saved ${name} — applied to the running service.`);
    } catch (err: any) {
      setCapMsg(`Failed: ${err?.response?.data?.detail || err?.message || err}`);
    } finally {
      setCapSaving("");
    }
  };

  const loadExtractors = async () => {
    try {
      const exts = await contextV2Api.getExtractionTemplates();
      setExtractors(exts || []);
      // selection = the template's real status (what the engine actually honours)
      setSelExt(Object.fromEntries((exts || []).map((e: any) => [e.id, e.status !== "disabled"])));
    } catch {
      // ignore
    }
  };

  // Save the whole Extraction tab: call groups + concurrency (runtime config) AND the
  // active/disabled status of every extractor whose checkbox changed.
  const handleSaveExtraction = async () => {
    setExtSaving(true);
    setExtMsg("");
    try {
      const res = await contextV2Api.updateConfig(cfgValues);
      setCfgValues(res.tunable || cfgValues);
      const changed = extractors.filter((e: any) => (e.status !== "disabled") !== !!selExt[e.id]);
      for (const e of changed) {
        await contextV2Api.setExtractorStatus(e.id, selExt[e.id] ? "active" : "disabled");
      }
      if (changed.length) await loadExtractors();
      setExtMsg(`Saved — applied to the running service${changed.length ? ` · ${changed.length} extractor(s) toggled` : ""}.`);
    } catch (err: any) {
      setExtMsg(`Failed: ${err?.response?.data?.detail || err?.message || err}`);
    } finally {
      setExtSaving(false);
    }
  };

  // classify an extractor into a display bucket
  const extBucket = (e: any): "relationship" | "builtin" | "custom" =>
    String(e.mechanism?.ref || "").startsWith("relate.") ? "relationship"
      : String(e.name || "").startsWith("builtin:") ? "builtin" : "custom";

  const handleExport = async () => {
    setBackupBusy(true);
    setBackupMsg("");
    try {
      const doc = await contextV2Api.exportGraph(true);
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      a.download = `opencortyx-backup-${stamp}.json`;
      a.click();
      URL.revokeObjectURL(url);
      const c = doc?.counts || {};
      setBackupMsg(`Exported ${c.nodes ?? 0} nodes, ${c.edges ?? 0} edges, ${c.templates ?? 0} templates, ${c.pipelines ?? 0} pipelines.`);
    } catch (err: any) {
      setBackupMsg(`Export failed: ${err?.response?.data?.detail || err?.message || err}`);
    } finally {
      setBackupBusy(false);
    }
  };

  const handleImportFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (importMode === "replace" && !window.confirm("Replace mode wipes this org's current nodes & edges before importing. Continue?")) {
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    setBackupBusy(true);
    setBackupMsg("");
    try {
      const data = JSON.parse(await file.text());
      const res = await contextV2Api.importGraph(data, importMode);
      const r = res.imported || {};
      setBackupMsg(`Imported (${res.mode}): ${r.nodes ?? 0} nodes, ${r.edges ?? 0} edges, ${r.templates ?? 0} templates, ${r.pipelines ?? 0} pipelines.`);
    } catch (err: any) {
      setBackupMsg(`Import failed: ${err?.response?.data?.detail || err?.message || "invalid file"}`);
    } finally {
      setBackupBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const loadConfig = async () => {
    try {
      const res = await contextV2Api.getConfig();
      setCfgSchema(res.schema || []);
      setCfgValues(res.tunable || {});
      setCfgReadonly(res.readonly || {});
    } catch {
      // ignore
    }
  };

  const handleSaveConfig = async () => {
    setCfgSaving(true);
    setCfgMsg("");
    try {
      const res = await contextV2Api.updateConfig(cfgValues);
      setCfgValues(res.tunable || cfgValues);
      setCfgMsg("Saved — applied to the running service.");
    } catch (err: any) {
      setCfgMsg(`Failed: ${err?.response?.data?.detail || err?.message || err}`);
    } finally {
      setCfgSaving(false);
    }
  };

  const loadLogs = async () => {
    setLoading(true);
    try {
      const res = await contextV2Api.auditLogs(50);
      setLogs(res.items || []);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  };


  const TABS = [
    { id: "performance", label: "General", icon: SlidersHorizontal },
    { id: "parallelism", label: "Parallelism", icon: Network },
    { id: "chunking", label: "Chunking", icon: Scissors },
    { id: "capabilities", label: "Capabilities", icon: Boxes },
    { id: "extraction", label: "Extraction", icon: Layers },
    { id: "agents", label: "Agents", icon: Shield },
    { id: "backup", label: "Backup & Export", icon: HardDriveDownload },
    { id: "audit", label: "Audit Log", icon: Server },
    { id: "danger", label: "Danger Zone", icon: AlertCircle },
  ].filter((tb) => !HIDDEN_SETTINGS_TABS.has(tb.id));

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      <PageHeader
        title="Settings"
        icon={Settings2}
        description={FULL_UI ? "Performance, backup, agents and audit — your control hub."
                             : "Your workspace, its models and people, and how answers are checked."}
      />

      {/* What left the sidebar in the simple product (lib/simple.ts), one click from here. */}
      {!FULL_UI && (
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
          {[
            { href: "/dashboard/models", title: "Models", text: "The models answers and reading use" },
            { href: "/dashboard/users", title: "Users & access", text: "Who can see and change what" },
            { href: "/dashboard/eval", title: "Quality checks", text: "Questions with known answers, run on demand" },
            { href: "/dashboard/progress", title: "Runs", text: "Every ingest, step by step" },
            { href: "/dashboard/health", title: "System health", text: "Is every part up" },
          ].map((c) => (
            <Link key={c.href} href={c.href as any}
                  className="rounded-lg border border-stone-200 bg-white px-3 py-2.5 hover:border-stone-300 hover:bg-stone-50 transition-colors">
              <div className="text-sm font-semibold text-stone-800">{c.title}</div>
              <div className="text-xs text-stone-500">{c.text}</div>
            </Link>
          ))}
        </div>
      )}

      {/* sub-tabs */}
      <div className="flex flex-wrap gap-1 border-b border-stone-200">
        {TABS.map((tb) => (
          <button key={tb.id} onClick={() => setTab(tb.id as typeof tab)}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-semibold rounded-t-lg border-b-2 -mb-px transition-colors ${tab === tb.id ? "border-emerald-500 text-emerald-600 bg-emerald-50/40" : "border-transparent text-stone-500 hover:text-stone-800 hover:bg-stone-50"}`}>
            <tb.icon size={15} /> {tb.label}
          </button>
        ))}
      </div>

      {/* Performance & Scaling */}
      {tab === "performance" && (
      <div className="bg-white rounded-xl shadow-sm border border-stone-200 overflow-hidden">
        <div className="px-6 py-5 border-b border-stone-100 bg-stone-50 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <SlidersHorizontal size={18} className="text-emerald-500" />
            <div>
              <h2 className="font-semibold text-stone-800">General</h2>
              <p className="text-xs text-stone-500">{FULL_UI ? "Your workspace name, the built-in ontologies, and live-tunable retrieval, caching and quality knobs."
                : "Your workspace, and how answers and files are handled."}</p>
            </div>
          </div>
          <button
            onClick={handleSaveConfig}
            disabled={cfgSaving}
            className="flex items-center gap-2 px-4 py-2 bg-stone-900 hover:bg-stone-800 text-white text-sm font-semibold rounded-lg transition-colors disabled:opacity-50"
          >
            <Save size={15} /> {cfgSaving ? "Saving…" : "Save & Apply"}
          </button>
        </div>

        <div className="p-6 space-y-6">
          {/* Workspace mode toggle removed — the graph now always shows BOTH the Person and Team
              ontologies (plus everything you author); visibility is controlled per-type on the Nodes &
              Relationships page, not by a workspace switch. */}

          {/* Rebuild the built-in ontologies AS GRAPH. They are materialised at signup, so a reset
              database (or an import that replaced the graph) leaves the type vocabulary intact but the
              hierarchy itself gone — and there was no way to get it back without creating a new
              workspace. Idempotent: re-running adopts what is already there instead of duplicating,
              so it is safe to press at any time. */}
          {SHOW_OPERATOR_PANELS && (
          <div>
            <h3 className="text-xs font-bold uppercase tracking-wider text-stone-400 mb-3">Built-in ontologies</h3>
            <div className="border border-stone-200 rounded-xl p-4 bg-stone-50/60 space-y-3">
              <p className="text-xs text-stone-500 leading-relaxed">
                Puts the <b>Person</b> and <b>Company</b> hierarchies back on the graph as real nodes and
                relationships, so they render in Lineage and can be anchored to. Safe to re-run — existing
                nodes are reused, never duplicated.
              </p>
              <div className="flex flex-wrap items-center gap-2">
                {(["person", "organization"] as const).map((t) => (
                  <button key={t} type="button" disabled={ontoBusy !== null}
                    onClick={async () => {
                      setOntoBusy(t); setOntoMsg("");
                      try {
                        const r: any = await contextV2Api.materializeOntology(t);
                        setOntoMsg(`${t === "person" ? "Person" : "Company"}: ${r.nodes ?? 0} nodes, ${r.edges_made ?? 0} relationships on the graph — visible on the Lineage page in a few seconds.`);
                      } catch (e: any) {
                        setOntoMsg(e?.response?.data?.detail || `Could not rebuild the ${t} ontology.`);
                      } finally { setOntoBusy(null); }
                    }}
                    className="px-3 py-2 rounded-lg text-xs font-semibold border border-stone-200 bg-white text-stone-700 hover:bg-stone-50 disabled:opacity-50 flex items-center gap-1.5">
                    <RefreshCw size={13} className={ontoBusy === t ? "animate-spin" : ""} />
                    {ontoBusy === t ? "Rebuilding…" : `Rebuild ${t === "person" ? "Person" : "Company"}`}
                  </button>
                ))}
                {ontoMsg && <span className="text-xs text-stone-600">{ontoMsg}</span>}
              </div>
            </div>
          </div>
          )}
          {["Workspace", "Memory Accuracy", "Latency", "Retrieval", "Ingestion", "Local files", "Caching", "Quality"].map((group) => {
            /* "Parallelism" is deliberately absent: every concurrency knob now lives in its own
               tab, so the one place you tune throughput is not scattered across three sections. */
            // The simple product shows the few a person can judge; the rest keep their defaults or `.env`.
            const items = cfgSchema.filter((s: any) => s.group === group && (!GENERAL_SETTINGS || GENERAL_SETTINGS.has(s.key)));
            if (items.length === 0) return null;
            return (
              <div key={group}>
                <h3 className="text-xs font-bold uppercase tracking-wider text-stone-400 mb-3">{group}</h3>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-4">
                  {items.map((s: any) => (
                    <div key={s.key} className="flex items-center justify-between gap-4">
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-medium text-stone-800">{SETTING_COPY[s.key]?.label || s.label}</div>
                        <div className="text-xs text-stone-400">{SETTING_COPY[s.key]?.help || s.help}</div>
                      </div>
                      {s.key === "grounding_mode" && SETTING_COPY[s.key] ? (
                        <button aria-label="Check answers strictly"
                          onClick={() => setCfgValues({ ...cfgValues, grounding_mode: cfgValues.grounding_mode === "entailment" ? "cosine" : "entailment" })}
                          className={`shrink-0 w-11 h-6 rounded-full transition-colors relative ${cfgValues.grounding_mode === "entailment" ? "bg-emerald-500" : "bg-stone-300"}`}
                        >
                          <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform ${cfgValues.grounding_mode === "entailment" ? "translate-x-5" : ""}`} />
                        </button>
                      ) : s.kind === "bool" ? (
                        <button
                          onClick={() => setCfgValues({ ...cfgValues, [s.key]: !cfgValues[s.key] })}
                          className={`shrink-0 w-11 h-6 rounded-full transition-colors relative ${cfgValues[s.key] ? "bg-emerald-500" : "bg-stone-300"}`}
                        >
                          <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform ${cfgValues[s.key] ? "translate-x-5" : ""}`} />
                        </button>
                      ) : s.kind === "select" ? (
                        <select
                          value={cfgValues[s.key] ?? ""}
                          onChange={(e) => setCfgValues({ ...cfgValues, [s.key]: e.target.value })}
                          className="shrink-0 border border-stone-200 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20"
                        >
                          {(s.options || []).map((o: string) => <option key={o} value={o}>{o}</option>)}
                        </select>
                      ) : s.kind === "text" ? (
                        <input
                          type="text"
                          value={cfgValues[s.key] ?? ""}
                          onChange={(e) => setCfgValues({ ...cfgValues, [s.key]: e.target.value })}
                          className="shrink-0 w-40 sm:w-56 border border-stone-200 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20"
                        />
                      ) : (
                        <input
                          type="number"
                          step={s.kind === "float" ? "0.05" : "1"}
                          min={s.min}
                          max={s.max}
                          value={cfgValues[s.key] ?? ""}
                          onChange={(e) =>
                            setCfgValues({
                              ...cfgValues,
                              [s.key]: s.kind === "float" ? parseFloat(e.target.value) : parseInt(e.target.value || "0", 10),
                            })
                          }
                          className="shrink-0 w-24 border border-stone-200 rounded-md px-3 py-1.5 text-sm text-right font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500/20"
                        />
                      )}
                    </div>
                  ))}
                </div>
              </div>
            );
          })}

          {cfgMsg && (
            <div className="text-sm font-medium text-stone-600 bg-stone-50 border border-stone-200 rounded-lg px-3 py-2">{cfgMsg}</div>
          )}

          {/* Compute: where generation goes and what this container may use. The model is chosen
              on the Models page; the memory ceiling is a container limit fixed before the process
              starts. Shown because this is the screen you are on when generation is failing or a
              worker is being killed. */}
          {SHOW_OPERATOR_PANELS && (<>
          <div className="pt-4 border-t border-stone-100">
            <h3 className="text-xs font-bold uppercase tracking-wider text-stone-400 mb-3 flex items-center gap-1.5">
              <Cpu size={12} /> Compute &amp; memory
            </h3>
            <div className="grid gap-3 md:grid-cols-2">
              <div className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] font-bold uppercase tracking-wider text-stone-400">Generation model</span>
                  <span className="text-[11px] font-mono text-stone-600 truncate">{String(cfgReadonly.llm_model ?? "—")}</span>
                </div>
                <div className="text-[11.5px] text-stone-500 mt-1.5">
                  Generation goes to <span className="font-mono">{String(cfgReadonly.generation_endpoint ?? "—")}</span>.
                </div>
                <div className="text-[11.5px] text-stone-500 mt-1.5">
                  To change it, open <b>Models</b> and connect a model server (Ollama, LM Studio, a hosted
                  API, ...) by its address.
                </div>
              </div>

              <div className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] font-bold uppercase tracking-wider text-stone-400 flex items-center gap-1.5">
                    <MemoryStick size={11} /> API container memory
                  </span>
                  <span className="text-[11px] font-mono text-stone-600">
                    {String(cfgReadonly.api_memory_in_use ?? "—")} / {String(cfgReadonly.api_memory_ceiling ?? "—")}
                  </span>
                </div>
                <div className="text-[11.5px] text-stone-500 mt-1.5">
                  {cfgReadonly.api_memory_headroom
                    ? <>Headroom <b>{String(cfgReadonly.api_memory_headroom)}</b>. A container over its ceiling is killed, not slowed.</>
                    : <>No container limit detected — this process can take the whole machine.</>}
                </div>
                <div className="text-[11.5px] text-stone-500 mt-1.5">
                  This container only. For the whole stack against your VM, run
                  {" "}<span className="font-mono">python scripts/check-memory-budget.py --stats</span> — the
                  sum of every ceiling is what decides whether the kernel starts killing things.
                </div>
              </div>
            </div>
          </div>

          {/* read-only infra knobs (restart / reindex required) */}
          <div className="pt-4 border-t border-stone-100">
            <h3 className="text-xs font-bold uppercase tracking-wider text-stone-400 mb-3 flex items-center gap-1.5">
              <Lock size={12} /> Infrastructure (restart / reindex required)
            </h3>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
              {Object.entries(cfgReadonly).map(([k, v]) => (
                <div key={k} className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">{k.replace(/_/g, " ")}</div>
                  <div className="text-sm font-mono text-stone-700 truncate">
                    {typeof v === "boolean" ? (v ? "true" : "false") : String(v)}
                  </div>
                </div>
              ))}
            </div>
          </div>
          </>)}
        </div>
      </div>
      )}

      {/* Parallelism — every concurrency knob in ONE place.
          These were previously scattered across three sections (retrievers under "Latency",
          ingestion under "Ingestion", LLM calls under "Extraction"), which made it impossible to
          reason about total load: they all contend for the same Postgres pool and the same model
          server. Tuning throughput is one decision, so it gets one screen. */}
      {tab === "parallelism" && (
      <div className="bg-white rounded-xl border border-stone-200/80 overflow-hidden">
        <div className="px-6 py-4 border-b border-stone-100">
          <h2 className="font-semibold text-stone-900 flex items-center gap-2">
            <Network size={16} /> Parallelism
          </h2>
          <p className="text-sm text-stone-500 mt-1">
            How much work runs at once, per stage. Every value applies to the running service
            immediately — no restart. Raise these until the model server or Postgres saturates;
            past that point they add queueing, not throughput.
          </p>
        </div>

        <div className="p-6 space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-5">
            {cfgSchema.filter((s: any) => s.group === "Parallelism").map((s: any) => (
              <div key={s.key} className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-stone-800">{s.label}</div>
                  <div className="text-xs text-stone-400 mt-0.5">{s.help}</div>
                  <div className="text-[10px] font-mono text-stone-300 mt-1">
                    {s.key} · {s.min}–{s.max}
                  </div>
                </div>
                <input
                  type="number"
                  min={s.min}
                  max={s.max}
                  value={cfgValues[s.key] ?? ""}
                  onChange={(e) =>
                    setCfgValues({ ...cfgValues, [s.key]: parseInt(e.target.value || "1", 10) })
                  }
                  className="shrink-0 w-24 border border-stone-200 rounded-md px-3 py-1.5 text-sm text-right font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500/20"
                />
              </div>
            ))}
          </div>

          {/* EFFECTIVE in-flight work. The knobs above are PER JOB, so what actually reaches
              Postgres and the model server is pool x knob — a detail that is easy to get wrong by a
              factor of 8 when the two numbers live on different screens. Computed here so the
              product is visible rather than inferred. */}
          {(() => {
            const pool = (k: string, d: number) => Number((cfgReadonly as any)[k] ?? d);
            const extractPool = pool("extraction_worker_pool", 4);
            const embedPool = pool("embedding_worker_pool", 4);
            const ingestPool = pool("ingest_worker_pool", 4);
            const llm = Number(cfgValues.llm_concurrency ?? 0);
            const emb = Number(cfgValues.embed_concurrency ?? 0);
            const dbPool = pool("db_pool_size", 10) + pool("db_max_overflow", 20);
            const modelLoad = extractPool * llm + embedPool * emb;
            const dbLoad = extractPool + embedPool + ingestPool;
            const rows = [
              { what: "Extraction LLM calls", calc: `${extractPool} workers x ${llm} per doc`, n: extractPool * llm },
              { what: "Embedding calls", calc: `${embedPool} workers x ${emb} per batch`, n: embedPool * emb },
              { what: "Ingestion jobs", calc: `${ingestPool} workers`, n: ingestPool },
            ];
            return (
              <div className="pt-4 border-t border-stone-100">
                <h3 className="text-xs font-bold uppercase tracking-wider text-stone-400 mb-2 flex items-center gap-1.5">
                  <Calculator size={12} /> Effective max in-flight
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                  {rows.map((r) => (
                    <div key={r.what} className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2">
                      <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">{r.what}</div>
                      <div className="text-lg font-mono text-stone-800 leading-tight">{r.n}</div>
                      <div className="text-[11px] text-stone-400">{r.calc}</div>
                    </div>
                  ))}
                </div>
                <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
                  <div className="text-xs text-stone-500 bg-stone-50 border border-stone-100 rounded-lg px-3 py-2">
                    <span className="font-semibold text-stone-700">Model server:</span> up to{" "}
                    <span className="font-mono">{modelLoad}</span> concurrent requests. Raise until
                    its throughput stops improving — past that you are only adding queueing.
                  </div>
                  <div className={`text-xs rounded-lg px-3 py-2 border ${dbLoad > dbPool ? "text-amber-700 bg-amber-50 border-amber-200" : "text-stone-500 bg-stone-50 border-stone-100"}`}>
                    <span className="font-semibold">Database:</span> ~
                    <span className="font-mono">{dbLoad}</span> connections held (one per worker
                    process) against a pool of <span className="font-mono">{dbPool}</span>.
                    {dbLoad > dbPool && " Raise DB_POOL_SIZE / DB_MAX_OVERFLOW."}
                  </div>
                </div>
              </div>
            );
          })()}

          {/* Worker pool sizes are env-level: a Celery pool is fixed at process start, so changing
              it needs a container restart. Showing them here — clearly marked — keeps the whole
              concurrency picture on one screen instead of hiding half of it in a compose file. */}
          <div className="pt-4 border-t border-stone-100">
            <h3 className="text-xs font-bold uppercase tracking-wider text-stone-400 mb-2 flex items-center gap-1.5">
              <Lock size={12} /> Worker pools (set in .env, restart required)
            </h3>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              {[
                { env: "CELERY_WORKER_CONCURRENCY", queue: "graphrag", what: "Ingestion — parse, chunk, write nodes" },
                { env: "EXTRACTION_WORKER_CONCURRENCY", queue: "extraction", what: "Per-chunk LLM extraction" },
                { env: "EMBEDDING_WORKER_CONCURRENCY", queue: "embedding", what: "Vector generation" },
              ].map((w) => (
                <div key={w.env} className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">{w.queue} queue</div>
                  <div className="text-xs font-mono text-stone-700 truncate">{w.env}</div>
                  <div className="text-[11px] text-stone-400 mt-1">{w.what}</div>
                </div>
              ))}
            </div>
          </div>

          {cfgMsg && (
            <div className="text-sm font-medium text-stone-600 bg-stone-50 border border-stone-200 rounded-lg px-3 py-2">{cfgMsg}</div>
          )}

          <div className="flex justify-end">
            <button
              onClick={handleSaveConfig}
              disabled={cfgSaving}
              className="inline-flex items-center gap-2 bg-stone-900 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-stone-800 disabled:opacity-50"
            >
              <Save size={14} /> {cfgSaving ? "Saving…" : "Save parallelism"}
            </button>
          </div>
        </div>
      </div>
      )}

      {/* Chunking — how documents are cut for search and extraction. Its own tab because the
          values interact (a chunk must fit what the embedding model reads; overlap is a share of
          the chunk) and none of them belongs among the retrieval knobs on General. */}
      {tab === "chunking" && (
      <div className="bg-white dark:bg-stone-900 rounded-xl border border-stone-200/80 dark:border-stone-800 overflow-hidden">
        <div className="px-6 py-4 border-b border-stone-100 dark:border-stone-800">
          <h2 className="font-semibold text-stone-900 dark:text-stone-100 flex items-center gap-2">
            <Scissors size={16} /> Chunking
          </h2>
          <p className="text-sm text-stone-500 dark:text-stone-400 mt-1">
            A document is read into elements by its layout (headings, paragraphs, lists, tables, code),
            and each element is searchable on its own. These settings cut what is too long. They
            apply to files read after you save; a file already in keeps its chunks until it is read
            again.
          </p>
        </div>
        <div className="p-6 space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-5">
            {cfgSchema.filter((s: any) => s.group === "Chunking").map((s: any) => (
              <div key={s.key} className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <label htmlFor={`cfg-${s.key}`} className="text-sm font-medium text-stone-800 dark:text-stone-200">{s.label}</label>
                  <div className="text-xs text-stone-500 dark:text-stone-400 mt-0.5">{s.help}</div>
                  <div className="text-[10px] font-mono text-stone-400 mt-1">
                    {s.key}{s.kind === "select" || s.kind === "bool" ? "" : ` · ${s.min}–${s.max}`}
                  </div>
                </div>
                {s.kind === "bool" ? (
                  <input
                    id={`cfg-${s.key}`}
                    type="checkbox"
                    checked={!!cfgValues[s.key]}
                    onChange={(e) => setCfgValues({ ...cfgValues, [s.key]: e.target.checked })}
                    className="shrink-0 mt-1 h-4 w-4 accent-emerald-600"
                  />
                ) : s.kind === "select" ? (
                  <select
                    id={`cfg-${s.key}`}
                    value={cfgValues[s.key] ?? ""}
                    onChange={(e) => setCfgValues({ ...cfgValues, [s.key]: e.target.value })}
                    className="shrink-0 w-28 border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-950 rounded-md px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20"
                  >
                    {(s.options || []).map((o: string) => <option key={o} value={o}>{o}</option>)}
                  </select>
                ) : (
                <input
                  id={`cfg-${s.key}`}
                  type="number"
                  min={s.min}
                  max={s.max}
                  value={cfgValues[s.key] ?? ""}
                  onChange={(e) =>
                    setCfgValues({ ...cfgValues, [s.key]: parseInt(e.target.value || "0", 10) })
                  }
                  className="shrink-0 w-28 border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-950 rounded-md px-3 py-1.5 text-sm text-right font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500/20"
                />
                )}
              </div>
            ))}
          </div>
          {Number(cfgValues.chunk_overlap_chars ?? 0) * 2 > Number(cfgValues.chunk_window_chars ?? 2000) && (
            <div className="text-xs rounded-lg px-3 py-2 border text-amber-700 bg-amber-50 border-amber-200">
              Overlap is more than half the chunk size; it is capped at half when files are read.
            </div>
          )}
          {cfgMsg && (
            <div className="text-sm font-medium text-stone-600 bg-stone-50 border border-stone-200 rounded-lg px-3 py-2">{cfgMsg}</div>
          )}
          <div className="flex justify-end">
            <button
              onClick={handleSaveConfig}
              disabled={cfgSaving}
              className="inline-flex items-center gap-2 bg-stone-900 text-white px-4 py-2 rounded-lg text-sm font-medium hover:bg-stone-800 disabled:opacity-50"
            >
              <Save size={14} /> {cfgSaving ? "Saving…" : "Save chunking"}
            </button>
          </div>
        </div>
      </div>
      )}

      {/* Capabilities — the platform tree as pluggable modules (schema-driven) */}
      {tab === "capabilities" && (() => {
        const setField = (name: string, key: string, value: any) =>
          setCapVals({ ...capVals, [name]: { ...(capVals[name] || {}), [key]: value } });
        const editable = caps.filter((c) => (c.schema || []).length > 0);
        const adapters = caps.filter((c) => (c.schema || []).length === 0);

        const renderField = (c: any, f: any) => {
          const v = capVals[c.name]?.[f.key];
          if (f.kind === "bool") {
            return (
              <button onClick={() => setField(c.name, f.key, !v)}
                className={`shrink-0 w-11 h-6 rounded-full transition-colors relative ${v ? "bg-emerald-500" : "bg-stone-300"}`}>
                <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform ${v ? "translate-x-5" : ""}`} />
              </button>
            );
          }
          if (f.kind === "select") {
            return (
              <select value={v ?? ""} onChange={(e) => setField(c.name, f.key, e.target.value)}
                className="shrink-0 border border-stone-200 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20">
                {(f.options || []).map((o: string) => <option key={o} value={o}>{o}</option>)}
              </select>
            );
          }
          if (f.kind === "text") {
            return (
              <input type="text" value={v ?? ""} onChange={(e) => setField(c.name, f.key, e.target.value)}
                className="shrink-0 w-64 border border-stone-200 rounded-md px-3 py-1.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500/20" />
            );
          }
          if (f.kind === "json") {
            return (
              <textarea value={typeof v === "string" ? v : JSON.stringify(v ?? null, null, 2)}
                onChange={(e) => setField(c.name, f.key, e.target.value)} rows={6}
                className="w-full mt-1 border border-stone-200 rounded-md px-3 py-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500/20" />
            );
          }
          return (
            <input type="number" step={f.kind === "float" ? "0.05" : "1"} min={f.min} max={f.max}
              value={v ?? ""}
              onChange={(e) => setField(c.name, f.key, f.kind === "float" ? parseFloat(e.target.value) : parseInt(e.target.value || "0", 10))}
              className="shrink-0 w-24 border border-stone-200 rounded-md px-3 py-1.5 text-sm text-right font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500/20" />
          );
        };

        return (
          <div className="space-y-6">
            <div className="bg-white rounded-xl shadow-sm border border-stone-200 p-6">
              <div className="flex items-center gap-2 mb-1">
                <Boxes size={18} className="text-emerald-500" />
                <h2 className="font-semibold text-stone-800">Capabilities</h2>
              </div>
              <p className="text-xs text-stone-500">The knowledge platform as pluggable capability modules — each owns its config and applies across every datasource (documents, tables, JSON). Toggle and tune the knowledge-quality capabilities here; the rest of the tree links to where it is configured. New SDK capabilities appear here automatically.</p>
            </div>

            {/* the configurable knowledge-quality capabilities (Versioning, Contradiction, Trust, Abstain).
                Rendered from each capability's own config_schema(), so a module's fields are declared in
                ONE place and this list never needs editing when they change. */}
            {editable.map((c) => {
              const enabledNow = c.enable_key ? !!capVals[c.name]?.[c.enable_key] : true;
              return (
                <div key={c.name} className="bg-white rounded-xl shadow-sm border border-stone-200 overflow-hidden">
                  <div className="px-6 py-4 border-b border-stone-100 bg-stone-50 flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <h3 className="font-semibold text-stone-800">{c.title}</h3>
                        <span className="text-[10px] font-bold uppercase tracking-wider text-stone-400">{c.group}</span>
                        {c.hooks?.map((h: string) => (
                          <span key={h} className="text-[10px] px-1.5 py-0.5 rounded bg-stone-100 border border-stone-200 text-stone-500 font-mono">{h}</span>
                        ))}
                        {c.enable_key && (
                          <span className={`text-[10px] px-1.5 py-0.5 rounded font-semibold ${enabledNow ? "bg-emerald-50 text-emerald-600 border border-emerald-200" : "bg-stone-100 text-stone-400 border border-stone-200"}`}>{enabledNow ? "On" : "Off"}</span>
                        )}
                      </div>
                      <p className="text-xs text-stone-400 mt-0.5 max-w-3xl">{c.description}</p>
                    </div>
                    <Button variant="primary" icon={Save} busy={capSaving === c.name} onClick={() => handleSaveCapability(c.name)}>
                      {capSaving === c.name ? "Saving…" : "Save"}
                    </Button>
                  </div>
                  {/* Fields keep their DECLARED order and a new heading starts wherever `section`
                      changes, so a capability lays out its own card by ordering its schema — this
                      component needs no knowledge of any particular capability. A schema with no
                      `section` renders exactly as it always did: one flat list, no headings. */}
                  <div className="p-6 space-y-4">
                    {(c.schema || []).map((f: any, i: number) => {
                      const prev = i > 0 ? (c.schema[i - 1] || {}).section : undefined;
                      const heading = f.section && f.section !== prev ? f.section : null;
                      return (
                        <div key={f.key}>
                          {heading && (
                            <div className={i === 0 ? "mb-3" : "mt-7 mb-3 pt-5 border-t border-stone-100"}>
                              <div className="text-[11px] font-bold uppercase tracking-wider text-stone-500">{heading}</div>
                              {f.section_help && (
                                <p className="text-xs text-stone-400 mt-1 max-w-3xl">{f.section_help}</p>
                              )}
                            </div>
                          )}
                          {f.kind === "json" ? (
                            <div>
                              <div className="text-sm font-medium text-stone-800">{f.label}</div>
                              <div className="text-xs text-stone-400">{f.help}</div>
                              {renderField(c, f)}
                            </div>
                          ) : (
                            <div className="flex items-center justify-between gap-4">
                              <div className="min-w-0">
                                <div className="text-sm font-medium text-stone-800">{f.label}</div>
                                <div className="text-xs text-stone-400">{f.help}</div>
                              </div>
                              {renderField(c, f)}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}

            {/* the rest of the platform tree — represented nodes linking to where they are configured */}
            <div className="bg-white rounded-xl shadow-sm border border-stone-200 overflow-hidden">
              <div className="px-6 py-4 border-b border-stone-100 bg-stone-50">
                <h3 className="font-semibold text-stone-800">Platform tree</h3>
                <p className="text-xs text-stone-500">The rest of the capability tree — mature subsystems, each configured on its own page.</p>
              </div>
              <div className="p-6 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                {adapters.map((c) => (
                  <div key={c.name} className="border border-stone-100 rounded-lg p-3 bg-stone-50/40">
                    <div className="text-sm font-semibold text-stone-700">{c.title}</div>
                    <p className="text-[11px] text-stone-400 mt-0.5 mb-2">{c.description}</p>
                    {c.ui_route ? (
                      <Link href={c.ui_route} className="inline-flex items-center gap-1 text-[11px] font-semibold text-emerald-600 hover:text-emerald-700">
                        Configure <ExternalLink size={11} />
                      </Link>
                    ) : (
                      <span className="text-[11px] text-stone-300">—</span>
                    )}
                  </div>
                ))}
              </div>
            </div>

            {capMsg && <div className="text-sm font-medium text-stone-600 bg-stone-50 border border-stone-200 rounded-lg px-3 py-2">{capMsg}</div>}
          </div>
        );
      })()}

      {/* Extraction */}
      {tab === "extraction" && (() => {
        // Answer and memory prompts (`deterministic` — run by their own engines when a question is
        // answered or a session saved) are edited in the Studio; they have no on/off here because
        // nothing would honour it.
        const switchable = extractors.filter((e: any) => !e.deterministic);
        const selected = switchable.filter((e: any) => selExt[e.id]);
        // bound to text → runs once per chunk; bound to tables or whole assets → once per node
        const perChunk = (e: any) => e.source?.bound === "text";
        const refOf = (e: any) => String(e.mechanism?.ref || "");
        const promptLen = (e: any) => (e.mechanism?.prompt || "").length;
        const setAll = (v: boolean) => setSelExt({ ...selExt, ...Object.fromEntries(switchable.map((e: any) => [e.id, v])) });
        const buckets: { key: string; label: string; icon: any }[] = [
          { key: "builtin", label: "Built-in extractors", icon: Settings2 },
          { key: "custom", label: "Custom extractors", icon: Workflow },
          { key: "relationship", label: "Relationships", icon: Network },
        ];
        // only numeric/select knobs render generically; the call-groups (kind json) get their own builder
        const extItems = cfgSchema.filter((s: any) => s.group === "Extraction" && s.kind !== "json");
        // per-chunk LLM extractors — the only kind that can be folded into a shared call
        const groupable = extractors.filter((e: any) => e.executable && e.mechanism?.prompt && !refOf(e).startsWith("relate.") && perChunk(e));
        const byName: Record<string, any> = Object.fromEntries(extractors.map((e: any) => [e.name, e]));
        const groups: { name?: string; extractors: string[] }[] =
          Array.isArray(cfgValues.extraction_call_groups) ? cfgValues.extraction_call_groups : [];
        const setGroups = (g: any[]) => setCfgValues({ ...cfgValues, extraction_call_groups: g });
        const inGroup = new Set(groups.flatMap((g) => g.extractors || []));
        const ungrouped = groupable.filter((e: any) => !inGroup.has(e.name));
        const shortName = (n: string) => String(n).replace("builtin:", "");
        // extractors that CANNOT be folded into a shared LLM call, with the reason why —
        // shown so the full inventory is visible (nothing is "missing", it just can't fold)
        const groupableIds = new Set(groupable.map((e: any) => e.id));
        const foldReason = (e: any): string =>
          refOf(e).startsWith("relate.") ? "relationship — runs after all groups, over the entities they create"
            : e.deterministic ? "answer / memory prompt — runs when a question is answered or a session saved; prompt/model editable in the Studio"
              : !e.mechanism?.prompt ? "no prompt — not an LLM call (regex / dictionary / keyword / NER)"
                : !perChunk(e) ? "runs once per table or asset it is bound to, not per chunk"
                  : "view-only";
        const notFoldable = extractors.filter((e: any) => !groupableIds.has(e.id));
        const foldBuckets: [string, any[]][] = [
          ["Relationships — run after all groups", notFoldable.filter((e: any) => refOf(e).startsWith("relate."))],
          ["Answer & memory prompts — run when a question is answered or a session is saved", notFoldable.filter((e: any) => !refOf(e).startsWith("relate.") && e.deterministic)],
          ["No LLM call (regex / dictionary / keyword / NER)", notFoldable.filter((e: any) => !refOf(e).startsWith("relate.") && !e.deterministic && !e.mechanism?.prompt)],
          ["Per table or asset (not per chunk)", notFoldable.filter((e: any) => !refOf(e).startsWith("relate.") && !e.deterministic && e.mechanism?.prompt && !perChunk(e))],
        ];

        // ── live call budget ──
        const isSel = (n: string) => { const e = byName[n]; return e ? !!selExt[e.id] : false; };
        const activeGroups = groups
          .map((g) => (g.extractors || []).filter((n) => byName[n] && isSel(n)))
          .filter((m) => m.length > 0);
        const groupedActive = new Set(activeGroups.flat());
        const singlesActive = groupable.filter((e: any) => selExt[e.id] && !groupedActive.has(e.name));
        const relateActive = selected.filter((e: any) => refOf(e).startsWith("relate.") && e.executable && perChunk(e));
        const callsPerChunk = activeGroups.length + singlesActive.length + relateActive.length;
        const chunksPerDoc = Math.max(1, Math.ceil(Math.max(calcDocChars, 1) / Math.max(calcChunkChars, 1)));
        const callsPerDoc = callsPerChunk * chunksPerDoc;
        const tokOf = (chars: number) => Math.ceil(chars / 4);
        const callTokens = [
          ...activeGroups.map((m) => tokOf(m.reduce((a, n) => a + promptLen(byName[n]), 0) + calcChunkChars) + 400 * m.length),
          ...singlesActive.map((e: any) => tokOf(promptLen(e) + calcChunkChars) + 800),
        ];
        const maxCallTokens = callTokens.length ? Math.max(...callTokens) : 0;
        const fits = maxCallTokens <= calcCtxTokens;
        return (
        <div className="space-y-6">
          {/* call groups + concurrency */}
          <div className="bg-white rounded-xl shadow-sm border border-stone-200 overflow-hidden">
            <div className="px-6 py-5 border-b border-stone-100 bg-stone-50 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Layers size={18} className="text-emerald-500" />
                <div>
                  <h2 className="font-semibold text-stone-800">Extraction</h2>
                  <p className="text-xs text-stone-500">Fold extractors into shared LLM calls, order them by dependency, and set parallelism.</p>
                </div>
              </div>
              <Button variant="primary" icon={Save} busy={extSaving} onClick={handleSaveExtraction}>{extSaving ? "Saving…" : "Save & Apply"}</Button>
            </div>
            <div className="p-6 space-y-6">
              {extItems.length > 0 && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-4">
                  {extItems.map((s: any) => (
                    <div key={s.key} className="flex items-center justify-between gap-4">
                      <div className="min-w-0">
                        <div className="text-sm font-medium text-stone-800">{s.label}</div>
                        <div className="text-xs text-stone-400">{s.help}</div>
                      </div>
                      {s.kind === "select" ? (
                        <select value={cfgValues[s.key] ?? ""} onChange={(e) => setCfgValues({ ...cfgValues, [s.key]: e.target.value })} className="shrink-0 border border-stone-200 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20">
                          {(s.options || []).map((o: string) => <option key={o} value={o}>{o}</option>)}
                        </select>
                      ) : (
                        <input type="number" step="1" min={s.min} max={s.max} value={cfgValues[s.key] ?? ""} onChange={(e) => setCfgValues({ ...cfgValues, [s.key]: parseInt(e.target.value || "0", 10) })} className="shrink-0 w-24 border border-stone-200 rounded-md px-3 py-1.5 text-sm text-right font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500/20" />
                      )}
                    </div>
                  ))}
                </div>
              )}

              {/* call groups builder */}
              <div>
                <h3 className="text-xs font-bold uppercase tracking-wider text-stone-400 mb-1">Call groups</h3>
                <p className="text-xs text-stone-500 mb-3">
                  Each group is <b>one LLM call per chunk</b> that runs all its extractors together (same prompts, one combined response).
                  Groups run <b>in order</b> — put extractors whose output others depend on in an earlier group.
                  Extractors not in any group run as their own call. If a group response misses an extractor&apos;s section, that extractor automatically falls back to its own call — folding saves calls, never extractions.
                </p>
                <div className="space-y-2">
                  {groups.map((g, gi) => (
                    <div key={gi} className="border border-stone-200 rounded-lg p-3 bg-stone-50/50">
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-sm font-semibold text-stone-700">Call {gi + 1}<span className="ml-2 text-[10px] font-normal text-stone-400">runs {gi === 0 ? "first" : `after Call ${gi}`}</span></span>
                        <div className="flex items-center gap-1 text-xs">
                          <button title="Run earlier" disabled={gi === 0} onClick={() => { const c = [...groups]; [c[gi - 1], c[gi]] = [c[gi], c[gi - 1]]; setGroups(c); }} className="px-2 py-1 rounded border border-stone-200 text-stone-500 hover:bg-white disabled:opacity-30">↑</button>
                          <button title="Run later" disabled={gi === groups.length - 1} onClick={() => { const c = [...groups]; [c[gi + 1], c[gi]] = [c[gi], c[gi + 1]]; setGroups(c); }} className="px-2 py-1 rounded border border-stone-200 text-stone-500 hover:bg-white disabled:opacity-30">↓</button>
                          <button onClick={() => setGroups(groups.filter((_, i) => i !== gi))} className="px-2 py-1 rounded border border-red-200 text-red-500 hover:bg-red-50">Remove</button>
                        </div>
                      </div>
                      <div className="flex flex-wrap items-center gap-1.5">
                        {(g.extractors || []).map((n) => (
                          <span key={n} className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-[11px] font-medium border ${byName[n] ? "bg-emerald-50 border-emerald-200 text-emerald-700" : "bg-stone-100 border-stone-200 text-stone-400 line-through"}`}>
                            {shortName(n)}
                            <button onClick={() => setGroups(groups.map((gg, i) => i === gi ? { ...gg, extractors: (gg.extractors || []).filter((x) => x !== n) } : gg))} className="text-current opacity-60 hover:opacity-100">×</button>
                          </span>
                        ))}
                        <select value="" onChange={(e) => { const n = e.target.value; if (n) setGroups(groups.map((gg, i) => i === gi ? { ...gg, extractors: [...(gg.extractors || []), n] } : gg)); }}
                          className="text-[11px] border border-dashed border-stone-300 rounded-full px-2 py-1 text-stone-500 bg-white focus:outline-none">
                          <option value="">+ add extractor…</option>
                          {ungrouped.map((e: any) => <option key={e.name} value={e.name}>{shortName(e.name)}</option>)}
                        </select>
                      </div>
                    </div>
                  ))}
                  <button onClick={() => setGroups([...groups, { name: `Call ${groups.length + 1}`, extractors: [] }])}
                    className="w-full border border-dashed border-stone-300 rounded-lg py-2 text-xs font-semibold text-stone-500 hover:bg-stone-50">+ Add call group</button>
                </div>
                {ungrouped.length > 0 && (
                  <div className="mt-3">
                    <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400 mb-1.5">Ungrouped LLM extractors — each runs its own call ({ungrouped.length})</div>
                    <div className="flex flex-wrap gap-1.5">
                      {ungrouped.map((e: any) => (
                        <span key={e.name} className="px-2 py-1 rounded-full text-[11px] font-medium bg-stone-100 border border-stone-200 text-stone-600">{shortName(e.name)}</span>
                      ))}
                    </div>
                  </div>
                )}
                {/* the rest of the inventory — visible so nothing looks missing, with WHY it can't fold */}
                {notFoldable.length > 0 && (
                  <div className="mt-4 border-t border-stone-100 pt-3">
                    <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400 mb-1">
                      Not foldable ({notFoldable.length}) — of {extractors.length} extractors total, only prompt-based per-chunk LLM extractors ({groupable.length}) can share a call
                    </div>
                    <p className="text-[11px] text-stone-400 mb-2">These still run — they just don&apos;t share a per-chunk call: some never call the LLM, relationships must run after the groups, table- and asset-bound ones run once per node rather than per chunk, and answer / memory prompts run outside ingestion.</p>
                    {foldBuckets.map(([label, items]) => items.length > 0 && (
                      <div key={label} className="mb-2">
                        <div className="text-[10px] font-semibold text-stone-400 mb-1">{label} ({items.length})</div>
                        <div className="flex flex-wrap gap-1.5">
                          {items.map((e: any) => (
                            <span key={e.id} title={foldReason(e)} className="px-2 py-1 rounded-full text-[11px] bg-white border border-dashed border-stone-200 text-stone-400">{shortName(e.name)}</span>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
              {extMsg && <div className="text-sm font-medium text-stone-600 bg-stone-50 border border-stone-200 rounded-lg px-3 py-2">{extMsg}</div>}
            </div>
          </div>

          {/* extractor selection — built-in / custom / relationships */}
          <div className="bg-white rounded-xl shadow-sm border border-stone-200 overflow-hidden">
            <div className="px-6 py-5 border-b border-stone-100 bg-stone-50 flex items-center justify-between">
              <div>
                <h2 className="font-semibold text-stone-800">Active extractors</h2>
                <p className="text-xs text-stone-500">Pick which extractors run — built-in, custom, and relationships. <b>{selected.length}</b>/{switchable.length} selected.</p>
              </div>
              <div className="flex gap-2 text-xs">
                <button onClick={() => setAll(true)} className="px-3 py-1.5 rounded-md border border-stone-200 font-semibold text-stone-600 hover:bg-stone-50">Select all</button>
                <button onClick={() => setAll(false)} className="px-3 py-1.5 rounded-md border border-stone-200 font-semibold text-stone-600 hover:bg-stone-50">None</button>
              </div>
            </div>
            <div className="p-6 space-y-5">
              {buckets.map((b) => {
                const items = switchable.filter((e: any) => extBucket(e) === b.key);
                if (!items.length) return null;
                return (
                  <div key={b.key}>
                    <h3 className="text-xs font-bold uppercase tracking-wider text-stone-400 mb-2 flex items-center gap-1.5"><b.icon size={13} /> {b.label} <span className="text-stone-300">({items.length})</span></h3>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                      {items.map((e: any) => (
                        <label key={e.id} className="flex items-center gap-2.5 bg-stone-50 border border-stone-100 rounded-lg px-3 py-2 cursor-pointer hover:bg-stone-100/60">
                          <input type="checkbox" checked={!!selExt[e.id]} onChange={() => setSelExt({ ...selExt, [e.id]: !selExt[e.id] })} className="text-emerald-600 focus:ring-emerald-500" />
                          <span className="min-w-0 flex-1">
                            <span className="block text-sm font-medium text-stone-800 truncate">{String(e.name).replace("builtin:", "")}</span>
                            <span className="block text-[10px] text-stone-400 font-mono truncate">{e.mechanism?.ref || "—"} · prompt {promptLen(e)} chars</span>
                          </span>
                        </label>
                      ))}
                    </div>
                  </div>
                );
              })}
              {extractors.length === 0 && <p className="text-sm text-stone-400">No extractors loaded.</p>}
              <p className="text-[11px] text-stone-400">Unchecked extractors are saved as <b>disabled</b> — the engine skips them everywhere (ingestion, pipelines, manual runs) until re-enabled. Save with the button above.</p>
            </div>
          </div>

          {/* call budget — live math from the groups + selection above */}
          <div className="bg-white rounded-xl shadow-sm border border-stone-200 overflow-hidden">
            <div className="px-6 py-5 border-b border-stone-100 bg-stone-50 flex items-center gap-2">
              <Calculator size={18} className="text-emerald-500" />
              <div>
                <h2 className="font-semibold text-stone-800">Call budget</h2>
                <p className="text-xs text-stone-500">Live estimate from your call groups + active extractors (≈4 chars/token).</p>
              </div>
            </div>
            <div className="p-6 space-y-5">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <div className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">Chunk size (chars)</div>
                  <input type="number" value={calcChunkChars} onChange={(e) => setCalcChunkChars(parseInt(e.target.value || "0", 10))} className="w-full bg-transparent text-sm font-mono text-stone-700 focus:outline-none" />
                </div>
                <div className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">Document size (chars)</div>
                  <input type="number" value={calcDocChars} onChange={(e) => setCalcDocChars(parseInt(e.target.value || "0", 10))} className="w-full bg-transparent text-sm font-mono text-stone-700 focus:outline-none" />
                </div>
                <div className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">Model context (tokens)</div>
                  <input type="number" value={calcCtxTokens} onChange={(e) => setCalcCtxTokens(parseInt(e.target.value || "0", 10))} className="w-full bg-transparent text-sm font-mono text-stone-700 focus:outline-none" />
                </div>
                <div className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">Chunks / document</div>
                  <div className="text-sm font-mono text-stone-700">{chunksPerDoc.toLocaleString()}</div>
                </div>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <div className="border border-stone-200 rounded-lg px-3 py-2">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">Calls / chunk</div>
                  <div className="text-sm font-mono text-stone-700">{callsPerChunk}</div>
                  <div className="text-[10px] text-stone-400">{activeGroups.length} group{activeGroups.length === 1 ? "" : "s"} + {singlesActive.length} single + {relateActive.length} relate</div>
                </div>
                <div className="border border-stone-200 rounded-lg px-3 py-2">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">Calls / document</div>
                  <div className="text-sm font-mono text-stone-700">{callsPerDoc.toLocaleString()}</div>
                </div>
                <div className="border border-stone-200 rounded-lg px-3 py-2">
                  <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">Largest call (tokens)</div>
                  <div className="text-sm font-mono text-stone-700">{maxCallTokens.toLocaleString()}</div>
                  <div className="text-[10px] text-stone-400">prompts + chunk + output allowance</div>
                </div>
                <div className={`border rounded-lg px-3 py-2 ${fits ? "border-emerald-200 bg-emerald-50/50" : "border-red-200 bg-red-50/50"}`}>
                  <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">Fits context?</div>
                  <div className={`text-sm font-semibold ${fits ? "text-emerald-700" : "text-red-600"}`}>{fits ? "Yes" : "No — split a group or raise context"}</div>
                </div>
              </div>
              <p className="text-[11px] text-stone-400">Grouping extractors cuts calls/document; the trade-off is a bigger combined prompt per call — keep the largest call inside the model&apos;s context window.</p>
            </div>
          </div>
        </div>
        );
      })()}

      {/* Agents */}
      {tab === "agents" && (
      <div className="bg-white rounded-xl shadow-sm border border-stone-200 overflow-hidden">
        <div className="px-6 py-5 border-b border-stone-100 bg-stone-50 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Shield size={18} className="text-emerald-500" />
            <div>
              <h2 className="font-semibold text-stone-800">Agents</h2>
              <p className="text-xs text-stone-500">Connected AI agents (MCP) and what they may capture.</p>
            </div>
          </div>
          <Button variant="primary" icon={Save} busy={cfgSaving} onClick={handleSaveConfig}>{cfgSaving ? "Saving…" : "Save & Apply"}</Button>
        </div>
        <div className="p-6 space-y-4">
          {cfgSchema.filter((s: any) => s.group === "Agents").map((s: any) => (
            <div key={s.key} className="flex items-center justify-between gap-4">
              <div className="min-w-0">
                <div className="text-sm font-medium text-stone-800">{s.label}</div>
                <div className="text-xs text-stone-400">{s.help}</div>
              </div>
              {s.kind === "bool" ? (
                <button onClick={() => setCfgValues({ ...cfgValues, [s.key]: !cfgValues[s.key] })}
                  className={`shrink-0 w-11 h-6 rounded-full transition-colors relative ${cfgValues[s.key] ? "bg-emerald-500" : "bg-stone-300"}`}>
                  <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform ${cfgValues[s.key] ? "translate-x-5" : ""}`} />
                </button>
              ) : (
                <input type="number" step={s.kind === "float" ? "0.05" : "1"} min={s.min} max={s.max}
                  value={cfgValues[s.key] ?? ""}
                  onChange={(e) => setCfgValues({ ...cfgValues, [s.key]: s.kind === "float" ? parseFloat(e.target.value) : parseInt(e.target.value || "0", 10) })}
                  className="shrink-0 w-24 border border-stone-200 rounded-md px-3 py-1.5 text-sm text-right font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500/20" />
              )}
            </div>
          ))}
          {cfgMsg && <div className="text-sm font-medium text-stone-600 bg-stone-50 border border-stone-200 rounded-lg px-3 py-2">{cfgMsg}</div>}
          <p className="text-xs text-stone-400">Captured activity appears on the <b>Conversations</b> page. Connect agents in <b>Connections → Connect</b>.</p>
        </div>
      </div>
      )}

      {/* Backup & Export (no lock-in) */}
      {tab === "backup" && (
      <div className="bg-white rounded-xl shadow-sm border border-stone-200 overflow-hidden">
        <div className="px-6 py-5 border-b border-stone-100 bg-stone-50 flex items-center gap-2">
          <HardDriveDownload size={18} className="text-emerald-500" />
          <div>
            <h2 className="font-semibold text-stone-800">Backup & Export</h2>
            <p className="text-xs text-stone-500">Your whole knowledge graph in one portable JSON — no lock-in. Re-import into any OpenCortyx.</p>
          </div>
        </div>
        <div className="p-6 space-y-5">
          <div className="flex flex-wrap items-center gap-3">
            <button onClick={handleExport} disabled={backupBusy}
              className="flex items-center gap-2 px-4 py-2 bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-semibold rounded-lg transition-colors disabled:opacity-50">
              <Download size={15} /> {backupBusy ? "Working…" : "Export backup (.json)"}
            </button>
            <div className="h-6 w-px bg-stone-200" />
            <select value={importMode} onChange={(e) => setImportMode(e.target.value as "merge" | "replace")}
              className="border border-stone-200 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20">
              <option value="merge">Merge (keep existing)</option>
              <option value="replace">Replace (wipe first)</option>
            </select>
            <button onClick={() => fileRef.current?.click()} disabled={backupBusy}
              className="flex items-center gap-2 px-4 py-2 bg-white border border-stone-300 hover:bg-stone-50 text-stone-700 text-sm font-semibold rounded-lg transition-colors disabled:opacity-50">
              <Upload size={15} /> Restore / Import
            </button>
            <input ref={fileRef} type="file" accept="application/json,.json" onChange={handleImportFile} className="hidden" />
          </div>
          <p className="text-xs text-stone-400">
            Includes nodes, edges, ontology, extraction templates & pipelines (embeddings included for an offline restore).
            Secrets (connector passwords, model API keys) are never exported.
          </p>
          {backupMsg && <div className="text-sm font-medium text-stone-600 bg-stone-50 border border-stone-200 rounded-lg px-3 py-2">{backupMsg}</div>}
        </div>
      </div>
      )}

      {tab === "audit" && (
      <div className="bg-white rounded-xl shadow-sm border border-stone-200 overflow-hidden flex flex-col">
        <div className="px-6 py-5 border-b border-stone-100 bg-stone-50 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Server size={18} className="text-stone-400" />
            <h2 className="font-semibold text-stone-800">System Audit Logs</h2>
          </div>
          <button onClick={loadLogs} className="text-xs font-semibold text-emerald-600 hover:text-emerald-700">
            Refresh Logs
          </button>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left">
            <thead>
              <tr className="bg-white border-b border-stone-100 text-[11px] font-bold text-stone-400 uppercase tracking-wider">
                <th className="px-6 py-3">Timestamp</th>
                <th className="px-6 py-3">Action</th>
                <th className="px-6 py-3">Latency (ms)</th>
                <th className="px-6 py-3">Trust Score</th>
                <th className="px-6 py-3">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-stone-50">
              {loading ? (
                <tr>
                  <td colSpan={5} className="px-6 py-8 text-center text-sm text-stone-400">Loading audit events...</td>
                </tr>
              ) : logs.length === 0 ? (
                <tr>
                  <td colSpan={5} className="px-6 py-8 text-center text-sm text-stone-400">No events found.</td>
                </tr>
              ) : (
                logs.map((log) => (
                  <tr key={log.id} className="hover:bg-stone-50/50 transition-colors">
                    <td className="px-6 py-3">
                      <div className="flex items-center gap-2 text-xs text-stone-500 font-mono">
                        <Clock size={12} className="text-stone-400" />
                        {new Date(log.created_at).toLocaleString()}
                      </div>
                    </td>
                    <td className="px-6 py-3">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-semibold bg-stone-100 text-stone-600 border border-stone-200">
                        {log.action}
                      </span>
                    </td>
                    <td className="px-6 py-3 text-sm font-mono text-stone-600">
                      {log.latency_ms ? `${log.latency_ms}ms` : "-"}
                    </td>
                    <td className="px-6 py-3">
                      {log.trust_score !== null ? (
                        <div className="flex items-center gap-2">
                          <div className="w-16 h-1.5 bg-stone-100 rounded-full overflow-hidden">
                            <div 
                              className={`h-full rounded-full ${log.trust_score > 0.8 ? 'bg-emerald-500' : log.trust_score > 0.4 ? 'bg-amber-400' : 'bg-rose-500'}`} 
                              style={{ width: `${Math.max(5, log.trust_score * 100)}%` }}
                            ></div>
                          </div>
                          <span className="text-xs font-bold text-stone-700">{log.trust_score.toFixed(2)}</span>
                        </div>
                      ) : (
                        <span className="text-stone-300">-</span>
                      )}
                    </td>
                    <td className="px-6 py-3 text-xs text-stone-500 font-mono truncate max-w-[200px]" title={JSON.stringify(log.details)}>
                      {JSON.stringify(log.details)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
      )}

      {tab === "danger" && (
      <div className="bg-red-50 rounded-xl shadow-sm border border-red-200 p-6 mt-8">
        <div className="flex items-center gap-3 mb-4">
          <div className="bg-red-100 text-red-600 p-2 rounded-lg">
            <AlertCircle size={20} />
          </div>
          <div>
            <h3 className="font-semibold text-red-900">Danger Zone</h3>
            <p className="text-xs text-red-700">Irreversible Actions</p>
          </div>
        </div>
        <p className="text-sm text-red-800 mb-6">
          Resetting the database will permanently delete all structured and unstructured nodes, edges, and graphs
          — and clears the task queue (queued + running + scheduled ingestion tasks), so nothing from the old
          data keeps executing after the wipe. Your login and configuration will remain intact. This action cannot be undone.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <button
            onClick={() => {
              if (window.confirm("WARNING: Are you absolutely sure you want to permanently delete all data in the database? This also clears the task queue.")) {
                setComputing(true);
                contextV2Api.resetDatabase()
                  .then((r: any) => {
                    const q = r?.queue || {};
                    alert(`Database has been reset. Queue: ${q.purged_queued ?? 0} queued dropped, ${q.revoked ?? 0} running/scheduled revoked.`);
                    loadLogs();
                  })
                  .catch(err => alert("Error resetting database: " + err))
                  .finally(() => setComputing(false));
              }
            }}
            disabled={computing}
            className="px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm font-semibold transition-colors disabled:opacity-50"
          >
            Factory Reset Database
          </button>
          <button
            onClick={() => {
              if (window.confirm("Clear the task queue? Every queued, running and scheduled ingestion task is dropped/terminated. Already-ingested data is NOT touched.")) {
                setComputing(true);
                contextV2Api.purgeQueue()
                  .then((r: any) => {
                    alert(`Queue cleared: ${r.purged_queued ?? 0} queued dropped, ${r.revoked ?? 0} running/scheduled revoked, ${r.runs_canceled ?? 0} progress runs canceled.`);
                    loadLogs();
                  })
                  .catch(err => alert("Error clearing queue: " + err))
                  .finally(() => setComputing(false));
              }
            }}
            disabled={computing}
            className="px-4 py-2 bg-white border border-red-300 text-red-700 hover:bg-red-100 rounded-lg text-sm font-semibold transition-colors disabled:opacity-50"
          >
            Clear Task Queue Only
          </button>
        </div>
        <p className="text-[11px] text-red-700/70 mt-3">
          Use &quot;Clear Task Queue Only&quot; when work is stuck behind stale/zombie tasks (e.g. an old giant
          ingest still queued) — it drains the broker even when no worker is running, without touching your data.
        </p>
      </div>
      )}
    </div>
  );
}
