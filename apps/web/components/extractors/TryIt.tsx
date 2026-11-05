"use client";

/**
 * Try an unsaved extractor on pasted text or on a file in the workspace, and see what it would
 * create: things with their fields, connections, fields on the input. Nothing is written.
 */

import { useState } from "react";
import { AlertTriangle, Play } from "lucide-react";
import Button from "@/components/ui/Button";
import { contextV2Api, type MappingAsset } from "@/lib/api/contextV2";
import { toTemplate, type ExtractorForm } from "@/lib/extractors/form";
import { input } from "./ui";

export const SAMPLE = "This Master Services Agreement (MSA-2026) between Acme Ltd and Globex, signed by J. Patel for Acme, " +
  "runs for 24 months from 1 March 2026 and is governed by the laws of England. Globex disputes the late-payment fees Acme claims.";

interface Attr { key: string; value: string; filled: boolean }
interface PNode { id: string; type: string; label: string; attributes: Attr[] }
interface PEdge { rel: string; from: string; to: string; from_id?: string | null; to_id?: string | null }
interface Preview { error?: string | null; warnings: string[]; nodes: PNode[]; edges: PEdge[]; source_attributes: Attr[]; sample: string }

export const errorText = (e: any): string => {
  const d = e?.response?.data?.detail;
  if (Array.isArray(d)) return d.map((x: any) => String(x?.msg || x).replace(/^Value error, /, "")).join("; ");
  return d || e?.message || "Something went wrong";
};

const Tag = ({ children, tone }: { children: React.ReactNode; tone: "thing" | "model" | "input" | "new" }) => (
  <span className={`inline-block rounded-md px-1.5 text-[12px] font-medium ${{
    thing: "bg-teal-50 text-teal-800 dark:bg-teal-950 dark:text-teal-300",
    model: "bg-violet-100 text-violet-800 dark:bg-violet-950 dark:text-violet-300",
    input: "bg-sky-50 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
    new: "bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  }[tone]}`}>{children}</span>
);

