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
  type UploadBatch,
} from "@/lib/api/contextV2";
import { uploadAll } from "@/lib/uploadQueue";

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
//: After this many quick polls a run is waiting behind other work (a folder dropped earlier is read
//: first), so it is asked about less often, never given up on: the view only says done when it is.
const POLL_FAST_TICKS = 150;
const POLL_SLOW_MS = 15_000;

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
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (pollRef.current) clearTimeout(pollRef.current); }, []);

  const reset = () => {
    if (pollRef.current) clearTimeout(pollRef.current);
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
      let finished = false;
      try {
        const next = await contextV2Api.getOutcome(runId);
        setOutcome(next);
        // `pending`: the file is in, its deferred extraction is not — keep watching it land.
        finished = !["queued", "running"].includes(next.status) && !next.pending;
        if (finished) setPhase("done");
      } catch {
        // A transient read failure must not end the poll — the run is still going.
      }
      if (!finished) pollRef.current = setTimeout(tick, ++ticks < POLL_FAST_TICKS ? POLL_MS : POLL_SLOW_MS);
    };
    tick();
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

type Failure = { name: string; error: string };

/** How often a batch's progress is read: one request for the whole batch, however many files. */
const BATCH_POLL_MS = 4_000;

/**
 * Several files at once: one batch, the pipeline per kind.
 *
 * The files go up a few at a time with retries (lib/uploadQueue), and the batch's progress is one
 * request for all of it. It used to send them one after another, count any error as a lost file, and
 * then ask about every file separately every two seconds — a thousand requests a poll for a thousand
 * files — and stop asking after five minutes, while reading them takes hours.
 */
function BatchAdd({ files, setFiles, pipelines, setPipelines, tags, setTags }: {
  files: File[];
  setFiles: (f: File[]) => void;
  pipelines: PipelineChoice;
  setPipelines: (v: PipelineChoice) => void;
  tags: string[];
  setTags: (t: string[]) => void;
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
      const res = await contextV2Api.addSource(files[i], { pipelineIds: pipelines, tags, batchId });
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

  if (sent) {
    const counts = batch?.counts || {};
    const read = batch?.done ?? 0;
    const failures = [...sent.failed,
      ...(batch?.failed || []).map((f) => ({ name: f.asset, error: f.error || "failed" }))];
    const finished = !busy && !!batch && batch.total >= accepted && read >= batch.total;
    const pct = (n: number) => `${Math.round((100 * n) / Math.max(1, files.length))}%`;
    return (
      <div className="space-y-4">
        <h2 className="text-lg font-semibold text-stone-900 dark:text-stone-100 flex items-center gap-2">
          {finished ? <CheckCircle2 size={18} className="text-emerald-500" />
                    : <Loader2 size={18} className="animate-spin text-stone-400" />}
          {busy ? `Sending ${sent.done} of ${files.length} files…`
            : finished ? `Added ${files.length - failures.length} of ${files.length} files`
            : `Reading ${read} of ${batch?.total ?? accepted} files…`}
        </h2>
        <div className="space-y-2" aria-label="Progress">
          {[["Sent", sent.done], ["Read", read]].map(([label, n]) => (
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
