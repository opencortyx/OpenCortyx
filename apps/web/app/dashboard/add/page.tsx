"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { FileUp, Sparkles, Upload } from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import TagInput from "@/components/ui/TagInput";
import DraftCard from "@/components/authoring/DraftCard";
import GoalExtractor from "@/components/authoring/GoalExtractor";
import IngestOutcomeCard from "@/components/authoring/IngestOutcomeCard";
import {
  contextV2Api, type Draft, type DraftResponse, type IngestOutcome, type OutcomeStep,
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

type Phase = "idle" | "reading" | "reviewing" | "ingesting" | "done";

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
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const reset = () => {
    if (pollRef.current) clearInterval(pollRef.current);
    setFile(null); setPhase("idle"); setRead(null); setPipelineDraft(null);
    setOutcome(null); setSampleText(""); setShowExtractor(false); setError(""); setTags([]);
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
      const res = await contextV2Api.addSource(file, { pipelineId, replace, tags });
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
        description="Drop in a file. It will be read, and you will be shown what came out."
        actions={file && <Button variant="secondary" size="sm" onClick={reset}>Start over</Button>}
      />

      {phase === "idle" && (
        <DropZone dragging={dragging} setDragging={setDragging} onPick={inspect} />
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
              Just add it with the defaults
            </Button>
          </div>
          <p className="text-xs text-stone-400 dark:text-stone-500">
            Either works. The defaults already read every field — saving the reading above only
            makes it explicit, reusable, and something you can correct later.
          </p>
        </div>
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
  onPick: (f: File) => void;
}) {
  return (
    <label
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        const dropped = e.dataTransfer.files?.[0];
        if (dropped) onPick(dropped);
      }}
      className={`block rounded-xl border-2 border-dashed px-6 py-14 text-center cursor-pointer
                  transition-colors ${dragging
                    ? "border-primary bg-primary/5"
                    : "border-stone-300 dark:border-stone-700 hover:border-stone-400 dark:hover:border-stone-600"}`}
    >
      <input type="file" className="sr-only"
             onChange={(e) => { const f = e.target.files?.[0]; if (f) onPick(f); }} />
      <Upload size={28} className="mx-auto text-stone-400 dark:text-stone-500" />
      <p className="mt-3 font-medium text-stone-800 dark:text-stone-200">
        Drop a file here, or click to choose one
      </p>
      <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">
        Documents, spreadsheets, JSON, CSV — it works out which is which.
      </p>
    </label>
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
