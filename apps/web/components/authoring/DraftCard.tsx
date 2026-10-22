"use client";
import { useState } from "react";
import {
  AlertTriangle, ArrowRight, Check, ChevronDown, ChevronRight,
  Database, ExternalLink, Layers, Network, Scissors,
  Sparkles,
} from "lucide-react";
import Button from "@/components/ui/Button";
import EvidenceTree from "./EvidenceTree";
import type { Draft, DraftConfidence } from "@/lib/api/contextV2";

/**
 * One drafted mapping, pipeline or extractor, and the reasons behind it.
 *
 * The evidence list is the point of this component, not decoration. A draft a user can only
 * accept or reject wholesale is a black box they have to trust; a draft that says *"`body` →
 * prose that gets read, because it averages 412 characters"* is one they can correct a single
 * line of. That is the difference between asking someone to review a reading of their data and
 * asking them to author one — which is the whole reason drafting exists.
 *
 * Warnings sit ABOVE the evidence and are never collapsed. They are the half a confident-looking
 * summary would otherwise hide: a sample too thin to generalise from, an extractor that found
 * nothing, a model that disagreed with the measurement. A draft that quietly looked fine and was
 * wrong is worse than no draft.
 */

const CONFIDENCE: Record<DraftConfidence, { label: string; tone: string; note: string }> = {
  high: {
    label: "Measured",
    tone: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:border-emerald-800",
    note: "Every decision below was read from your data.",
  },
  medium: {
    label: "Measured + reviewed",
    tone: "bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-800",
    note: "Read from your data, then refined by a model. Worth a look before saving.",
  },
  low: {
    label: "Needs your eyes",
    tone: "bg-amber-50 text-amber-800 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-800",
    note: "There was not enough to measure from. Read this one before saving.",
  },
};

