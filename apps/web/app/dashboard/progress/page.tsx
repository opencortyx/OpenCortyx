"use client";

import { useEffect, useRef, useState } from "react";
import { contextV2Api, ProgressRun, ProgressSummary, ProgressStep } from "@/lib/api/contextV2";
import {
  Activity, CheckCircle2, XCircle, Loader2, Clock, Trash2, RefreshCw,
  ChevronDown, ChevronUp, FileText, Database, Ban, AlertCircle
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";

const STATUS_STYLES: Record<string, { badge: string; bar: string; label: string }> = {
  queued: { badge: "bg-stone-100 text-stone-600", bar: "bg-stone-300", label: "Queued" },
  running: { badge: "bg-teal-100 text-teal-700", bar: "bg-teal-500", label: "Running" },
  completed: { badge: "bg-emerald-100 text-emerald-700", bar: "bg-emerald-500", label: "Completed" },
  failed: { badge: "bg-red-100 text-red-700", bar: "bg-red-500", label: "Failed" },
  canceled: { badge: "bg-amber-100 text-amber-700", bar: "bg-amber-400", label: "Canceled" },
  duplicate: { badge: "bg-indigo-100 text-indigo-700", bar: "bg-indigo-400", label: "Duplicate" },
};

function StepIcon({ status }: { status: ProgressStep["status"] }) {
  if (status === "completed") return <CheckCircle2 size={15} className="text-emerald-500" />;
  if (status === "failed") return <XCircle size={15} className="text-red-500" />;
  if (status === "skipped") return <Ban size={15} className="text-stone-400" />;
  return <Loader2 size={15} className="text-teal-500 animate-spin" />;
}

function SummaryCard({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="bg-white border border-stone-200 rounded-xl px-4 py-3 flex-1 min-w-[110px]">
      <div className={`text-2xl font-bold ${tone}`}>{value}</div>
      <div className="text-xs text-stone-500 mt-0.5">{label}</div>
    </div>
  );
}

export default function ProgressPage() {
  const [runs, setRuns] = useState<ProgressRun[]>([]);
  const [summary, setSummary] = useState<ProgressSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchRuns = async () => {
    try {
      const data = await contextV2Api.listProgress(100);
      setRuns(data?.items || []);
      setSummary(data?.summary || null);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  // poll while anything is active; otherwise refresh lazily
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (!alive) return;
      await fetchRuns();
      if (!alive) return;
      const active = (s: ProgressSummary | null) => s && (s.running > 0 || s.queued > 0);
      const delay = active(summaryRef.current) ? 2000 : 8000;
      timer.current = setTimeout(tick, delay);
    };
    tick();
    return () => { alive = false; if (timer.current) clearTimeout(timer.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // keep a ref of the latest summary so the poll loop can read it without re-subscribing
  const summaryRef = useRef<ProgressSummary | null>(null);
  useEffect(() => { summaryRef.current = summary; }, [summary]);

  const deleteRun = async (run: ProgressRun) => {
    const active = run.status === "running" || run.status === "queued";
    const msg = active
      ? `Cancel & delete "${run.asset}"? The in-progress task will be stopped and all nodes already created for this ${run.kind} (including derived/ghost nodes) will be removed.`
      : `Delete this progress record for "${run.asset}"? The asset and its knowledge are kept — only the log entry is removed. To remove the asset itself, delete it from Assets.`;
    if (!confirm(msg)) return;
    setBusy(run.id);
    try {
      // Only an ACTIVE run is canceled and its partial output purged; a finished run's record is
      // removed without touching the asset (other runs of the same file share it).
      await contextV2Api.deleteProgressRun(run.id, run.status === "queued" || run.status === "running");
      await fetchRuns();
    } catch (e) {
      alert("Failed to delete run.");
    } finally {
      setBusy(null);
    }
  };

  const clearTerminal = async () => {
    if (!confirm("Clear all completed, failed, and canceled records? Active runs are kept. Ingested nodes are not affected.")) return;
    try {
      await contextV2Api.clearProgress();
      await fetchRuns();
    } catch {
      alert("Failed to clear.");
    }
  };

  const resultChips = (r: ProgressRun) => {
    const res = r.result || {};
    const keys = ["chunks", "summary", "keywords", "entities", "claims",
      "segments", "computations", "business_semantics", "data_quality", "business_rules"];
    return keys
      .filter((k) => typeof res[k] === "number" && res[k] > 0)
      .map((k) => `${res[k]} ${k}`);
  };

  if (loading) {
    return <div className="p-8 text-stone-500 animate-pulse">Loading ingestion progress…</div>;
  }

  return (
    <div className="max-w-7xl mx-auto">
      <PageHeader
        title="Runs"
        icon={Activity}
        description="Live status of every document & structured pipeline — step by step."
        actions={
          <>
            <Button variant="secondary" icon={RefreshCw} onClick={fetchRuns}>Refresh</Button>
            <Button variant="ghost" icon={Trash2} onClick={clearTerminal}>Clear finished</Button>
          </>
        }
      />

      {/* summary cards */}
      {summary && (
        <div className="flex flex-wrap gap-3 mb-6">
          <SummaryCard label="Total" value={summary.total} tone="text-stone-900" />
          <SummaryCard label="Queued" value={summary.queued} tone="text-stone-500" />
          <SummaryCard label="In progress" value={summary.running} tone="text-teal-600" />
          <SummaryCard label="Completed" value={summary.completed} tone="text-emerald-600" />
          <SummaryCard label="Failed" value={summary.failed} tone="text-red-600" />
          <SummaryCard label="Canceled" value={summary.canceled} tone="text-amber-600" />
        </div>
      )}

      {runs.length === 0 ? (
        <div className="bg-stone-50 border border-stone-200 rounded-2xl p-12 text-center text-stone-500 flex flex-col items-center">
          <Activity size={48} className="mb-4 text-stone-300" />
          <h2 className="text-xl font-bold mb-2 text-stone-700">No ingestion runs yet</h2>
          <p>Deploy a document or structured pipeline and its progress will appear here.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {runs.map((r) => {
            const st = STATUS_STYLES[r.status] || STATUS_STYLES.queued;
            const isOpen = expanded === r.id;
            const chips = resultChips(r);
            return (
              <div key={r.id} className="bg-white border border-stone-200 rounded-xl shadow-sm overflow-hidden">
                <div className="p-4 cursor-pointer hover:bg-stone-50 transition-colors"
                  onClick={() => setExpanded(isOpen ? null : r.id)}>
                  <div className="flex items-center justify-between gap-4">
                    <div className="flex items-center gap-3 min-w-0">
                      <div className={`p-2 rounded-lg ${r.kind === "structured" ? "bg-emerald-100 text-emerald-600" : "bg-teal-100 text-teal-600"}`}>
                        {r.kind === "structured" ? <Database size={18} /> : <FileText size={18} />}
                      </div>
                      <div className="min-w-0">
                        <h3 className="text-sm font-bold text-stone-900 truncate">{r.asset || "(unnamed)"}</h3>
                        <p className="text-xs text-stone-500 mt-0.5">
                          {r.kind} · step {r.current_step}/{r.total_steps || "?"}
                          {r.created_at ? ` · ${new Date(r.created_at).toLocaleString()}` : ""}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-3 shrink-0">
                      <span className={`text-[11px] font-bold px-2 py-1 rounded-md ${st.badge}`}>{st.label}</span>
                      <button
                        onClick={(e) => { e.stopPropagation(); deleteRun(r); }}
                        disabled={busy === r.id}
                        title={r.status === "running" || r.status === "queued" ? "Cancel & delete" : "Delete record"}
                        className="p-1.5 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-md disabled:opacity-50">
                        {busy === r.id ? <Loader2 size={16} className="animate-spin" /> : <Trash2 size={16} />}
                      </button>
                      {isOpen ? <ChevronUp size={18} className="text-stone-400" /> : <ChevronDown size={18} className="text-stone-400" />}
                    </div>
                  </div>

                  {/* progress bar */}
                  <div className="mt-3 flex items-center gap-3">
                    <div className="flex-1 h-2 bg-stone-100 rounded-full overflow-hidden">
                      <div className={`h-full ${st.bar} transition-all duration-500`}
                        style={{ width: `${Math.max(2, Math.min(100, r.percent))}%` }} />
                    </div>
                    <span className="text-xs font-mono text-stone-500 w-10 text-right">{r.percent}%</span>
                  </div>
                </div>

                {isOpen && (
                  <div className="px-4 pb-4 border-t border-stone-100 bg-stone-50">
                    {/* per-stage steps */}
                    <h4 className="text-xs font-bold text-stone-500 uppercase tracking-wider mt-3 mb-2">Stages</h4>
                    {(!r.steps || r.steps.length === 0) ? (
                      <p className="text-xs text-stone-400 flex items-center gap-2">
                        <Clock size={14} /> Waiting for the worker to pick this up…
                      </p>
                    ) : (
                      <ul className="space-y-1.5">
                        {r.steps.map((s) => (
                          <li key={s.i} className="flex items-center justify-between text-sm bg-white border border-stone-100 rounded-lg px-3 py-2">
                            <span className="flex items-center gap-2 text-stone-700">
                              <StepIcon status={s.status} /> {s.name}
                            </span>
                            <span className="text-xs text-stone-400 font-mono">
                              {typeof s.count === "number" ? `${s.count} created` : s.status}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}

                    {/* error */}
                    {r.error && (
                      <div className="mt-3 text-xs bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 flex items-start gap-2">
                        <AlertCircle size={14} className="mt-0.5 shrink-0" />
                        <span className="font-mono whitespace-pre-wrap">{r.error}</span>
                      </div>
                    )}

                    {/* result summary chips */}
                    {chips.length > 0 && (
                      <div className="mt-3 flex flex-wrap gap-2">
                        {chips.map((c) => (
                          <span key={c} className="text-[11px] bg-emerald-50 text-emerald-700 px-2 py-1 rounded-md font-mono">{c}</span>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
