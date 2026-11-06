"use client";

/**
 * What an extractor creates: kinds of thing (each named by the person or the model, with fields
 * named by either), and fields written onto what it read. A pattern makes one kind, a node per
 * match; Python one kind from the records its code returns.
 */

import type { ExtractorForm, Field, Kind } from "@/lib/extractors/form";
import { blankKind, kindEnd } from "@/lib/extractors/form";
import { Add, Ask, FieldRow, Step, WhoNames, input, mono } from "./ui";

type Update = (f: (prev: ExtractorForm) => ExtractorForm) => void;

const editRows = (rows: Field[], i: number, patch: Partial<Field>) => rows.map((f, j) => (j === i ? { ...f, ...patch } : f));

function Fields({ rows, onChange, hideAsk, label }: { rows: Field[]; onChange: (rows: Field[]) => void; hideAsk?: boolean; label: string }) {
  return (
    <div>
      {rows.map((f, i) => (
        <FieldRow key={i} name={f.key} ask={f.ask} hideAsk={hideAsk}
          onName={(v) => onChange(editRows(rows, i, { key: v }))} onAsk={(v) => onChange(editRows(rows, i, { ask: v }))}
          onRemove={() => onChange(rows.filter((_, j) => j !== i))} />
      ))}
      <Add onClick={() => onChange([...rows, { key: "", ask: "" }])}>+ Add {label}</Add>
    </div>
  );
}

function KindBlock({ k, i, form, set }: { k: Kind; i: number; form: ExtractorForm; set: Update }) {
  const edit = (patch: Partial<Kind>) => set((f) => ({ ...f, kinds: f.kinds.map((x, j) => (j === i ? { ...x, ...patch } : x)) }));
  // a renamed kind keeps its connections
  const rename = (type: string) => set((f) => {
    const was = kindEnd(f.kinds[i].type.trim()), now = kindEnd(type.trim());
    const end = (e: string) => (e === was && f.kinds[i].type.trim() ? now : e);
    return { ...f, kinds: f.kinds.map((x, j) => (j === i ? { ...x, type } : x)),
             conns: f.conns.map((c) => ({ ...c, from: end(c.from), to: end(c.to) })) };
  });
  const setByModel = (byModel: boolean) => set((f) => {
    const was = byModel ? kindEnd(f.kinds[i].type.trim()) : "model";
    const now = byModel ? "model" : kindEnd(f.kinds[i].type.trim());
    const end = (e: string) => (e === was ? now : e);
    return { ...f, kinds: f.kinds.map((x, j) => (j === i ? { ...x, byModel, ...(byModel ? { fieldsByModel: true } : {}) } : x)),
             conns: f.conns.map((c) => ({ ...c, from: end(c.from), to: end(c.to) })) };
  });
  const otherByModel = form.kinds.some((x, j) => j !== i && x.byModel);
  return (
    <article aria-label={k.byModel ? "A kind the model names" : `Kind ${k.type || i + 1}`}
      className={`mb-3 rounded-lg border p-3.5 ${k.byModel ? "border-violet-200 dark:border-violet-900 bg-violet-50/40 dark:bg-violet-950/20"
        : "border-stone-200 dark:border-stone-700"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded-full px-2 text-[11.5px] font-semibold ${k.byModel
          ? "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300" : "bg-teal-50 text-teal-800 dark:bg-teal-950 dark:text-teal-300"}`}>Kind</span>
        <WhoNames label="Who names this kind" byModel={k.byModel} onChange={setByModel} disabled={!k.byModel && otherByModel} />
        {k.byModel
          ? <span className="text-[13px] text-violet-800 dark:text-violet-300">Person, Organization, Jurisdiction… whatever fits</span>
          : <input value={k.type} onChange={(e) => rename(e.target.value)} placeholder="Contract" aria-label="Kind name"
              className={`${input} ${mono} max-w-[220px]`} />}
        <span className="flex-1" />
        {form.kinds.length > 1 && (
          <button type="button" className="text-[13px] text-stone-500 hover:text-red-700"
            onClick={() => set((f) => ({ ...f, kinds: f.kinds.filter((_, j) => j !== i) }))}>Remove kind</button>
        )}
      </div>
      {k.byModel
        ? <Ask label="What things to look for" value={k.ask} onChange={(ask) => edit({ ask })}
            placeholder="Parties, people, courts and places a contract or dispute names" />
        : <input value={k.ask} onChange={(e) => edit({ ask: e.target.value })} aria-label="What to look for"
            placeholder="What to look for (optional): the agreement itself, not ones it refers to" className={`${input} mt-2.5`} />}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="w-14 text-[13px] text-stone-500">Fields</span>
        <WhoNames label="Who names the fields" byModel={k.fieldsByModel}
          onChange={(v) => edit({ fieldsByModel: v, ...(!v && !k.fields.length ? { fields: [{ key: "", ask: "" }] } : {}) })} />
      </div>
      <div className="mt-2">
        {k.fieldsByModel ? (<>
          <p className="text-[13px] text-violet-800 dark:text-violet-300">Facts the text states about it become fields: term = &quot;24 months&quot;, founded = 1998.</p>
          <Ask label="What facts to look for" value={k.fieldsAsk} onChange={(fieldsAsk) => edit({ fieldsAsk })}
            placeholder="Dates, durations, amounts and obligations; skip addresses and boilerplate" />
        </>) : <Fields rows={k.fields} label="field" onChange={(fields) => edit({ fields })} />}
      </div>
    </article>
  );
}

