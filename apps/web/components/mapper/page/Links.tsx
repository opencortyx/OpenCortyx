"use client";

/**
 * How a file's kinds connect: each relationship, its name, what it was found from, and a way to add
 * one the data did not show ("each Order refers to a Customer through customer_id").
 */

import { useState } from "react";
import { Plus, X } from "lucide-react";

export interface LinkRow {
  id: string;
  from: string;
  to: string;
  name: string;
  /** the name was taken from the target's name (and follows it) */
  auto: boolean;
  on: boolean;
  why: string;
  /** added on this page and not saved yet */
  added: boolean;
}

export interface LinkEnd { value: string; name: string }

const select = "rounded-md border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 px-2 py-1 text-sm";

export const linkName = (v: string) => v.toUpperCase().replace(/[^A-Z0-9_]+/g, "_");

export default function Links({ rows, sources, targets, fieldsOf, onName, onToggle, onRemove, onAdd }: {
  rows: LinkRow[];
  /** the kinds read from this file */
  sources: LinkEnd[];
  /** every kind a record can refer to (it has an identifier) */
  targets: LinkEnd[];
  fieldsOf: (source: string) => string[];
  onName: (id: string, name: string) => void;
  onToggle: (id: string, on: boolean) => void;
  onRemove: (id: string) => void;
  onAdd: (source: string, field: string, target: string) => void;
}) {
  const [adding, setAdding] = useState(false);
  const [from, setFrom] = useState("");
  const [field, setField] = useState("");
  const [to, setTo] = useState("");
  const fields = from ? fieldsOf(from) : [];

  return (
    <div className="flex flex-col gap-2">
      {!rows.length && <p className="text-sm text-stone-500">No relationships found for these kinds yet.</p>}
      {rows.map((r) => (
        <div key={r.id}
          className={`grid grid-cols-[auto_1fr] sm:grid-cols-[auto_minmax(90px,1fr)_minmax(170px,1.4fr)_minmax(90px,1fr)_auto]
                      items-center gap-x-3 gap-y-1 rounded-xl border border-stone-200 dark:border-stone-700 px-3 py-2.5
                      ${r.on ? "" : "opacity-55"}`}>
          <input type="checkbox" checked={r.on} onChange={(ev) => onToggle(r.id, ev.target.checked)}
                 aria-label={`Keep ${r.from} to ${r.to}`} />
          <span className="font-semibold text-stone-900 dark:text-stone-100 truncate">{r.from}</span>
          <span className="col-span-2 sm:col-span-1 flex items-center gap-1.5 text-stone-400">
            <span aria-hidden="true">─</span>
            <input value={r.name} onChange={(ev) => onName(r.id, linkName(ev.target.value))} aria-label="Relationship name"
              className="min-w-0 flex-1 rounded-md border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 px-2 py-0.5
                         font-mono text-[12.5px] text-stone-800 dark:text-stone-200 focus:outline-none focus:border-primary" />
            <span aria-hidden="true">▶</span>
          </span>
          <span className="font-semibold text-stone-900 dark:text-stone-100 truncate">{r.to}</span>
          <span className="flex items-center gap-2 justify-self-end">
            {r.auto && <span className="text-[11px] font-semibold text-teal-700 dark:text-teal-300" title="Named after its target; follows a rename">auto</span>}
            {r.added && (
              <button type="button" aria-label="Remove this relationship" onClick={() => onRemove(r.id)}
                className="text-stone-400 hover:text-red-600"><X size={14} /></button>)}
          </span>
          <span className="col-span-2 sm:col-start-2 sm:col-span-4 text-[12.5px] text-stone-500">{r.why}</span>
        </div>
      ))}

      {adding ? (
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-dashed border-primary/60 px-3 py-2.5 text-sm text-stone-600 dark:text-stone-300">
          Each
          <select className={select} value={from} aria-label="Kind that refers"
            onChange={(ev) => { setFrom(ev.target.value); setField(""); }}>
            <option value="">kind…</option>
            {sources.map((s) => <option key={s.value} value={s.value}>{s.name}</option>)}
          </select>
          refers to a
          <select className={select} value={to} onChange={(ev) => setTo(ev.target.value)} aria-label="Kind referred to">
            <option value="">kind…</option>
            {targets.filter((t) => t.value !== from).map((t) => <option key={t.value} value={t.value}>{t.name}</option>)}
          </select>
          through
          <select className={select} value={field} onChange={(ev) => setField(ev.target.value)} aria-label="Field that refers" disabled={!from}>
            <option value="">field…</option>
            {fields.map((f) => <option key={f}>{f}</option>)}
          </select>
          <button type="button" disabled={!from || !field || !to}
            className="rounded-md bg-primary px-3 py-1 font-semibold text-white disabled:opacity-40"
            onClick={() => { onAdd(from, field, to); setAdding(false); setFrom(""); setField(""); setTo(""); }}>Add</button>
          <button type="button" className="text-stone-500 hover:underline" onClick={() => setAdding(false)}>Cancel</button>
        </div>
      ) : (
        sources.length > 0 && targets.length > 0 && (
          <button type="button" onClick={() => setAdding(true)}
            className="self-start inline-flex items-center gap-1 rounded-lg border border-dashed border-stone-300 dark:border-stone-600
                       px-3 py-1.5 text-sm font-semibold text-primary hover:border-primary">
            <Plus size={14} /> Add a relationship
          </button>
        )
      )}
    </div>
  );
}
