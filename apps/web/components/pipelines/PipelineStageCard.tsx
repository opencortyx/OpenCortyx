"use client";

/**
 * One card for every processing step, of every pipeline kind.
 *
 * Stages 2..N were duplicated three times — the same "Input Source", the same execution rules, the
 * same delete button, drifting apart in three files. They were never actually different: a step
 * reads something the anchor (or an earlier step) produced, runs an extractor over it, and emits
 * nodes and edges. Whether the thing it reads came from a chunk, a table row or a JSON path is the
 * ANCHOR's business and stops mattering here.
 *
 * What genuinely varies is per STAGE TYPE, not per pipeline kind — a `relate` step needs an edge to
 * traverse whether its input came from SQL or from prose. So type-specific fields arrive as
 * `children` and can be reused across kinds, which is the whole reason the three pages collapse.
 */

import { Trash2, Database, GripVertical, ChevronUp, ChevronDown } from "lucide-react";
import type { PipelineStage } from "./model";

export interface StageOutput { nodes: { type: string; attrs: string[] }[]; edges: string[] }
export interface SourceOption { value: string; label: string }

export const stageName = (t?: string, fallback?: string) =>
  fallback || String(t || "step").replace(/_/g, " ");

const field =
  "w-full px-2.5 py-1.5 rounded-md border border-stone-200 bg-white text-[12.5px] text-stone-800 " +
  "focus:outline-none focus:ring-2 focus:ring-primary/25 focus:border-primary/40";
const Label = ({ children, hint }: { children: React.ReactNode; hint?: string }) => (
  <label className="block text-[10px] font-semibold uppercase tracking-wider text-stone-500 mb-1">
    {children}{hint && <span className="ml-1 normal-case font-normal text-stone-400">{hint}</span>}
  </label>
);

