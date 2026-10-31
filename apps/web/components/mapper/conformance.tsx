"use client";

/**
 * Does the graph this asset produced MATCH the mapping? Exact names, both directions.
 *
 * The question a mapping author actually has after a run is not "did it work" but "did it do what I
 * declared" — and those differ in two directions at once. `missing` is declared-but-absent: the rule
 * is not being honoured, or the data has no such content. `extra` is produced-but-never-declared,
 * which is the half people forget to look for and the half that quietly grows a graph nobody asked
 * for. `platform` is written regardless of any mapping (Date links, provenance, reading order) and
 * is explained rather than counted as a violation.
 *
 * The comparison is done server-side on purpose. An earlier version of this panel matched names in
 * the browser with an ALIAS rule — a produced `Chunk` satisfied a declared `TextChunk` because both
 * contain "chunk" — so it reported conformance over a graph that did not conform.
 */

import { useEffect, useState } from "react";
import { ShieldCheck, AlertTriangle, CheckCircle2, Loader2, Search } from "lucide-react";
import Button from "@/components/ui/Button";
import { contextV2Api, type MappingAsset, type MappingConformance } from "@/lib/api/contextV2";

const Names = ({ title, tone, nodes, edges, blurb }: {
  title: string; tone: "ok" | "warn" | "muted";
  nodes?: string[]; edges?: string[]; blurb: string;
}) => {
  const n = nodes || [], e = edges || [];
  const style = tone === "warn" ? "border-amber-300 bg-amber-50"
    : tone === "ok" ? "border-lime-300 bg-lime-50" : "border-stone-200 bg-stone-50";
  const text = tone === "warn" ? "text-amber-800" : tone === "ok" ? "text-lime-800" : "text-stone-600";
  return (
    <div className={`rounded-lg border px-3 py-2.5 ${style}`}>
      <div className={`text-[12px] font-semibold ${text}`}>
        {title} <span className="tabular-nums font-normal">({n.length + e.length})</span>
      </div>
      <p className={`text-[11.5px] mt-0.5 ${text} opacity-80`}>{blurb}</p>
      {!!(n.length || e.length) && (
        <div className="flex flex-wrap gap-1.5 mt-2">
          {n.map((x) => (
            <span key={`n${x}`} className="font-mono text-[11px] px-1.5 py-0.5 rounded bg-white border border-stone-200 text-stone-700">
              ⬢ {x}
            </span>
          ))}
          {e.map((x) => (
            <span key={`e${x}`} className="font-mono text-[11px] px-1.5 py-0.5 rounded bg-white border border-stone-200 text-stone-700">
              ──► {x}
            </span>
          ))}
        </div>
      )}
    </div>
  );
};

export default function ConformancePanel({ mappingId }: { mappingId: string }) {
  const [assetId, setAssetId] = useState("");
  const [files, setFiles] = useState<MappingAsset[]>([]);
  useEffect(() => { contextV2Api.mappingAssets().then(setFiles).catch(() => setFiles([])); }, []);
  const [res, setRes] = useState<MappingConformance | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const run = async () => {
    const typed = assetId.trim();
    const id = files.find((f) => f.name === typed)?.id || typed;
    if (!id) { setErr("Choose a file this mapping read."); return; }
    setBusy(true); setErr(""); setRes(null);
    try {
      setRes(await contextV2Api.schemaMappingConformance(mappingId, id));
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || "Could not read conformance for that asset");
    } finally { setBusy(false); }
  };

  return (
    <div>
      <div className="flex flex-wrap gap-2 items-center mb-3">
        <div className="relative flex-1 min-w-[280px]">
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-stone-400" />
          <input value={assetId} onChange={(e) => setAssetId(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") run(); }}
            placeholder="file name or asset id" list="conformance-files"
            className="w-full pl-8 pr-3 py-2 rounded-lg border border-stone-200 bg-white text-[12.5px]
                       focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary/40" />
        </div>
        <datalist id="conformance-files">{files.map((f) => <option key={f.id} value={f.name} />)}</datalist>
        <Button variant="secondary" size="sm" icon={ShieldCheck} busy={busy} onClick={run}>Check</Button>
      </div>

      {err && (
        <div className="flex items-start gap-2 px-3 py-2.5 rounded-lg border border-red-200 bg-red-50 text-[12.5px] text-red-800">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />{err}
        </div>
      )}

      {busy && !res && (
        <div className="flex items-center gap-2 text-[12.5px] text-stone-500">
          <Loader2 size={14} className="animate-spin" /> comparing the graph with the mapping…
        </div>
      )}

      {res && (
        <div className="grid gap-2.5">
          <div className={`flex items-center gap-2 px-3 py-2.5 rounded-lg border text-[12.5px] font-semibold
                           ${res.conformant ? "border-lime-300 bg-lime-50 text-lime-800"
                                            : "border-amber-300 bg-amber-50 text-amber-800"}`}>
            {res.conformant ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
            {res.conformant
              ? "This asset's graph matches the mapping exactly."
              : "This asset's graph does not match the mapping."}
            {res.strict && <span className="ml-auto font-normal opacity-80">strict mode on</span>}
          </div>
          <Names title="Declared but not produced" tone="warn" nodes={res.missing?.nodes} edges={res.missing?.edges}
            blurb="Either the rule is not being honoured, or this data has no such content." />
          <Names title="Produced but never declared" tone="warn" nodes={res.extra?.nodes} edges={res.extra?.edges}
            blurb="Written into the graph by something other than this mapping." />
          <Names title="Written by the platform" tone="muted" nodes={res.platform?.nodes} edges={res.platform?.edges}
            blurb="Date links, provenance and reading order — written regardless of any mapping, so not a violation." />
          <Names title="Declared" tone="ok" nodes={res.declared?.nodes} edges={res.declared?.edges}
            blurb="The vocabulary this mapping promises." />
        </div>
      )}
    </div>
  );
}
