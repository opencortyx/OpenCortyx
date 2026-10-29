"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { CheckCircle2, ChevronRight, FileUp, Loader2, Upload, X, XCircle } from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import TagInput from "@/components/ui/TagInput";
import IngestOutcomeCard from "@/components/authoring/IngestOutcomeCard";
import {
  contextV2Api, type IngestOutcome, type OutcomeStep, type Pipeline, type SchemaMappingRecord, type UploadBatch,
} from "@/lib/api/contextV2";
import { uploadAll } from "@/lib/uploadQueue";

/**
 * Add data — the only screen a new user needs: drop files, choose what goes into the knowledge
 * graph, Add.
 *
 * Nothing has to be configured. Every file is kept and searchable, and its shape is always captured
 * (a table's columns, a JSON file's keys, a document's outline, the links detected between them).
 * Two choices add knowledge on top - entities and relationships, the whole data as nodes - the same
 * for every kind of file.
 *
 * Everything else is under Custom, folded: a pipeline, a mapping that shapes the graph, extractors,
 * tags. Those are for people who already know they want them; they are never a step to complete.
 */

const POLL_MS = 2_000;
//: After this many quick polls a run is waiting behind other work (a folder dropped earlier is read
//: first), so it is asked about less often, never given up on: the view only says done when it is.
const POLL_FAST_TICKS = 150;
const POLL_SLOW_MS = 15_000;

type Phase = "idle" | "selected" | "single" | "batch";

/** The three kinds the server sorts files into, and the pipeline kind that serves each. */
type FileKind = "unstructured" | "structured" | "semistructured";
type PipelineChoice = Record<FileKind, string>;     // "" = the default pipeline for that kind
const NO_CHOICE: PipelineChoice = { unstructured: "", structured: "", semistructured: "" };
const KINDS: { key: FileKind; pipelineKind: Pipeline["kind"]; label: string }[] = [
  { key: "unstructured", pipelineKind: "document", label: "Documents" },
  { key: "structured", pipelineKind: "structured", label: "Tables" },
  { key: "semistructured", pipelineKind: "semistructured", label: "JSON and similar" },
];
/** The kind a file will most likely be read as, from its extension — only to decide which choices to
 *  SHOW. The server still detects the kind itself. */
function kindOf(file: File): FileKind {
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (["json", "jsonl", "ndjson", "xml", "yaml", "yml"].includes(ext)) return "semistructured";
  if (["csv", "tsv", "psv", "tab", "parquet", "pq", "xlsx", "xlsm", "xls"].includes(ext)) return "structured";
  return "unstructured";
}

/** Custom: what a person who knows they want it can set. Empty = the defaults. */
type CustomOptions = { pipelines: PipelineChoice; mappings: string[]; extractors: string[]; tags: string[] };
const NO_CUSTOM: CustomOptions = { pipelines: { ...NO_CHOICE }, mappings: [], extractors: [], tags: [] };