export default function PipelineStageCard({
  stage, index, total, number, produces, sourceOptions, typeLabel,
  onRemove, onMove, onChange, onConfig, children,
}: {
  stage: PipelineStage;
  index: number;
  total: number;
  /** displayed step number — the anchor is 1, so these start at 2 */
  number: number;
  produces?: StageOutput;
  sourceOptions: SourceOption[];
  typeLabel?: string;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
  onChange: (key: string, value: any) => void;
  onConfig: (key: string, value: any) => void;
  children?: React.ReactNode;
}) {
  const out = produces || { nodes: [], edges: [] };
  const cfg = stage.config || {};

  return (
    <div className="relative flex gap-3">
      <div className="flex flex-col items-center pt-5">
        <div className="w-7 h-7 rounded-full bg-stone-800 text-white flex items-center justify-center
                        text-[11px] font-bold z-10 shadow-sm">{number}</div>
        {index < total - 1 && <div className="w-0.5 bg-stone-200 flex-1 my-1" />}
      </div>

      <div className="flex-1 border border-stone-200 rounded-xl overflow-hidden bg-white">
        <div className="px-4 py-3 border-b border-stone-100 flex items-center gap-2.5 bg-stone-50/70">
          <div className="p-1.5 bg-white rounded border border-stone-100"><Database size={17} className="text-emerald-500" /></div>
          <h3 className="font-semibold text-[13.5px] text-stone-800">{stageName(stage.type, typeLabel)}</h3>
          <div className="ml-auto flex items-center gap-0.5">
            <button type="button" onClick={() => onMove(-1)} disabled={index === 0} title="Move earlier"
              className="p-1 text-stone-400 hover:text-stone-700 disabled:opacity-30"><ChevronUp size={15} /></button>
            <button type="button" onClick={() => onMove(1)} disabled={index === total - 1} title="Move later"
              className="p-1 text-stone-400 hover:text-stone-700 disabled:opacity-30"><ChevronDown size={15} /></button>
            <button type="button" onClick={onRemove} title="Remove step"
              className="p-1 text-stone-400 hover:text-red-600"><Trash2 size={15} /></button>
          </div>
        </div>

        <div className="p-4 space-y-3.5">
          {/* WHAT THIS STEP PRODUCES — above the inputs, because it is what the NEXT step can read. */}
          {(out.nodes.length > 0 || out.edges.length > 0) && (
            <div className="flex items-start gap-3">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-stone-500 w-20 pt-1 shrink-0">Produces</span>
              <div className="flex-1 flex flex-wrap items-center gap-1.5">
                {out.nodes.map((n) => (
                  <span key={n.type} className="inline-flex items-center gap-1 rounded-md border border-teal-200 bg-teal-50 px-2 py-1">
                    <span className="text-[11px] font-semibold text-teal-800">{n.type}</span>
                    {!!n.attrs.length && <span className="text-[10px] font-mono text-teal-600">{n.attrs.join(" · ")}</span>}
                  </span>
                ))}
                {out.edges.map((e) => (
                  <span key={e} className="rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-[10px] font-mono font-semibold text-amber-700">{e}</span>
                ))}
                <span className="text-[10px] text-stone-400 ml-0.5">available to later steps</span>
              </div>
            </div>
          )}

          <div className="flex items-center gap-3">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-stone-500 w-20 shrink-0">Reads</span>
            <select value={stage.source || ""} onChange={(e) => onChange("source", e.target.value)} className={field}>
              {sourceOptions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
          </div>

          {/* stage-type-specific fields — reusable across kinds, which is why they live outside */}
          {children}

          <details className="pt-3 border-t border-stone-100 group">
            <summary className="cursor-pointer list-none flex items-center gap-2 text-[11px] font-semibold
                                uppercase tracking-wider text-stone-500 hover:text-stone-700">
              <GripVertical size={12} className="text-stone-300" />
              Execution &amp; filtering rules
              <span className="ml-auto normal-case font-normal text-[10px] text-stone-400 group-open:hidden">
                {[cfg.include_keys && "include", cfg.exclude_keys && "exclude",
                  (cfg.min_words || cfg.max_words) && "length", cfg.min_confidence && "confidence",
                  cfg.hash_dedup && "dedup"].filter(Boolean).join(" · ") || "defaults"}
              </span>
            </summary>
            <div className="grid md:grid-cols-2 gap-3 mt-3">
              <div>
                <Label hint="(node:Type, attr:Name)">Include keys</Label>
                <input className={`${field} font-mono`} value={cfg.include_keys || ""}
                  placeholder="e.g. node:Incident, attr:content"
                  onChange={(e) => onConfig("include_keys", e.target.value)} />
              </div>
              <div>
                <Label hint="(PII / noise redaction)">Exclude keys</Label>
                <input className={`${field} font-mono`} value={cfg.exclude_keys || ""}
                  placeholder="e.g. attr:ip, attr:ssn, attr:user_agent"
                  onChange={(e) => onConfig("exclude_keys", e.target.value)} />
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <Label>Min words</Label>
                  <input type="number" className={field} value={cfg.min_words || ""} placeholder="Min words"
                    onChange={(e) => onConfig("min_words", e.target.value)} />
                </div>
                <div>
                  <Label>Max words</Label>
                  <input type="number" className={field} value={cfg.max_words || ""} placeholder="Max words"
                    onChange={(e) => onConfig("max_words", e.target.value)} />
                </div>
              </div>
              <div>
                <Label>Min confidence</Label>
                <input type="number" step="0.05" className={field} value={cfg.min_confidence || ""} placeholder="0.75"
                  onChange={(e) => onConfig("min_confidence", e.target.value)} />
              </div>
              <label className="md:col-span-2 flex items-center gap-2 text-[12px] text-stone-700">
                <input type="checkbox" className="accent-[hsl(var(--primary))]" checked={!!cfg.hash_dedup}
                  onChange={(e) => onConfig("hash_dedup", e.target.checked)} />
                Skip duplicate content hashes
              </label>
            </div>
          </details>
        </div>
      </div>
    </div>
  );
}
