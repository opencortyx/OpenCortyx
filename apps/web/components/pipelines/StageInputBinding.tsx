"use client";

/**
 * What a step actually runs over: which node types, and which attributes on them.
 *
 * "Reads: the previous step" is only half the story. A step is bound to specific NODE TYPES and
 * specific ATTRIBUTES, because one extractor is meant to be reusable across structures — the same
 * custom extractor runs over a Turn in one pipeline and over an Incident in another, and the
 * binding is what tells it which.
 *
 * The warning is the point of the whole component. Three things on an extractor read a field off
 * the node it runs over — `emit.from_source`, a `copy` attribute override, and a `{placeholder}` in
 * the prompt — and each fails SILENTLY when the binding does not supply it. The step runs, reports
 * success, and leaves the attribute empty. Only this screen can know whether the binding is
 * sufficient, because only here are the extractor and the chosen nodes both in view.
 *
 * A `relate.*` extractor is the exception: it does not read attributes at all. It uses the bound
 * node as an ANCHOR, follows one edge type out of it, and connects what it finds. So it gets the
 * traversal controls instead — the edge, and the two endpoint types.
 */

import { AlertTriangle } from "lucide-react";
import type { PipelineStage, VocabNode } from "./model";
import { relateSpec, stageTakesNodes, unmetFields } from "./model";

const field =
  "px-2.5 py-1.5 rounded-md border border-stone-200 bg-white text-[12px] text-stone-800 " +
  "focus:outline-none focus:ring-2 focus:ring-primary/25 focus:border-primary/40";