export default function AddDataPage() {
  const [files, setFiles] = useState<File[]>([]);
  const [phase, setPhase] = useState<Phase>("idle");
  const [graph, setGraph] = useState<GraphOptions>({ knowledge: false, wholeData: false });
  const [custom, setCustom] = useState<CustomOptions>(NO_CUSTOM);
  const [outcome, setOutcome] = useState<IngestOutcome | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (pollRef.current) clearTimeout(pollRef.current); }, []);

  const reset = () => {
    if (pollRef.current) clearTimeout(pollRef.current);
    setFiles([]); setPhase("idle"); setOutcome(null); setError(""); setBusy(false);
    setGraph({ knowledge: false, wholeData: false }); setCustom({ ...NO_CUSTOM, pipelines: { ...NO_CHOICE } });
  };

  const pick = (picked: File[]) => { setFiles(picked); setError(""); setPhase("selected"); };
  const kinds = Array.from(new Set(files.map(kindOf)));
  const options = uploadOptions(graph, custom);

  /** One file: added and followed to its result. `replace` reads it again even when an identical copy
   *  is already in (its knowledge went missing, say). */
  const addOne = async (replace = false) => {
    if (!files[0]) return;
    setBusy(true);
    setError("");
    try {
      const res = await contextV2Api.addSource(files[0], { ...options, replace });
      setPhase("single");
      if (res.run_id) startPolling(res.run_id);
      else setError("The file was accepted but no run was recorded, so there is nothing to follow.");
    } catch (e: any) {
      setError(e?.response?.data?.detail || "Could not start the ingestion.");
    } finally {
      setBusy(false);
    }
  };

  const startPolling = (runId: string) => {
    let ticks = 0;
    const tick = async () => {
      let finished = false;
      try {
        const next = await contextV2Api.getOutcome(runId);
        setOutcome(next);
        // `pending`: the file is in, what is read from it is not yet — keep watching it land.
        finished = !["queued", "running"].includes(next.status) && !next.pending;
      } catch {
        // A transient read failure must not end the poll — the run is still going.
      }
      if (!finished) pollRef.current = setTimeout(tick, ++ticks < POLL_FAST_TICKS ? POLL_MS : POLL_SLOW_MS);
    };
    tick();
  };

  /** Steps this page can answer in place; the rest follow their link (Extractor Studio, the asset…). */
  const handleStep = (step: OutcomeStep): boolean | void => {
    if (step.action === "retry") { addOne(); return true; }
    if (step.action === "replace") { addOne(true); return true; }
  };

  return (
    <div className="max-w-3xl mx-auto">
      <PageHeader
        title="Add data"
        icon={Upload}
        description="Drop in files and choose what goes into the knowledge graph. Nothing else is needed."
        actions={phase !== "idle"
          ? <Button variant="secondary" size="sm" onClick={reset}>Start over</Button>
          : <Link href="/dashboard/progress" className="text-sm text-primary hover:underline">Recent runs</Link>}
      />

      {phase === "idle" && <DropZone dragging={dragging} setDragging={setDragging} onPick={pick} />}

      {error && (
        <div className="rounded-lg border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/50
                        px-3 py-2 text-sm text-red-800 dark:text-red-300 mb-4">
          {error}
        </div>
      )}

      {phase === "selected" && (
        <div className="space-y-4">
          <ul className="rounded-xl border border-stone-200 dark:border-stone-800 divide-y divide-stone-100 dark:divide-stone-800 bg-white dark:bg-stone-900">
            {files.map((f) => (
              <li key={f.name} className="flex items-center gap-2 px-3 py-2 text-sm">
                <span className="truncate text-stone-800 dark:text-stone-200">{f.name}</span>
                <span className="ml-auto text-xs text-stone-400 shrink-0">{KINDS.find((k) => k.key === kindOf(f))?.label}</span>
                <button aria-label={`Remove ${f.name}`}
                        onClick={() => { const rest = files.filter((x) => x !== f); if (rest.length) setFiles(rest); else reset(); }}
                        className="p-1 text-stone-400 hover:text-red-600"><X size={14} /></button>
              </li>
            ))}
          </ul>
          <GraphOptionsChoice kinds={kinds} value={graph} onChange={setGraph} />
          <CustomSection kinds={kinds} value={custom} onChange={setCustom} />
          <Button variant="primary" icon={FileUp} busy={busy} disabled={busy}
                  onClick={() => (files.length === 1 ? addOne() : setPhase("batch"))}>
            {files.length === 1 ? "Add" : `Add ${files.length} files`}
          </Button>
        </div>
      )}

      {phase === "single" && (
        outcome ? <IngestOutcomeCard outcome={outcome} onStep={handleStep} /> : <Skeleton label="Starting…" />
      )}

      {phase === "batch" && <BatchProgress files={files} options={options} />}
    </div>
  );
}

/** What the upload call carries for these choices. */
function uploadOptions(graph: GraphOptions, custom: CustomOptions) {
  return {
    pipelineIds: custom.pipelines, tags: custom.tags, mappingIds: custom.mappings, extractors: custom.extractors,
    extractKnowledge: graph.knowledge, wholeData: graph.wholeData,
  };
}

