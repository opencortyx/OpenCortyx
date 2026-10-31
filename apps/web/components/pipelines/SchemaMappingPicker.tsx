"use client";


import { CheckCircle2, Plus, X } from "lucide-react";

export interface MappingOption {
  id: string;
  name: string;
}

export default function SchemaMappingPicker({
  options,
  value,
  onChange,
  engineHref,
  accent = "emerald",
  emptyHint = "— none (inline authoring below) —",
  boundHint,
}: {
  options: MappingOption[];
  value: string[];
  onChange: (ids: string[]) => void;
  engineHref: string;
  accent?: "emerald" | "teal" | "violet";
  emptyHint?: string;
  boundHint?: string;
}) {
  const ring = {
    emerald: "focus:ring-emerald-500/20",
    teal: "focus:ring-teal-500/20",
    violet: "focus:ring-violet-500/20",
  }[accent];
  const link = {
    emerald: "text-emerald-600",
    teal: "text-teal-600",
    violet: "text-violet-600",
  }[accent];
  const chip = {
    emerald: "bg-emerald-50 border-emerald-200 text-emerald-800",
    teal: "bg-teal-50 border-teal-200 text-teal-800",
    violet: "bg-violet-50 border-violet-200 text-violet-800",
  }[accent];

  const remaining = options.filter((o) => !value.includes(o.id));
  const add = (id: string) => { if (id && !value.includes(id)) onChange([...value, id]); };
  const remove = (id: string) => onChange(value.filter((x) => x !== id));

  return (
    <div className="space-y-2">
      {/* already-bound mappings */}
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {value.map((id) => {
            const opt = options.find((o) => o.id === id);
            return (
              <span key={id}
                className={`inline-flex items-center gap-1.5 pl-2.5 pr-1.5 py-1 rounded-lg border text-[11px] font-semibold ${chip}`}>
                {opt?.name || <span className="font-mono opacity-60">{id.slice(0, 8)}… (missing)</span>}
                <button onClick={() => remove(id)} title="Unbind this mapping"
                  className="opacity-60 hover:opacity-100">
                  <X size={12} />
                </button>
              </span>
            );
          })}
        </div>
      )}

      {/* add another */}
      <div className="flex items-center gap-2">
        <select
          value=""
          onChange={(e) => add(e.target.value)}
          disabled={remaining.length === 0}
          className={`flex-1 border border-stone-200 rounded-lg px-3 py-2 text-[12px] bg-white focus:outline-none focus:ring-2 ${ring} disabled:opacity-60`}
        >
          <option value="">
            {options.length === 0
              ? "No saved mappings yet — author one in the engine"
              : remaining.length === 0
                ? "All saved mappings are bound"
                : value.length === 0 ? emptyHint : "+ add another mapping…"}
          </option>
          {remaining.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
        </select>
        <a href={engineHref} className={`text-[11px] font-semibold ${link} hover:underline whitespace-nowrap`}>
          Edit mappings ↗
        </a>
      </div>

      {value.length > 0 ? (
        <p className="text-[11px] text-emerald-600 flex items-start gap-1.5">
          <CheckCircle2 size={12} className="mt-0.5 shrink-0" />
          <span>
            {value.length} mapping{value.length === 1 ? "" : "s"} used; edits to {value.length === 1 ? "it" : "them"} apply on the next run. {boundHint}
          </span>
        </p>
      ) : (
        <p className="text-[11px] text-stone-400 flex items-start gap-1.5">
          <Plus size={12} className="mt-0.5 shrink-0" />
          <span>One per kind of table or record.</span>
        </p>
      )}
    </div>
  );
}
