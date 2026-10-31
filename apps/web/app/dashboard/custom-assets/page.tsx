"use client";

import { useEffect, useState } from "react";
import { Network, Settings2, Plus, Workflow, SlidersHorizontal, Search, X, CheckCircle2, AlertCircle } from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import { useExtractorDraft, type TabId, type ExtractorDraft } from "./useExtractorDraft";
import ExtractorStudio from "./ExtractorStudio";
import SimpleExtractor, { fitsSimple } from "./SimpleExtractor";
import { FULL_UI } from "@/lib/simple";
import RelationshipStudio from "./RelationshipStudio";
import { CustomList, RelationshipList, BuiltinList } from "./LibraryViews";

/** Saved / deleted / failed. Successes clear themselves; errors stay until dismissed. */
function Notice({ d }: { d: ExtractorDraft }) {
  const { notice, setNotice } = d;
  useEffect(() => {
    if (!notice || notice.kind === "error") return;
    const t = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(t);
  }, [notice, setNotice]);
  if (!notice) return null;
  const err = notice.kind === "error";
  return (
    <div role={err ? "alert" : "status"}
      className={`fixed bottom-24 right-6 z-50 max-w-sm flex items-start gap-2.5 rounded-xl border px-4 py-3 text-sm shadow-lg ${err ? "bg-rose-50 border-rose-200 text-rose-800" : "bg-white border-stone-200 text-stone-800"}`}>
      {err ? <AlertCircle size={17} className="shrink-0 mt-0.5 text-rose-600" /> : <CheckCircle2 size={17} className="shrink-0 mt-0.5 text-emerald-600" />}
      <p className="flex-1 [overflow-wrap:anywhere]">{notice.text}</p>
      <button type="button" onClick={() => setNotice(null)} className="shrink-0 text-stone-400 hover:text-stone-700" aria-label="Dismiss"><X size={15} /></button>
    </div>
  );
}

export default function DeclarativeExtractionStudio() {
  const d = useExtractorDraft();
  const [q, setQ] = useState("");
  // The short form unless the extractor needs more than it shows (decided when the editor opens).
  const [full, setFull] = useState(FULL_UI);
  useEffect(() => { if (d.isAdding) setFull(FULL_UI || !fitsSimple(d)); }, [d.isAdding]);  // eslint-disable-line react-hooks/exhaustive-deps

  const counts = {
    extractions: d.savedExtractions.filter((e: any) => !e.name.startsWith("builtin:") && !String(e.mechanism?.ref || "").startsWith("relate.")).length,
    relationship: d.savedExtractions.filter((e: any) => String(e.mechanism?.ref || "").startsWith("relate.")).length,
    "built-in": d.savedExtractions.filter((e: any) => e.name.startsWith("builtin:")).length,
  };
  const tabs: { id: TabId; label: string; icon: any }[] = [
    { id: "extractions", label: "Your extractors", icon: Workflow },
    { id: "relationship", label: "Relationships", icon: Network },
    { id: "built-in", label: "Built-in", icon: Settings2 },
  ];
  const editing = d.isAdding || d.isAddingRel;

  const newAction = d.activeTab === "extractions"
    ? <Button variant="primary" icon={Plus} onClick={d.startNewExtraction}>New extractor</Button>
    : d.activeTab === "relationship"
      ? <Button variant="primary" icon={Plus} onClick={() => { d.resetRel(); d.setIsAddingRel(true); }}>New relationship</Button>
      : null;

  return (
    <div className={`${editing ? "max-w-[1600px]" : "max-w-6xl"} mx-auto ${editing ? "" : "pb-32"}`}>
      <PageHeader
        title="Extractors"
        icon={SlidersHorizontal}
        description="Pull out what you need from documents and records."
        actions={!editing ? newAction : undefined}
      />

      <div className="flex flex-wrap items-end gap-3 border-b border-stone-200 mb-5">
        <div className="flex gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden" role="tablist">
          {tabs.map(t => {
            const on = d.activeTab === t.id;
            return (
              <button key={t.id} role="tab" aria-selected={on}
                onClick={() => { d.setActiveTab(t.id); d.setIsAdding(false); d.setIsAddingRel(false); }}
                className={`shrink-0 whitespace-nowrap px-3.5 py-2.5 text-sm font-semibold border-b-2 -mb-px transition-colors flex items-center gap-2 ${on ? "border-primary text-stone-900" : "border-transparent text-stone-500 hover:text-stone-800"}`}>
                <t.icon size={15} className={on ? "text-primary" : ""} /> {t.label}
                <span className={`text-[11px] font-semibold tabular-nums rounded-full px-1.5 ${on ? "bg-primary/10 text-primary" : "bg-stone-100 text-stone-500"}`}>{counts[t.id]}</span>
                {t.id === "built-in" && FULL_UI && d.proposals.length > 0 && (
                  <span className="text-[11px] font-semibold rounded-full px-1.5 bg-amber-100 text-amber-800">{d.proposals.length} to review</span>
                )}
              </button>
            );
          })}
        </div>
        {!editing && (
          <label className="ml-auto mb-2 flex items-center gap-2 border border-stone-200 bg-white rounded-lg px-2.5 py-1.5 w-full sm:w-72 focus-within:ring-2 focus-within:ring-primary/25">
            <Search size={14} className="text-stone-400 shrink-0" />
            <input value={q} onChange={e => setQ(e.target.value)} placeholder="Filter by name, node type or relationship"
              className="w-full bg-transparent text-sm outline-none" aria-label="Filter extractors" />
          </label>
        )}
      </div>

      {d.activeTab === "extractions" && (d.isAdding
        ? (full ? <ExtractorStudio d={d} /> : <SimpleExtractor d={d} onFull={() => setFull(true)} />)
        : <CustomList d={d} q={q} />)}
      {d.activeTab === "relationship" && (d.isAddingRel ? <RelationshipStudio d={d} /> : <RelationshipList d={d} q={q} />)}
      {/* A built-in is edited by the SAME editor as a custom one — no second, cut-down built-in form. */}
      {d.activeTab === "built-in" && (d.isAdding ? <ExtractorStudio d={d} /> : <BuiltinList d={d} q={q} />)}

      <Notice d={d} />
    </div>
  );
}
