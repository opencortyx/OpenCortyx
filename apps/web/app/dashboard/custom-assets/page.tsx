"use client";

/**
 * Extractors: what a person pulls out of documents and records, and the built-ins they can use.
 * One editor for every extractor (components/extractors/ExtractorEditor); the logic it saves with is
 * lib/extractors/form.ts.
 */

import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Plus, SlidersHorizontal, X } from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import ExtractorEditor from "@/components/extractors/ExtractorEditor";
import { BuiltinExtractors, YourExtractors, attachable } from "@/components/extractors/ExtractorList";
import { errorText } from "@/components/extractors/TryIt";
import { contextV2Api, type MappingAsset } from "@/lib/api/contextV2";
import { EXAMPLES, emptyForm, fromTemplate, type ExtractorForm, type SavedTemplate } from "@/lib/extractors/form";

type Tab = "yours" | "builtin";

export default function ExtractorsPage() {
  const [templates, setTemplates] = useState<SavedTemplate[] | null>(null);
  const [types, setTypes] = useState<string[]>([]);
  const [relations, setRelations] = useState<string[]>([]);
  const [models, setModels] = useState<{ id: string; name: string }[]>([]);
  const [assets, setAssets] = useState<MappingAsset[]>([]);
  const [tab, setTab] = useState<Tab>("yours");
  const [editing, setEditing] = useState<ExtractorForm | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const flash = (text: string, ok = true) => setMsg({ ok, text });

  const load = () => contextV2Api.getExtractionTemplates().then(setTemplates)
    .catch((e: unknown) => { setTemplates([]); flash(`Couldn't load the extractors: ${errorText(e)}`, false); });
  useEffect(() => {
    load();
    contextV2Api.ontology().then((d: any) => {
      const rows = (d?.types || []).filter((t: any) => t.status !== "disabled");
      setTypes(Array.from(new Set<string>(rows.filter((t: any) => t.kind !== "edge" && t.in_data).map((t: any) => String(t.name)))).sort());
      setRelations(Array.from(new Set<string>(rows.filter((t: any) => t.kind === "edge").map((t: any) => String(t.name)))));
    }).catch(() => {});
    contextV2Api.listModels("generation").then((d: any) => setModels((d?.items || []).map((m: any) => ({ id: m.id, name: m.name }))))
      .catch(() => {});
    contextV2Api.mappingAssets(true).then(setAssets).catch(() => setAssets([]));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!msg?.ok) return;
    const t = setTimeout(() => setMsg(null), 5000);
    return () => clearTimeout(t);
  }, [msg]);

  const yours = (templates || []).filter((t) => !String(t.name).startsWith("builtin:"))
    .sort((a, b) => a.name.localeCompare(b.name));
  const builtins = (templates || []).filter(attachable).sort((a, b) => a.name.localeCompare(b.name));
  const open = (f: ExtractorForm) => { setMsg(null); setEditing(f); };

  const notice = msg && (
    <div role={msg.ok ? "status" : "alert"}
      className="sticky bottom-3 z-10 mt-4 flex items-start gap-2 rounded-xl bg-stone-900 px-4 py-2.5 text-sm text-stone-50 shadow-lg dark:bg-stone-100 dark:text-stone-900">
      {msg.ok ? <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-emerald-400 dark:text-emerald-600" />
              : <AlertTriangle size={15} className="mt-0.5 shrink-0 text-amber-400 dark:text-amber-600" />}
      <span className="flex-1">{msg.text}</span>
      <button type="button" aria-label="Dismiss" onClick={() => setMsg(null)} className="text-stone-400 hover:text-stone-100 dark:hover:text-stone-700"><X size={15} /></button>
    </div>
  );

  if (editing) {
    return (
      <div className="mx-auto max-w-[1500px] pb-16">
        <ExtractorEditor key={editing.id || "new"} initial={editing}
          taken={yours.filter((t) => t.id !== editing.id).map((t) => t.name)}
          existingTypes={types} knownRelations={relations} models={models} assets={assets}
          onCancel={() => setEditing(null)}
          onDone={(text) => { setEditing(null); flash(text); load(); }} />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl pb-16">
      <PageHeader title="Extractors" icon={SlidersHorizontal}
        description="Pull what you need out of documents and records: things, their fields and how they connect."
        actions={<Button variant="primary" icon={Plus} onClick={() => open(emptyForm())}>New extractor</Button>} />

      <div role="tablist" aria-label="Extractors" className="mb-4 flex gap-1 border-b border-stone-200 dark:border-stone-800">
        {([["yours", "Yours", yours.length], ["builtin", "Built-in", builtins.length]] as [Tab, string, number][]).map(([id, label, n]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => setTab(id)}
            className={`-mb-px flex items-center gap-2 border-b-2 px-3.5 py-2.5 text-sm font-semibold ${tab === id
              ? "border-primary text-stone-900 dark:text-stone-100" : "border-transparent text-stone-500 hover:text-stone-800"}`}>
            {label}
            <span className={`rounded-full px-1.5 text-[11px] tabular-nums ${tab === id ? "bg-primary/10 text-primary" : "bg-stone-100 text-stone-500 dark:bg-stone-800"}`}>{templates ? n : "…"}</span>
          </button>
        ))}
      </div>

      {templates === null ? <p className="text-sm text-stone-500">Loading…</p>
        : tab === "builtin" ? (
          builtins.length ? <BuiltinExtractors items={builtins} models={models} onChanged={load} flash={flash} />
            : <p className="text-sm text-stone-500">No built-in extractors here.</p>
        ) : yours.length ? (
          <YourExtractors items={yours} flash={flash} onChanged={load}
            onEdit={(t) => { const f = fromTemplate(t); if (f) open(f); }} />
        ) : (
          <section className="rounded-xl border border-dashed border-stone-300 dark:border-stone-700 p-8 text-center">
            <h2 className="font-semibold text-stone-900 dark:text-stone-100">Make your first extractor</h2>
            <p className="mx-auto mt-1 max-w-lg text-sm text-stone-500">
              Say what to find in your files: kinds of thing, their fields and how they connect. Name them yourself, or let the model name them.
            </p>
            <div className="mt-4 flex flex-wrap justify-center gap-2">
              <Button variant="primary" icon={Plus} onClick={() => open(emptyForm())}>New extractor</Button>
              {(Object.keys(EXAMPLES) as (keyof typeof EXAMPLES)[]).map((k) => (
                <Button key={k} onClick={() => open(EXAMPLES[k].form())}>Start from: {EXAMPLES[k].label}</Button>
              ))}
            </div>
          </section>
        )}
      {tab === "yours" && yours.length > 0 && (
        <p className="mt-3 text-sm text-stone-500">
          Start from an example:{" "}
          {(Object.keys(EXAMPLES) as (keyof typeof EXAMPLES)[]).map((k, i) => (
            <span key={k}>{i ? " · " : ""}<button type="button" className="text-primary hover:underline" onClick={() => open(EXAMPLES[k].form())}>{EXAMPLES[k].label}</button></span>
          ))}
        </p>
      )}
      {notice}
    </div>
  );
}
