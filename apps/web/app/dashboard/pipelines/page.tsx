"use client";

import { SHOW_DEFAULT_PIPELINES } from "@/lib/simple";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { contextV2Api, Pipeline, SYSTEM_DEFAULT_PIPELINES_LIST } from "@/lib/api/contextV2";
import {
  GitBranch, FileText, Database, Braces, Plus, Trash2, Pencil, Copy, Layers,
  Clock, Search, ArrowRight, Sparkles
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";

const SYSTEM_DEFAULT_PIPELINES = SYSTEM_DEFAULT_PIPELINES_LIST;

export default function PipelinesDashboard() {
  const router = useRouter();
  const [items, setItems] = useState<Pipeline[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "document" | "structured" | "semistructured">("all");
  const [busy, setBusy] = useState<string | null>(null);

  const fetchItems = async () => {
    try {
      const data = await contextV2Api.listPipelines();
      const loaded = data?.items || [];
      // The server's copy of a default wins: it carries the real stages (and their count). The client
      // constant is only a fallback for a server that has not seeded its defaults yet.
      const byId = new Map(loaded.map((p: any) => [p.id, p]));
      const combined = [
        ...(SHOW_DEFAULT_PIPELINES ? SYSTEM_DEFAULT_PIPELINES.map((d) => byId.get(d.id) || d) : []),
        ...loaded.filter((p: any) => !SYSTEM_DEFAULT_PIPELINES.some((d) => d.id === p.id)),
      ];
      setItems(combined);
    } catch (e) {
      setItems(SHOW_DEFAULT_PIPELINES ? SYSTEM_DEFAULT_PIPELINES : []);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchItems(); }, []);

  const onDelete = async (p: Pipeline) => {
    if (!confirm(`Delete pipeline "${p.name}"? This only removes the saved design — already-ingested data is unaffected.`)) return;
    setBusy(p.id);
    try {
      await contextV2Api.deletePipeline(p.id);
      setItems((xs) => xs.filter((x) => x.id !== p.id));
    } catch {
      alert("Failed to delete pipeline.");
    } finally {
      setBusy(null);
    }
  };

  const onDuplicate = async (p: Pipeline) => {
    setBusy(p.id);
    try {
      await contextV2Api.createPipeline({
        name: `${p.name} (copy)`, description: p.description, kind: p.kind, stages: p.stages,
      });
      await fetchItems();
    } catch {
      alert("Failed to duplicate.");
    } finally {
      setBusy(null);
    }
  };

  const visible = items.filter((p) =>
    (filter === "all" || p.kind === filter) &&
    (!query || p.name.toLowerCase().includes(query.toLowerCase()) ||
      (p.description || "").toLowerCase().includes(query.toLowerCase())));

  const counts = {
    all: items.length,
    document: items.filter((p) => p.kind === "document").length,
    structured: items.filter((p) => p.kind === "structured").length,
    semistructured: items.filter((p) => p.kind === "semistructured").length,
  };

  return (
    <div className="max-w-7xl mx-auto pb-20">
      <PageHeader
        title="Pipelines"
        icon={GitBranch}
        description="Design reusable extraction pipelines once, then assign them to any source from Ingestion. Every stage runs on top of a prior step's output — for both documents and structured data."
        actions={
          <Button variant="primary" icon={Plus}
            onClick={() => router.push("/dashboard/pipelines/new" as any)}>New Pipeline</Button>
        }
      />

      {/* Controls */}
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 mb-6">
        <div className="inline-flex bg-stone-100 rounded-lg p-1 text-sm">
          {([["all", "All"], ["structured", "Structured"], ["semistructured", "Semi-Structured"], ["document", "Unstructured"]] as const).map(([k, l]) => (
            <button key={k} onClick={() => setFilter(k)}
              className={`px-4 py-1.5 rounded-md font-medium transition-colors ${filter === k ? "bg-white shadow-sm text-stone-900" : "text-stone-500 hover:text-stone-700"}`}>
              {l} <span className="text-xs text-stone-400">{counts[k]}</span>
            </button>
          ))}
        </div>
        <div className="relative sm:w-72">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-stone-400" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search pipelines…"
            className="w-full pl-9 pr-3 py-2 border border-stone-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20" />
        </div>
      </div>

      {/* List */}
      {loading ? (
        <div className="text-stone-500 animate-pulse p-8">Loading pipelines…</div>
      ) : visible.length === 0 ? (
        <div className="bg-stone-50 border border-dashed border-stone-300 rounded-2xl p-12 text-center text-stone-500 flex flex-col items-center">
          <Sparkles size={44} className="mb-4 text-stone-300" />
          <h2 className="text-xl font-bold mb-2 text-stone-700">
            {items.length === 0 ? "No pipelines yet" : "No matches"}
          </h2>
          <p className="max-w-md">
            {items.length === 0
              ? "Create a document or structured pipeline above. You'll then be able to assign it to a source in the Ingestion Hub."
              : "Try a different filter or search term."}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {visible.map((p) => {
            const isStruct = p.kind === "structured";
            const isSemi = p.kind === "semistructured";
            return (
              <div key={p.id}
                onClick={() => router.push(`/dashboard/pipelines/${p.id}` as any)}
                className="group bg-white border border-stone-200 rounded-xl shadow-sm hover:shadow-md hover:border-emerald-300 transition-all cursor-pointer flex flex-col">
                <div className="p-5 flex-1">
                  <div className="flex items-start justify-between mb-3">
                    <div className={`p-2.5 rounded-lg ${isSemi ? "bg-violet-100 text-violet-600" : isStruct ? "bg-emerald-100 text-emerald-600" : "bg-teal-100 text-teal-600"}`}>
                      {isSemi ? <Braces size={20} /> : isStruct ? <Database size={20} /> : <FileText size={20} />}
                    </div>
                    <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-1 rounded-full ${isSemi ? "bg-violet-50 text-violet-600" : isStruct ? "bg-emerald-50 text-emerald-600" : "bg-teal-50 text-teal-600"}`}>
                      {isSemi ? "Semi-Structured" : isStruct ? "Structured" : "Unstructured"}
                    </span>
                  </div>
                  <h3 className="font-bold text-stone-900 truncate group-hover:text-emerald-600 transition-colors">{p.name}</h3>
                  <p className="text-sm text-stone-500 mt-1 line-clamp-2 min-h-[2.5rem]">
                    {p.description || "No description."}
                  </p>
                  <div className="flex items-center gap-4 mt-4 text-xs text-stone-400">
                    <span className="flex items-center gap-1.5"><Layers size={13} /> {(() => { const n = p.stage_count ?? p.stages?.length ?? 0; return `${n} stage${n === 1 ? "" : "s"}`; })()}</span>
                    {p.updated_at && (
                      <span className="flex items-center gap-1.5"><Clock size={13} /> {new Date(p.updated_at).toLocaleDateString()}</span>
                    )}
                  </div>
                </div>
                <div className="border-t border-stone-100 px-3 py-2 flex items-center justify-between">
                  <span className="text-xs font-semibold text-emerald-600 flex items-center gap-1 pl-2 opacity-0 group-hover:opacity-100 transition-opacity">
                    Edit <ArrowRight size={12} />
                  </span>
                  <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                    <button onClick={() => router.push(`/dashboard/pipelines/${p.id}` as any)}
                      title="Edit" className="p-1.5 text-stone-400 hover:text-emerald-600 hover:bg-emerald-50 rounded-md"><Pencil size={15} /></button>
                    <button onClick={() => onDuplicate(p)} disabled={busy === p.id}
                      title="Duplicate" className="p-1.5 text-stone-400 hover:text-stone-700 hover:bg-stone-100 rounded-md disabled:opacity-50"><Copy size={15} /></button>
                    <button onClick={() => onDelete(p)} disabled={busy === p.id}
                      title="Delete" className="p-1.5 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-md disabled:opacity-50"><Trash2 size={15} /></button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
