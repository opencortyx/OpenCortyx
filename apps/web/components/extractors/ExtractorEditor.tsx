"use client";

/**
 * The one editor for an extractor: what it reads, how it finds things, what it creates, how that
 * connects, and - where the model names something - which names it may use. Try it beside it.
 */

import { useMemo, useState } from "react";
import { AlertTriangle, ArrowLeft, Save } from "lucide-react";
import Button from "@/components/ui/Button";
import { contextV2Api, type MappingAsset } from "@/lib/api/contextV2";
import { check, namesByModel, toTemplate, type ExtractorForm, type Method, type ReadsKind, type Vocabulary } from "@/lib/extractors/form";
import ConnectionsStep from "./ConnectionsStep";
import CreatesStep from "./CreatesStep";
import TryIt, { errorText } from "./TryIt";
import { Choice, Step, input, mono } from "./ui";

const VOCABULARY: Record<Vocabulary, { label: string; note: string }> = {
  known: { label: "Reuse the workspace's names, add new ones if needed",
           note: "It is shown the kinds and relationships the workspace already has and reuses them when they fit." },
  open: { label: "Any name it chooses", note: "It names things freely, in its own words." },
  only: { label: "Only names the workspace already has",
          note: "Anything it cannot name from the workspace's kinds and relationships (and the ones named here) is left out." },
};

