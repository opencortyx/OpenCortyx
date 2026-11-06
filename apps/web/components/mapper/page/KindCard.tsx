"use client";

/**
 * One kind of thing a file holds (an entity of a mapping's knowledge): its name, what identifies a
 * record, what a record is shown as, the attributes its graph node carries, and whether it is in the
 * graph at all. Every choice is built from what the data was measured to support. A thing (a kind with
 * an identifier) also says when two of its records, from any files, are the same thing (Resolution).
 */

import { useState } from "react";
import { AlertTriangle, KeyRound } from "lucide-react";
import ResolutionPanel from "@/components/resolution/ResolutionPanel";
import type { KnowledgeEntity } from "@/lib/api/contextV2";
import { carriedFields, identifierChoices, lent, type Measured } from "@/lib/mapper/knowledge";

const select = "w-full min-w-0 rounded-md border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 px-2 py-1 text-sm " +
  "focus:outline-none focus:ring-2 focus:ring-primary/30";

export interface KindCardProps {
  entity: KnowledgeEntity;
  /** the name as it is now (a rename is shown here before it is saved) */
  name: string;
  /** the name it was saved under */
  savedName: string;
  measured: Measured;
  /** the fields other kinds' relationships read from this kind */
  refs: Set<string>;
  duplicate: boolean;
  /** detected, not sure enough to be used until a person saves it */
  draft: boolean;
  otherFiles: number;
  onRename: (name: string) => void;
  onChange: (patch: Partial<KnowledgeEntity>) => void;
  /** a message for the page (Resolution saved, or not) */
  onFlash: (text: string, ok?: boolean) => void;
}

