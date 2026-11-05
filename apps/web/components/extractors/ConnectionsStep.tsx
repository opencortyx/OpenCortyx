"use client";

/**
 * How what an extractor creates connects: to the input it read, to each other, and to nodes already
 * in the graph. Between things it creates, the relationship can be the model's to name and the
 * connection the model's to decide - each with its own instruction.
 */

import type { Conn, ExtractorForm, How } from "@/lib/extractors/form";
import { blankConn, endChoices, isCreated, modelCanDecide } from "@/lib/extractors/form";
import { Add, Ask, Remove, Step, WhoNames, input, mono } from "./ui";

type Update = (f: (prev: ExtractorForm) => ExtractorForm) => void;

export default function ConnectionsStep({ form, set, existingTypes }: {
  form: ExtractorForm; set: Update; existingTypes: string[];
}) {
  const choices = endChoices(form, existingTypes);
  const edit = (i: number, patch: Partial<Conn>) => set((f) => ({
    ...f, conns: f.conns.map((c, j) => {
      if (j !== i) return c;
      const next = { ...c, ...patch };
      // the model only decides between things the extractor creates
      if (!modelCanDecide(f, next)) return { ...next, byModel: false, how: "always" as How };
      if (next.byModel) return { ...next, how: "model" as How };
      return next;
    }),
  }));
  const select = (value: string, onChange: (v: string) => void, label: string) => (
    <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={label} className={input}>
      {!choices.some((c) => c.value === value) && <option value={value}>{value.replace(/^kind:/, "") || "(choose)"} — not created any more</option>}
      {choices.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
    </select>
  );

  return (
    <Step n={4} title="Connections"
      hint="How what it creates links to the input, to each other, and to nodes you already have.">
      {!form.conns.length && <p className="text-sm text-stone-500">No connections: what it creates stands on its own.</p>}
      {form.conns.map((c, i) => {
        const canDecide = modelCanDecide(form, c);
        const asksModel = canDecide && (c.byModel || c.how === "model");
        return (
          <article key={i} aria-label={`Connection ${i + 1}`}
            className={`mb-3 rounded-lg border p-3.5 ${c.byModel ? "border-violet-200 dark:border-violet-900 bg-violet-50/40 dark:bg-violet-950/20"
              : "border-stone-200 dark:border-stone-700"}`}>
            <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto] items-center gap-2">
              {select(c.from, (from) => edit(i, { from }), `Connection ${i + 1} from`)}
              <span aria-hidden className="text-stone-400">→</span>
              {select(c.to, (to) => edit(i, { to }), `Connection ${i + 1} to`)}
              <Remove label={`Remove connection ${i + 1}`} onClick={() => set((f) => ({ ...f, conns: f.conns.filter((_, j) => j !== i) }))} />
            </div>
            <div className="mt-2.5 flex flex-wrap items-center gap-2">
              <span className="text-[13px] text-stone-500">Relationship</span>
              {form.method === "model" && (
                <WhoNames label={`Who names connection ${i + 1}`} byModel={c.byModel} disabled={!canDecide}
                  onChange={(byModel) => edit(i, { byModel })} />
              )}
              {c.byModel
                ? <span className="text-[13px] text-violet-800 dark:text-violet-300">HAS_PARTY, SIGNED_FOR, GOVERNED_BY…</span>
                : <input value={c.rel} onChange={(e) => edit(i, { rel: e.target.value })} placeholder="HAS_PARTY"
                    aria-label={`Connection ${i + 1} relationship`} className={`${input} ${mono} max-w-[200px]`} />}
              <span className="flex-1" />
              {form.method === "model" && !c.byModel && (
                <label className="flex items-center gap-1.5 text-[13px] text-stone-500">
                  when
                  <select value={canDecide ? c.how : "always"} disabled={!canDecide} aria-label={`When connection ${i + 1} is made`}
                    onChange={(e) => edit(i, { how: e.target.value as How })} className={`${input} w-auto`}
                    title={canDecide ? undefined : "Always: the model only decides between things this extractor creates"}>
                    <option value="always">{isCreated(c.from) && isCreated(c.to) ? "always, when both are found" : "always"}</option>
                    <option value="model">the model decides</option>
                  </select>
                </label>
              )}
            </div>
            {asksModel && (
              <Ask label="What relationship to look for" value={c.ask} onChange={(ask) => edit(i, { ask })}
                placeholder={c.byModel ? "Disputes between the parties: who disputes, claims against or owes whom"
                  : "Only organizations that sign or are bound by it, not ones merely mentioned"} />
            )}
          </article>
        );
      })}
      <Add onClick={() => set((f) => ({ ...f, conns: [...f.conns, { ...blankConn(), rel: "RELATES_TO" }] }))}>+ Add connection</Add>
    </Step>
  );
}