export default function ExtractorEditor({ initial, taken, existingTypes, knownRelations, models, assets, onDone, onCancel }: {
  initial: ExtractorForm; taken: string[]; existingTypes: string[]; knownRelations: string[];
  models: { id: string; name: string }[]; assets: MappingAsset[];
  onDone: (text: string) => void; onCancel: () => void;
}) {
  const [form, setForm] = useState<ExtractorForm>(initial);
  const [issues, setIssues] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const set = (f: (prev: ExtractorForm) => ExtractorForm) => { setForm(f); setIssues([]); };
  const dirty = useMemo(() => JSON.stringify(form) !== JSON.stringify(initial), [form, initial]);

  const setMethod = (method: Method) => set((f) => {
    if (method === "model") return { ...f, method };
    // a pattern or code makes one kind, and only the model names things or decides connections:
    // connections to the kinds left behind go with them
    const kind = { ...(f.kinds.find((k) => !k.byModel) || f.kinds[0]), byModel: false, fieldsByModel: false };
    const kept = (e: string) => !e.startsWith("kind:") || e === `kind:${kind.type.trim()}`;
    return {
      ...f, method, kinds: [kind],
      conns: f.conns.map((c) => ({ ...c, byModel: false, how: "always" as const,
        from: c.from === "model" ? "any" : c.from, to: c.to === "model" ? "any" : c.to }))
        .filter((c) => kept(c.from) && kept(c.to)),
    };
  });
  const setReads = (patch: Partial<ExtractorForm["reads"]>) => set((f) => ({ ...f, reads: { ...f.reads, ...patch } }));

  const save = async () => {
    const c = check(form, taken);
    if (c.errors.length) { setIssues(c.errors); return; }
    setSaving(true);
    try {
      await contextV2Api.createExtractionTemplate({ ...toTemplate(form), id: form.id, new: !form.id });
      onDone(`Saved ${form.name.trim()}.`);
    } catch (e) { setIssues([errorText(e)]); } finally { setSaving(false); }
  };
  const cancel = () => { if (!dirty || window.confirm("Discard your changes to this extractor?")) onCancel(); };

  const r = form.reads;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" onClick={cancel} className="inline-flex items-center gap-1 text-sm text-stone-500 hover:text-stone-800">
          <ArrowLeft size={15} /> Extractors
        </button>
        <input value={form.name} onChange={(e) => set((f) => ({ ...f, name: e.target.value }))} placeholder="Name this extractor"
          aria-label="Extractor name" autoFocus={!form.id}
          className="min-w-0 flex-1 basis-60 rounded-md border border-transparent bg-transparent px-2 py-1 text-lg font-semibold text-stone-900 dark:text-stone-100 hover:border-stone-200 focus:border-primary focus:outline-none" />
        <Button onClick={cancel}>Cancel</Button>
        <Button variant="primary" icon={Save} busy={saving} onClick={save}>Save extractor</Button>
      </div>

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-4">
          <Step n={1} title="What it reads" hint="Not tied to a kind of file: attach it to any upload, sync or file and it reads what it is given.">
            <div role="radiogroup" aria-label="What it reads" className="grid gap-2 sm:grid-cols-3">
              {([["any", "Whatever it's given", "Document passages, table rows, JSON records"],
                 ["type", "Records of a type", "Customer, Turn… across every file"],
                 ["graph", "Nodes already in the graph", "Enrich or connect what is there"]] as [ReadsKind, string, string][]).map(([k, t, x]) => (
                <Choice key={k} on={r.kind === k} title={t} text={x} onClick={() => setReads({ kind: k })} />
              ))}
            </div>
            <div className="mt-3">
              {r.kind === "any" && (
                <label className="block text-sm text-stone-500">Only these <span className="text-stone-400">(optional)</span>
                  <input value={r.only} onChange={(e) => setReads({ only: e.target.value })} className={`${input} mt-1`}
                    placeholder="Columns, JSON keys or paths ($.party.name), or section headings, comma separated. Empty: everything" />
                </label>
              )}
              {r.kind === "type" && (
                <div className="grid gap-2 sm:grid-cols-2">
                  <label className="text-sm text-stone-500">Type
                    <input list="extractor-types" value={r.type} onChange={(e) => setReads({ type: e.target.value })} placeholder="Customer" className={`${input} ${mono} mt-1`} />
                  </label>
                  <label className="text-sm text-stone-500">One field of it <span className="text-stone-400">(optional)</span>
                    <input value={r.field} onChange={(e) => setReads({ field: e.target.value })} placeholder="notes" className={`${input} ${mono} mt-1`} />
                  </label>
                </div>
              )}
              {r.kind === "graph" && (
                <div className="grid gap-2 sm:grid-cols-2">
                  <label className="text-sm text-stone-500">Nodes of type
                    <input list="extractor-types" value={r.nodeType} onChange={(e) => setReads({ nodeType: e.target.value })} placeholder="Organization" className={`${input} ${mono} mt-1`} />
                  </label>
                  <label className="text-sm text-stone-500">Reading these fields <span className="text-stone-400">(optional)</span>
                    <input value={r.fields} onChange={(e) => setReads({ fields: e.target.value })} placeholder="name, description" className={`${input} ${mono} mt-1`} />
                  </label>
                </div>
              )}
              <datalist id="extractor-types">{existingTypes.map((t) => <option key={t} value={t} />)}</datalist>
            </div>
          </Step>

          <Step n={2} title="How it finds things" hint="Three ways. Everything else is a setting of one of them.">
            <div role="radiogroup" aria-label="How it finds things" className="grid gap-2 sm:grid-cols-3">
              <Choice on={form.method === "model"} title="Model" text="Describe it in words" onClick={() => setMethod("model")} />
              <Choice on={form.method === "pattern"} title="Pattern" text="Patterns or a word list" onClick={() => setMethod("pattern")} />
              <Choice on={form.method === "python"} title="Python" text="Your own code" onClick={() => setMethod("python")} />
            </div>
          </Step>

          <CreatesStep form={form} set={set} />
          <ConnectionsStep form={form} set={set} existingTypes={existingTypes} />

          {namesByModel(form) && (
            <Step n="M" tone="model" title="When the model names something" hint="Applies to every name above set to “The model names it”.">
              <select value={form.vocabulary} aria-label="Names the model may use" className={`${input} max-w-md`}
                onChange={(e) => set((f) => ({ ...f, vocabulary: e.target.value as Vocabulary }))}>
                {(Object.keys(VOCABULARY) as Vocabulary[]).map((v) => <option key={v} value={v}>{VOCABULARY[v].label}</option>)}
              </select>
              <p className="mt-1.5 text-[13px] text-stone-500">{VOCABULARY[form.vocabulary].note}</p>
            </Step>
          )}

          {(form.method === "model" || form.python.modelCapable) && (
            <details className="rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900 px-4 py-3 sm:px-5">
              <summary className="cursor-pointer font-semibold text-stone-900 dark:text-stone-100">Advanced <span className="font-normal text-sm text-stone-500">— optional</span></summary>
              <label className="mt-3 flex flex-wrap items-center gap-2 text-sm text-stone-500">Model
                <select value={form.model} onChange={(e) => set((f) => ({ ...f, model: e.target.value }))} aria-label="Model" className={`${input} max-w-sm`}>
                  <option value="">The workspace&apos;s extraction model</option>
                  {models.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                </select>
              </label>
            </details>
          )}
        </div>

        <div className="min-w-0 lg:sticky lg:top-4">
          <TryIt form={form} assets={assets} knownTypes={existingTypes} knownRelations={knownRelations} />
        </div>
      </div>

      {issues.length > 0 && (
        <div role="alert" className="sticky bottom-3 z-10 rounded-xl bg-stone-900 px-4 py-2.5 text-stone-50 shadow-lg dark:bg-stone-100 dark:text-stone-900">
          <p className="flex items-center gap-1.5 text-sm font-semibold"><AlertTriangle size={15} className="text-amber-400" /> Not saved yet:</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-amber-200 dark:text-amber-800">{issues.map((x, i) => <li key={i}>{x}</li>)}</ul>
        </div>
      )}
    </div>
  );
}
