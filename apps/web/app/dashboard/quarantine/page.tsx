"use client";

import { useEffect, useState } from "react";
import { contextV2Api } from "@/lib/api/contextV2";
import { AlertCircle, CheckCircle2, Trash2, RefreshCw, ChevronDown, ChevronUp, FileText, Wand2 } from "lucide-react";
import ReviewTabs from "@/components/layout/ReviewTabs";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";

interface QuarantineItem {
  id: string;
  chunk_node_id: string | null;
  error_type: string;
  error_message: string;
  failed_output: string;
  chunk_text: string;
  extractor_type: string;
  template_name?: string;
  created_at: string;
}

export default function QuarantinePage() {
  const [items, setItems] = useState<QuarantineItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [editData, setEditData] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [writesOpen, setWritesOpen] = useState<number | null>(null);

  const fetchItems = () => {
    setLoading(true);
    contextV2Api.getQuarantine().then(data => {
      setItems(data?.items || []);
      setLoading(false);
    }).catch(err => {
      console.error(err);
      setLoading(false);
    });
  };

  useEffect(() => {
    fetchItems();
  }, []);

  const handleDismiss = async (id: string) => {
    if (!confirm("Are you sure you want to dismiss this anomaly? It will be deleted permanently.")) return;
    try {
      await contextV2Api.dismissQuarantine(id);
      setItems(items.filter(i => i.id !== id));
    } catch (err) {
      alert("Failed to dismiss anomaly.");
    }
  };

  const handleRetry = async (id: string) => {
    const item = items.find(i => i.id === id);
    if (!item) return;
    
    const correctedJson = editData[id] || item.failed_output;
    setSubmitting(id);
    try {
      const res = await contextV2Api.retryQuarantine(id, correctedJson);
      if (res.status === "ok") {
        alert("Extraction succeeded! Anomaly resolved.");
        setItems(items.filter(i => i.id !== id));
      } else {
        alert(`Failed: ${res.message}`);
      }
    } catch (err: any) {
      alert(`Error: ${err.message}`);
    } finally {
      setSubmitting(null);
    }
  };

  const handleRerun = async (id: string) => {
    setSubmitting(id);
    try {
      const res = await contextV2Api.rerunQuarantine(id);
      if (res.status === "ok") {
        alert("Re-ran with the model — data rescued.");
        setItems(items.filter(i => i.id !== id));
      } else if (res.status === "requarantined") {
        alert(res.message || "The model failed again — re-quarantined. Try editing the JSON manually.");
        fetchItems();
      } else {
        alert(`Failed: ${res.message}`);
      }
    } catch (err: any) {
      alert(`Error: ${err.message}`);
    } finally {
      setSubmitting(null);
    }
  };

  const toggleExpand = (item: QuarantineItem) => {
    if (expandedId === item.id) {
      setExpandedId(null);
    } else {
      setExpandedId(item.id);
      if (!editData[item.id]) {
        setEditData({ ...editData, [item.id]: item.failed_output });
      }
    }
  };

  if (loading) {
    return <div className="p-8 text-stone-500 animate-pulse">Loading quarantine queue...</div>;
  }

  return (
    <div className="max-w-7xl mx-auto">
      <PageHeader
        title="Quarantine"
        icon={AlertCircle}
        description="What did not make it into the graph: model outputs that could not be parsed, and writes the substrate recorded instead of dropping."
      />
      <ReviewTabs />

      <WriteLosses onLoaded={setWritesOpen} />

      {items.length === 0 && writesOpen === 0 ? (
        <div className="bg-emerald-50 border border-emerald-200 rounded-2xl p-12 text-center text-emerald-800 flex flex-col items-center">
          <CheckCircle2 size={48} className="mb-4 text-emerald-500" />
          <h2 className="text-xl font-bold mb-2">Zero Anomalies</h2>
          <p className="text-emerald-600">Your extraction pipeline is running flawlessly. No quarantined items.</p>
        </div>
      ) : items.length === 0 ? (
        <p className="text-sm text-stone-500">No malformed model outputs to repair.</p>
      ) : (
        <div className="space-y-4">
          {items.map(item => (
            <div key={item.id} className="bg-white border border-stone-200 rounded-xl shadow-sm overflow-hidden">
              <div className="p-4 flex items-center justify-between cursor-pointer hover:bg-stone-50 transition-colors" onClick={() => toggleExpand(item)}>
                <div className="flex items-center gap-4">
                  <div className="bg-red-100 text-red-700 font-mono text-xs px-2 py-1 rounded-md font-bold">
                    {item.extractor_type === "template" ? `TEMPLATE: ${item.template_name}` : "BUILT-IN"}
                  </div>
                  <div>
                    <h3 className="text-sm font-bold text-stone-900">{item.error_message || "Unknown Validation Error"}</h3>
                    <p className="text-xs text-stone-500 mt-0.5">{new Date(item.created_at).toLocaleString()}</p>
                  </div>
                </div>
                <div>
                  {expandedId === item.id ? <ChevronUp className="text-stone-400" /> : <ChevronDown className="text-stone-400" />}
                </div>
              </div>

              {expandedId === item.id && (
                <div className="p-4 border-t border-stone-100 bg-stone-50">
                  <div className="grid grid-cols-2 gap-6">
                    {/* Source Text Context */}
                    <div>
                      <h4 className="text-xs font-bold text-stone-500 uppercase tracking-wider mb-2 flex items-center gap-2">
                        <FileText size={14} /> Source Text
                      </h4>
                      <div className="bg-white border border-stone-200 p-3 rounded-lg text-sm text-stone-700 whitespace-pre-wrap h-96 overflow-y-auto font-serif leading-relaxed">
                        {item.chunk_text}
                      </div>
                    </div>

                    {/* LLM Output Editor */}
                    <div className="flex flex-col h-full">
                      <div className="flex items-center justify-between mb-2">
                        <h4 className="text-xs font-bold text-stone-500 uppercase tracking-wider flex items-center gap-2">
                          <AlertCircle size={14} className="text-red-500" /> Malformed Output
                        </h4>
                        <span className="text-[10px] bg-emerald-100 text-emerald-700 px-2 py-0.5 rounded font-mono">EDITABLE</span>
                      </div>
                      <textarea
                        className="w-full flex-grow bg-stone-900 text-emerald-400 font-mono text-xs p-4 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 resize-none h-96"
                        value={editData[item.id] !== undefined ? editData[item.id] : item.failed_output}
                        onChange={e => setEditData({ ...editData, [item.id]: e.target.value })}
                        spellCheck={false}
                      />
                    </div>
                  </div>

                  <div className="mt-6 flex items-center justify-end gap-3 border-t border-stone-200 pt-4">
                    <Button variant="ghost" icon={Trash2} onClick={() => handleDismiss(item.id)}>Dismiss Anomaly</Button>
                    <Button variant="secondary" icon={Wand2} busy={submitting === item.id} onClick={() => handleRerun(item.id)}>Re-run with LLM</Button>
                    <Button variant="primary" icon={RefreshCw} busy={submitting === item.id} onClick={() => handleRetry(item.id)}>Save &amp; Retry JSON</Button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


type WriteRow = {
  id: string; kind: string; reason: string; retryable: boolean; attempts: number; asset?: string | null;
  error?: string | null; payload: Record<string, any>; created_at?: string | null; resolved: boolean; open: boolean;
};

/** One line saying what an unwritten edge or record was meant to be. */
function describeWrite(r: WriteRow): string {
  const p = r.payload || {};
  if (r.kind === "edge") {
    const side = p.unresolved ? ` — ${p.unresolved} not found` : "";
    return `${p.type || "edge"}${p.template ? ` from ${p.template}` : ""}${side}`;
  }
  return `${r.kind}${p.type ? ` ${p.type}` : ""}${p.label ? ` "${p.label}"` : ""}`;
}

/**
 * The substrate's write quarantine — the zero-data-loss record. It was counted by the API and shown
 * nowhere, so this page reported "Zero Anomalies" while edges sat recorded as unwritten.
 */
function WriteLosses({ onLoaded }: { onLoaded: (open: number) => void }) {
  const [data, setData] = useState<{ summary: any; items: WriteRow[] } | null>(null);
  useEffect(() => {
    contextV2Api.getWriteQuarantine()
      .then((d: any) => { setData(d); onLoaded((d?.items || []).filter((r: WriteRow) => !r.resolved).length); })
      .catch(() => onLoaded(0));
  }, [onLoaded]);
  const rows = (data?.items || []).filter((r) => !r.resolved);
  if (!data || rows.length === 0) return null;
  const s = data.summary || {};
  return (
    <div className="bg-white border border-amber-200 rounded-2xl shadow-sm mb-6 overflow-hidden">
      <div className="px-5 py-4 border-b border-amber-100 bg-amber-50/60">
        <h2 className="font-semibold text-stone-900 text-sm">Writes that did not land</h2>
        <p className="text-xs text-stone-600 mt-0.5">
          {s.pending || 0} waiting to be retried · {s.unrecoverable || 0} will not be written as they stand · {s.recovered || 0} recovered since
        </p>
      </div>
      <ul className="divide-y divide-stone-100">
        {rows.slice(0, 50).map((r) => (
          <li key={r.id} className="px-5 py-3 flex items-start gap-3 text-sm">
            <span className={`mt-0.5 text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${r.open ? "bg-amber-100 text-amber-700" : "bg-stone-100 text-stone-500"}`}>
              {r.open ? "pending" : "stopped"}
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-stone-800 break-words">{describeWrite(r)}</div>
              <div className="text-[11px] text-stone-400 mt-0.5">
                {r.reason.replace(/_/g, " ")}{r.asset ? ` · ${r.asset}` : ""} · {r.attempts} attempt{r.attempts === 1 ? "" : "s"}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
