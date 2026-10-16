"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { CheckCircle2, FileUp, Loader2, Sparkles, Upload, X, XCircle } from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import TagInput from "@/components/ui/TagInput";
import DraftCard from "@/components/authoring/DraftCard";
import GoalExtractor from "@/components/authoring/GoalExtractor";
import IngestOutcomeCard from "@/components/authoring/IngestOutcomeCard";
import {
  contextV2Api, type Draft, type DraftResponse, type IngestOutcome, type OutcomeStep, type Pipeline,
} from "@/lib/api/contextV2";

/**
 * Add data — the result-first path, and the only screen a new user needs.
 *
 * The product already ingests any file with no configuration at all: the route is detected, a
 * Universal pipeline runs, and no mapping has to be bound. What it never did was SAY so. The
 * sidebar offers Mapper and Pipelines beside Ingestion as equals, which reads as three steps to
 * complete, and the Mapper alone asks sixty-five questions. So people conclude the flexibility is
 * mandatory and either work through it or stop.
 *
 * This screen inverts that. One drop zone, then two things in order:
 *
 *   BEFORE  — how the file WOULD be read, derived from the file itself, every decision carrying
 *             the measurement behind it. Accepting is one click; so is ignoring it entirely and
 *             letting the defaults run, which is the honest default because it already works.
 *   AFTER   — what the run actually produced, in entities rather than chunks, with the one or two
 *             refinements this particular result earns.
 *
 * Refinement is therefore offered at the two moments a person can judge whether it is needed —
 * looking at a reading of their own file, and looking at what came out of it — instead of waiting
 * behind a menu item they have no way to evaluate on day one.
 */

const POLL_MS = 2_000;
//: A run can sit queued behind other work; this bounds the polling rather than the run.
const POLL_LIMIT = 150;

type Phase = "idle" | "reading" | "reviewing" | "ingesting" | "done" | "batch";

/** The three kinds the server sorts files into, and the pipeline kind that serves each. */
type FileKind = "unstructured" | "structured" | "semistructured";
type PipelineChoice = Record<FileKind, string>;     // "" = the default pipeline for that kind
const NO_CHOICE: PipelineChoice = { unstructured: "", structured: "", semistructured: "" };
const KINDS: { key: FileKind; pipelineKind: Pipeline["kind"]; label: string }[] = [
  { key: "unstructured", pipelineKind: "document", label: "Documents" },
  { key: "structured", pipelineKind: "structured", label: "Tables" },
  { key: "semistructured", pipelineKind: "semistructured", label: "JSON and similar" },
];
/** The kind a file will most likely be read as, from its extension — only to decide which pipeline
 *  choice to SHOW. The server still detects the kind itself and uses the choice for that kind. */
function kindOf(file: File): FileKind {
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (["json", "jsonl", "ndjson", "xml", "yaml", "yml"].includes(ext)) return "semistructured";
  if (["csv", "tsv", "psv", "tab", "parquet", "pq", "xlsx", "xlsm", "xls"].includes(ext)) return "structured";
  return "unstructured";
}