export default function StageInputBinding({
  stage, vocab, templates, nodeTypeVocab, edgeVocab, onConfig,
}: {
  stage: PipelineStage;
  vocab: VocabNode[];
  templates: any[];
  nodeTypeVocab: string[];
  edgeVocab: string[];
  onConfig: (key: string, value: any) => void;
}) {
  const rel = relateSpec(stage.type, templates);
  const takesNodes = stageTakesNodes(stage.type);
  if (!takesNodes && !rel) return null;

  const bound: any[] = stage.config?.input_nodes || [];
  const unmet = unmetFields(stage, templates);

  const setNode = (nodeType: string, next: any) => {
    const rest = bound.filter((x) => x.node_type !== nodeType);
    onConfig("input_nodes", next ? [...rest, next] : rest);
  };

  return (
    <div className="pt-3 border-t border-stone-100 space-y-2.5">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-stone-500">
          Input — node types &amp; attributes
        </span>
        <span className="text-[10px] text-stone-400">
          {bound.length ? "runs over these nodes" : "empty = runs over the reading unit from step 1"}
        </span>
      </div>

      {unmet.length > 0 && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-2.5 py-2">
          <p className="text-[11px] text-amber-800 leading-relaxed flex gap-1.5">
            <AlertTriangle size={13} className="mt-0.5 shrink-0" />
            <span>
              <b className="font-semibold">
                This extractor reads {unmet.map((f, i) => (
                  <span key={String(f)}>{i > 0 ? ", " : ""}<code className="font-mono">{String(f)}</code></span>
                ))} off the node
              </b>, and nothing selected below supplies it. It will run, report success, and leave{" "}
              {unmet.length > 1 ? "those fields" : "that field"} empty. Tick{" "}
              {unmet.length > 1 ? "them" : "it"} on a node that has {unmet.length > 1 ? "them" : "it"}.
            </span>
          </p>
        </div>
      )}

      {takesNodes && (
        vocab.length === 0 ? (
          <p className="text-[11px] text-stone-400">
            No node types yet — bind a mapping in step 1, or add an earlier step that emits nodes.
          </p>
        ) : (
          <div className="space-y-1.5 max-h-[260px] overflow-auto pr-1">
            {vocab.map((nv) => {
              const picked = bound.find((x) => x.node_type === nv.node_type);
              return (
                <div key={nv.node_type}
                  className={`rounded-lg border px-2.5 py-1.5 ${picked ? "border-primary/40 bg-primary/5" : "border-stone-200"}`}>
                  <div className="flex items-center gap-2 flex-wrap">
                    <input type="checkbox" className="accent-[hsl(var(--primary))]" checked={!!picked}
                      onChange={() => setNode(nv.node_type, picked ? null : {
                        node_type: nv.node_type, attrs: [],
                        ...(nv.from_stage ? { from_stage: nv.from_stage } : {}),
                        ...(nv.from_extractor ? { from_extractor: nv.from_extractor } : {}),
                      })} />
                    <span className="text-[12px] font-semibold text-primary">{nv.node_type}</span>
                    {nv.origin === "step" && (
                      <span title="Created by an earlier step in this pipeline, not by the reading rule"
                        className="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider
                                   bg-amber-50 text-amber-700 border border-amber-200">
                        from an earlier step
                      </span>
                    )}
                    <span className="text-[10px] text-stone-400">{nv.attrs.length} attributes</span>
                  </div>
                  {picked && (
                    <div className="flex flex-wrap gap-1.5 mt-1.5 pl-6">
                      {nv.attrs.map((a) => {
                        const on = (picked.attrs || []).includes(a);
                        return (
                          <button key={a} type="button"
                            onClick={() => setNode(nv.node_type, {
                              ...picked, node_type: nv.node_type,
                              attrs: on ? picked.attrs.filter((x: string) => x !== a) : [...(picked.attrs || []), a],
                            })}
                            className={`px-2 py-0.5 rounded-md text-[10px] font-mono border transition-colors
                              ${on ? "bg-primary text-primary-foreground border-primary"
                                   : "bg-white text-stone-500 border-stone-200 hover:bg-stone-50"}`}>
                            {a}
                          </button>
                        );
                      })}
                      {!nv.attrs.length && <span className="text-[10px] text-stone-400">no attributes declared</span>}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )
      )}

      {rel && (
        <div className="rounded-lg border border-stone-200 bg-stone-50/70 px-3 py-2.5 space-y-2">
          <span className="block text-[10px] font-semibold uppercase tracking-wider text-stone-500">
            Relationship — what it traverses
          </span>
          <div className="flex items-center gap-2 flex-wrap text-[11px]">
            <span className="text-stone-500">Traverse edge</span>
            <input list={`via-${stage.id}`} className={`${field} font-mono w-44`}
              value={stage.config?.via ?? rel.via}
              onChange={(e) => onConfig("via", e.target.value.trim().toUpperCase())} />
            <datalist id={`via-${stage.id}`}>{edgeVocab.map((v) => <option key={v} value={v} />)}</datalist>
            <span className="text-stone-400">from the anchor node selected above</span>
          </div>
          <div className="flex items-center gap-2 flex-wrap text-[11px]">
            <span className="text-stone-500">Connect</span>
            <input list={`nt-${stage.id}`} className={`${field} font-mono w-36`}
              value={stage.config?.from_type ?? rel.from}
              onChange={(e) => onConfig("from_type", e.target.value.trim())} />
            <span className="text-stone-400">to</span>
            <input list={`nt-${stage.id}`} className={`${field} font-mono w-36`}
              value={stage.config?.to_type ?? rel.to}
              onChange={(e) => onConfig("to_type", e.target.value.trim())} />
            <datalist id={`nt-${stage.id}`}>{nodeTypeVocab.map((v) => <option key={v} value={v} />)}</datalist>
            <span className="text-stone-400">as</span>
            <input className={`${field} font-mono w-40 font-semibold text-primary`}
              value={stage.config?.relation ?? rel.relation}
              onChange={(e) => onConfig("relation", e.target.value.trim().toUpperCase())} />
          </div>
          <p className="text-[10.5px] text-stone-400">
            A relate step reads no attributes. It follows that edge out of the anchor and connects what it finds.
          </p>
        </div>
      )}
    </div>
  );
}