export default function DraftCard({
  draft,
  onSave,
  onEdit,
  saving = false,
  saved = false,
  defaultOpen = false,
}: {
  draft: Draft;
  onSave?: (draft: Draft) => void;
  onEdit?: (draft: Draft) => void;
  saving?: boolean;
  saved?: boolean;
  /** Evidence starts open for a LOW-confidence draft regardless — that is when it matters most. */
  defaultOpen?: boolean;
}) {
  const grade = CONFIDENCE[draft.confidence] ?? CONFIDENCE.medium;
  const [open, setOpen] = useState(defaultOpen || draft.confidence === "low");
  const empty = !draft.payload || Object.keys(draft.payload).length === 0;

  return (
    <div className="bg-white dark:bg-stone-900 rounded-xl border border-stone-200 dark:border-stone-800 shadow-xs overflow-hidden">
      <div className="p-4 space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="font-semibold text-stone-900 dark:text-stone-100 truncate">{draft.name}</h3>
              <span className={`text-[11px] font-medium px-2 py-0.5 rounded-full border ${grade.tone}`}>
                {grade.label}
              </span>
            </div>
            {draft.summary && (
              <p className="text-sm text-stone-600 dark:text-stone-400 mt-1">{draft.summary}</p>
            )}
          </div>

          {!empty && (
            <div className="flex items-center gap-2 shrink-0">
              {onEdit && (
                <Button variant="secondary" size="sm" iconRight={ExternalLink}
                        onClick={() => onEdit(draft)}>
                  Change it
                </Button>
              )}
              {onSave && (
                <Button variant={saved ? "secondary" : "primary"} size="sm"
                        icon={saved ? Check : undefined} busy={saving} disabled={saved}
                        onClick={() => onSave(draft)}>
                  {saved ? "Saved" : "Use this"}
                </Button>
              )}
            </div>
          )}
        </div>

        {draft.warnings.map((warning, i) => (
          <div key={i}
               className="flex gap-2 text-sm rounded-lg border border-amber-200 dark:border-amber-900
                          bg-amber-50 dark:bg-amber-950/50 px-3 py-2 text-amber-900 dark:text-amber-200">
            <AlertTriangle size={15} className="shrink-0 mt-0.5" />
            <span>{warning}</span>
          </div>
        ))}

        {!empty && <p className="text-xs text-stone-500 dark:text-stone-500">{grade.note}</p>}
      </div>

      {draft.kind === "pipeline" && Array.isArray(draft.payload?.stages) && draft.payload.stages.length > 0 && (
        <PipelineFlowView stages={draft.payload.stages} />
      )}

      {draft.evidence.length > 0 && (
        <>
          <button
            onClick={() => setOpen(!open)}
            aria-expanded={open}
            className="w-full flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium
                       text-stone-700 dark:text-stone-300 border-t border-stone-200 dark:border-stone-800
                       hover:bg-stone-50 dark:hover:bg-stone-800/60 transition-colors"
          >
            {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            How it read your data
            <span className="text-stone-400 dark:text-stone-500 font-normal">
              ({draft.evidence.length})
            </span>
          </button>

          {open && (
            <EvidenceTree
              evidence={draft.evidence}
              defaultEntityName={
                draft.name.includes("→") ? draft.name.split("→")[1].trim() : undefined
              }
            />
          )}
        </>
      )}

      {draft.model && (
        <div className="px-4 py-2 border-t border-stone-100 dark:border-stone-800
                        text-xs text-stone-400 dark:text-stone-500 flex items-center gap-1.5">
          <Sparkles size={12} /> refined by {draft.model}
        </div>
      )}
    </div>
  );
}

function getStageInfo(stage: any, index: number) {
  const type = String(stage.type || stage.action || "").toLowerCase();
  if (type.includes("chunk")) {
    return {
      title: "Document Reader",
      // what the reader will actually do: layout unless something asks for sliding windows
      subtitle: /^sliding/.test(String(stage.config?.strategy || ""))
        ? "Sliding-window chunks" : "Layout: titles, sections, tables, lists",
      icon: Scissors,
      color: "bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-800",
    };
  }
  if (type.includes("schema") || type.includes("table")) {
    return {
      title: "Tabular Mapper",
      subtitle: "Rows → Entities, Columns → Props & FKs",
      icon: Database,
      color: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:border-emerald-800",
    };
  }
  if (type.includes("structure")) {
    return {
      title: "JSON Structure Walker",
      subtitle: "Nested Objects & Arrays → Graph",
      icon: Network,
      color: "bg-indigo-50 text-indigo-700 border-indigo-200 dark:bg-indigo-950 dark:text-indigo-300 dark:border-indigo-800",
    };
  }
  return {
    title: stage.name || stage.id || `Stage ${index + 1}`,
    subtitle: type || "Processing stage",
    icon: Layers,
    color: "bg-stone-50 text-stone-700 border-stone-200 dark:bg-stone-800 dark:text-stone-300 dark:border-stone-700",
  };
}

function PipelineFlowView({ stages }: { stages: any[] }) {
  return (
    <div className="px-4 py-3 border-t border-stone-200 dark:border-stone-800 bg-stone-50/60 dark:bg-stone-900/40">
      <div className="flex items-center justify-between mb-2.5">
        <span className="text-xs font-semibold text-stone-500 uppercase tracking-wider">
          Ingestion Stages ({stages.length})
        </span>
        <span className="text-[11px] text-stone-400">Sequential Execution</span>
      </div>
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2 overflow-x-auto pb-1">
        {stages.map((stage, i) => {
          const info = getStageInfo(stage, i);
          const Icon = info.icon;
          return (
            <div key={stage.id || i} className="flex sm:flex-row flex-col items-center gap-2 shrink-0">
              <div className="flex items-center gap-2.5 px-3 py-2 rounded-lg border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900/80 shadow-2xs min-w-[200px]">
                <div className={`p-1.5 rounded-md border ${info.color} shrink-0`}>
                  <Icon size={16} />
                </div>
                <div className="min-w-0">
                  <div className="text-xs font-medium text-stone-900 dark:text-stone-100 truncate">
                    {info.title}
                  </div>
                  <div className="text-[11px] text-stone-500 dark:text-stone-400 truncate">
                    {info.subtitle}
                  </div>
                </div>
              </div>
              {i < stages.length - 1 && (
                <ArrowRight size={14} className="text-stone-400 hidden sm:block shrink-0" />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
