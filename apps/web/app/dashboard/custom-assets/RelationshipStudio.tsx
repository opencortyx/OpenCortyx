"use client";

// Relationship editor (relate.* templates): connect two entities already attached to a shared
// anchor node. Same state and save as the old Relationship Builder, laid out as one sentence.
import { useEffect, useState } from "react";
import { X, Save } from "lucide-react";
import Button from "@/components/ui/Button";
import type { ExtractorDraft } from "./useExtractorDraft";
import { RELATE_MODES, VIA_EDGES } from "./extractorVocab";
import { Section, Choice, Field, inputCls } from "./ui";

// Plain titles for the three modes; the engine refs and their descriptions come from RELATE_MODES.
const MODE_TITLE: Record<string, string> = {
  "relate.llm": "Ask the model", "relate.fixed": "Every pair", "relate.regex": "Named together",
};

export default function RelationshipStudio({ d }: { d: ExtractorDraft }) {
  // Unsaved changes = the relationship exactly as it would be saved, compared with it on open.
  const specNow = JSON.stringify({ name: d.relName.trim(), source: { select: d.relAnchor }, mechanism: d.buildRelMechanism() });
  const [baseline] = useState(specNow);
  const dirty = specNow !== baseline;
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  useEffect(() => {
    if (!confirmDiscard) return;
    const t = setTimeout(() => setConfirmDiscard(false), 4000);
    return () => clearTimeout(t);
  }, [confirmDiscard]);
  useEffect(() => {
    if (!dirty) return;
    const h = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);
  const close = () => {
    if (dirty && !confirmDiscard) { setConfirmDiscard(true); return; }
    d.setIsAddingRel(false); d.resetRel();
  };
  // An anchor saved on a type the ontology no longer lists: show it rather than silently displaying
  // a different type than the one that will be saved.
  const anchorMissing = !!d.relAnchor && !d.availableNodes.includes(d.relAnchor);
  const typeOptions = (
    <>
      <option value="entity">any entity (entity)</option>
      {d.availableNodes.filter(n => n.toLowerCase() !== "entity").map(n => <option key={n} value={n}>{n}</option>)}
    </>
  );
  const sel = `${inputCls} w-auto inline-block text-sm py-1`;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-xs text-stone-400">Extractors / Relationships</div>
          <h2 className="font-mono text-lg font-semibold text-stone-800 [overflow-wrap:anywhere]">
            {d.relName ? d.relName.replace("builtin:", "") : d.relEditId ? "Edit relationship" : "New relationship"}
          </h2>
        </div>
        <button type="button" onClick={close} className="p-2 rounded-lg text-stone-400 hover:text-stone-700 hover:bg-stone-100" aria-label={confirmDiscard ? "Discard changes and close" : "Close editor"}>
          {confirmDiscard ? <span className="text-xs font-semibold text-rose-600 px-1">Discard changes?</span> : <X size={18} />}
        </button>
      </div>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px] items-start">
        <div className="space-y-4 min-w-0">
          <section className="bg-white border border-stone-200 rounded-xl px-5 py-4">
            <Field label="Name">
              <input value={d.relName} onChange={e => d.setRelName(e.target.value)} className={`${inputCls} font-mono`} placeholder="e.g. person_likes_food" />
            </Field>
          </section>

          <Section lane="rel" title="The connection" aside={<span className="text-xs text-stone-400">Draws relationships between what&apos;s already there — creates no new nodes.</span>}>
            <div className="text-[15px] leading-[2.6] text-stone-600">
              In each{" "}
              <select value={d.relAnchor} onChange={e => d.setRelAnchor(e.target.value)} className={sel} aria-label="Context anchor">
                {anchorMissing && <option value={d.relAnchor}>{d.relAnchor} (not in the ontology)</option>}
                {d.availableNodes.map(n => <option key={n} value={n}>{n}</option>)}
              </select>
              {" "}look at the entities attached to it via{" "}
              <input list="via-edges" value={d.relVia} onChange={e => d.setRelVia(e.target.value)} className={`${sel} font-mono w-44`} placeholder="MENTIONS" aria-label="Attached via edge" />
              <datalist id="via-edges">{VIA_EDGES.map(v => <option key={v} value={v} />)}</datalist>
              .<br />
              Connect{" "}
              <select value={d.relFrom} onChange={e => d.setRelFrom(e.target.value)} className={sel} aria-label="From type">{typeOptions}</select>
              {" "}
              <input value={d.relRelation} onChange={e => d.setRelRelation(e.target.value.toUpperCase().replace(/\s+/g, "_"))}
                className={`${sel} w-44 font-mono font-bold text-indigo-700 text-center`} placeholder="RELATION" aria-label="Relationship type" />
              {" "}
              <select value={d.relTo} onChange={e => d.setRelTo(e.target.value)} className={sel} aria-label="To type">{typeOptions}</select>
            </div>
            <label className="flex items-center gap-2 text-[13px] text-stone-600 cursor-pointer mt-1">
              <input type="checkbox" checked={d.relDirection === "both"} onChange={e => d.setRelDirection(e.target.checked ? "both" : "from_to")} className="accent-indigo-600" />
              Both ways (also draw To → From)
            </label>
            <p className="text-[12.5px] text-stone-500 mt-3 max-w-[70ch]">Document extractors produce the generic type <code>entity</code> — use it to relate any entities. A subtype (Person, Organization, …) narrows to entities of that kind. From and To can be the same.</p>
          </Section>

          <Section lane="logic" title="How to decide which pairs connect">
            <div className="grid sm:grid-cols-3 gap-2">
              {RELATE_MODES.map(m => (
                <Choice key={m.ref} on={d.relMode === m.ref} title={MODE_TITLE[m.ref] || m.label} desc={m.desc} tech={m.ref} onClick={() => d.setRelMode(m.ref)} />
              ))}
            </div>
            {d.relMode === "relate.llm" && (
              <div className="space-y-3 mt-3">
                <Field label="Instructions for the model">
                  <textarea value={d.relPrompt} onChange={e => d.setRelPrompt(e.target.value)} className={`${inputCls} h-20 resize-y`}
                    placeholder={`How should the model decide a "${d.relRelation || "RELATION"}" link? e.g. "Link a person to a food only if the passage says they ate, cooked, or liked it."`} />
                </Field>
                <Field label={<><span>Model</span><a href="/dashboard/models" className="text-[11px] font-semibold text-emerald-700 hover:underline">+ Register a model</a></>}
                  hint="Entities are referenced to the model by index, so duplicate names never collide.">
                  <select value={d.relModel} onChange={e => d.setRelModel(e.target.value)} className={inputCls}>
                    <option value="">Org default generation model</option>
                    {d.genModels.map((m) => <option key={m.id} value={m.id}>{m.name} · {m.provider}/{m.model_id}{m.is_default ? " (default)" : ""}</option>)}
                  </select>
                </Field>
              </div>
            )}
          </Section>
        </div>

        {/* Plain-English preview */}
        <aside className="bg-stone-900 rounded-xl p-4 text-sm text-stone-300 leading-relaxed space-y-3 xl:sticky xl:top-0" aria-live="polite">
          <p className="text-[10.5px] font-semibold uppercase tracking-wider text-emerald-300/80">What this does</p>
          <p>For each <b className="text-teal-300">{d.relAnchor}</b>, look at the <b className="text-indigo-300">{d.relFrom}</b> and <b className="text-indigo-300">{d.relTo}</b> entities attached to it via <code className="text-emerald-300">{d.relVia || "MENTIONS"}</code>.</p>
          <p>{d.relMode === "relate.llm" ? "Ask the model which pairs relate (using the chunk as context)," : d.relMode === "relate.fixed" ? "Connect every pair," : "Connect pairs whose names both appear in the text,"} then draw:</p>
          <p className="font-mono text-xs bg-stone-800 rounded-lg p-2.5 text-emerald-300">{d.relFrom} —{d.relRelation || "RELATES_TO"}→ {d.relTo}{d.relDirection === "both" ? "  (both ways)" : ""}</p>
        </aside>
      </div>

      <div className="sticky bottom-0 z-10 -mx-3 sm:-mx-4 lg:-mx-6 px-3 sm:px-4 lg:px-6 py-3 bg-white/90 backdrop-blur border-t border-stone-200 flex flex-wrap items-center gap-3">
        <span className="text-[13px] text-stone-600 flex flex-wrap items-center gap-x-2">
          <span className={`w-2 h-2 rounded-full ${dirty ? "bg-amber-500" : "bg-stone-300"}`} />
          <b className="font-semibold">{dirty ? "Unsaved changes" : "No changes"}</b>
          {!d.relName.trim() && <span className="text-rose-600">· needs a name to save</span>}
          {!d.relRelation.trim() && <span className="text-amber-700">· no relationship type — RELATES_TO will be used</span>}
        </span>
        <span className="flex-1" />
        <Button variant="ghost" onClick={close} className={confirmDiscard ? "!text-rose-600" : ""}>{confirmDiscard ? "Discard changes?" : "Cancel"}</Button>
        <Button variant="primary" icon={Save} onClick={d.handleSaveRelationship} disabled={!d.relName.trim()}>Save Relationship</Button>
      </div>
    </div>
  );
}