export default function AddDataPage() {
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [read, setRead] = useState<DraftResponse | null>(null);
  const [pipelineDraft, setPipelineDraft] = useState<Draft | null>(null);
  const [outcome, setOutcome] = useState<IngestOutcome | null>(null);
  const [sampleText, setSampleText] = useState("");
  const [showExtractor, setShowExtractor] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [tags, setTags] = useState<string[]>([]);
  const [pipelines, setPipelines] = useState<PipelineChoice>({ ...NO_CHOICE });
  const [batch, setBatch] = useState<File[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const reset = () => {
    if (pollRef.current) clearInterval(pollRef.current);
    setFile(null); setPhase("idle"); setRead(null); setPipelineDraft(null);
    setOutcome(null); setSampleText(""); setShowExtractor(false); setError(""); setTags([]);
    setPipelines({ ...NO_CHOICE }); setBatch([]);
  };

  /** One file is read and previewed first; several go straight to the batch list. */
  const pick = (picked: File[]) => {
    if (picked.length === 1) { inspect(picked[0]); return; }
    reset();
    setBatch(picked);
    setPhase("batch");
  };

  /** Read the file and show what would happen. Nothing is ingested and nothing is saved. */
  const inspect = useCallback(async (picked: File) => {
    reset();
    setFile(picked);
    setPhase("reading");
    setError("");
    try {
      // Both readings in parallel: they sample the same file independently and neither needs
      // the other's answer until a mapping is actually saved.
      const [mapping, pipeline] = await Promise.all([
        contextV2Api.draftFromFile(picked, "mapping"),
        contextV2Api.draftFromFile(picked, "pipeline"),
      ]);
      setRead(mapping);
      setPipelineDraft(pipeline.drafts[0] ?? null);
      setPhase("reviewing");
      // Kept for the extractor box below, so a goal typed later is previewed against this file
      // rather than against nothing.
      if (picked.size < 2_000_000) setSampleText(await picked.text().catch(() => ""));
    } catch (e: any) {
      setError(e?.response?.data?.detail || "Could not read that file.");
      setPhase("idle");
    }
  }, []);

  /** Ingest. `bind` saves the drafted reading first and runs the file through it; `replace` reads
   *  the file again even when an identical copy is already in (its knowledge went missing, say). */
  const lastBind = useRef(false);
  const ingest = async (bind: boolean, replace = false) => {
    if (!file) return;
    lastBind.current = bind;
    setBusy(bind ? "bind" : "default");
    setError("");
    try {
      let pipelineId: string | undefined;
      if (bind) {
        const mappingDraft = read?.drafts[0];
        if (mappingDraft) {
          const saved = await contextV2Api.saveDraft(acceptSuggestedTypes(mappingDraft));
          // Re-draft the pipeline with the saved mapping so the binding is written in the
          // spelling that engine reads. Doing it here rather than patching the stage client-side
          // keeps one implementation of which key each executor looks under.
          const rebound = await contextV2Api.draftFromFile(file, "pipeline", {
            useModel: false, context: { mapping_id: String(saved.id) },
          });
          const bound = rebound.drafts[0] ?? pipelineDraft;
          if (bound) pipelineId = String((await contextV2Api.saveDraft(bound)).id);
        } else if (pipelineDraft) {
          pipelineId = String((await contextV2Api.saveDraft(pipelineDraft)).id);
        }
      }
      // A pipeline chosen below applies unless the reading above was bound to its own.
      const res = await contextV2Api.addSource(file, {
        pipelineId, pipelineIds: pipelineId ? undefined : pipelines, replace, tags });
      setPhase("ingesting");
      if (res.run_id) startPolling(res.run_id);
      else setError("The file was accepted but no run was recorded, so there is nothing to follow.");
    } catch (e: any) {
      setError(e?.response?.data?.detail || "Could not start the ingestion.");
    } finally {
      setBusy("");
    }
  };

  const startPolling = (runId: string) => {
    let ticks = 0;
    const tick = async () => {
      try {
        const next = await contextV2Api.getOutcome(runId);
        setOutcome(next);
        // `pending`: the file is in, its deferred extraction is not — keep watching it land.
        const finished = !["queued", "running"].includes(next.status) && !next.pending;
        if (finished || ++ticks > POLL_LIMIT) {
          if (pollRef.current) clearInterval(pollRef.current);
          setPhase("done");
        }
      } catch {
        // A transient read failure must not end the poll — the run is still going.
        if (++ticks > POLL_LIMIT && pollRef.current) clearInterval(pollRef.current);
      }
    };
    tick();
    pollRef.current = setInterval(tick, POLL_MS);
  };

  /** Steps this page can answer without navigating away from the result they refer to. */
  const handleStep = (step: OutcomeStep): boolean | void => {
    if (step.action === "extract_more") {
      setShowExtractor(true);
      return true;
    }
    if (step.action === "retry" && file) {
      ingest(false);
      return true;
    }
    if (step.action === "replace" && file) {
      ingest(lastBind.current, true);     // the same reading as before, forced to run again
      return true;
    }
  };

  return (
    <div className="max-w-3xl mx-auto">
      <PageHeader
        title="Add data"
        icon={Upload}
        description="Drop in files. They will be read, and you will be shown what came out."
        actions={(file || batch.length > 0) && <Button variant="secondary" size="sm" onClick={reset}>Start over</Button>}
      />

      {phase === "idle" && (
        <DropZone dragging={dragging} setDragging={setDragging} onPick={pick} />
      )}

      {phase === "reading" && (
        <Skeleton label={`Reading ${file?.name ?? "your file"}…`} />
      )}

      {error && (
        <div className="rounded-lg border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/50
                        px-3 py-2 text-sm text-red-800 dark:text-red-300 mb-4">
          {error}
        </div>
      )}

      {phase === "reviewing" && read && (
        <div className="space-y-4">
          <div>
            <h2 className="text-lg font-semibold text-stone-900 dark:text-stone-100">
              Here is how it would read {file?.name}
            </h2>
            <p className="text-sm text-stone-500 dark:text-stone-400 mt-0.5">
              {_sampleLine(read)} Nothing has been saved or ingested yet.
            </p>
          </div>

          {read.drafts.map((draft) => (
            <DraftCard key={draft.drafter} draft={draft} />
          ))}
          {pipelineDraft && <DraftCard draft={pipelineDraft} />}

          {read.drafts.length === 0 && !pipelineDraft && (
            <p className="text-sm text-stone-500 dark:text-stone-400">
              Nothing needed configuring for this one — it will be read with the defaults.
            </p>
          )}

          <PipelinePicker kinds={file ? [kindOf(file)] : []} value={pipelines} onChange={setPipelines} />

          <div>
            <label htmlFor="add-tags" className="block text-sm font-medium text-stone-800 dark:text-stone-200 mb-1">
              Tags <span className="font-normal text-stone-500 dark:text-stone-400">(optional)</span>
            </label>
            <TagInput id="add-tags" value={tags} onChange={setTags}
              placeholder={`Untagged, it is filed under “${_extension(file)}”`} />
            <p className="text-xs text-stone-400 dark:text-stone-500 mt-1">
              The graph groups assets by tag. You can change them later on the asset&apos;s page.
            </p>
          </div>

          <div className="flex flex-wrap gap-2 pt-1">
            <Button variant="primary" icon={FileUp} busy={busy === "bind"}
                    disabled={!!busy} onClick={() => ingest(true)}>
              Use this reading and add it
            </Button>
            <Button variant="secondary" busy={busy === "default"} disabled={!!busy}
                    onClick={() => ingest(false)}>
              {Object.values(pipelines).some(Boolean) ? "Add it with the chosen pipeline" : "Just add it with the defaults"}
            </Button>
          </div>
          <p className="text-xs text-stone-400 dark:text-stone-500">
            Either works. The defaults already read every field — saving the reading above only
            makes it explicit, reusable, and something you can correct later.
          </p>
        </div>
      )}

      {phase === "batch" && (
        <BatchAdd files={batch} setFiles={setBatch} pipelines={pipelines} setPipelines={setPipelines}
                  tags={tags} setTags={setTags} />
      )}

      {(phase === "ingesting" || phase === "done") && (
        <div className="space-y-4">
          {outcome
            ? <IngestOutcomeCard outcome={outcome} onStep={handleStep} />
            : <Skeleton label="Starting…" />}

          {showExtractor && (
            <div className="pt-2">
              <GoalExtractor content={sampleText} filename={file?.name ?? ""} />
            </div>
          )}

          {phase === "done" && !showExtractor && (
            <button
              onClick={() => setShowExtractor(true)}
              className="inline-flex items-center gap-1.5 text-sm font-medium text-primary hover:underline"
            >
              <Sparkles size={15} /> Pull something else out of this
            </button>
          )}
        </div>
      )}
    </div>
  );
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

type BatchRow = { name: string; runId?: string; status: string; nodes?: number; error?: string };

/** Several files at once: one batch, the pipeline per kind, then each file's run as it lands. */
function BatchAdd({ files, setFiles, pipelines, setPipelines, tags, setTags }: {
  files: File[];
  setFiles: (f: File[]) => void;
  pipelines: PipelineChoice;
  setPipelines: (v: PipelineChoice) => void;
  tags: string[];
  setTags: (t: string[]) => void;
}) {
  const [rows, setRows] = useState<BatchRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const finished = (r: BatchRow) => !!r.error || (!!r.runId && !["queued", "running", "uploading"].includes(r.status));

  const add = async () => {
    setBusy(true);
    const batchId = crypto.randomUUID();       // the files land as ONE ingestion
    const out: BatchRow[] = files.map((f) => ({ name: f.name, status: "uploading" }));
    setRows([...out]);
    for (let i = 0; i < files.length; i++) {
      try {
        const res = await contextV2Api.addSource(files[i], { pipelineIds: pipelines, tags, batchId });
        out[i] = res.run_id ? { ...out[i], runId: res.run_id, status: "queued" }
                            : { ...out[i], status: "failed", error: "accepted, but no run was recorded" };
      } catch (e: any) {
        out[i] = { ...out[i], status: "failed", error: e?.response?.data?.detail || "upload failed" };
      }
      setRows([...out]);
    }
    setBusy(false);
    let ticks = 0;
    const tick = async () => {
      await Promise.all(out.map(async (r, i) => {
        if (!r.runId || finished(r)) return;
        try {
          const o = await contextV2Api.getOutcome(r.runId);
          const status = o.pending ? "running" : o.status;
          out[i] = { ...r, status, nodes: o.total_nodes, error: status === "failed" ? (o.error || "failed") : undefined };
        } catch { /* a transient read failure must not end the poll */ }
      }));
      setRows([...out]);
      if (out.every(finished) || ++ticks > POLL_LIMIT) {
        if (pollRef.current) clearInterval(pollRef.current);
      }
    };
    pollRef.current = setInterval(tick, POLL_MS);
    tick();
  };

  if (rows) {
    const done = rows.filter(finished).length;
    const failed = rows.filter((r) => r.error).length;
    return (
      <div className="space-y-3">
        <h2 className="text-lg font-semibold text-stone-900 dark:text-stone-100">
          {done < rows.length ? `Adding ${rows.length} files…` : `Added ${rows.length - failed} of ${rows.length} files`}
        </h2>
        <ul className="rounded-xl border border-stone-200 dark:border-stone-800 divide-y divide-stone-100 dark:divide-stone-800 bg-white dark:bg-stone-900">
          {rows.map((r) => (
            <li key={r.name} className="flex items-center gap-2 px-3 py-2 text-sm">
              {r.error ? <XCircle size={15} className="text-red-500 shrink-0" />
                : finished(r) ? <CheckCircle2 size={15} className="text-emerald-500 shrink-0" />
                : <Loader2 size={15} className="animate-spin text-stone-400 shrink-0" />}
              <span className="truncate text-stone-800 dark:text-stone-200">{r.name}</span>
              <span className="ml-auto text-xs text-stone-500 dark:text-stone-400 shrink-0">
                {r.error ? r.error : finished(r) ? `${r.status}${r.nodes ? ` · ${r.nodes} nodes` : ""}` : r.status}
              </span>
            </li>
          ))}
        </ul>
        {done === rows.length && (
          <p className="text-sm text-stone-500 dark:text-stone-400">
            See them in <Link href="/dashboard/catalogue" className="text-primary hover:underline">Assets</Link>,
            or each run in <Link href="/dashboard/progress" className="text-primary hover:underline">Runs</Link>.
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold text-stone-900 dark:text-stone-100">{files.length} files to add</h2>
      <ul className="rounded-xl border border-stone-200 dark:border-stone-800 divide-y divide-stone-100 dark:divide-stone-800 bg-white dark:bg-stone-900">
        {files.map((f) => (
          <li key={f.name} className="flex items-center gap-2 px-3 py-2 text-sm">
            <span className="truncate text-stone-800 dark:text-stone-200">{f.name}</span>
            <span className="ml-auto text-xs text-stone-400 shrink-0">{KINDS.find((k) => k.key === kindOf(f))?.label}</span>
            <button aria-label={`Remove ${f.name}`} onClick={() => setFiles(files.filter((x) => x !== f))}
                    className="p-1 text-stone-400 hover:text-red-600"><X size={14} /></button>
          </li>
        ))}
      </ul>
      <PipelinePicker kinds={Array.from(new Set(files.map(kindOf)))} value={pipelines} onChange={setPipelines} />
      <div>
        <label htmlFor="batch-tags" className="block text-sm font-medium text-stone-800 dark:text-stone-200 mb-1">
          Tags <span className="font-normal text-stone-500 dark:text-stone-400">(optional, for every file)</span>
        </label>
        <TagInput id="batch-tags" value={tags} onChange={setTags} placeholder="Untagged, each is filed under its type" />
      </div>
      <Button variant="primary" icon={FileUp} busy={busy} disabled={busy || files.length === 0} onClick={add}>
        Add {files.length} files
      </Button>
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

/**
 * "Use this reading" accepts the reading as drafted — including the entity types it only suggested.
 *
 * The drafter marks the types it named itself (`Project`, `Risk`, …) as `auto_type`, and the engine
 * deliberately keeps an unaccepted suggestion as a label on a generic `KeyNode`. That is right for a
 * mapping nobody has looked at, but this button IS the person looking at it and saying yes: left as
 * suggestions, the saved mapping showed Project and Risk while the graph got 26 KeyNodes.
 */
function acceptSuggestedTypes(draft: Draft): Draft {
  const spec = structuredClone(draft.payload);
  for (const structure of spec?.payload?.structures ?? []) {
    for (const key of structure?.keys ?? []) {
      if (key?.node_type && key.auto_type) delete key.auto_type;
    }
  }
  return { ...draft, payload: spec };
}

/** The tag an untagged file gets (its extension) — what the server falls back to. */
function _extension(file: File | null): string {
  const ext = (file?.name.split(".").pop() || "").toLowerCase();
  return file && ext && ext !== file.name.toLowerCase() ? ext : "its type";
}

/** One line describing what was actually looked at, because a wrong draft's first question is why. */
function _sampleLine(read: DraftResponse): string {
  const s = read.sample || {};
  const bits: string[] = [];
  if (s.format) bits.push(`Read as ${s.format}`);
  if (s.records) bits.push(`${s.records} record${s.records === 1 ? "" : "s"} sampled`);
  else if (s.columns?.length) bits.push(`${s.columns.length} columns`);
  else if (s.text_chars) bits.push(`${s.text_chars.toLocaleString()} characters of text`);
  if (s.truncated) bits.push("from the start of the file");
  return bits.length ? `${bits.join(", ")}.` : "";
}
