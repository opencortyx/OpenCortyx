"use client";
import { useState } from "react";
import { X } from "lucide-react";

/** Tags as the server stores them: trimmed, lower case, single-spaced (knowledge/graph_tree.py). */
export const cleanTag = (t: string) => t.trim().toLowerCase().split(/\s+/).join(" ").slice(0, 80);

/**
 * A row of tag chips with a text box: Enter or a comma adds what was typed, Backspace on an empty
 * box removes the last chip. `placeholder` says what an untagged asset falls back to.
 */
export default function TagInput({ value, onChange, placeholder = "Add a tag…", disabled = false, id }: {
  value: string[]; onChange: (tags: string[]) => void; placeholder?: string; disabled?: boolean; id?: string;
}) {
  const [draft, setDraft] = useState("");
  const add = (text: string) => {
    const next = [...value];
    for (const part of text.split(",")) {
      const t = cleanTag(part);
      if (t && !next.includes(t)) next.push(t);
    }
    if (next.length !== value.length) onChange(next);
    setDraft("");
  };
  return (
    <div className={`flex flex-wrap items-center gap-1.5 rounded-lg border border-stone-300 dark:border-stone-700
                     bg-white dark:bg-stone-900 px-2 py-1.5 ${disabled ? "opacity-60" : ""}`}>
      {value.map((t) => (
        <span key={t} className="inline-flex items-center gap-1 rounded-md bg-stone-100 dark:bg-stone-800
                                 px-2 py-0.5 text-xs font-medium text-stone-700 dark:text-stone-200">
          {t}
          {!disabled && (
            <button type="button" aria-label={`Remove tag ${t}`} onClick={() => onChange(value.filter((x) => x !== t))}
              className="text-stone-400 hover:text-stone-700 dark:hover:text-stone-100">
              <X size={11} />
            </button>
          )}
        </span>
      ))}
      <input id={id} value={draft} disabled={disabled} aria-label="Add a tag"
        onChange={(e) => (e.target.value.includes(",") ? add(e.target.value) : setDraft(e.target.value))}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); if (draft.trim()) add(draft); }
          else if (e.key === "Backspace" && !draft && value.length) onChange(value.slice(0, -1));
        }}
        onBlur={() => { if (draft.trim()) add(draft); }}
        placeholder={value.length ? "" : placeholder}
        className="flex-1 min-w-[8rem] bg-transparent text-sm text-stone-800 dark:text-stone-100 placeholder:text-stone-400
                   focus:outline-none py-0.5" />
    </div>
  );
}
