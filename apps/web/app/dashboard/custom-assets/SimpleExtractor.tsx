"use client";

// The short extractor form: a name, what it reads, the fields to pull out (each a name and what to
// look for), where the results go, and a sample to try it on. It writes the same draft as the full
// editor (useExtractorDraft), so "Full editor" opens this very extractor with everything the form
// set; the full editor is where Python, patterns, one-prompt mode, conditions and resolution live.
import { useEffect, useState } from "react";
import { Plus, Save, Trash2, X } from "lucide-react";
import Button from "@/components/ui/Button";
import { contextV2Api, type MappingAsset, type SemanticLayer } from "@/lib/api/contextV2";
import type { ExtractorDraft } from "./useExtractorDraft";
import { inputCls } from "./ui";

// "What to look for" is the field's prompt; the text it reads is appended for the model and hidden here.
const TEXT = "\nAnswer with the value only, or nothing if the text does not say.\n\nTEXT:\n{text}";
const lookFor = (prompt?: string) => (prompt || "").replace(TEXT, "");

/** Whether an extractor is one this form can show without hiding part of it. */
export function fitsSimple(d: ExtractorDraft): boolean {
  if (d.editingBuiltin || d.sourceMode === "independent") return false;
  if (d.mechRef === "unstructured.ner") return d.emitNodes.length <= 1 && !d.emitAttrs.length;
  if (!["unstructured.llm", "unstructured.attributes"].includes(d.mechRef) || d.mechPrompt.trim()) return false;
  if (d.emitNodes.length > 1 || d.emitEdges.length > 1 || d.emitAttrsRest.length) return false;
  if (d.emitNodes.length && d.emitAttrs.length) return false;
  if (d.emitEdges.some((e: any) => e.mode === "relate.llm" || e.when || e.from === "match_on" || e.to === "match_on")) return false;
  return Object.values(d.attrOverrides).every((o: any) =>
    (!o?.mechanism || o.mechanism === "llm") && (!o?.prompt || o.prompt.endsWith(TEXT)));
}

/** Named things, one node each (merged by name across every text), linked from the text they are in,
 *  and to each other by the verb of the sentence that connects them ("Alice -JOIN-> Acme"). */
function applyNamed(d: ExtractorDraft) {
  d.setMechRef("unstructured.ner"); d.setMechPrompt("");
  [...d.emitAttrs.map((a: any) => a.key), ...(d.emitNodes[0]?.attributes || [])].forEach((k: string) => d.removeAttr(k));
  d.setEmitMode("node");
  d.emitNodes.slice(1).forEach(() => d.removeNode(1));
  if (!d.emitNodes.length) d.addNode();
  // each find is a node of the type the model names it, identified by type and name: the same book
  // found by any extractor in any file is one node
  d.setNode(0, { type: "$type", id: "", dedup: true, name: "$name", attributes: [] });
  d.setEmitEdges([
    { type: "MENTIONS", from: "source", to: "emitted" },
    // the object by name: the same node an entity of that name is, created if not seen yet
    { type: "$relation", from: "emitted", to: { resolve: "entity", name: "$object", type: "$object_type" }, when: "$relation" },
  ]);
}