function DropZone({ dragging, setDragging, onPick }: {
  dragging: boolean;
  setDragging: (v: boolean) => void;
  onPick: (files: File[]) => void;
}) {
  return (
    <label
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        const dropped = Array.from(e.dataTransfer.files || []);
        if (dropped.length) onPick(dropped);
      }}
      className={`block rounded-xl border-2 border-dashed px-6 py-14 text-center cursor-pointer
                  transition-colors ${dragging
                    ? "border-primary bg-primary/5"
                    : "border-stone-300 dark:border-stone-700 hover:border-stone-400 dark:hover:border-stone-600"}`}
    >
      <input type="file" multiple className="sr-only"
             onChange={(e) => { const fs = Array.from(e.target.files || []); if (fs.length) onPick(fs); }} />
      <Upload size={28} className="mx-auto text-stone-400 dark:text-stone-500" />
      <p className="mt-3 font-medium text-stone-800 dark:text-stone-200">
        Drop files here, or click to choose them
      </p>
      <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">
        Documents, spreadsheets, JSON, CSV — it works out which is which.
      </p>
    </label>
  );
}

/** Add data's two graph options - the same for every kind of file, in Auto and in Custom. */
export type GraphOptions = { knowledge: boolean; wholeData: boolean };

/**
 * What goes into the knowledge graph besides what always does. The data itself is always kept in the
 * data store (rows and records read with SQL, documents searchable), and the graph always gets its
 * shape: a table's columns and their types, a JSON file's keys and their types, the links found
 * between them, a document's outline.
 *   knowledge   entities and relationships. Tables and JSON: the records of the entity types found,
 *               linked to each other; free-text fields and documents: what a small in-process model
 *               reads in the text (no LLM), matched by name to the records it mentions.
 *   wholeData   the data itself as graph nodes too: every row, every key and value, every passage.
 */
function GraphOptionsChoice({ kinds, value, onChange }: {
  kinds: FileKind[]; value: GraphOptions; onChange: (v: GraphOptions) => void;
}) {
  if (!kinds.length) return null;
  const records = kinds.some((k) => k === "structured" || k === "semistructured");
  const documents = kinds.includes("unstructured");
  const what = [records && "tables and JSON", documents && "documents"].filter(Boolean).join(" and ");
  return (
    <fieldset className="rounded-lg border border-stone-200 dark:border-stone-800 p-3 text-sm space-y-2">
      <legend className="px-1 text-xs text-stone-500 dark:text-stone-400">Knowledge graph</legend>
      <p className="text-xs text-stone-500 dark:text-stone-400">
        Always: the data is kept and searchable, and the graph gets its shape - {records ? "columns and keys with their types, the links between them" : ""}
        {records && documents ? "; " : ""}{documents ? "the document's outline (headings, tables, figures)" : ""}.
      </p>
      <label className="flex items-start gap-2 cursor-pointer">
        <input type="checkbox" className="mt-0.5" checked={value.knowledge}
               onChange={(e) => onChange({ ...value, knowledge: e.target.checked })} />
        <span>
          <span className="font-medium text-stone-800 dark:text-stone-200">Extract entities and relationships</span>
          <span className="block text-xs text-stone-500 dark:text-stone-400 mt-0.5">
            {records && <>Tables and JSON: each record of the entity types found becomes a node, linked to the records it refers to. </>}
            {documents && <>Documents: </>}{records && <>Free-text fields{documents ? " and documents" : ""}: </>}
            the people, organisations, places and things named in the text, and how the sentences connect them - read by a small
            model on this server, no LLM - matched by name to the records they mention.
          </span>
        </span>
      </label>
      <label className="flex items-start gap-2 cursor-pointer">
        <input type="checkbox" className="mt-0.5" checked={value.wholeData}
               onChange={(e) => onChange({ ...value, wholeData: e.target.checked })} />
        <span>
          <span className="font-medium text-stone-800 dark:text-stone-200">Add the whole data to the graph</span>
          <span className="block text-xs text-stone-500 dark:text-stone-400 mt-0.5">
            The {what} themselves as nodes too: {[records && "every row, every key and value", documents && "every heading, paragraph and table"]
              .filter(Boolean).join("; ")}. Large files make large graphs; the data stays in the data store either way.
          </span>
        </span>
      </label>
    </fieldset>
  );
}

/**
 * Custom — folded, for people who already know they want it: a pipeline per kind of file, mappings
 * that shape the graph for tables and JSON, extractors that read documents, tags. Everything here is
 * optional; empty means the defaults.
 */
