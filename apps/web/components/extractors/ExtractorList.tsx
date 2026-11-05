"use client";

/**
 * The extractors a workspace has: a person's own (opened in the editor, switched on or off, run over
 * the graph when they read nodes, deleted) and the built-ins a pipeline or an upload can use (their
 * prompt, code or model tuned in place, and reset).
 */

import { useState } from "react";
import { Pencil, Play, RotateCcw, Trash2 } from "lucide-react";
import Button from "@/components/ui/Button";
import { contextV2Api } from "@/lib/api/contextV2";
import { describe, fromTemplate, methodOf, type SavedTemplate } from "@/lib/extractors/form";
import { errorText } from "./TryIt";
import { input, mono } from "./ui";

type Flash = (text: string, ok?: boolean) => void;

const METHOD_TONE: Record<string, string> = {
  Model: "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300",
  Pattern: "bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  Python: "bg-sky-50 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
};
const Method = ({ t }: { t: SavedTemplate }) => {
  const m = methodOf(t);
  return <span className={`rounded-full px-2 text-[11.5px] font-semibold ${METHOD_TONE[m] || "bg-stone-100 text-stone-600 dark:bg-stone-800 dark:text-stone-300"}`}>{m}</span>;
};

function OnOff({ t, onChanged, flash }: { t: SavedTemplate; onChanged: () => void; flash: Flash }) {
  const [busy, setBusy] = useState(false);
  const on = (t.status || "active") === "active";
  const name = String(t.name).replace(/^builtin:/, "");
  return (
    <label className="inline-flex cursor-pointer items-center gap-1.5 text-[13px] text-stone-600 dark:text-stone-300"
      title={on ? "On: pipelines, uploads and syncs can run it" : "Off: skipped everywhere"}>
      <input type="checkbox" checked={on} disabled={busy} aria-label={`${name} is on`} onChange={async (e) => {
        setBusy(true);
        try { await contextV2Api.setExtractorStatus(t.id!, e.target.checked ? "active" : "disabled"); onChanged(); }
        catch (err) { flash(`Couldn't switch ${name}: ${errorText(err)}`, false); } finally { setBusy(false); }
      }} /> On
    </label>
  );
}

