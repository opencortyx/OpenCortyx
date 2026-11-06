"use client";

/** The small pieces every part of the Extractors form is built from. */

import type { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

export const input =
  "w-full min-w-0 rounded-md border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 px-2.5 py-1.5 text-sm " +
  "text-stone-900 dark:text-stone-100 placeholder:text-stone-400 focus:outline-none focus:ring-2 focus:ring-primary/30";
export const mono = "font-mono text-[13px]";

/** One numbered step of the form. */
export function Step({ n, title, hint, children, tone }: {
  n: number | string; title: string; hint?: string; children: ReactNode; tone?: "model";
}) {
  return (
    <section aria-label={title} className="rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900 p-4 sm:p-5">
      <h2 className="flex items-center gap-2.5 font-semibold text-stone-900 dark:text-stone-100">
        <span className={`inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold ${tone === "model"
          ? "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300" : "bg-primary/10 text-primary"}`}>{n}</span>
        {title}
      </h2>
      {hint && <p className="mt-1 mb-3 text-sm text-stone-500 sm:pl-[34px]">{hint}</p>}
      <div className={`${hint ? "" : "mt-3"} sm:pl-[34px]`}>{children}</div>
    </section>
  );
}

/** A card among a few, one of which is chosen. */
export function Choice({ on, title, text, onClick, disabled }: {
  on: boolean; title: string; text: string; onClick: () => void; disabled?: boolean;
}) {
  return (
    <button type="button" role="radio" aria-checked={on} aria-label={title} title={text} onClick={onClick} disabled={disabled}
      className={`rounded-lg border px-3.5 py-2.5 text-left transition-colors disabled:opacity-50 ${on
        ? "border-primary ring-1 ring-primary bg-primary/5"
        : "border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 hover:border-stone-400"}`}>
      <span className="block text-sm font-semibold text-stone-900 dark:text-stone-100">{title}</span>
      <span className="block text-[13px] text-stone-500">{text}</span>
    </button>
  );
}

/** Who names it: the person, or the model. */
export function WhoNames({ byModel, onChange, label, disabled }: {
  byModel: boolean; onChange: (byModel: boolean) => void; label: string; disabled?: boolean;
}) {
  const seg = (on: boolean, model: boolean) =>
    `px-3 py-1 text-[13px] font-medium transition-colors disabled:cursor-not-allowed ${on
      ? model ? "bg-violet-600 text-white" : "bg-stone-800 text-white dark:bg-stone-200 dark:text-stone-900"
      : "bg-white dark:bg-stone-900 text-stone-600 dark:text-stone-300 hover:bg-stone-50 dark:hover:bg-stone-800"}`;
  return (
    <span role="radiogroup" aria-label={label}
      className="inline-flex overflow-hidden rounded-md border border-stone-300 dark:border-stone-600">
      <button type="button" role="radio" aria-checked={!byModel} className={seg(!byModel, false)} onClick={() => onChange(false)}>You name it</button>
      <button type="button" role="radio" aria-checked={byModel} disabled={disabled}
        title={disabled ? "The model only names connections between things this extractor creates" : undefined}
        className={`${seg(byModel, true)} border-l border-stone-300 dark:border-stone-600`} onClick={() => onChange(true)}>The model names it</button>
    </span>
  );
}

/** The model's instruction for one slot: required in spirit, so an empty one says what happens. */
export function Ask({ label, value, placeholder, onChange }: {
  label: string; value: string; placeholder: string; onChange: (v: string) => void;
}) {
  const empty = !value.trim();
  return (
    <label className="mt-2.5 block">
      <span className="text-[13px] font-medium text-violet-800 dark:text-violet-300">
        {label} <span className="font-normal text-stone-400">— the model&apos;s instruction</span>
      </span>
      <textarea rows={2} value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)}
        aria-invalid={empty}
        className={`${input} mt-1 ${empty ? "border-amber-400 dark:border-amber-600" : ""}`} />
      {empty && (
        <span className="mt-1 flex items-center gap-1 text-[12.5px] text-amber-800 dark:text-amber-300">
          <AlertTriangle size={12} /> Say what to look for, or the model returns everything it can find.
        </span>
      )}
    </label>
  );
}

export function Remove({ onClick, label }: { onClick: () => void; label: string }) {
  return (
    <button type="button" onClick={onClick} aria-label={label} title={label}
      className="shrink-0 rounded-md px-1.5 text-lg leading-none text-stone-400 hover:bg-stone-100 hover:text-stone-700 dark:hover:bg-stone-800">×</button>
  );
}

export function Add({ onClick, children, disabled }: { onClick: () => void; children: ReactNode; disabled?: boolean }) {
  return (
    <button type="button" onClick={onClick} disabled={disabled}
      className="mt-2 text-sm font-medium text-primary hover:underline disabled:text-stone-400 disabled:no-underline">{children}</button>
  );
}

/** A field: its name and what to look for. */
export function FieldRow({ name, ask, onName, onAsk, onRemove, askPlaceholder = "What to look for", hideAsk }: {
  name: string; ask: string; onName: (v: string) => void; onAsk: (v: string) => void; onRemove: () => void;
  askPlaceholder?: string; hideAsk?: boolean;
}) {
  return (
    <div className={`mb-2 grid items-center gap-2 ${hideAsk ? "grid-cols-[minmax(0,1fr)_auto]" : "grid-cols-[minmax(0,180px)_minmax(0,1fr)_auto]"}`}>
      <input value={name} onChange={(e) => onName(e.target.value)} placeholder="field_name" aria-label="Field name" className={`${input} ${mono}`} />
      {!hideAsk && <input value={ask} onChange={(e) => onAsk(e.target.value)} placeholder={askPlaceholder} aria-label="What to look for" className={input} />}
      <Remove onClick={onRemove} label={`Remove the field ${name || ""}`.trim()} />
    </div>
  );
}