export default function KindCard({ entity: e, name, savedName, measured: m, refs, duplicate, draft, otherFiles,
                                   onRename, onChange, onFlash }: KindCardProps) {
  const [sameOpen, setSameOpen] = useState(false);
  const key = e.keys[0] || "";
  const ignored = e.status === "rejected";
  const choices = identifierChoices(m);
  const names = m.fields.filter((f) => f.nameable && f.name !== key);
  const carried = carriedFields(e, m, refs);
  const shown = m.fields.filter((f) => !lent(f) || f.name === key);
  const count = m.records || e.rows || 0;
  // an example a person recognises: its name, its identifier, else its longest text
  const sampleField = e.label || key || [...shown].sort((a, b) => b.words - a.words)[0]?.name;
  const sample = sampleField ? m.rows.map((r) => r[sampleField]).find((v) => v != null && v !== "") : undefined;
  const id = (s: string) => `${s}-${savedName.replace(/[^A-Za-z0-9]+/g, "-")}`;

  return (
    <article aria-label={name}
      className={`flex flex-col gap-3 rounded-xl border p-3.5 bg-white dark:bg-stone-900 ${
        ignored ? "border-stone-200 dark:border-stone-800 opacity-60" : "border-stone-200 dark:border-stone-700"}`}>
      <div className="flex items-center gap-2">
        <input id={id("name")} value={name} onChange={(ev) => onRename(ev.target.value)} aria-label="Name of this kind"
          className="flex-1 min-w-0 -ml-1.5 rounded-md border border-transparent bg-transparent px-1.5 py-0.5 text-base font-semibold
                     text-stone-900 dark:text-stone-100 hover:border-stone-200 focus:border-primary focus:outline-none" />
        <span className="text-sm tabular-nums text-stone-500 whitespace-nowrap">
          {m.loading && !count ? "…" : count.toLocaleString()}
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5 text-[11.5px] font-semibold">
        {key ? <span className="rounded-full bg-teal-50 text-teal-700 dark:bg-teal-950 dark:text-teal-300 px-2">Thing: same id, same node</span>
             : <span className="rounded-full bg-violet-50 text-violet-700 dark:bg-violet-950 dark:text-violet-300 px-2">Event: each record is new</span>}
        {duplicate && (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300 px-2">
            <AlertTriangle size={11} /> Another kind here has this name
          </span>)}
        {name.trim() !== savedName && savedName && (
          <span className="rounded-full bg-stone-100 dark:bg-stone-800 text-stone-500 font-medium px-2">was {savedName}</span>)}
        {draft && e.status !== "accepted" && (
          <span className="rounded-full bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300 px-2">Detection was unsure: check it</span>)}
      </div>

      <label htmlFor={id("key")} className="grid grid-cols-[100px_1fr] items-center gap-2 text-sm">
        <span className="text-stone-500 flex items-center gap-1"><KeyRound size={12} /> Identified by</span>
        <select id={id("key")} className={select} value={key}
          onChange={(ev) => onChange({ keys: ev.target.value ? [ev.target.value] : [], kind: ev.target.value ? "entity" : "event" })}>
          <option value="">Nothing: each record is an event</option>
          {key && !choices.some((c) => c.field.name === key) && (
            <option value={key}>{key}{m.loading ? "" : " (not unique in every file)"}</option>)}
          {choices.map((c) => (
            <option key={c.field.name} value={c.field.name}>
              {c.field.name}{c.checked ? "" : " (one record, so not checked)"}
            </option>))}
        </select>
      </label>

      <label htmlFor={id("label")} className="grid grid-cols-[100px_1fr] items-center gap-2 text-sm">
        <span className="text-stone-500">Shown as</span>
        <select id={id("label")} className={select} value={e.label && e.label !== key ? e.label : ""}
          onChange={(ev) => onChange({ label: ev.target.value })}>
          <option value="">{key ? `its identifier (${key})` : "its content"}</option>
          {e.label && e.label !== key && !names.some((f) => f.name === e.label) && <option value={e.label}>{e.label}</option>}
          {names.map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}
        </select>
      </label>

      {shown.length > 0 && (
        <div className="grid grid-cols-[100px_1fr] items-start gap-2 text-sm">
          <span className="text-stone-500 pt-0.5">Attributes</span>
          <div className="flex flex-wrap gap-1.5">
            {shown.map((f) => {
              const on = f.name === key || carried.includes(f.name);
              const tag = f.name === key ? "ID" : f.name === e.label ? "NAME" : "";
              return (
                <button key={f.name} type="button" aria-pressed={on} disabled={f.name === key || !e.materialize}
                  title={f.name === key ? "The identifier: always carried"
                    : !e.materialize ? "Turn on \"In the graph\" to choose what its nodes carry; every field stays queryable"
                    : on ? "Carried by the node. Click to leave it in the data store" : "In the data store only. Click to carry it on the node"}
                  onClick={() => onChange({ attributes: on ? carried.filter((x) => x !== f.name) : [...carried, f.name] })}
                  className={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 font-mono text-[12px] ${on
                    ? "border-transparent bg-teal-50 text-teal-900 dark:bg-teal-950 dark:text-teal-200"
                    : "border-stone-200 dark:border-stone-700 text-stone-500"} disabled:cursor-default`}>
                  {f.name}{tag && <span className="font-sans text-[9.5px] font-bold text-teal-700 dark:text-teal-300">{tag}</span>}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {sample != null && (
        <p className="border-t border-dashed border-stone-200 dark:border-stone-700 pt-2 text-[12.5px] text-stone-500 break-words">
          e.g. <span className="font-mono text-stone-800 dark:text-stone-200">{String(sample).slice(0, 140)}</span>
        </p>
      )}
      {m.errors.length > 0 && <p className="text-sm text-red-700">Could not be read: {m.errors.slice(0, 2).join("; ")}</p>}

      {key && !ignored && (
        <details className="text-[13px]" onToggle={(ev) => setSameOpen((ev.target as HTMLDetailsElement).open)}>
          <summary className="cursor-pointer text-stone-500">Same thing across files</summary>
          {sameOpen && (name.trim() !== savedName
            ? <p className="mt-2 text-stone-500">Save the new name first.</p>
            : <div className="mt-2"><ResolutionPanel name={savedName} flash={onFlash} onSaved={() => {}} /></div>)}
        </details>
      )}

      <div className="mt-auto flex flex-wrap items-center gap-x-4 gap-y-1 text-[13px] text-stone-600 dark:text-stone-400">
        <label className="inline-flex items-center gap-1.5 cursor-pointer">
          <input type="checkbox" checked={e.materialize} disabled={ignored}
            onChange={(ev) => onChange({ materialize: ev.target.checked })} />
          In the graph
        </label>
        <label className="inline-flex items-center gap-1.5 cursor-pointer" title="Off: this is not a kind of thing; nothing reads it">
          <input type="checkbox" checked={e.status === "accepted"} onChange={(ev) => onChange({ status: ev.target.checked ? "accepted" : "rejected" })} />
          Use this kind
        </label>
        {otherFiles > 0 && <span className="text-stone-400">also in {otherFiles} other file{otherFiles > 1 ? "s" : ""}</span>}
      </div>
    </article>
  );
}