function CustomSection({ kinds, value, onChange }: {
  kinds: FileKind[]; value: CustomOptions; onChange: (v: CustomOptions) => void;
}) {
  const [open, setOpen] = useState(false);
  const [mappings, setMappings] = useState<SchemaMappingRecord[] | null>(null);
  const [extractors, setExtractors] = useState<{ name: string; label: string }[] | null>(null);
  const records = kinds.filter((k): k is "structured" | "semistructured" => k !== "unstructured");
  const documents = kinds.includes("unstructured");
  useEffect(() => {
    if (!open) return;
    if (records.length && mappings === null) {
      contextV2Api.listSchemaMappings(undefined, true)
        .then((r) => setMappings((r.items || []).filter((m) => (records as string[]).includes(String(m.paradigm)))))
        .catch(() => setMappings([]));
    }
    if (documents && extractors === null) {
      contextV2Api.getExtractionTemplates().then((ts: any[]) => {
        const { builtin, custom } = contextV2Api.stageableTemplates(ts, "unstructured");
        setExtractors([...custom, ...builtin].map((t: any) => ({ name: t.name, label: t.label })));
      }).catch(() => setExtractors([]));
    }
  }, [open, records.length, documents, mappings, extractors]);  // eslint-disable-line react-hooks/exhaustive-deps

  const set = Object.values(value.pipelines).filter(Boolean).length + value.mappings.length
    + value.extractors.length + (value.tags.length ? 1 : 0);
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  return (
    <div className="rounded-lg border border-stone-200 dark:border-stone-800 text-sm">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
              className="w-full flex items-center gap-1.5 px-3 py-2 text-left text-stone-700 dark:text-stone-300">
        <ChevronRight size={14} className={`transition-transform ${open ? "rotate-90" : ""}`} />
        <span className="font-medium">Custom</span>
        <span className="text-xs text-stone-400">{set ? `${set} set` : "optional - pipeline, mapping, extractors, tags"}</span>
      </button>
      {open && (
        <div className="space-y-4 px-3 pb-3">
          <PipelinePicker kinds={kinds} value={value.pipelines} onChange={(pipelines) => onChange({ ...value, pipelines })} />

          {records.length > 0 && (
            <div>
              <p className="text-sm font-medium text-stone-800 dark:text-stone-200">Shape the graph with a mapping</p>
              <p className="text-xs text-stone-500 dark:text-stone-400 mb-1">
                The nodes and edges a mapping declares, instead of what detection finds.{" "}
                <Link href="/dashboard/mapper" className="text-primary hover:underline">Create or edit mappings</Link>
              </p>
              {mappings === null && <Loader2 className="animate-spin text-stone-400" size={14} />}
              {mappings?.length === 0 && <p className="text-xs text-stone-400">No mappings for tables or JSON yet.</p>}
              <div className="flex flex-wrap gap-1.5">
                {mappings?.map((m) => (
                  <label key={m.id} className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs cursor-pointer ${
                    value.mappings.includes(String(m.id)) ? "border-teal-300 bg-teal-50 dark:bg-teal-950" : "border-stone-200 dark:border-stone-700"}`}>
                    <input type="checkbox" checked={value.mappings.includes(String(m.id))}
                           onChange={() => onChange({ ...value, mappings: toggle(value.mappings, String(m.id)) })} />
                    {m.name}
                  </label>
                ))}
              </div>
            </div>
          )}

          {documents && (
            <div>
              <p className="text-sm font-medium text-stone-800 dark:text-stone-200">Extractors for documents</p>
              <p className="text-xs text-stone-500 dark:text-stone-400 mb-1">
                Read the documents with your own extractors as well.{" "}
                <Link href="/dashboard/custom-assets" className="text-primary hover:underline">Create extractors</Link>
              </p>
              {extractors === null && <Loader2 className="animate-spin text-stone-400" size={14} />}
              {extractors?.length === 0 && <p className="text-xs text-stone-400">No extractors yet.</p>}
              <div className="flex flex-wrap gap-1.5">
                {extractors?.map((x) => (
                  <label key={x.name} className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs cursor-pointer ${
                    value.extractors.includes(x.name) ? "border-teal-300 bg-teal-50 dark:bg-teal-950" : "border-stone-200 dark:border-stone-700"}`}>
                    <input type="checkbox" checked={value.extractors.includes(x.name)}
                           onChange={() => onChange({ ...value, extractors: toggle(value.extractors, x.name) })} />
                    {x.label}
                  </label>
                ))}
              </div>
            </div>
          )}

          <div>
            <label htmlFor="add-tags" className="block text-sm font-medium text-stone-800 dark:text-stone-200 mb-1">Tags</label>
            <TagInput id="add-tags" value={value.tags} onChange={(tags) => onChange({ ...value, tags })}
                      placeholder="Untagged, each file is filed under its type" />
          </div>
        </div>
      )}
    </div>
  );
}