export function YourExtractors({ items, onEdit, onChanged, flash }: {
  items: SavedTemplate[]; onEdit: (t: SavedTemplate) => void; onChanged: () => void; flash: Flash;
}) {
  const [confirming, setConfirming] = useState("");
  const [busy, setBusy] = useState("");
  const remove = async (t: SavedTemplate) => {
    setBusy(`delete:${t.id}`);
    try { await contextV2Api.deleteExtractionTemplate(t.id!); setConfirming(""); flash(`Deleted ${t.name}.`); onChanged(); }
    catch (e) { flash(`Couldn't delete ${t.name}: ${errorText(e)}`, false); } finally { setBusy(""); }
  };
  const run = async (t: SavedTemplate) => {
    setBusy(`run:${t.id}`);
    try { await contextV2Api.runExtractionTemplate(t.id!); flash(`Running ${t.name} over the graph. What it finds appears as it goes.`); }
    catch (e) { flash(`Couldn't run ${t.name}: ${errorText(e)}`, false); } finally { setBusy(""); }
  };
  return (
    <ul className="flex flex-col divide-y divide-stone-200 dark:divide-stone-800 rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900">
      {items.map((t) => {
        const form = fromTemplate(t);
        const readsGraph = form?.reads.kind === "graph";
        return (
          <li key={t.id} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
            <div className="min-w-0 flex-1 basis-72">
              <p className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-stone-900 dark:text-stone-100">{t.name}</span><Method t={t} />
              </p>
              <p className="text-sm text-stone-500">
                {form ? describe(t) : `${describe(t) ? describe(t) + ". " : ""}Made with an earlier editor: it still runs; recreate it here to change it.`}
              </p>
            </div>
            <OnOff t={t} onChanged={onChanged} flash={flash} />
            {confirming === t.id ? (
              <span className="flex items-center gap-2 text-sm">
                <span className="text-stone-600 dark:text-stone-300">Delete {t.name}? Uploads and setups that use it will skip it.</span>
                <Button size="sm" variant="danger" busy={busy === `delete:${t.id}`} onClick={() => remove(t)}>Delete</Button>
                <Button size="sm" onClick={() => setConfirming("")}>Keep</Button>
              </span>
            ) : (
              <span className="flex items-center gap-1.5">
                {readsGraph && <Button size="sm" icon={Play} busy={busy === `run:${t.id}`} onClick={() => run(t)}
                  title="Read every node it names now">Run</Button>}
                {form && <Button size="sm" icon={Pencil} onClick={() => onEdit(t)}>Edit</Button>}
                <Button size="sm" variant="ghost" icon={Trash2} aria-label={`Delete ${t.name}`} onClick={() => setConfirming(t.id!)} />
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function BuiltinRow({ t, models, onChanged, flash }: { t: SavedTemplate; models: { id: string; name: string }[]; onChanged: () => void; flash: Flash }) {
  const m = t.mechanism || {};
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState<string>(m.prompt || "");
  const [code, setCode] = useState<string>(m.code || "");
  const [model, setModel] = useState<string>(m.model || "");
  const [busy, setBusy] = useState("");
  const name = String(t.name).replace(/^builtin:/, "");
  const about = m.description || String(m.prompt || "").split("\n")[0].slice(0, 160);
  const save = async () => {
    setBusy("save");
    try {
      await contextV2Api.createExtractionTemplate({ name: t.name, source: t.source, emit: {},
        mechanism: { ...(m.prompt !== undefined ? { prompt } : {}), ...(m.code !== undefined ? { code } : {}), model: model || null } });
      flash(`Saved ${name}.`); onChanged(); setOpen(false);
    } catch (e) { flash(`Couldn't save ${name}: ${errorText(e)}`, false); } finally { setBusy(""); }
  };
  const reset = async () => {
    if (!window.confirm(`Put ${name} back as it ships? Your edits to it are dropped.`)) return;
    setBusy("reset");
    try { await contextV2Api.resetExtractionTemplate(t.id!); flash(`${name} is back as it ships.`); onChanged(); setOpen(false); }
    catch (e) { flash(`Couldn't reset ${name}: ${errorText(e)}`, false); } finally { setBusy(""); }
  };
  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1 basis-72">
          <p className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-stone-900 dark:text-stone-100">{name}</span><Method t={t} />
            <span className="text-[12px] text-stone-400">{t.category}</span>
            {m._user_edited && <span className="rounded-full bg-amber-50 px-2 text-[11.5px] font-semibold text-amber-800 dark:bg-amber-950 dark:text-amber-300">edited</span>}
          </p>
          {about && <p className="text-sm text-stone-500">{about}</p>}
        </div>
        <OnOff t={t} onChanged={onChanged} flash={flash} />
        <Button size="sm" icon={Pencil} aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Close" : "Tune"}</Button>
      </div>
      {open && (
        <div className="mt-3 flex flex-col gap-2.5 rounded-lg bg-stone-50 dark:bg-stone-950 p-3">
          {m.prompt !== undefined && (
            <label className="text-sm text-stone-500">Prompt
              <textarea rows={6} value={prompt} onChange={(e) => setPrompt(e.target.value)} className={`${input} ${mono} mt-1`} />
            </label>
          )}
          {m.code !== undefined && (
            <label className="text-sm text-stone-500">Code
              <textarea rows={10} value={code} spellCheck={false} onChange={(e) => setCode(e.target.value)} className={`${input} ${mono} mt-1`} />
            </label>
          )}
          <label className="flex flex-wrap items-center gap-2 text-sm text-stone-500">Model
            <select value={model} onChange={(e) => setModel(e.target.value)} className={`${input} max-w-sm`}>
              <option value="">The workspace&apos;s extraction model</option>
              {models.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            </select>
          </label>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="primary" busy={busy === "save"} onClick={save}>Save</Button>
            <Button size="sm" icon={RotateCcw} busy={busy === "reset"} onClick={reset}>Reset to default</Button>
          </div>
        </div>
      )}
    </li>
  );
}

/** Built-ins a pipeline or an upload can use: not the prompts that run at answer time or on a saved chat. */
export const attachable = (t: SavedTemplate) =>
  String(t.name).startsWith("builtin:") && !["answer", "memory"].includes(String(t.category || "")) && !!t.executable;

export function BuiltinExtractors({ items, models, onChanged, flash }: {
  items: SavedTemplate[]; models: { id: string; name: string }[]; onChanged: () => void; flash: Flash;
}) {
  return (
    <ul className="flex flex-col divide-y divide-stone-200 dark:divide-stone-800 rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900">
      {items.map((t) => <BuiltinRow key={t.id} t={t} models={models} onChanged={onChanged} flash={flash} />)}
    </ul>
  );
}
