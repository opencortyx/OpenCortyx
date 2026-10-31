"use client";
import Link from "next/link";
import {
  AlertCircle, ArrowRight, CheckCircle2, Loader2, MessageSquare, RotateCw,
  Sparkles, Workflow,
} from "lucide-react";
import type { IngestOutcome, OutcomeStep } from "@/lib/api/contextV2";

/**
 * What an ingestion produced — the answer to "did that work?", in the user's terms.
 *
 * The findings and the plumbing are shown differently on purpose. Someone who imported contracts
 * wants to hear *42 Vendors*; the 128 Chunks that made that possible are how it works, not what
 * it found. They are still shown, quietly, because "128 chunks and no entities" is itself the
 * answer when an extraction came back empty — the case this card exists to make visible instead
 * of letting it read as a green tick.
 *
 * `next_steps` come from the server, not from a menu here. The server is what knows whether this
 * run found entities, produced only scaffolding, or failed, and refinement is only worth offering
 * at the moment a user can judge whether it is needed — which is while they are looking at this.
 */

const STEP_ICON = {
  ask: MessageSquare,
  refine_mapping: Workflow,
  extract_more: Sparkles,
  open_asset: ArrowRight,
  retry: RotateCw,
  replace: RotateCw,
} as const;

export default function IngestOutcomeCard({
  outcome,
  onStep,
}: {
  outcome: IngestOutcome;
  /** Handles a step the page can do in place (extract_more, retry). Unhandled ones follow href. */
  onStep?: (step: OutcomeStep) => boolean | void;
}) {
  const running = outcome.status === "queued" || outcome.status === "running" || !!outcome.pending;
  const failed = outcome.status === "failed";
  const nothing = !running && outcome.found.length === 0;

  return (
    <div className="bg-white dark:bg-stone-900 rounded-xl border border-stone-200 dark:border-stone-800 shadow-xs overflow-hidden">
      <div className="p-4 flex items-start gap-3">
        <div className="mt-0.5 shrink-0">
          {running ? <Loader2 size={20} className="animate-spin text-stone-400" />
            : failed ? <AlertCircle size={20} className="text-red-600" />
            : nothing ? <AlertCircle size={20} className="text-amber-500" />
            : <CheckCircle2 size={20} className="text-emerald-600" />}
        </div>

        <div className="min-w-0 flex-1">
          <h3 className="font-semibold text-stone-900 dark:text-stone-100">{_headline(outcome)}</h3>
          <p className="text-sm text-stone-500 dark:text-stone-400 mt-0.5 truncate">
            {outcome.asset}
            {outcome.duration_seconds != null && ` · ${outcome.duration_seconds}s`}
          </p>
          {failed && outcome.error && (
            <p className="text-sm text-red-700 dark:text-red-400 mt-2 font-mono break-words">
              {outcome.error}
            </p>
          )}
        </div>
      </div>

      {outcome.found.length > 0 && (
        <div className="px-4 pb-4 flex flex-wrap gap-2">
          {outcome.found.map((row) => (
            <div key={row.type}
                 className="rounded-lg border border-stone-200 dark:border-stone-800
                            bg-stone-50 dark:bg-stone-800/40 px-3 py-2 min-w-[8rem]">
              <div className="text-lg font-semibold text-stone-900 dark:text-stone-100 tabular-nums">
                {row.count.toLocaleString()}
              </div>
              <div className="text-xs font-medium text-stone-600 dark:text-stone-400">{row.type}</div>
              {row.examples.length > 0 && (
                <div className="text-[11px] text-stone-400 dark:text-stone-500 truncate mt-0.5"
                     title={row.examples.join(", ")}>
                  {row.examples[0]}
                  {row.examples.length > 1 && ` +${row.examples.length - 1}`}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {outcome.structural.length > 0 && (
        // Quiet by design: this is how the ingest works, not what it found. It stays visible
        // because an empty `found` beside a large `structural` is the shape of a bad mapping.
        <div className="px-4 pb-3 text-xs text-stone-500 dark:text-stone-500">
          Plus the structure behind it:{" "}
          {outcome.structural.map((r) => `${r.count.toLocaleString()} ${r.type}`).join(" · ")}
        </div>
      )}

      {outcome.notes.map((note, i) => (
        <div key={i} className="px-4 pb-2 text-xs text-stone-500 dark:text-stone-500 flex gap-1.5">
          <AlertCircle size={13} className="shrink-0 mt-0.5" />
          <span>{note}</span>
        </div>
      ))}

      {outcome.next_steps.length > 0 && (
        <div className="border-t border-stone-200 dark:border-stone-800 divide-y divide-stone-100 dark:divide-stone-800">
          {outcome.next_steps.map((step, i) => (
            <StepRow key={i} step={step} onStep={onStep} />
          ))}
        </div>
      )}
    </div>
  );
}

function StepRow({ step, onStep }: { step: OutcomeStep; onStep?: (s: OutcomeStep) => boolean | void }) {
  const Icon = STEP_ICON[step.action] ?? ArrowRight;
  const body = (
    <>
      <Icon size={16} className="shrink-0 text-stone-400 dark:text-stone-500 mt-0.5" />
      <span className="min-w-0">
        <span className="block text-sm font-medium text-stone-800 dark:text-stone-200">{step.label}</span>
        <span className="block text-xs text-stone-500 dark:text-stone-400">{step.reason}</span>
      </span>
      <ArrowRight size={14} className="ml-auto shrink-0 text-stone-300 dark:text-stone-600 mt-1" />
    </>
  );
  const shell = "w-full flex items-start gap-2.5 px-4 py-3 text-left hover:bg-stone-50 dark:hover:bg-stone-800/60 transition-colors";

  // A page that can act on a step in place says so by returning true; anything else is a link.
  // Without that, "pull out something else" would navigate away from the result it refers to.
  if (onStep) {
    return <button className={shell} onClick={() => { if (onStep(step) !== true && step.href) window.location.href = _href(step); }}>{body}</button>;
  }
  return step.href
    // `as any`: typedRoutes cannot know a href the server chose at runtime. Matches how the
    // catalogue links to a node by id.
    ? <Link href={_href(step) as any} className={shell}>{body}</Link>
    : <div className={shell}>{body}</div>;
}

/** A suggested question rides along as a query param so the destination opens ready to run. */
function _href(step: OutcomeStep): string {
  const suggested = step.payload?.suggested;
  if (step.action === "ask" && suggested && step.href) {
    return `${step.href}?q=${encodeURIComponent(String(suggested))}`;
  }
  return step.href || "#";
}

function _headline(outcome: IngestOutcome): string {
  if (outcome.status === "queued") return "Waiting to start…";
  if (outcome.status === "running") return "Reading your file…";
  if (outcome.pending) {
    return outcome.found.length
      ? `Extracting… ${outcome.found.reduce((n, t) => n + t.count, 0)} found so far`
      : "Your file is in — reading what it holds into the graph…";
  }
  if (outcome.status === "failed") return "This one did not go through";
  if (outcome.status === "duplicate") return "Already had this one";
  if (outcome.found.length === 0) {
    return outcome.structural.length > 0
      ? "Read the file, but did not recognise anything in it"
      : "Finished, but nothing came out";
  }
  const parts = outcome.found.slice(0, 3)
    .map((r) => `${r.count.toLocaleString()} ${r.type}${r.count === 1 ? "" : "s"}`);
  const rest = outcome.found.length - parts.length;
  return `Found ${parts.join(", ")}${rest > 0 ? ` and ${rest} more kind${rest === 1 ? "" : "s"}` : ""}`;
}