/** A pipeline for each kind of file being added; blank means that kind's default pipeline. */
function PipelinePicker({ kinds, value, onChange }: {
  kinds: FileKind[];
  value: PipelineChoice;
  onChange: (v: PipelineChoice) => void;
}) {
  const [saved, setSaved] = useState<Pipeline[]>([]);
  useEffect(() => {
    contextV2Api.listPipelines().then((r) => setSaved(r.items || [])).catch(() => setSaved([]));
  }, []);
  const shown = KINDS.filter((k) => kinds.includes(k.key));
  if (!shown.length) return null;
  return (
    <div>
      <p className="block text-sm font-medium text-stone-800 dark:text-stone-200 mb-1">
        Pipeline <span className="font-normal text-stone-500 dark:text-stone-400">(optional)</span>
      </p>
      <div className={`grid gap-2 ${shown.length > 1 ? "sm:grid-cols-3" : ""}`}>
        {shown.map((k) => (
          <label key={k.key} className="block text-xs text-stone-500 dark:text-stone-400">
            {shown.length > 1 && <span className="block mb-1">{k.label}</span>}
            <select aria-label={`Pipeline for ${k.label.toLowerCase()}`} value={value[k.key]}
                    onChange={(e) => onChange({ ...value, [k.key]: e.target.value })}
                    className="w-full rounded-lg border border-stone-300 dark:border-stone-700 bg-white dark:bg-stone-900
                               px-2.5 py-1.5 text-sm text-stone-800 dark:text-stone-200">
              <option value="">Automatic (by file type)</option>
              {saved.filter((pl) => pl.kind === k.pipelineKind).map((pl) => (
                <option key={pl.id} value={pl.id}>{pl.name}</option>
              ))}
            </select>
          </label>
        ))}
      </div>
    </div>
  );
}

type Failure = { name: string; error: string };

/** How often a batch's progress is read: one request for the whole batch, however many files. */
const BATCH_POLL_MS = 4_000;

/**
 * Several files at once: one batch, sent as soon as it is shown (the choices are made above).
 *
 * The files go up a few at a time with retries (lib/uploadQueue), and the batch's progress is one
 * request for all of it. It used to send them one after another, count any error as a lost file, and
 * then ask about every file separately every two seconds — a thousand requests a poll for a thousand
 * files — and stop asking after five minutes, while reading them takes hours.
 */
