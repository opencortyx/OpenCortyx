"use client";

/**
 * Ingestion — one screen.
 *
 * There used to be three ways in: a chooser modal asked "what are you ingesting?", then handed you
 * to one of two wizards that differed only in which connectors and pipelines they listed. That fork
 * was routing dressed up as a question — a paradigm is a PROPERTY of the ingestion, the same way it
 * is a property of a mapping, not a reason to be somewhere else. So it is a control on this form,
 * and everything downstream (which connectors are offered, which pipelines, whether you pick tables
 * or folders, which upload endpoint a device file goes to) reads it.
 *
 * There are no wizard steps either. Source, pipeline, what-to-ingest, anchoring, schedule and name
 * are one scrolling panel, because "Next" between two half-filled forms is the same fork at a
 * smaller scale — and it hid the summary until after every decision had already been made.
 */

import { FULL_UI, SHOW_DEFAULT_PIPELINES, SHOW_UPLOAD_INGESTIONS } from "@/lib/simple";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { contextV2Api, Pipeline, Connector, Ingestion, BrowseSchema, SchemaSelection, ProgressRun, IngestFormats, SYSTEM_DEFAULT_PIPELINES_LIST } from "@/lib/api/contextV2";
import {
  Database, AlertCircle, X, Plus, GitBranch, Activity, UploadCloud, FileText,
  CheckCircle2, Zap, Cable, Play, Trash2, Pencil, Table2, Eye, Search, Loader2, ChevronRight,
  Layers, Clock, FolderTree, Braces, Pause, PlayCircle, ArrowRight,
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import TagInput from "@/components/ui/TagInput";
import { ScheduleControl, MappingsEditor, cleanMappings, detectDefaultKind, IngestMode, Mapping } from "./scheduling";
import AnchorRulesEditor from "./anchors";

export type AnchorRule = {
  match: string;
  edge_type: string;
  node_id?: string;
  node_label?: string;      // display only; the backend stores and matches on node_id
  node_type?: string;       // display only; what the picked node IS, so the editor can say so
  resolve_as?: string;
  create_missing?: boolean;
  as_type?: string;
};

type Paradigm = "structured" | "semistructured" | "unstructured";

/**
 * Everything a paradigm changes about this form. What used to be "which wizard am I in" is a lookup
 * here, which is what lets one screen carry all three without dropping a per-paradigm behaviour.
 */
const PARADIGMS: {
  id: Paradigm; label: string; blurb: string; icon: any;
  /** the `Pipeline.kind` to offer, and where "New pipeline" goes */
  pipelineKind: string; pipelineHref: string;
  accent: "emerald" | "violet" | "teal";
  on: string; tone: string;
}[] = [
  {
    id: "structured", label: "Structured", icon: Database,
    blurb: "Database tables & views, or tabular files (CSV · TSV · Parquet · Excel).",
    pipelineKind: "structured", pipelineHref: "/dashboard/pipelines/structured",
    accent: "emerald", on: "border-emerald-400 bg-emerald-50/60", tone: "bg-emerald-100 text-emerald-600",
  },
  {
    id: "semistructured", label: "Semi-Structured", icon: Braces,
    blurb: "JSON / JSONL / NDJSON — nested records & arrays, from device or connector.",
    pipelineKind: "semistructured", pipelineHref: "/dashboard/pipelines/semistructured",
    accent: "violet", on: "border-violet-400 bg-violet-50/60", tone: "bg-violet-100 text-violet-600",
  },
  {
    id: "unstructured", label: "Unstructured", icon: FolderTree,
    blurb: "PDFs, Word, text, Markdown, HTML — from your device or a file/lake connector.",
    pipelineKind: "document", pipelineHref: "/dashboard/pipelines/document",
    accent: "teal", on: "border-teal-400 bg-teal-50/60", tone: "bg-teal-100 text-teal-600",
  },
];
const META = (p: Paradigm) => PARADIGMS.find((x) => x.id === p)!;

/** Connector kinds that hold files rather than tables. */
const FILE_CONN_KINDS = ["local", "s3", "gcs", "adls", "files"];

export default function IngestPage() {
  const router = useRouter();
  const [ingestions, setIngestions] = useState<Ingestion[]>([]);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [formats, setFormats] = useState<IngestFormats | null>(null);
  const [formatsOpen, setFormatsOpen] = useState(false);
  useEffect(() => { contextV2Api.ingestFormats().then(setFormats).catch(() => {}); }, []);
  const [pipelines, setPipelines] = useState<Pipeline[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState({ text: "", type: "" });
  const [running, setRunning] = useState<string | null>(null);
  // expanded run details for one ingestion (lazy-loaded, cached per ingestion)
  const [openRuns, setOpenRuns] = useState<string | null>(null);
  const [runsById, setRunsById] = useState<Record<string, ProgressRun[]>>({});

  // ── the one form ──
  const [open, setOpen] = useState(false);
  const [editId, setEditId] = useState("");
  const [mappings, setMappings] = useState<Mapping[]>([]);   // N:N connector → pipeline
  const [selConnector, setSelConnector] = useState("");      // mirrors mappings[0]: what browsing uses
  const [name, setName] = useState("");
  // Nothing extracts unless a pipeline names it: a new sync never asks for the default enrichment.
  // A saved one keeps the value it was saved with.
  const [enrich, setEnrich] = useState(false);
  const [mode, setMode] = useState<IngestMode>("on_demand");
  const [schedule, setSchedule] = useState("0 * * * *");     // cron (5-field) — used when mode=scheduled
  const [pipelineId, setPipelineId] = useState("");          // ingestion-level default (a mapping wins)
  // Attach ingested assets to nodes that already exist, by path. Empty = entity resolution only,
  // so this stays invisible until wanted.
  const [anchors, setAnchors] = useState<AnchorRule[]>([]);
  const [uploadTags, setUploadTags] = useState<string[]>([]);   // device uploads: every file gets these
  // Per-mapping bulk device files (keyed by mapping index) — each mapping's files run through ITS pipeline.
  const [filesByMap, setFilesByMap] = useState<Record<number, File[]>>({});
  const [previewPaths, setPreviewPaths] = useState<string[]>([]);   // what the anchor match preview runs on
  const [docProgress, setDocProgress] = useState("");               // per-file upload progress
  const [fileFormats, setFileFormats] = useState<string[]>([]);     // ingestion-level format filter

  const refresh = async () => {
    // allSettled, NOT Promise.all: a failure in ONE call (e.g. listIngestions 500'ing on a bad row)
    // must NOT blank the others and show a false "No ingestions yet". Surface the real error instead.
    const [ingR, conR, plR] = await Promise.allSettled([
      contextV2Api.listIngestions(), contextV2Api.connectors(), contextV2Api.listPipelines(),
    ]);
    if (ingR.status === "fulfilled") setIngestions((ingR.value?.items || [])
      .filter((i: Ingestion) => SHOW_UPLOAD_INGESTIONS || i.mode !== "one_time"));
    else {
      console.error("listIngestions failed", ingR.reason);
      const detail = (ingR.reason as any)?.response?.data?.detail || (ingR.reason as any)?.message || String(ingR.reason);
      setMsg({ text: `Couldn't load saved ingestions: ${detail}`, type: "error" });
    }
    if (conR.status === "fulfilled") setConnectors(conR.value?.connectors || []);
    if (plR.status === "fulfilled") setPipelines((plR.value?.items || [])
      .filter((p: Pipeline) => SHOW_DEFAULT_PIPELINES || !SYSTEM_DEFAULT_PIPELINES_LIST.some((d) => d.id === p.id)));
    setLoading(false);
  };
  useEffect(() => { refresh(); }, []);

  // Expand an ingestion into its runs (one per uploaded file). Always re-fetches on open so a run
  // that was still going when the row was last expanded shows its final state.
  const toggleRuns = async (id: string) => {
    if (openRuns === id) { setOpenRuns(null); return; }
    setOpenRuns(id);
    setRunsById((m) => { const { [id]: _drop, ...rest } = m; return rest; });
    try {
      const r = await contextV2Api.ingestionRuns(id);
      setRunsById((m) => ({ ...m, [id]: r.items || [] }));
    } catch {
      setRunsById((m) => ({ ...m, [id]: [] }));
    }
  };

  const connName = (id?: string | null) => connectors.find((c) => c.id === id)?.name || "—";
  const plName = (id?: string | null) => (id ? pipelines.find((p) => p.id === id)?.name || "—" : "Default");
  const isFileConnKind = (id?: string) => FILE_CONN_KINDS.includes(connectors.find((c) => c.id === id)?.kind || "");

  // Folders live PER-MAPPING (inside MappingsEditor). The union is only for validation + the summary.
  const cleanDirs = () => mappings.flatMap((m) => (m.directories || []).map((d) => d.trim())).filter(Boolean);
  const allFiles = () => Object.values(filesByMap).flat();
  const uploadMode = allFiles().length > 0;

  useEffect(() => {
    setPreviewPaths(Object.values(filesByMap).flat().map((f: any) => f.webkitRelativePath || f.name).filter(Boolean));
  }, [filesByMap]);

  useEffect(() => {
    if (!open || !connectors.length) return;
    if (mappings.length === 1 && !mappings[0].connector_id) {
      onMappings([{ ...mappings[0], connector_id: connectors[0].id }]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, connectors, mappings]);

  /** The mapping rows ARE the connector→pipeline selector; browsing follows the FIRST row. */
  const onMappings = (m: Mapping[]) => {
    setMappings(m);
    const cid = m[0]?.connector_id || "";
    setSelConnector(cid);
  };

  const resetForm = () => {
    setEditId(""); setName(""); setEnrich(false); setPipelineId("");
    setMode("on_demand"); setSchedule("0 * * * *");
    setAnchors([]); setUploadTags([]); setDocProgress(""); setMsg({ text: "", type: "" });
    setFilesByMap({});
    const first = connectors[0]?.id || "";
    const firstKind = (connectors[0]?.kind && FILE_CONN_KINDS.includes(connectors[0].kind)) ? "unstructured" : "structured";
    setMappings([{ connector_id: first, pipeline_id: "", kind: firstKind as any, enabled: true }]);
    setSelConnector(first);
    setFileFormats([]);
  };
  const openNew = () => { resetForm(); setOpen(true); };

  const openEdit = (ing: Ingestion) => {
    resetForm();
    setEditId(ing.id);
    setName(ing.name); setEnrich(ing.enrich); setPipelineId(ing.pipeline_id || "");
    setMode((ing.mode as IngestMode) || "on_demand"); setSchedule(ing.schedule || "0 * * * *");
    // Saved rules live in the ingestion config; without loading them an edit would DROP them,
    // because the spec only sends `anchors` when the editor holds some.
    setAnchors((((ing as any).anchors || (ing as any).config?.anchors) || []) as AnchorRule[]);
    const defaultKind: Paradigm = ing.kind === "semistructured" ? "semistructured"
      : (ing.kind === "unstructured" || ing.kind === "document") ? "unstructured" : "structured";
    const seeded = (ing.mappings || []).map((m: any) => ({
      connector_id: m.connector_id || "", pipeline_id: m.pipeline_id || "",
      kind: m.kind || defaultKind,
      pipelines: m.pipelines || undefined,
      enabled: m.enabled !== false,
      directories: m.directories || [], formats: m.formats || [],
      // without this the checkbox reads OFF on every edit and silently turns the feature off on save
      directory_nodes: !!m.directory_nodes,
      selections: m.selections || undefined,
    }));
    setMappings(seeded.length ? seeded : [{
      connector_id: ing.connector_id || "", pipeline_id: ing.pipeline_id || "",
      kind: defaultKind, enabled: true
    }]);
    setSelConnector(seeded[0]?.connector_id || ing.connector_id || "");
    setFileFormats(ing.formats || []);
    setOpen(true);
  };

  // ── save (and optionally run) a re-runnable ingestion ──
  const save = async (runAfter: boolean) => {
    if (!mappings.some((m) => m.connector_id)) { setMsg({ text: "Pick at least one connector.", type: "error" }); return; }
    if (!name.trim()) { setMsg({ text: "Name your ingestion.", type: "error" }); return; }
    setBusy(true); setMsg({ text: "Saving…", type: "info" });
    try {
      const maps = cleanMappings(mappings);
      const primaryKind = mappings[0]?.kind || "structured";
      const common = {
        name, connector_id: mappings[0]?.connector_id || selConnector,
        pipeline_id: mappings[0]?.pipeline_id || pipelineId || undefined,
        enrich, mode, schedule: mode === "scheduled" ? schedule : undefined,
        mappings: maps.length ? maps : undefined,   // per-mapping folders/formats travel inside `maps`
        anchors: anchors.length ? anchors.filter((a) => a.match.trim()) : undefined,
        kind: primaryKind,
        directories: cleanDirs().length ? cleanDirs() : undefined,
        formats: fileFormats.length ? fileFormats : undefined,
      };
      const saved = editId ? await contextV2Api.updateIngestion(editId, common as any) : await contextV2Api.createIngestion(common as any);
      setOpen(false);
      // Optimistically show it NOW so a flaky/failed list refresh can't hide a just-saved ingestion
      if (saved?.id) setIngestions((xs) => [saved as Ingestion, ...xs.filter((x) => x.id !== saved.id)]);
      if (runAfter) { await contextV2Api.runIngestion(saved.id); router.push("/dashboard/progress"); return; }
      await refresh();
    } catch (e: any) {
      setMsg({ text: `Error: ${e?.response?.data?.detail || e?.message || e}`, type: "error" });
    } finally { setBusy(false); }
  };

  // PER-MAPPING browser upload: each mapping's queued files run through ITS pipeline (authoritative —
  // pipeline stages only; no pipeline → chunk-only), filtered by that mapping's own formats.
  const ext = (f: File) => ((f as any).webkitRelativePath || f.name).split(".").pop()?.toLowerCase() || "";
  const uploadFromDevice = async () => {
    const entries = Object.entries(filesByMap).filter(([, fs]) => fs?.length);
    if (!entries.length) { setMsg({ text: "Queue files in a mapping first (Bulk upload).", type: "error" }); return; }
    setBusy(true); setMsg({ text: "", type: "" });
    // ONE batch id for this upload action → the server saves it as a single one-time ingestion that
    // lists every file, instead of one entry per file (or none at all, as before).
    const batchId = `upload-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    let ok = 0, fail = 0, n = 0;
    for (const [idxStr, files] of entries) {
      const map = mappings[Number(idxStr)];
      const rowKind = map?.kind || "unstructured";
      const pl = map?.pipeline_id ? pipelines.find((p) => p.id === map.pipeline_id) : null;
      const fmts = map?.formats || [];
      const picked = fmts.length ? files.filter((f) => fmts.includes(ext(f))) : files;
      for (const f of picked) {
        n++;
        const apath = (f as any).webkitRelativePath || f.name;
        const anch = anchors.filter((a) => a.match.trim());
        setDocProgress(`Uploading ${n} — ${apath}`);
        try {
          if (rowKind === "auto")
            await contextV2Api.uploadAutoDetected(f, map?.pipelines || {}, batchId,
              anch.length ? anch : undefined, apath, uploadTags);
          else if (rowKind === "semistructured")
            await contextV2Api.ingestJson(f, {
              ...(map?.pipeline_id ? { pipeline_id: map.pipeline_id } : {}),
              ...(anch.length ? { anchors: anch, asset_path: apath } : {}),
            }, batchId, uploadTags);
          else if (pl)
            await contextV2Api.uploadDocumentWithPipeline(
              f, { pipeline: pl.stages || [], ...(anch.length ? { anchors: anch, asset_path: apath } : {}) }, batchId,
              uploadTags);
          else
            await contextV2Api.uploadDocument(f, ["chunking"], undefined, undefined, batchId,
              anch.length ? anch : undefined, apath, uploadTags);
          ok++;
        } catch { fail++; }
      }
    }
    setBusy(false); setDocProgress("");
    if (fail && !ok) { setMsg({ text: `All ${fail} upload(s) failed.`, type: "error" }); return; }
    setOpen(false);
    router.push("/dashboard/progress");
  };

  // ── saved-row actions ──
  const runNow = async (ing: Ingestion) => {
    setRunning(ing.id);
    try { await contextV2Api.runIngestion(ing.id); router.push("/dashboard/progress"); }
    catch (e: any) { alert(`Run failed: ${e?.response?.data?.detail || e?.message}`); setRunning(null); }
  };
  // Cheap change-detection sync (add/update/remove only) — file ingestions (unstructured / semi-structured).
  const syncNow = async (ing: Ingestion) => {
    setRunning(ing.id);
    try { await contextV2Api.syncIngestion(ing.id); router.push("/dashboard/progress"); }
    catch (e: any) { alert(`Sync failed: ${e?.response?.data?.detail || e?.message}`); setRunning(null); }
  };
  const pauseIng = async (ing: Ingestion) => {
    try { await contextV2Api.pauseIngestion(ing.id); await refresh(); }
    catch (e: any) { alert(`Pause failed: ${e?.response?.data?.detail || e?.message}`); }
  };
  const resumeIng = async (ing: Ingestion) => {
    setRunning(ing.id);
    try { const r = await contextV2Api.resumeIngestion(ing.id); if (r.run_id) { router.push("/dashboard/progress"); } else { setRunning(null); await refresh(); } }
    catch (e: any) { alert(`Resume failed: ${e?.response?.data?.detail || e?.message}`); setRunning(null); }
  };
  const onDelete = async (ing: Ingestion) => {
    if (!confirm(`Delete ingestion "${ing.name}"? Already-ingested data is unaffected — this only removes the job.`)) return;
    try { await contextV2Api.deleteIngestion(ing.id); setIngestions((xs) => xs.filter((x) => x.id !== ing.id)); }
    catch { alert("Failed to delete."); }
  };

  const statusTone: Record<string, string> = {
    idle: "bg-stone-100 text-stone-600", running: "bg-teal-100 text-teal-700",
    completed: "bg-emerald-100 text-emerald-700", failed: "bg-red-100 text-red-700",
  };

  // The one form's sections, numbered in the order they render — "Tables & views" only exists for a
  // database source, so the numbers are positional rather than hardcoded.
  // The form sections:
  // Section 1: Sources & Pipelines (with per-source data types, active toggles, and expandable configurations)
  // Section 2: Attach to existing nodes (anchors)
  // Section 3: Name & schedule
  const sections: { title: string; hint?: string; body: React.ReactNode }[] = [];
  const [more, setMore] = useState(FULL_UI);
  const first = mappings[0];
  const firstConn = connectors.find((c) => c.id === first?.connector_id);
  const setFirst = (patch: Partial<Mapping>) => onMappings([{ ...(first || { enabled: true } as Mapping), ...patch }, ...mappings.slice(1)]);
  const field = "w-full border border-stone-200 rounded-lg px-3 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-teal-500/20";
  const short = (
    <div className="space-y-4">
      <div className="grid sm:grid-cols-2 gap-3">
        <label className="text-xs font-semibold text-stone-500">Connection
          <select className={`${field} mt-1`} value={first?.connector_id || ""}
            onChange={(e) => setFirst({ connector_id: e.target.value,
                                        kind: detectDefaultKind(connectors.find((c) => c.id === e.target.value)?.kind) as any })}>
            {connectors.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>
        <label className="text-xs font-semibold text-stone-500">Read with
          <select className={`${field} mt-1`} value={first?.pipeline_id || ""} onChange={(e) => setFirst({ pipeline_id: e.target.value })}>
            <option value="">Automatic (as Add data)</option>
            {pipelines.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </label>
      </div>
      {FILE_CONN_KINDS.includes(firstConn?.kind || "") && (
        <label className="block text-xs font-semibold text-stone-500">Folders
          <input className={`${field} mt-1`} placeholder="all of it, or e.g. reports/2026, contracts"
            value={(first?.directories || []).join(", ")}
            onChange={(e) => setFirst({ directories: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} />
        </label>
      )}
      <ScheduleControl mode={mode} schedule={schedule} onMode={setMode} onSchedule={setSchedule} accent="teal" />
      <label className="block text-xs font-semibold text-stone-500">Name
        <input className={`${field} mt-1`} value={name} onChange={(e) => setName(e.target.value)}
          placeholder={firstConn ? `${firstConn.name} sync` : "e.g. Contracts, nightly"} />
      </label>
    </div>
  );

  sections.push({
    title: "Sources & pipelines",
    hint: "map each source to a pipeline and configure its data type",
    body: connectors.length === 0 ? (
      <div className="text-sm text-stone-500 bg-amber-50 border border-amber-100 rounded-lg p-3 flex items-center gap-2">
        <AlertCircle size={16} className="text-amber-500" />
        No connectors yet —{" "}
        <button className="underline font-semibold" onClick={() => router.push("/dashboard/connectors")}>add one</button>
      </div>
    ) : (
      <MappingsEditor
        mappings={mappings}
        onChange={onMappings}
        connectors={connectors}
        pipelines={pipelines}
        connectorLabel="Source"
        onNewPipeline={() => router.push("/dashboard/pipelines")}
        pendingFiles={filesByMap}
        onPickFiles={(i: number, fs: File[]) => setFilesByMap((m) => ({ ...m, [i]: [...(m[i] || []), ...fs] }))}
        onClearFiles={(i: number) => setFilesByMap((m) => { const n = { ...m }; delete n[i]; return n; })}
      />
    ),
  });

  sections.push({
    title: "Attach to existing nodes",
    hint: "optional",
    body: <AnchorRulesEditor rules={anchors} onChange={setAnchors} paths={previewPaths} />,
  });

  sections.push({
    title: uploadMode ? "Upload" : "Name & schedule",
    body: uploadMode ? (
      <div className="space-y-3">
        <div>
          <label className="block text-xs font-bold text-stone-500 uppercase tracking-wider mb-1.5">Tags (optional)</label>
          <TagInput value={uploadTags} onChange={setUploadTags}
            placeholder="Given to every file here. Untagged files are filed under their extension" />
        </div>
        <div className="text-xs text-stone-600 bg-amber-50 border border-amber-100 rounded-lg p-3">
          This is a <b>one-time device upload</b> — it ingests immediately and isn&apos;t saved as a re-runnable,
          schedulable job. To schedule / watch for changes, clear the device files above and use <b>Watch folders</b> instead.
        </div>
      </div>
    ) : (
      <div className="space-y-4">
        <div>
          <label className="block text-xs font-bold text-stone-500 uppercase tracking-wider mb-1.5">Ingestion name</label>
          <input value={name} onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Multi-Source Pipeline — nightly catalogue"
            className="w-full border border-stone-200 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-teal-500/20" />
        </div>
        <ScheduleControl mode={mode} schedule={schedule} onMode={setMode} onSchedule={setSchedule} accent="teal" />
      </div>
    ),
  });

  return (
    <div className="max-w-7xl mx-auto space-y-6 pb-24">
      {/* Hero */}
      <PageHeader
        title={FULL_UI ? "Ingestion" : "Scheduled syncs"}
        icon={UploadCloud}
        description={FULL_UI
          ? "Create re-runnable ingestions for Structured, Semi-Structured, and Unstructured data: pick a connector, choose what to ingest, assign a pipeline, and run on-demand, on a schedule, or in real-time."
          : "Read a connection again on demand, on a schedule or as it changes."}
        actions={
          <Button variant="primary" icon={Plus} onClick={openNew}>{FULL_UI ? "New Ingestion" : "New sync"}</Button>
        }
      />

      {/* quick links */}
      {FULL_UI && <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <QuickLink icon={<Cable size={18} className="text-teal-500" />} title="Connectors" sub={`${connectors.length} saved`} onClick={() => router.push("/dashboard/connectors")} />
        <QuickLink icon={<GitBranch size={18} className="text-emerald-500" />} title="Pipeline Studio" sub={`${pipelines.length} pipelines`} onClick={() => router.push("/dashboard/pipelines")} />
        <QuickLink icon={<Activity size={18} className="text-emerald-500" />} title="Progress" sub="Track every run" onClick={() => router.push("/dashboard/progress")} />
      </div>}

      {/* SUPPORTED FILE TYPES — grouped by the pipeline that reads them, straight from the routing
          registry. The route is not cosmetic: a .jsonl is WALKED into nodes, never chunked, and a
          .csv is read as a table — which is why the grouping is the useful part, not the count. */}
      {FULL_UI && formats && (
        <div className="bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-800 rounded-xl">
          <button onClick={() => setFormatsOpen((v) => !v)}
            className="w-full flex items-center gap-2 px-4 py-3 text-left">
            <FileText size={16} className="text-teal-500 shrink-0" />
            <span className="text-sm font-bold text-stone-800 dark:text-stone-100">
              Supported file types
            </span>
            <span className="text-[11px] font-bold text-teal-700 bg-teal-50 dark:bg-teal-950 px-2 py-0.5 rounded-full">
              {formats.total}
            </span>
            <span className="text-[11px] text-stone-400 hidden sm:inline">
              — each routed to the pipeline that reads it best
            </span>
            <ChevronRight size={15}
              className={`ml-auto text-stone-400 transition-transform ${formatsOpen ? "rotate-90" : ""}`} />
          </button>
          {formatsOpen && (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3 px-4 pb-4">
              {formats.routes.map((r) => (
                <div key={r.route} className="border border-stone-200 dark:border-stone-800 rounded-lg p-3">
                  <div className="flex items-baseline gap-2">
                    <span className="text-xs font-bold text-stone-800 dark:text-stone-100">{r.label}</span>
                    <span className="text-[10px] font-bold text-stone-400">{r.count}</span>
                  </div>
                  <p className="text-[11px] text-stone-500 mt-0.5 leading-relaxed">{r.reads}</p>
                  <div className="flex flex-wrap gap-1 mt-2">
                    {r.formats.map((f) => (
                      <span key={f.ext} title={f.name}
                        className="px-1.5 py-0.5 bg-stone-100 dark:bg-stone-800 text-stone-600 dark:text-stone-300 rounded text-[10px] font-mono">
                        .{f.ext}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Saved ingestions */}
      <div>
        {FULL_UI && <h2 className="text-xl font-bold text-stone-900 mb-4">Saved ingestions</h2>}
        {loading ? (
          <div className="text-stone-500 animate-pulse p-6">Loading…</div>
        ) : ingestions.length === 0 ? (
          <div className="bg-stone-50 border border-dashed border-stone-300 rounded-2xl p-12 text-center text-stone-500 flex flex-col items-center">
            <Database size={42} className="mb-4 text-stone-300" />
            <h3 className="text-lg font-bold mb-2 text-stone-700">{FULL_UI ? "No ingestions yet" : "No syncs yet"}</h3>
            {FULL_UI && <p className="max-w-md">Create one above — pick what you&apos;re ingesting, choose a connector and a pipeline. It&apos;ll live here so you can run it on-demand, on a schedule, or in real-time.</p>}
          </div>
        ) : (
          <div className="space-y-3">
            {ingestions.map((ing) => {
              const isSemi = ing.kind === "semistructured";
              const isFile = isSemi || ing.kind === "unstructured" || ing.kind === "document";
              const kindLabel = isSemi ? "Semi-Structured" : isFile ? "Unstructured" : "Structured";
              // A one-time device upload: it already ran, and can't be re-run or scheduled — the files
              // live on the user's machine. It still belongs here as a record of what was ingested.
              const isOneTime = ing.mode === "one_time";
              const canSchedule = ing.mode && ing.mode !== "on_demand" && !isOneTime;
              const files = ing.files || [];
              return (
                <div key={ing.id} className="bg-white border border-stone-200 rounded-xl shadow-sm">
                  <div className="p-4 flex items-center justify-between gap-4">
                    <div className="flex items-center gap-3 min-w-0">
                      <div className={`p-2.5 rounded-lg ${isSemi ? "bg-violet-100 text-violet-600" : isFile ? "bg-teal-100 text-teal-600" : "bg-emerald-100 text-emerald-600"}`}>
                        {isSemi ? <Braces size={20} /> : isFile ? <FolderTree size={20} /> : <Database size={20} />}
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <h3 className="font-bold text-stone-900 truncate">{ing.name}</h3>
                          <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded-full bg-stone-100 text-stone-500">{kindLabel}</span>
                          <span className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-full ${statusTone[ing.status] || statusTone.idle}`}>{ing.status}</span>
                          {isOneTime && (
                            <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded-full bg-indigo-100 text-indigo-700 flex items-center gap-1"
                              title="Uploaded from your device — ran once, can't be re-run">
                              <UploadCloud size={10} /> one-time upload
                            </span>
                          )}
                          {canSchedule && (
                            <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded-full bg-teal-100 text-teal-700 flex items-center gap-1">
                              <Clock size={10} /> {ing.mode === "realtime" ? "real-time" : "scheduled"}
                            </span>
                          )}
                          {ing.paused && (
                            <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 flex items-center gap-1"><Pause size={10} /> paused</span>
                          )}
                          {ing.hibernated && (
                            <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded-full bg-orange-100 text-orange-700" title="Auto-paused after a failure — retries automatically">hibernated</span>
                          )}
                          {!!ing.failure_count && (
                            <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded-full bg-red-50 text-red-600 flex items-center gap-1" title={(ing.failures || []).slice(-1).map((f) => `${f.category}: ${f.message}`).join("")}>
                              <AlertCircle size={10} /> {ing.failure_count} fail{ing.failure_count === 1 ? "" : "s"}
                            </span>
                          )}
                        </div>
                        {isOneTime ? (
                          <p className="text-xs text-stone-500 mt-0.5 truncate">
                            <span className="font-medium">{files.length} file{files.length === 1 ? "" : "s"}</span>
                            {files.length ? ` · ${files.slice(0, 3).join(", ")}${files.length > 3 ? ` +${files.length - 3} more` : ""}` : ""}
                            {` · pipeline: ${plName(ing.pipeline_id)}`}
                            {ing.last_run_at ? ` · uploaded ${new Date(ing.last_run_at).toLocaleString()}` : ""}
                          </p>
                        ) : (
                          <p className="text-xs text-stone-500 mt-0.5 truncate">
                            <span className="font-medium">{connName(ing.connector_id)}</span>
                            {(ing.mappings?.length || 0) > 1 ? ` +${(ing.mappings!.length - 1)} more connector${ing.mappings!.length - 1 === 1 ? "" : "s"}` : ""}
                            {isFile ? (
                              <>
                                {` · ${ing.directories?.length || 0} folder${(ing.directories?.length || 0) === 1 ? "" : "s"}`}
                                {` · ${ing.formats?.length ? ing.formats.join(", ") : "all formats"}`}
                              </>
                            ) : (
                              <>
                                {ing.schemas?.length ? ` · ${ing.schemas.length} schema${ing.schemas.length > 1 ? "s" : ""}` : ""} · {ing.object_count || "all"} objects
                              </>
                            )}
                            {` · pipeline: ${plName(ing.pipeline_id)}`}
                            {ing.last_run_at ? ` · last run ${new Date(ing.last_run_at).toLocaleString()}` : ""}
                          </p>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      {isOneTime ? (
                        <button onClick={() => toggleRuns(ing.id)}
                          className="px-3 py-1.5 text-xs font-bold border border-stone-200 text-stone-700 rounded-md hover:bg-stone-50 flex items-center gap-1.5">
                          <Activity size={14} /> {openRuns === ing.id ? "Hide runs" : "View runs"}
                          <ChevronRight size={13} className={`transition-transform ${openRuns === ing.id ? "rotate-90" : ""}`} />
                        </button>
                      ) : (
                        <>
                          <button onClick={() => runNow(ing)} disabled={running === ing.id}
                            className="px-3 py-1.5 text-xs font-bold bg-stone-900 text-white rounded-md hover:bg-stone-800 flex items-center gap-1.5 disabled:opacity-50">
                            {running === ing.id ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />} Run
                          </button>
                          {isFile && (
                            <button onClick={() => syncNow(ing)} disabled={running === ing.id} title="Sync changes only (add / update / remove)"
                              className="px-3 py-1.5 text-xs font-bold border border-stone-200 text-stone-700 rounded-md hover:bg-stone-50 flex items-center gap-1.5 disabled:opacity-50">
                              <Activity size={14} /> Sync
                            </button>
                          )}
                          {(canSchedule || ing.paused || ing.hibernated) && (
                            ing.paused ? (
                              <button onClick={() => resumeIng(ing)} disabled={running === ing.id} title="Resume — continues from where it left off"
                                className="p-1.5 text-emerald-500 hover:text-emerald-700 hover:bg-emerald-50 rounded-md"><PlayCircle size={16} /></button>
                            ) : (
                              <button onClick={() => pauseIng(ing)} title="Pause schedule"
                                className="p-1.5 text-stone-400 hover:text-amber-600 hover:bg-amber-50 rounded-md"><Pause size={16} /></button>
                            )
                          )}
                          <button onClick={() => openEdit(ing)} className="p-1.5 text-stone-400 hover:text-emerald-600 hover:bg-emerald-50 rounded-md"><Pencil size={15} /></button>
                        </>
                      )}
                      <button onClick={() => onDelete(ing)} className="p-1.5 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-md"><Trash2 size={15} /></button>
                    </div>
                  </div>

                  {/* run details — one row per uploaded file, with its stages and any error */}
                  {openRuns === ing.id && (
                    <div className="border-t border-stone-100 px-4 py-3 bg-stone-50/60 rounded-b-xl">
                      {!runsById[ing.id] ? (
                        <div className="text-xs text-stone-500 flex items-center gap-2"><Loader2 size={13} className="animate-spin" /> Loading runs…</div>
                      ) : runsById[ing.id].length === 0 ? (
                        <div className="text-xs text-stone-500">No run records for this ingestion.</div>
                      ) : (
                        <div className="space-y-2">
                          {runsById[ing.id].map((r) => (
                            <div key={r.id} className="bg-white border border-stone-200 rounded-lg px-3 py-2">
                              <div className="flex items-center justify-between gap-3">
                                <div className="min-w-0">
                                  <div className="text-xs font-bold text-stone-800 truncate">{r.asset}</div>
                                  <div className="text-[11px] text-stone-500">
                                    {/* the tabular executor reports no stages — show just the percentage there */}
                                    {r.total_steps > 0 ? `${r.current_step}/${r.total_steps} stages · ` : ""}{r.percent}%
                                    {r.finished_at ? ` · finished ${new Date(r.finished_at).toLocaleTimeString()}` : ""}
                                  </div>
                                </div>
                                <div className="flex items-center gap-2 shrink-0">
                                  <span className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-full ${statusTone[r.status] || statusTone.idle}`}>{r.status}</span>
                                  <button onClick={() => router.push("/dashboard/progress")}
                                    className="text-[11px] font-semibold text-emerald-600 hover:underline">Progress →</button>
                                </div>
                              </div>
                              {!!r.steps?.length && (
                                <div className="mt-1.5 flex flex-wrap gap-1">
                                  {r.steps.map((s) => (
                                    <span key={s.i} title={s.status}
                                      className={`text-[10px] px-1.5 py-0.5 rounded border ${s.status === "completed" ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                                          : s.status === "failed" ? "bg-red-50 text-red-700 border-red-200"
                                            : s.status === "skipped" ? "bg-stone-50 text-stone-400 border-stone-200"
                                              : "bg-teal-50 text-teal-700 border-teal-200"}`}>
                                      {s.name}{s.count != null ? ` · ${s.count}` : ""}
                                    </span>
                                  ))}
                                </div>
                              )}
                              {r.error && <div className="mt-1.5 text-[11px] text-red-600 break-words">{r.error}</div>}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* ── The one ingestion form: every paradigm, every step, one panel ── */}
      {open && (
        <div className="fixed inset-0 bg-stone-900/40 backdrop-blur-sm z-[60] flex items-center justify-center p-4" onClick={() => !busy && setOpen(false)}>
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-5xl max-h-[92vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
            <div className="px-6 py-4 border-b border-stone-100 flex items-center justify-between">
              <h2 className="font-bold text-stone-900 flex items-center gap-2">
                <UploadCloud size={18} className="text-emerald-500" />
                {FULL_UI ? (editId ? "Edit ingestion" : "New ingestion") : (editId ? "Edit sync" : "New sync")}
              </h2>
              <button onClick={() => setOpen(false)} className="p-1.5 text-stone-400 hover:bg-stone-100 rounded-md cursor-pointer"><X size={18} /></button>
            </div>

            <div className="p-6 flex-1 overflow-y-auto space-y-7">
              {!more && connectors.length > 0 && short}
              {!FULL_UI && (
                <button type="button" onClick={() => setMore((v) => !v)} className="text-sm text-stone-500 hover:text-stone-900">
                  {more ? "Fewer options" : "More options"}
                </button>
              )}
              {(more || connectors.length === 0) && sections.map((s, i) => (
                <section key={s.title}>
                  <div className="flex items-center gap-2 mb-3">
                    <span className="w-5 h-5 rounded-full bg-stone-900 text-white text-[11px] font-bold flex items-center justify-center shrink-0">{i + 1}</span>
                    <h3 className="text-sm font-bold text-stone-900">{s.title}</h3>
                    {s.hint && <span className="text-xs text-stone-400 truncate">{s.hint}</span>}
                  </div>
                  <div className="pl-7">{s.body}</div>
                </section>
              ))}

              {/* Summary */}
              <div className="bg-stone-50 border border-stone-200 rounded-lg p-3 text-xs text-stone-600">
                <div className="font-bold text-stone-700 mb-1">Summary</div>
                {uploadMode ? (
                  <>Upload <b>{allFiles().length}</b> file(s) from this device — each source&apos;s files run through its own pipeline.</>
                ) : (
                  <>
                    <b>{mappings.filter((m) => m.connector_id).length || 1}</b> source(s) configured · {mode === "on_demand" ? "on-demand" : mode === "realtime" ? "real-time" : "scheduled"}
                    {cleanDirs().length > 0 && <> · <b>{cleanDirs().length}</b> watch folder(s)</>}
                  </>
                )}
              </div>

              {docProgress && (
                <div className="text-xs text-teal-700 bg-teal-50 border border-teal-100 rounded-lg px-3 py-2 flex items-center gap-2"><Loader2 size={13} className="animate-spin" /> {docProgress}</div>
              )}
              {msg.text && (
                <div className={`p-3 rounded-lg text-sm font-medium flex items-center gap-2 ${msg.type === "error" ? "bg-red-50 text-red-700 border border-red-100" : "bg-teal-50 text-teal-700 border border-teal-100"}`}>
                  {msg.type === "error" ? <AlertCircle size={16} /> : <Loader2 size={16} className="animate-spin" />} {msg.text}
                </div>
              )}
            </div>

            {/* footer */}
            <div className="px-6 py-4 border-t border-stone-100 flex justify-between gap-3">
              <button onClick={() => setOpen(false)} disabled={busy}
                className="px-5 py-2.5 text-sm font-semibold text-stone-600 hover:bg-stone-100 rounded-lg">Cancel</button>
              {uploadMode ? (
                <Button variant="primary" icon={Zap} busy={busy} onClick={uploadFromDevice}>Upload &amp; Ingest</Button>
              ) : (
                <div className="flex gap-2">
                  <button onClick={() => save(false)} disabled={busy || !name.trim()}
                    className="px-5 py-2.5 text-sm font-semibold border border-stone-200 text-stone-700 rounded-lg hover:bg-stone-50 disabled:opacity-50">Save</button>
                  <Button variant="primary" icon={Zap} busy={busy} disabled={!name.trim()} onClick={() => save(true)}>Save &amp; Run</Button>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

    </div>
  );
}

function QuickLink({ icon, title, sub, onClick }: { icon: React.ReactNode; title: string; sub: string; onClick: () => void }) {
  return (
    <button onClick={onClick} className="bg-white border border-stone-200 rounded-xl p-4 flex items-center gap-3 hover:border-emerald-300 hover:shadow-sm transition-all text-left">
      <div className="p-2 rounded-lg bg-stone-50">{icon}</div>
      <div className="min-w-0"><div className="text-sm font-bold text-stone-800">{title}</div><div className="text-xs text-stone-500 flex items-center gap-1"><Clock size={11} /> {sub}</div></div>
      <ArrowRight size={16} className="text-stone-300 ml-auto" />
    </button>
  );
}
