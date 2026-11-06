"use client";

/**
 * The kinds of thing extractors found (Person, Place…), in documents or in the text of records: not
 * read from a file's fields, so there is nothing to map - only whether each is used. Off: extraction stops producing it and
 * answers stop reading it.
 */

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { contextV2Api } from "@/lib/api/contextV2";

interface TypeRow { name: string; kind: string; status: string; source: string; count?: number | null }

export default function DocumentTypes({ fileTypes }: { fileTypes: Set<string> }) {
  const [rows, setRows] = useState<TypeRow[] | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    contextV2Api.ontology()
      .then((d: any) => setRows(((d.types || []) as TypeRow[])
        .filter((t) => t.source !== "system" && t.kind !== "edge" && (t.count || 0) > 0 && !fileTypes.has(t.name))
        .sort((a, b) => (b.count || 0) - (a.count || 0))))
      .catch(() => { setRows([]); setErr("Could not load the kinds extractors found."); });
  }, [fileTypes]);

  const toggle = async (t: TypeRow) => {
    const next = t.status === "disabled" ? "active" : "disabled";
    setRows((rs) => (rs || []).map((x) => (x.name === t.name ? { ...x, status: next } : x)));
    try { await contextV2Api.updateTypeStatus(t.name, t.kind, next); }
    catch {
      setRows((rs) => (rs || []).map((x) => (x.name === t.name ? { ...x, status: t.status } : x)));
      setErr(`Could not turn ${t.name} ${next === "active" ? "on" : "off"}.`);
    }
  };

  if (rows === null) return <div className="flex justify-center py-10"><Loader2 className="animate-spin text-stone-400" size={18} /></div>;
  return (
    <div className="flex flex-col gap-2">
      {err && <p className="text-sm text-red-700">{err}</p>}
      {!rows.length && <p className="text-sm text-stone-500">Extractors have not found anything yet.</p>}
      <ul className="grid gap-2 sm:grid-cols-2">
        {rows.map((t) => (
          <li key={t.name} className={`flex items-center gap-3 rounded-xl border border-stone-200 dark:border-stone-700 px-3 py-2.5
                                       bg-white dark:bg-stone-900 ${t.status === "disabled" ? "opacity-60" : ""}`}>
            <span className="flex-1 min-w-0 truncate font-semibold text-stone-900 dark:text-stone-100">{t.name}</span>
            <span className="text-sm tabular-nums text-stone-500">{(t.count || 0).toLocaleString()}</span>
            <label className="inline-flex items-center gap-1.5 text-[13px] text-stone-600 dark:text-stone-400 cursor-pointer">
              <input type="checkbox" checked={t.status !== "disabled"} onChange={() => toggle(t)} /> Use
            </label>
          </li>
        ))}
      </ul>
    </div>
  );
}