function BatchProgress({ files, options }: {
  files: File[];
  options: ReturnType<typeof uploadOptions>;
}) {
  const [sent, setSent] = useState<{ done: number; failed: Failure[] } | null>(null);
  const [batch, setBatch] = useState<UploadBatch | null>(null);
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const stopPolling = () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
  useEffect(() => stopPolling, []);

  // Closing the tab while files are still being sent stops the rest of them; the browser asks first.
  // Once sent, the server has them: reading them goes on without this page.
  useEffect(() => {
    if (!busy) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [busy]);

  const accepted = sent ? sent.done - sent.failed.length : 0;
  useEffect(() => {
    if (!busy && batch && sent && batch.total >= accepted && batch.done >= batch.total) stopPolling();
  }, [busy, batch, sent, accepted]);

  const add = async () => {
    setBusy(true);
    const batchId = crypto.randomUUID();       // the files land as ONE ingestion
    const failed: Failure[] = [];
    setSent({ done: 0, failed });
    const poll = async () => {
      try { setBatch(await contextV2Api.getUploadBatch(batchId)); }
      catch { /* a transient read failure must not end the poll */ }
    };
    pollRef.current = setInterval(poll, BATCH_POLL_MS);
    await uploadAll(files.length, async (i) => {
      const res = await contextV2Api.addSource(files[i], { ...options, batchId });
      return res.run_id as string | undefined;
    }, {
      concurrency: 3,
      onSettled: (r, done) => {
        if (!r.ok) failed.push({ name: files[r.index].name, error: r.error });
        else if (!r.value) failed.push({ name: files[r.index].name, error: "accepted, but no run was recorded" });
        setSent({ done, failed: [...failed] });
      },
    });
    setBusy(false);
    poll();
  };

  // Sent once, as soon as the view is shown.
  const started = useRef(false);
  useEffect(() => {
    if (!started.current) { started.current = true; add(); }
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const sentNow = sent || { done: 0, failed: [] as Failure[] };
  const counts = batch?.counts || {};
  const read = batch?.done ?? 0;
  const failures = [...sentNow.failed,
    ...(batch?.failed || []).map((f) => ({ name: f.asset, error: f.error || "failed" }))];
  const finished = !busy && !!batch && batch.total >= accepted && read >= batch.total;
  const pct = (n: number) => `${Math.round((100 * n) / Math.max(1, files.length))}%`;
  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold text-stone-900 dark:text-stone-100 flex items-center gap-2">
        {finished ? <CheckCircle2 size={18} className="text-emerald-500" />
                  : <Loader2 size={18} className="animate-spin text-stone-400" />}
        {busy ? `Sending ${sentNow.done} of ${files.length} files…`
          : finished ? `Added ${files.length - failures.length} of ${files.length} files`
          : `Reading ${read} of ${batch?.total ?? accepted} files…`}
      </h2>
      <div className="space-y-2" aria-label="Progress">
        {[["Sent", sentNow.done], ["Read", read]].map(([label, n]) => (
          <div key={label as string} className="flex items-center gap-3 text-sm">
            <span className="w-12 text-stone-500 dark:text-stone-400">{label}</span>
            <div className="flex-1 h-2 rounded bg-stone-200 dark:bg-stone-800 overflow-hidden">
              <div className="h-2 bg-primary transition-all" style={{ width: pct(n as number) }} />
            </div>
            <span className="w-24 text-right tabular-nums text-stone-600 dark:text-stone-300">
              {n as number} / {files.length}
            </span>
          </div>
        ))}
      </div>
      {batch && (
        <p className="text-sm text-stone-600 dark:text-stone-300">
          {[["waiting", counts.queued], ["reading", counts.running], ["added", counts.completed],
            ["failed", counts.failed], ["canceled", counts.canceled]]
            .filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(" · ")}
        </p>
      )}
      {!busy && !finished && (
        <p className="text-sm text-stone-500 dark:text-stone-400">
          The server has the files and reads them one after another; this goes on if you leave the
          page. What is read already can be asked about now.
        </p>
      )}
      {failures.length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-red-700 dark:text-red-400 mb-1">
            {failures.length} not added
          </h3>
          <ul className="rounded-xl border border-stone-200 dark:border-stone-800 divide-y divide-stone-100 dark:divide-stone-800 bg-white dark:bg-stone-900 max-h-80 overflow-auto">
            {failures.slice(0, 300).map((f, i) => (
              <li key={`${f.name}-${i}`} className="flex items-start gap-2 px-3 py-2 text-sm">
                <XCircle size={15} className="text-red-500 shrink-0 mt-0.5" />
                <span className="truncate text-stone-800 dark:text-stone-200 max-w-[40%]">{f.name}</span>
                <span className="ml-auto text-xs text-stone-500 dark:text-stone-400 text-right">{f.error}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="text-sm text-stone-500 dark:text-stone-400">
        See them in <Link href="/dashboard/catalogue" className="text-primary hover:underline">Assets</Link>,
        or each run in <Link href="/dashboard/progress" className="text-primary hover:underline">Runs</Link>.
      </p>
    </div>
  );
}

function Skeleton({ label }: { label: string }) {
  return (
    <div className="rounded-xl border border-stone-200 dark:border-stone-800
                    bg-white dark:bg-stone-900 p-6 text-center">
      <div className="h-2 w-40 mx-auto rounded bg-stone-200 dark:bg-stone-700 animate-pulse" />
      <p className="mt-3 text-sm text-stone-500 dark:text-stone-400">{label}</p>
    </div>
  );
}
