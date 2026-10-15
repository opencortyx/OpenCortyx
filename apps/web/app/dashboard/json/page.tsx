"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { contextV2Api } from "@/lib/api/contextV2";
import {
  FileJson, UploadCloud, Hexagon, Play, Loader2, CheckCircle2,
  AlertCircle, ListTree, Tag, Ban, Braces,
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";

type Decision = "node" | "attribute" | "skip";

const isScalar = (v: any) => v === null || ["string", "number", "boolean"].includes(typeof v);
const jtype = (v: any): string => {
  if (v === null) return "null";
  if (Array.isArray(v)) return `array[${v.length}]${v.length ? ` of ${jtype(v[0])}` : ""}`;
  if (typeof v === "object") return `object{${Object.keys(v).length}}`;
  return typeof v;
};
const sampleStr = (v: any): string => {
  if (isScalar(v)) return String(v);
  try { return JSON.stringify(v).slice(0, 80); } catch { return ""; }
};

export default function JsonIngestPage() {
  const [file, setFile] = useState<File | null>(null);
  const [parsed, setParsed] = useState<any>(null);
  const [parseErr, setParseErr] = useState("");
  const [mode, setMode] = useState<"auto" | "guided">("auto");

  // shared
  const [rootType, setRootType] = useState("");
  const [maxDepth, setMaxDepth] = useState(6);
  const [maxArray, setMaxArray] = useState(500);
  const [maxRecords, setMaxRecords] = useState(50);
  const [offset, setOffset] = useState(0);
  const [embedNodes, setEmbedNodes] = useState(false);
  // guided
  const [mapping, setMapping] = useState<Record<string, Decision>>({});
  // semi-structured pipeline (optional): structure graph + READ the text through its extractors
  const [pipelines, setPipelines] = useState<any[]>([]);
  const [pipelineId, setPipelineId] = useState("");

  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; type: "ok" | "err" } | null>(null);

  useEffect(() => {
    contextV2Api.listPipelines("semistructured")
      .then((r: any) => setPipelines(r?.items || []))
      .catch(() => {});
  }, []);

  const onFile = async (f: File | null) => {
    setMsg(null); setParseErr(""); setParsed(null); setFile(f);
    if (!f) return;
    try {
      const text = await f.text();
      const data = JSON.parse(text);
      setParsed(data);
      const record = Array.isArray(data) ? data[0] : data;
      if (record && typeof record === "object" && !Array.isArray(record)) {
        const m: Record<string, Decision> = {};
        Object.entries(record).forEach(([k, v]) => { m[k] = isScalar(v) ? "attribute" : "node"; });
        setMapping(m);
      }
    } catch (e: any) {
      setParseErr(`Not valid JSON: ${e?.message || e}`);
    }
  };

  const record = parsed ? (Array.isArray(parsed) ? parsed[0] : parsed) : null;
  const rootKind = Array.isArray(parsed) ? `array[${parsed.length}] records` : parsed ? "object" : "";

  // size math (cheap estimate from average record size, so we never re-stringify a huge array)
  const fileMb = file ? file.size / (1024 * 1024) : 0;
  const totalRecords = Array.isArray(parsed) ? parsed.length : parsed ? 1 : 0;
  const sliceCount = Array.isArray(parsed)
    ? Math.max(0, Math.min(maxRecords, totalRecords - offset)) : totalRecords;
  const avgBytes = file && totalRecords ? file.size / totalRecords : 0;
  const sliceMb = Array.isArray(parsed) ? (avgBytes * sliceCount) / (1024 * 1024) : fileMb;
  const overLimit = sliceMb > 45;
  const keys: { key: string; type: string; sample: string; scalar: boolean }[] =
    record && typeof record === "object" && !Array.isArray(record)
      ? Object.entries(record).map(([k, v]) => ({ key: k, type: jtype(v), sample: sampleStr(v), scalar: isScalar(v) }))
      : [];

  const ingest = async () => {
    if (!file || parsed == null) return;
    setBusy(true); setMsg(null);
    try {
      // slice client-side so a huge file is never uploaded whole; send the (small) JSON as a body.
      const data = Array.isArray(parsed) ? parsed.slice(offset, offset + maxRecords) : parsed;
      const approxMb = JSON.stringify(data).length / (1024 * 1024);
      if (approxMb > 45) {
        setMsg({ text: `Selected ${Array.isArray(data) ? data.length : 1} record(s) ≈ ${approxMb.toFixed(0)} MB — over the 50 MB request limit. Lower "Max records" and ingest in batches (the full LongMemEval set is ~264 MB).`, type: "err" });
        setBusy(false);
        return;
      }
      const opts: Record<string, any> = {
        max_depth: maxDepth, max_array: maxArray, embed_nodes: embedNodes,
      };
      if (rootType.trim()) opts.root_type = rootType.trim();
      if (mode === "guided") {
        opts.mapping = mapping;
      }
      if (pipelineId) opts.pipeline_id = pipelineId;   // semi-structured: walk + READ + extract
      // One batch id per FILE, so ingesting it in slices ("Max records" batches) accumulates into a
      // single one-time ingestion entry instead of one per slice.
      const r = await contextV2Api.ingestJsonInline(data, file.name, opts, `json:${file.name}:${file.size}`);
      setMsg({
        text: `Dispatched (${r.status}) — ${Array.isArray(data) ? data.length : 1} record(s) (~${approxMb.toFixed(1)} MB). ` +
          (pipelineId ? "Walking the structure AND extracting knowledge through the pipeline; watch the Runs page."
            : "Walking it into the graph; watch the Runs page."),
        type: "ok",
      });
    } catch (e: any) {
      setMsg({ text: `Failed: ${e?.response?.data?.detail || e?.message || e}`, type: "err" });
    } finally {
      setBusy(false);
    }
  };

  const ON_TONE: Record<Decision, string> = {
    node: "bg-emerald-600 text-white border-emerald-700",
    attribute: "bg-teal-600 text-white border-teal-700",
    skip: "bg-stone-500 text-white border-stone-600",
  };
  const decBtn = (k: string, d: Decision, icon: any, label: string) => {
    const Icon = icon;
    const on = mapping[k] === d;
    return (
      <button onClick={() => setMapping({ ...mapping, [k]: d })}
        className={`flex items-center gap-1 px-2 py-1 rounded text-[11px] font-semibold border transition-colors ${on ? ON_TONE[d] : "bg-white text-stone-500 border-stone-200 hover:bg-stone-50"}`}>
        <Icon size={12} /> {label}
      </button>
    );
  };

  return (
    <div className="max-w-7xl mx-auto pb-24">
      <PageHeader
        title="JSON Ingestion"
        icon={FileJson}
        description="Turn any JSON — a file or an API payload — into a hierarchical graph. Auto walks the whole structure for you; Guided lets you map each top-level key to a node, an attribute, or skip it."
      />

      {/* file picker */}
      <div className="bg-white border border-stone-200 rounded-xl p-6 shadow-sm mb-6">
        <label className="block">
          <div className="border-2 border-dashed border-stone-200 rounded-xl p-8 text-center cursor-pointer hover:border-emerald-300 hover:bg-emerald-50/30 transition-colors">
            <UploadCloud className="mx-auto text-stone-400 mb-2" size={28} />
            <div className="text-sm font-semibold text-stone-700">{file ? file.name : "Choose a .json file"}</div>
            <div className="text-xs text-stone-400 mt-1">{file ? `${(file.size / 1024).toFixed(0)} KB` : "or drop it here"}</div>
          </div>
          <input type="file" accept=".json,application/json" className="hidden"
            onChange={(e) => onFile(e.target.files?.[0] || null)} />
        </label>
        {parseErr && <div className="mt-3 text-sm text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2 flex items-center gap-2"><AlertCircle size={15} /> {parseErr}</div>}
      </div>

      {parsed && (
        <>
          {/* structure analysis */}
          <div className="bg-white border border-stone-200 rounded-xl p-6 shadow-sm mb-6">
            <div className="flex items-center gap-2 mb-3">
              <ListTree size={18} className="text-emerald-500" />
              <h2 className="font-semibold text-stone-800">Detected structure</h2>
              <span className="text-xs text-stone-400">root: {rootKind}</span>
            </div>
            <div className="overflow-hidden rounded-lg border border-stone-100">
              <table className="w-full text-sm">
                <thead className="bg-stone-50 text-stone-400 text-[11px] uppercase tracking-wider">
                  <tr><th className="text-left px-3 py-2 font-bold">Key</th><th className="text-left px-3 py-2 font-bold">Type</th><th className="text-left px-3 py-2 font-bold">Sample</th>{mode === "guided" && <><th className="text-left px-3 py-2 font-bold">Map as</th></>}</tr>
                </thead>
                <tbody>
                  {keys.map((k) => (
                    <tr key={k.key} className="border-t border-stone-100">
                      <td className="px-3 py-2 font-mono text-stone-800">{k.key}</td>
                      <td className="px-3 py-2 font-mono text-[11px] text-stone-500">{k.type}</td>
                      <td className="px-3 py-2 text-stone-400 truncate max-w-[180px]">{k.sample}</td>
                      {mode === "guided" && (
                        <>
                          <td className="px-3 py-2">
                            <div className="flex gap-1">
                              {decBtn(k.key, "node", Hexagon, "node")}
                              {decBtn(k.key, "attribute", Tag, "attr")}
                              {decBtn(k.key, "skip", Ban, "skip")}
                            </div>
                          </td>
                        </>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {keys.length === 0 && <p className="text-sm text-stone-400 mt-2">Root has no object record to inspect.</p>}
          </div>

          {/* mode + options */}
          <div className="bg-white border border-stone-200 rounded-xl p-6 shadow-sm mb-6">
            <div className="inline-flex bg-stone-100 rounded-lg p-1 text-sm mb-5">
              <button onClick={() => setMode("auto")} className={`px-4 py-1.5 rounded-md font-bold transition-colors ${mode === "auto" ? "bg-emerald-600 text-white" : "text-stone-500 hover:text-stone-700"}`}>Auto (Mode B)</button>
              <button onClick={() => setMode("guided")} className={`px-4 py-1.5 rounded-md font-bold transition-colors ${mode === "guided" ? "bg-emerald-600 text-white" : "text-stone-500 hover:text-stone-700"}`}>Guided (Mode A)</button>
            </div>
            <p className="text-xs text-stone-500 mb-4">{mode === "auto"
              ? "The walker decides everything: objects → nodes, scalars → attributes, arrays → child nodes + NEXT chains, parallel arrays zipped, ids deterministic, *_id values → REFERENCES."
              : "You decide per key (in the table above): make it a node, fold it onto the record as an attribute, or skip it. Everything deeper stays automatic."}</p>

            {/* semi-structured pipeline: structure graph + READ the text through its extractors */}
            <div className="border border-violet-200 bg-violet-50/50 rounded-lg p-4 mb-5">
              <div className="flex items-center gap-2 mb-1">
                <Braces size={15} className="text-violet-600" />
                <span className="text-sm font-semibold text-stone-800">Extraction pipeline (optional)</span>
              </div>
              <p className="text-[11px] text-stone-500 mb-2">
                Without a pipeline, only the structure graph is built (no reading, no embeddings of content).
                With a <b>Semi-structured pipeline</b>, records are matched to its structure cards, text-role keys
                become chunks, and its extractors run — entities, claims, and any custom extractors it names.
              </p>
              <div className="flex items-center gap-2">
                <select value={pipelineId} onChange={(e) => setPipelineId(e.target.value)}
                  className="border border-stone-200 rounded-md text-sm px-3 py-1.5 bg-white focus:ring-2 focus:ring-violet-500/20 min-w-[260px]">
                  <option value="">No pipeline — structure only</option>
                  {pipelines.map((p: any) => <option key={p.id} value={p.id}>{p.name} ({p.stage_count} stages)</option>)}
                </select>
                <Link href="/dashboard/pipelines/semistructured" className="text-[11px] font-semibold text-violet-600 hover:underline shrink-0">+ New pipeline</Link>
              </div>
            </div>

            {/* size / batch summary */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
              <div className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2">
                <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">File size</div>
                <div className="text-sm font-mono text-stone-700">{fileMb.toFixed(1)} MB</div>
              </div>
              <div className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2">
                <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">Total records</div>
                <div className="text-sm font-mono text-stone-700">{totalRecords.toLocaleString()}</div>
              </div>
              <div className="bg-stone-50 border border-stone-100 rounded-lg px-3 py-2">
                <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">This batch</div>
                <div className="text-sm font-mono text-stone-700">{Array.isArray(parsed) ? `${offset}–${offset + sliceCount} (${sliceCount})` : "1"}</div>
              </div>
              <div className={`rounded-lg px-3 py-2 border ${overLimit ? "bg-red-50 border-red-200" : "bg-stone-50 border-stone-100"}`}>
                <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400">Batch size (est.)</div>
                <div className={`text-sm font-mono ${overLimit ? "text-red-600 font-bold" : "text-stone-700"}`}>~{sliceMb.toFixed(1)} MB{overLimit ? " ⚠" : ""}</div>
              </div>
            </div>
            {overLimit && <p className="text-xs text-red-600 -mt-3 mb-4">Estimated batch exceeds the 50 MB request limit. Lower <b>Max records</b> (or raise the offset) to send a smaller slice.</p>}

            <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
              <div>
                <label className="block text-[11px] font-bold text-stone-500 uppercase tracking-wider mb-1">Record type {mode === "guided" ? "" : "(optional)"}</label>
                <input value={rootType} onChange={(e) => setRootType(e.target.value)} placeholder="e.g. Instance / Record" className="w-full border border-stone-200 rounded-md text-sm px-3 py-1.5 focus:ring-2 focus:ring-emerald-500/20" />
              </div>
              <div>
                <label className="block text-[11px] font-bold text-stone-500 uppercase tracking-wider mb-1">Max depth</label>
                <input type="number" min={1} max={12} value={maxDepth} onChange={(e) => setMaxDepth(parseInt(e.target.value || "6", 10))} className="w-full border border-stone-200 rounded-md text-sm px-3 py-1.5 font-mono focus:ring-2 focus:ring-emerald-500/20" />
              </div>
              <div>
                <label className="block text-[11px] font-bold text-stone-500 uppercase tracking-wider mb-1">Max array / level</label>
                <input type="number" min={1} max={100000} value={maxArray} onChange={(e) => setMaxArray(parseInt(e.target.value || "500", 10))} className="w-full border border-stone-200 rounded-md text-sm px-3 py-1.5 font-mono focus:ring-2 focus:ring-emerald-500/20" />
              </div>
              <div>
                <label className="block text-[11px] font-bold text-stone-500 uppercase tracking-wider mb-1">Max records {Array.isArray(parsed) ? `(of ${parsed.length})` : ""}</label>
                <input type="number" min={1} value={maxRecords} onChange={(e) => setMaxRecords(parseInt(e.target.value || "50", 10))} className="w-full border border-stone-200 rounded-md text-sm px-3 py-1.5 font-mono focus:ring-2 focus:ring-emerald-500/20" />
              </div>
              {Array.isArray(parsed) && (
                <div>
                  <label className="block text-[11px] font-bold text-stone-500 uppercase tracking-wider mb-1">Start at (offset)</label>
                  <input type="number" min={0} value={offset} onChange={(e) => setOffset(parseInt(e.target.value || "0", 10))} className="w-full border border-stone-200 rounded-md text-sm px-3 py-1.5 font-mono focus:ring-2 focus:ring-emerald-500/20" />
                </div>
              )}
              <div className="col-span-2 flex items-end">
                <label className="flex items-center gap-2 text-sm text-stone-700 cursor-pointer">
                  <input type="checkbox" checked={embedNodes} onChange={(e) => setEmbedNodes(e.target.checked)} className="text-amber-600 focus:ring-amber-500" />
                  Embed each node
                  <span className="text-xs text-amber-600">⚠ one embedding call per node — for conversation data that&apos;s hundreds per record (very slow). Leave OFF; nodes are still text-searchable.</span>
                </label>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-4">
            <Button variant="primary" icon={Play} busy={busy} disabled={!file} onClick={ingest}>{busy ? "Dispatching…" : `Ingest (${mode === "auto" ? "Auto" : "Guided"})`}</Button>
            {msg && (
              <div className={`flex items-center gap-2 text-sm font-medium rounded-lg px-3 py-2 ${msg.type === "ok" ? "text-emerald-700 bg-emerald-50 border border-emerald-100" : "text-red-700 bg-red-50 border border-red-100"}`}>
                {msg.type === "ok" ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />} {msg.text}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
