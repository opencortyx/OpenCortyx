"use client";

// Small building blocks shared by the Custom Metadata screens. Presentation only.
import type { ReactNode } from "react";

export const LANE = {
  input: "bg-teal-600",
  logic: "bg-emerald-600",
  output: "bg-rose-500",
  rel: "bg-indigo-500",
  safety: "bg-amber-500",
} as const;

export function Section({ id, lane, title, aside, children }: {
  id?: string; lane: keyof typeof LANE; title: ReactNode; aside?: ReactNode; children: ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-4 bg-white border border-stone-200 rounded-xl px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <h2 className="text-[15px] font-semibold text-stone-800 flex items-center gap-2.5">
          <span className={`w-1 h-4 rounded-full ${LANE[lane]}`} aria-hidden />{title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

/** A selectable card. `tech` shows the engine term underneath the plain one. */
export function Choice({ on, title, desc, tech, onClick, disabled, locked, icon }: {
  on: boolean; title: string; desc?: string; tech?: string; onClick?: () => void;
  disabled?: boolean; locked?: boolean; icon?: ReactNode;
}) {
  return (
    <button type="button" onClick={onClick} disabled={disabled} aria-pressed={on}
      title={locked ? "A built-in's mechanism is fixed by the catalog — duplicate it to change this" : undefined}
      className={`text-left rounded-lg border px-3 py-2.5 transition-colors ${
        on ? "border-emerald-500 bg-emerald-50/70 ring-1 ring-emerald-500"
           : "border-stone-200 bg-white hover:border-stone-300"
      } ${disabled ? (on ? "cursor-not-allowed" : "cursor-not-allowed opacity-50") : ""}`}>
      <div className={`text-[13.5px] font-semibold flex items-center gap-1.5 ${on ? "text-emerald-800" : "text-stone-700"}`}>
        {icon}{title}{locked && on && <span className="text-[10px] font-semibold uppercase tracking-wider text-emerald-600">· locked</span>}
      </div>
      {desc && <div className="text-xs text-stone-500 mt-0.5">{desc}</div>}
      {tech && <div className="text-[10.5px] font-mono text-stone-400 mt-0.5">{tech}</div>}
    </button>
  );
}

export function Field({ label, hint, children, className = "" }: {
  label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <label className={`block ${className}`}>
      <span className="flex items-center justify-between gap-2 text-xs font-semibold text-stone-600 mb-1">{label}</span>
      {children}
      {hint && <span className="block text-[11.5px] text-stone-400 mt-1 leading-relaxed">{hint}</span>}
    </label>
  );
}

export const inputCls =
  "w-full border border-stone-200 bg-white rounded-lg text-sm px-2.5 py-1.5 focus:outline-none focus:ring-2 focus:ring-emerald-500/25 focus:border-emerald-500";
export const codeCls =
  "w-full rounded-lg text-xs p-3 font-mono leading-relaxed bg-stone-950 text-emerald-100 border border-stone-950 focus:outline-none focus:ring-2 focus:ring-emerald-500/30";

export const chipCls = "px-1.5 py-0.5 rounded text-[10.5px] font-semibold uppercase tracking-wider whitespace-nowrap";