export default function SimpleExtractor({ d, onFull }: { d: ExtractorDraft; onFull: () => void }) {
  const [files, setFiles] = useState<MappingAsset[]>([]);
  const [layer, setLayer] = useState<SemanticLayer | null>(null);
  useEffect(() => {
    contextV2Api.mappingAssets().then(setFiles).catch(() => setFiles([]));
    contextV2Api.knowledgeLayer().then(setLayer).catch(() => setLayer(null));
  }, []);
  const reads = d.sourceMode === "data" && d.dataInput.type ? "type" : d.sourceMode;
  const typeFields = (layer?.types || []).find((t) => t.name === d.dataInput.type)?.properties || [];

  const named = d.mechRef === "unstructured.ner";
  const records = d.emitMode === "node";
  const target = records ? (d.emitNodes[0]?.type || "") : "__source";
  const keys: string[] = records ? (d.emitNodes[0]?.attributes || []) : d.emitAttrs.map((a: any) => a.key);
  const edge = d.emitEdges[0];

  // where the results go; switching moves nothing, so it clears the fields first
  const setRecords = (on: boolean) => {
    if (on === records) return;
    if (keys.length && !confirm("Switching clears the fields listed. Continue?")) return;
    keys.forEach((k) => d.removeAttr(k));
    d.setEmitMode(on ? "node" : "source");
    if (on) {
      if (!d.emitNodes.length) d.addNode();
      d.setNode(0, { type: "Finding", id: "" });
      d.setEmitEdges([{ type: "MENTIONS", from: "source", to: "emitted" }]);
    } else {
      d.emitNodes.forEach(() => d.removeNode(0));
      d.setEmitEdges([]);
    }
  };

  const addField = () => d.addAttrToNode(target || "Finding");
  const results = named
    ? Array.from(new Map((d.preview?.nodes || []).map((n: any) => [n.label, n])).values()).map((n: any) => ({ title: `${n.label} (${(n.attributes || []).find((a: any) => a.key === "type")?.value || n.type})`, attrs: [] }))
      .concat((d.preview?.edges || []).filter((e: any) => e.type && e.type !== "MENTIONS")
        .map((e: any) => ({ title: `${e.from} -${e.type}-> ${e.to}`, attrs: [] })))
    : records
    ? (d.preview?.nodes || []).map((n: any) => ({ title: n.label || n.type, attrs: n.attributes || [] }))
    : d.preview?.source_attributes?.length ? [{ title: "", attrs: d.preview.source_attributes }] : [];

  return (
    <div className="max-w-3xl space-y-4">
      <div className="flex items-center gap-2">
        <input value={d.extName} onChange={(e) => d.setExtName(e.target.value)} aria-label="Extractor name"
          placeholder="Name, e.g. invoice_details" className={`${inputCls} flex-1 text-base font-medium`} />
        <button type="button" onClick={onFull} className="text-sm text-stone-500 hover:text-stone-900 whitespace-nowrap">Full editor</button>
        <button type="button" onClick={() => d.setIsAdding(false)} aria-label="Close" className="p-2 text-stone-400 hover:text-stone-700"><X size={18} /></button>
      </div>

      <section className="bg-white border border-stone-200 rounded-xl p-4 space-y-3 text-[15px] text-stone-700">
        <div className="flex flex-wrap items-center gap-2">
          Read
          <select className={`${inputCls} w-auto`} aria-label="What it reads" value={reads}
            onChange={(e) => {
              const v = e.target.value;
              d.setSourceMode(v === "type" ? "data" : v as any);
              d.setDataInput({ asset: "", field: "", type: v === "type" ? (layer?.types?.[0]?.name || "") : "" });
            }}>
            <option value="dependent">the text it is given (documents, passages)</option>
            <option value="data">a field of a file</option>
            <option value="type">a field of each record of a type</option>
          </select>
          {reads === "type" && (<>
            <select className={`${inputCls} w-auto`} aria-label="Type" value={d.dataInput.type}
              onChange={(e) => d.setDataInput({ asset: "", field: "", type: e.target.value })}>
              {(layer?.types || []).map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
            </select>
            <select className={`${inputCls} w-auto`} aria-label="Field" value={d.dataInput.field}
              onChange={(e) => d.setDataInput({ ...d.dataInput, field: e.target.value })}>
              <option value="">all its fields</option>
              {typeFields.map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}
            </select>
          </>)}
          {reads === "data" && (<>
            <select className={`${inputCls} w-auto`} aria-label="File" value={d.dataInput.asset}
              onChange={(e) => d.setDataInput({ ...d.dataInput, asset: e.target.value, type: "" })}>
              <option value="">file…</option>
              {files.map((f) => <option key={f.id} value={f.name}>{f.name}</option>)}
            </select>
            <input className={`${inputCls} w-40`} aria-label="Field" placeholder="field, e.g. notes" value={d.dataInput.field}
              onChange={(e) => d.setDataInput({ ...d.dataInput, field: e.target.value, type: "" })} />
          </>)}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          Find with
          <select className={`${inputCls} w-auto`} aria-label="Find with" value={named ? "named" : "model"}
            onChange={(e) => {
              if (e.target.value === "named") applyNamed(d);
              else { d.setMechRef("unstructured.llm"); d.emitNodes.forEach(() => d.removeNode(0)); d.setEmitEdges([]); d.setEmitMode("source"); }
            }}>
            <option value="model">the model, field by field</option>
            <option value="named">people, places, things and how they connect (the model)</option>
          </select>
        </div>

        {named ? (
          <div className="flex flex-wrap items-center gap-2">
            each saved as what it is (Person, Org, Place…), linked as
            <input className={`${inputCls} w-32 font-mono text-xs`} aria-label="Link name" value={edge?.type || ""}
              onChange={(e) => d.setEmitEdges([{ ...edge, type: e.target.value.toUpperCase() }, ...d.emitEdges.slice(1)])} />
            <span className="text-stone-400 text-sm">and to each other by the sentence&apos;s verb</span>
          </div>
        ) : (
        <div className="flex flex-wrap items-center gap-2">
          and
          <select className={`${inputCls} w-auto`} aria-label="Where results go" value={records ? "node" : "source"}
            onChange={(e) => setRecords(e.target.value === "node")}>
            <option value="source">add what it finds to what it read</option>
            <option value="node">make each find a record</option>
          </select>
          {records && (<>
            called
            <input className={`${inputCls} w-36`} aria-label="Record type" value={d.emitNodes[0]?.type || ""} placeholder="Invoice"
              onChange={(e) => d.setNode(0, { type: e.target.value })} />
            linked as
            <input className={`${inputCls} w-32 font-mono text-xs`} aria-label="Link name" value={edge?.type || ""} placeholder="MENTIONS"
              onChange={(e) => d.setEmitEdges([{ ...(edge || { from: "source", to: "emitted" }), type: e.target.value.toUpperCase() }])} />
          </>)}
        </div>
        )}
      </section>

      {!named && <section className="bg-white border border-stone-200 rounded-xl overflow-hidden">
        <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)_32px] gap-2 px-4 py-2 text-xs font-semibold text-stone-500 bg-stone-50 border-b border-stone-100">
          <span>Field</span><span>What to look for</span><span />
        </div>
        {keys.map((k) => (
          <div key={k} className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)_32px] gap-2 px-4 py-2 border-b border-stone-100 items-start">
            <input defaultValue={k} aria-label="Field name" className={`${inputCls} font-mono text-xs`}
              onBlur={(e) => d.renameAttr(target, k, e.target.value.trim().replace(/\s+/g, "_"))} />
            <input value={lookFor(d.attrOverrides[k]?.prompt)} aria-label={`What to look for in ${k}`}
              placeholder="e.g. the total amount due, as a number"
              onChange={(e) => d.setAttrOv(k, { mechanism: "llm", prompt: e.target.value ? e.target.value + TEXT : "" })}
              className={`${inputCls} text-sm`} />
            <button type="button" onClick={() => d.removeAttr(k)} aria-label={`Remove ${k}`} className="p-1.5 text-stone-400 hover:text-rose-600"><Trash2 size={14} /></button>
          </div>
        ))}
        <div className="flex flex-wrap items-center gap-3 px-4 py-2.5">
          <button type="button" onClick={addField} disabled={records && !target}
            className="text-sm font-medium text-primary hover:underline flex items-center gap-1 disabled:text-stone-300"><Plus size={14} /> Field</button>
          {records && keys.length > 0 && (
            <label className="flex items-center gap-1.5 text-sm text-stone-600">
              one record per
              <select className={`${inputCls} w-auto text-sm`} value={d.emitNodes[0]?.id || ""} onChange={(e) => d.setNode(0, { id: e.target.value })}>
                <option value="">find</option>
                {keys.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </label>
          )}
          <label className="ml-auto flex items-center gap-1.5 text-sm text-stone-600">
            Model
            <select value={d.mechModel} onChange={(e) => d.setMechModel(e.target.value)} className={`${inputCls} w-auto text-sm`}>
              <option value="">default</option>
              {d.genModels.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
          </label>
        </div>
      </section>}

      <section className="bg-white border border-stone-200 rounded-xl p-4 space-y-2">
        <textarea value={d.sampleText} onChange={(e) => d.setSampleText(e.target.value)} aria-label="Sample"
          placeholder="Paste a sample to try it on" className={`${inputCls} h-24 text-sm resize-y`} />
        {d.previewing && <p className="text-xs text-stone-400 animate-pulse">trying…</p>}
        {d.previewErr && <p className="text-xs text-rose-700">{d.previewErr}</p>}
        {(d.preview?.warnings || []).map((w: string, i: number) => <p key={i} className="text-xs text-amber-800">{w}</p>)}
        {results.map((r: any, i: number) => (
          <div key={i} className="rounded-lg border border-stone-100 text-sm">
            {r.title && <div className="px-3 py-1 bg-stone-50 text-xs font-semibold text-stone-600">{r.title}</div>}
            {r.attrs.map((a: any) => (
              <div key={a.key} className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-2 px-3 py-1 border-t border-stone-100">
                <span className="font-mono text-xs text-stone-500">{a.key}</span>
                <span className={a.filled ? "text-stone-900" : "text-stone-400 italic"}>{a.filled ? String(a.value) : "not found"}</span>
              </div>
            ))}
          </div>
        ))}
      </section>

      <div className="flex justify-end">
        <Button variant="primary" icon={Save} onClick={d.handleSaveExtraction} disabled={!d.extName.trim() || (!named && !keys.length)}>Save</Button>
      </div>
    </div>
  );
}