export default function TryIt({ form, assets, knownTypes, knownRelations }: {
  form: ExtractorForm; assets: MappingAsset[]; knownTypes: string[]; knownRelations: string[];
}) {
  const [from, setFrom] = useState("text");
  const [text, setText] = useState(SAMPLE);
  const [result, setResult] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [ranFor, setRanFor] = useState("");
  const spec = JSON.stringify(toTemplate(form));
  const graph = form.reads.kind === "graph";

  const run = async () => {
    if (from === "text" && !text.trim()) { setError("Paste some text to try it on."); return; }
    setRunning(true); setError("");
    try {
      const t = toTemplate(form);
      const r = await contextV2Api.previewExtractionTemplate({
        name: t.name || "preview", source: t.source, mechanism: t.mechanism, emit: t.emit,
        sample_text: from === "text" ? text : "", ...(from !== "text" ? { sample_asset: from } : {}),
      });
      setResult(r); setRanFor(spec);
    } catch (e) { setResult(null); setError(errorText(e)); } finally { setRunning(false); }
  };

  const fixedKinds = new Set(form.kinds.filter((k) => !k.byModel && k.type.trim()).map((k) => k.type.trim().toLowerCase()));
  const fixedRels = new Set(form.conns.filter((c) => !c.byModel).map((c) => c.rel.trim().toUpperCase()));
  const known = new Set(knownTypes.map((t) => t.toLowerCase()));
  const knownRel = new Set(knownRelations.map((r) => r.toUpperCase()));
  const nodeLabel = new Map((result?.nodes || []).map((n) => [n.id, n]));
  const kindTag = (type: string, markNew = true) => (
    <><Tag tone={fixedKinds.has(type.toLowerCase()) || form.method !== "model" ? "thing" : "model"}>{type}</Tag>
      {markNew && !fixedKinds.has(type.toLowerCase()) && !known.has(type.toLowerCase()) && <> <Tag tone="new">new kind</Tag></>}</>
  );
  const end = (label: string, id?: string | null) => {
    if (id === "source" || label === "Sample") return <Tag tone="input">the input</Tag>;
    const n = id ? nodeLabel.get(id) : undefined;
    return n ? <span>{kindTag(n.type, false)} {n.label}</span> : <span className="text-stone-700 dark:text-stone-300">{label}</span>;
  };
  // a field that only repeats the thing's name (a pattern's match) says nothing new
  const shownOf = (n: PNode) => n.attributes.filter((a) => a.filled && !["name", "type"].includes(a.key) && a.value !== n.label);

  return (
    <section aria-label="Try it" className="rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900 p-4 sm:p-5">
      <h2 className="font-semibold text-stone-900 dark:text-stone-100">Try it</h2>
      <p className="mb-3 text-sm text-stone-500">Runs this extractor as it is now. Nothing is saved or written.</p>
      <div className="flex gap-2">
        <select value={from} onChange={(e) => setFrom(e.target.value)} aria-label="Try it on" className={input}>
          <option value="text">Pasted text</option>
          {!graph && assets.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <Button variant="primary" icon={Play} busy={running} onClick={run}>Run</Button>
      </div>
      {from === "text" && (
        <textarea rows={4} value={text} onChange={(e) => setText(e.target.value)} aria-label="Text to try it on" className={`${input} mt-2`} />
      )}
      {result && ranFor !== spec && <p className="mt-2 text-[12.5px] text-amber-800 dark:text-amber-300">Changed since this run. Run again to see it now.</p>}
      {error && (
        <p role="alert" className="mt-3 flex items-start gap-1.5 rounded-lg bg-red-50 dark:bg-red-950 px-3 py-2 text-sm text-red-800 dark:text-red-300">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" /> {error}
        </p>
      )}
      {result && (
        <div className="mt-4 flex flex-col gap-4 text-sm">
          {result.error && <p role="alert" className="rounded-lg bg-red-50 dark:bg-red-950 px-3 py-2 text-red-800 dark:text-red-300">{result.error}</p>}
          {from !== "text" && result.sample && (
            <p className="rounded-lg bg-stone-50 dark:bg-stone-950 px-3 py-2 text-[13px] text-stone-500 line-clamp-4">“{result.sample}”</p>
          )}
          <div>
            <h3 className="mb-1.5 text-[13px] font-semibold uppercase tracking-wide text-stone-500">Things ({result.nodes.length})</h3>
            {result.nodes.length ? (
              <ul className="flex flex-col divide-y divide-stone-100 dark:divide-stone-800">
                {result.nodes.map((n) => (
                  <li key={n.id} className="py-1.5">
                    {kindTag(n.type)} <span className="font-medium text-stone-900 dark:text-stone-100">{n.label}</span>
                    {shownOf(n).length > 0 && (
                      <span className="ml-1 text-stone-500"> · {shownOf(n).map((a) => (
                        <span key={a.key} className="mr-2"><span className="font-mono text-[12.5px]">{a.key}</span> {a.value}</span>))}</span>
                    )}
                  </li>
                ))}
              </ul>
            ) : <p className="text-stone-500">None found.</p>}
          </div>
          {result.edges.length > 0 && (
            <div>
              <h3 className="mb-1.5 text-[13px] font-semibold uppercase tracking-wide text-stone-500">Connections ({result.edges.length})</h3>
              <ul className="flex flex-col divide-y divide-stone-100 dark:divide-stone-800">
                {result.edges.map((e, i) => (
                  <li key={i} className="flex flex-wrap items-center gap-1.5 py-1.5">
                    {end(e.from, e.from_id)}
                    <span className="font-mono text-[12.5px] text-stone-600 dark:text-stone-300">—{e.rel}→</span>
                    {!fixedRels.has(e.rel.toUpperCase()) && !knownRel.has(e.rel.toUpperCase()) && <Tag tone="new">new</Tag>}
                    {end(e.to, e.to_id)}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {result.source_attributes.length > 0 && (
            <div>
              <h3 className="mb-1.5 text-[13px] font-semibold uppercase tracking-wide text-stone-500">On the input</h3>
              {result.source_attributes.map((a) => (
                <p key={a.key}><span className="font-mono text-[12.5px]">{a.key}</span> {a.filled ? a.value : <span className="text-stone-400">not stated</span>}</p>
              ))}
            </div>
          )}
          {result.warnings.map((w, i) => <p key={i} className="text-[13px] text-amber-800 dark:text-amber-300">{w}</p>)}
        </div>
      )}
    </section>
  );
}
