"use client";

/**
 * What extractors found in one file - a document's passages, or the text of a table's or JSON
 * file's records - and a way to run one over that file now.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { Loader2, Play, RefreshCw } from "lucide-react";
import { contextV2Api, type FoundKind } from "@/lib/api/contextV2";

const errorText = (e: any): string => e?.response?.data?.detail || e?.message || "Something went wrong";

export default function FileExtractors({ assetId, fileName, kind, found: given }: {
  assetId: string; fileName: string;
  /** the file's kind: the built-in extractors offered are the ones for it */
  kind: "structured" | "semistructured" | "unstructured";
  found?: FoundKind[];
}) {
  const [found, setFound] = useState<FoundKind[] | null>(given ?? null);
  const [extractors, setExtractors] = useState<{ name: string; label: string }[]>([]);
  const [chosen, setChosen] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = () => contextV2Api.fileFound(assetId).then(setFound).catch(() => setFound([]));
  useEffect(() => { if (given) setFound(given); else load(); }, [assetId, given]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    // a person's own extractors fit any file (they read what they are given); built-ins their kind - as in Add data
    contextV2Api.getExtractionTemplates().then((ts: any[]) => {
      const seen = new Map<string, { name: string; label: string }>();
      for (const k of ["unstructured", "structured", "semistructured"]) {
        const { builtin, custom } = contextV2Api.stageableTemplates(ts, k);
        for (const t of [...custom, ...(k === kind ? builtin : [])]) seen.set(t.name, { name: t.name, label: t.label });
      }
      setExtractors(Array.from(seen.values()));
    }).catch(() => setExtractors([]));
  }, [kind]);

  const run = async () => {
    setBusy("run"); setMsg(null);
    try {
      await contextV2Api.extractFromFile(assetId, chosen);
      setMsg({ ok: true, text: `Running over ${fileName}. What it finds appears here when it is done (Refresh).` });
    } catch (e) { setMsg({ ok: false, text: errorText(e) }); } finally { setBusy(""); }
  };
  const refresh = async () => { setBusy("refresh"); await load(); setBusy(""); };

  return (
    <div className="flex flex-col gap-3">
      {found === null ? <Loader2 className="animate-spin text-stone-400" size={16} />
        : found.length ? (
          <ul className="flex flex-wrap gap-2" aria-label="Kinds extractors found in this file">
            {found.map((k) => (
              <li key={k.type} className="inline-flex items-center gap-2 rounded-lg border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 px-2.5 py-1 text-sm">
                <span className="font-semibold text-stone-900 dark:text-stone-100">{k.type}</span>
                <span className="tabular-nums text-stone-500">{k.count.toLocaleString()}</span>
              </li>
            ))}
          </ul>
        ) : <p className="text-sm text-stone-500">No extractor has found anything in this file yet.</p>}

      <div className="flex flex-wrap items-center gap-2 text-sm">
        <label htmlFor={`run-${assetId}`} className="text-stone-600 dark:text-stone-400">Run an extractor on this file</label>
        <select id={`run-${assetId}`} value={chosen} onChange={(e) => setChosen(e.target.value)}
          className="rounded-md border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 px-2 py-1">
          <option value="">choose…</option>
          {extractors.map((x) => <option key={x.name} value={x.name}>{x.label}</option>)}
        </select>
        <button type="button" onClick={run} disabled={!chosen || !!busy}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1 font-semibold text-white disabled:opacity-40">
          {busy === "run" ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />} Run
        </button>
        <button type="button" onClick={refresh} disabled={!!busy} className="inline-flex items-center gap-1 text-primary hover:underline disabled:opacity-40">
          <RefreshCw size={13} /> Refresh
        </button>
        <Link href="/dashboard/custom-assets" className="text-primary hover:underline">Create an extractor</Link>
      </div>
      {msg && <p className={`text-sm ${msg.ok ? "text-emerald-700" : "text-red-700"}`}>{msg.text}</p>}
    </div>
  );
}
