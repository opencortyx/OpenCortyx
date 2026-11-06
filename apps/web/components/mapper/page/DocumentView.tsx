"use client";

/**
 * A document in the Mapper. A document has no fields to map: it is read by its layout - title,
 * section headings, paragraphs, list items, tables - and long paragraphs are split at sentence ends
 * into pieces of at most the split size. This shows that reading, lets the split size be changed
 * and the document read again with it, and shows what extractors found in it.
 */

import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import GraphPreview from "@/components/mapper/page/GraphPreview";
import FileExtractors from "@/components/mapper/page/FileExtractors";
import { contextV2Api, type DocumentReading } from "@/lib/api/contextV2";

/** Element types in the words a person uses. */
const ELEMENT: Record<string, string> = {
  Title: "Titles", SectionHeader: "Section headings", Text: "Paragraphs", Paragraph: "Paragraphs",
  ListItem: "List items", TableEntity: "Tables", Table: "Tables", Picture: "Pictures", Figure: "Pictures",
  Caption: "Captions", Footnote: "Footnotes", Code: "Code blocks", Formula: "Formulas",
  PageHeader: "Page headers", PageFooter: "Page footers", Reference: "References",
};
const SPLIT_MIN = 200;
const SPLIT_MAX = 8000;

const errorText = (e: any): string => e?.response?.data?.detail || e?.message || "Something went wrong";