function OnInput({ form, set, hideAsk }: { form: ExtractorForm; set: Update; hideAsk?: boolean }) {
  return (
    <div className="mt-4 rounded-lg bg-stone-50 dark:bg-stone-950 p-3.5">
      <p className="mb-2 flex flex-wrap items-center gap-2 text-sm">
        <span className="rounded-full bg-sky-50 px-2 text-[11.5px] font-semibold text-sky-800 dark:bg-sky-950 dark:text-sky-300">On the input</span>
        <span className="text-stone-500">Fields written onto the row, record, passage or node it read.</span>
      </p>
      <Fields rows={form.inputFields} label="field on the input" hideAsk={hideAsk}
        onChange={(inputFields) => set((f) => ({ ...f, inputFields }))} />
    </div>
  );
}

export default function CreatesStep({ form, set }: { form: ExtractorForm; set: Update }) {
  const kind = form.kinds[0] || blankKind();
  const setFirst = (patch: Partial<Kind>) => set((f) => {
    const was = kindEnd((f.kinds[0]?.type || "").trim()), now = kindEnd((patch.type ?? f.kinds[0]?.type ?? "").trim());
    const end = (e: string) => (e === was && patch.type !== undefined ? now : e);
    return { ...f, kinds: [{ ...(f.kinds[0] || blankKind()), byModel: false, ...patch }, ...f.kinds.slice(1)],
             conns: f.conns.map((c) => ({ ...c, from: end(c.from), to: end(c.to) })) };
  });

  if (form.method === "pattern") {
    const p = form.pattern;
    const setP = (patch: Partial<typeof p>) => set((f) => ({ ...f, pattern: { ...f.pattern, ...patch } }));
    return (
      <Step n={3} title="What it creates" hint="Each match becomes a node of the kind you name, one per distinct match.">
        <label className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-stone-500">Each match is a</span>
          <input value={kind.type} onChange={(e) => setFirst({ type: e.target.value })} placeholder="Term" aria-label="Kind name"
            className={`${input} ${mono} max-w-[200px]`} />
        </label>
        <div role="radiogroup" aria-label="What to match" className="mt-3 flex flex-wrap gap-1.5">
          {([["patterns", "Patterns"], ["words", "A word list"], ["frequent", "The most frequent terms"]] as const).map(([m, label]) => (
            <button key={m} type="button" role="radio" aria-checked={p.mode === m} onClick={() => setP({ mode: m })}
              className={`rounded-md border px-3 py-1 text-[13px] font-medium ${p.mode === m
                ? "border-primary bg-primary/10 text-primary" : "border-stone-200 dark:border-stone-700 text-stone-600 dark:text-stone-300"}`}>{label}</button>
          ))}
        </div>
        {p.mode === "frequent" ? (
          <label className="mt-3 flex items-center gap-2 text-sm text-stone-600 dark:text-stone-300">
            The <input type="number" min={1} max={50} value={p.topN} aria-label="How many terms"
              onChange={(e) => setP({ topN: Number(e.target.value) })} className={`${input} w-20`} /> most frequent words in each input
          </label>
        ) : (<>
          <textarea rows={4} value={p.lines} onChange={(e) => setP({ lines: e.target.value })} aria-label={p.mode === "patterns" ? "Patterns" : "Words"}
            placeholder={p.mode === "patterns" ? "net \\d+\nauto-renew\nterminat(e|ion)" : "SAR\nKYC\nsanctions"} className={`${input} ${mono} mt-3`} />
          <p className="mt-1 text-[12.5px] text-stone-500">
            {p.mode === "patterns" ? "One regular expression a line; a match is the text it matches." : "One word or phrase a line (or comma separated)."}
          </p>
          {p.mode === "patterns" && (
            <label className="mt-2 inline-flex items-center gap-1.5 text-sm text-stone-600 dark:text-stone-300">
              <input type="checkbox" checked={p.ignoreCase} onChange={(e) => setP({ ignoreCase: e.target.checked })} /> Ignore case
            </label>
          )}
        </>)}
      </Step>
    );
  }

  if (form.method === "python") {
    return (
      <Step n={3} title="What it creates" hint="extract(inputs) gets the text it read as inputs['text'] and returns one dict per thing found.">
        <textarea rows={9} value={form.python.code} spellCheck={false} aria-label="Python code"
          onChange={(e) => set((f) => ({ ...f, python: { ...f.python, code: e.target.value } }))} className={`${input} ${mono}`} />
        <label className="mt-2 inline-flex items-center gap-1.5 text-sm text-stone-600 dark:text-stone-300">
          <input type="checkbox" checked={form.python.modelCapable}
            onChange={(e) => set((f) => ({ ...f, python: { ...f.python, modelCapable: e.target.checked } }))} /> Let this code call the model
        </label>
        <label className="mt-4 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-stone-500">Each dict it returns is a</span>
          <input value={kind.type} onChange={(e) => setFirst({ type: e.target.value })} placeholder="Amount (optional)" aria-label="Kind name"
            className={`${input} ${mono} max-w-[200px]`} />
          <span className="text-stone-500">node</span>
        </label>
        {kind.type.trim() && (
          <div className="mt-2">
            <p className="mb-1.5 text-[13px] text-stone-500">Its fields (the keys of each dict):</p>
            <Fields rows={kind.fields} label="field" hideAsk onChange={(fields) => setFirst({ fields })} />
          </div>
        )}
        <OnInput form={form} set={set} hideAsk />
      </Step>
    );
  }

  return (
    <Step n={3} title="What it creates" hint="Each kind of thing, and each of its fields: you name it, or the model names it. Mix freely.">
      {form.kinds.map((k, i) => <KindBlock key={i} k={k} i={i} form={form} set={set} />)}
      <Add onClick={() => set((f) => ({ ...f, kinds: [...f.kinds, blankKind()] }))}>+ Add a kind of thing</Add>
      <OnInput form={form} set={set} />
    </Step>
  );
}