export default function DocumentView({ assetId }: { assetId: string }) {
  const [doc, setDoc] = useState<DocumentReading | null>(null);
  const [err, setErr] = useState("");
  const [size, setSize] = useState(800);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const d = await contextV2Api.documentReading(assetId);
      setDoc(d); setSize(d.split_at); setErr("");
    } catch (e) { setErr(errorText(e)); }
  }, [assetId]);
  useEffect(() => { setDoc(null); setMsg(null); load(); }, [load]);

  const saveSize = async (readAgain: boolean) => {
    setBusy(readAgain ? "again" : "save"); setMsg(null);
    try {
      if (doc && size !== doc.split_at) await contextV2Api.updateConfig({ chunk_window_chars: size });
      if (readAgain) {
        await contextV2Api.readDocumentAgain(assetId);
        setMsg({ ok: true, text: `Reading ${doc?.name} again with paragraphs split at ${size.toLocaleString()} characters. Refresh in a moment to see it.` });
      } else {
        setMsg({ ok: true, text: `Saved. Documents read from now on split paragraphs at ${size.toLocaleString()} characters.` });
      }
      await load();
    } catch (e) { setMsg({ ok: false, text: errorText(e) }); } finally { setBusy(""); }
  };

  if (err) return <p className="rounded-xl border border-red-200 bg-red-50 px-3.5 py-3 text-sm text-red-800">{err}</p>;
  if (!doc) return <div className="flex justify-center py-16"><Loader2 className="animate-spin text-stone-400" /></div>;

  const changed = size !== doc.split_at;
  const valid = size >= SPLIT_MIN && size <= SPLIT_MAX;
  const nodes = [{ name: doc.name, count: doc.passages, event: false, inGraph: true, here: true, note: `${doc.passages} passages` },
    ...doc.found.slice(0, 9).map((k) => ({ name: k.type, count: k.count, event: false, inGraph: true, here: false, note: `${k.count.toLocaleString()} found` }))];
  // every arrow is the same "mentions": named once, in the heading, not on each arrow
  const links = doc.found.slice(0, 9).map((k) => ({ from: doc.name, to: k.type, name: "" }));

  return (<>
    <section aria-labelledby="doc-preview-h" className="rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900">
      <div className="flex flex-wrap items-baseline gap-x-3 px-4 pt-3">
        <h2 id="doc-preview-h" className="font-semibold text-stone-900 dark:text-stone-100">How it looks in the graph</h2>
        <p className="text-sm text-stone-500">{doc.found.length ? "The document, and the things extractors found mentioned in it." : "Nothing has been extracted from it yet."}</p>
      </div>
      <div className="overflow-x-auto px-2 pb-2"><GraphPreview nodes={nodes} links={links} /></div>
    </section>

    <section aria-labelledby="doc-read-h" className="rounded-xl border border-stone-200 dark:border-stone-800 bg-stone-50/60 dark:bg-stone-950 p-4">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3">
        <h2 id="doc-read-h" className="font-semibold text-stone-900 dark:text-stone-100">How {doc.name} was read</h2>
        <p className="text-sm text-stone-500">By its layout: headings, paragraphs, lists and tables, in reading order.</p>
        <button type="button" onClick={load} className="ml-auto inline-flex items-center gap-1 text-sm text-primary hover:underline">
          <RefreshCw size={13} /> Refresh
        </button>
      </div>

      <ul className="mb-4 flex flex-wrap gap-2" aria-label="What the document is made of">
        {doc.elements.map((e) => (
          <li key={e.type} className="inline-flex items-center gap-2 rounded-lg border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 px-2.5 py-1 text-sm">
            <span className="text-stone-700 dark:text-stone-300">{ELEMENT[e.type] || e.type}</span>
            <span className="font-semibold tabular-nums text-stone-900 dark:text-stone-100">{e.count.toLocaleString()}</span>
          </li>
        ))}
      </ul>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="rounded-xl border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 p-3.5">
          <h3 className="mb-2 text-sm font-semibold text-stone-900 dark:text-stone-100">Sections</h3>
          {doc.sections.length ? (
            <ol className="flex max-h-72 flex-col gap-1 overflow-y-auto text-sm">
              {doc.sections.map((s, i) => (
                <li key={i} className="flex items-baseline gap-2" style={{ paddingLeft: `${Math.max(0, s.level - 1) * 14}px` }}>
                  <span className="min-w-0 flex-1 truncate text-stone-800 dark:text-stone-200">{s.title || "(untitled)"}</span>
                  <span className="shrink-0 tabular-nums text-xs text-stone-500">{s.items} item{s.items === 1 ? "" : "s"}</span>
                </li>
              ))}
            </ol>
          ) : <p className="text-sm text-stone-500">No headings: the file has no marked-up titles or sections, so it is read as paragraphs in order.</p>}
          {doc.sections.length > 0 && doc.outside_sections > 0 && (
            <p className="mt-2 text-xs text-stone-500">{doc.outside_sections} before the first heading.</p>)}
        </div>

        <div className="rounded-xl border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 p-3.5">
          <h3 className="mb-2 text-sm font-semibold text-stone-900 dark:text-stone-100">Long paragraphs</h3>
          <p className="text-sm text-stone-600 dark:text-stone-400">
            {doc.passages.toLocaleString()} passages, {doc.average.toLocaleString()} characters on average, the longest {doc.longest.toLocaleString()}.
          </p>
          <label htmlFor={`split-${assetId}`} className="mt-3 flex flex-wrap items-center gap-2 text-sm text-stone-700 dark:text-stone-300">
            Split paragraphs longer than
            <input id={`split-${assetId}`} type="number" min={SPLIT_MIN} max={SPLIT_MAX} step={100} value={size}
              onChange={(e) => setSize(Number(e.target.value) || 0)}
              className="w-24 rounded-md border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 px-2 py-1 tabular-nums" />
            characters, at sentence ends.
          </label>
          <p className="mt-1 text-xs text-stone-500">
            {valid ? "Tables, code and headings are never split. The size applies to every document read from now on."
                   : `Between ${SPLIT_MIN.toLocaleString()} and ${SPLIT_MAX.toLocaleString()} characters.`}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
            {doc.can_read_again ? (
              <button type="button" disabled={!valid || !!busy} onClick={() => saveSize(true)}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1 font-semibold text-white disabled:opacity-40">
                {busy === "again" && <Loader2 size={13} className="animate-spin" />}
                {changed ? "Save and read this document again" : "Read this document again"}
              </button>
            ) : (<>
              <button type="button" disabled={!valid || !changed || !!busy} onClick={() => saveSize(false)}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1 font-semibold text-white disabled:opacity-40">
                {busy === "save" && <Loader2 size={13} className="animate-spin" />} Save
              </button>
              <span className="text-xs text-stone-500">The server no longer keeps this file: add it again to read it with a new size.</span>
            </>)}
          </div>
          {msg && <p className={`mt-2 text-sm ${msg.ok ? "text-emerald-700" : "text-red-700"}`}>{msg.text}</p>}
        </div>
      </div>

      {doc.samples.length > 0 && (
        <div className="mt-4">
          <h3 className="mb-2 text-sm font-semibold text-stone-900 dark:text-stone-100">First passages</h3>
          <ol className="flex flex-col gap-2">
            {doc.samples.map((t, i) => (
              <li key={i} className="rounded-lg border border-dashed border-stone-200 dark:border-stone-700 px-3 py-2 text-[13px] text-stone-700 dark:text-stone-300">
                {t}{t.length >= 400 ? "…" : ""}
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>

    <section aria-labelledby="doc-found-h" className="rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900 p-4">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3">
        <h2 id="doc-found-h" className="font-semibold text-stone-900 dark:text-stone-100">What extractors found in it</h2>
        <p className="text-sm text-stone-500">Turn a kind off under Found by extractors.</p>
      </div>
      <FileExtractors assetId={assetId} fileName={doc.name} kind="unstructured" found={doc.found} />
    </section>
  </>);
}
