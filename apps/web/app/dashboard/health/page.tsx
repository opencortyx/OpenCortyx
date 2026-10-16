"use client";

import { useCallback, useEffect, useState } from "react";
import { contextV2Api, HealthResult, HealthCheck } from "@/lib/api/contextV2";
import {
  HeartPulse, CheckCircle2, AlertTriangle, XCircle, RefreshCw, Loader2,
  Database, Cpu, Server, Wrench, Boxes,
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";

const STATUS: Record<
  HealthCheck["status"],
  { dot: string; chip: string; ring: string; label: string; Icon: any }
> = {
  ok: { dot: "bg-emerald-500", chip: "bg-emerald-100 text-emerald-700", ring: "border-emerald-200", label: "Healthy", Icon: CheckCircle2 },
  warn: { dot: "bg-amber-500", chip: "bg-amber-100 text-amber-700", ring: "border-amber-200", label: "Attention", Icon: AlertTriangle },
  down: { dot: "bg-red-500", chip: "bg-red-100 text-red-700", ring: "border-red-200", label: "Down", Icon: XCircle },
};

const OVERALL: Record<HealthResult["overall"], { text: string; sub: string; bg: string; fg: string }> = {
  ok: { text: "All systems operational", sub: "Every check passed — you're ready to ingest and query.", bg: "bg-emerald-50 border-emerald-200", fg: "text-emerald-700" },
  warn: { text: "Running with warnings", sub: "OpenCortyx works, but something below needs your attention.", bg: "bg-amber-50 border-amber-200", fg: "text-amber-700" },
  down: { text: "Something is down", sub: "One or more core services are unavailable. Follow the fixes below.", bg: "bg-red-50 border-red-200", fg: "text-red-700" },
};

function iconFor(name: string) {
  const n = name.toLowerCase();
  if (n.includes("postgres") || n.includes("database")) return Database;
  if (n.includes("worker") || n.includes("celery")) return Boxes;
  if (n.includes("model server") || n.includes("embed")) return Cpu;
  if (n.includes("api")) return Server;
  return HeartPulse;
}

export default function HealthPage() {
  const [data, setData] = useState<HealthResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string>("");
  const [last, setLast] = useState<string>("");
  const [resetting, setResetting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const r = await contextV2Api.health();
      setData(r);
      setLast(new Date().toLocaleTimeString());
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || "Failed to reach the API.");
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const resetWorker = useCallback(async (hard: boolean) => {
    const msg = hard
      ? "HARD restart? This kills EVERYTHING: all pending tasks in Redis are dropped, hung/running tasks are killed, and the worker restarts with an empty queue. Ingested data is NOT touched."
      : "Reset the worker? Tasks it is CURRENTLY executing are killed and marked failed; queued tasks continue immediately. Data is not touched.";
    if (!window.confirm(msg)) return;
    setResetting(true);
    try {
      const r = await contextV2Api.resetWorker(hard);
      alert(`Worker ${hard ? "hard-restart" : "reset"}: ${r.terminated ?? 0} running/scheduled task(s) killed` +
            `${hard ? `, ${r.purged_queued ?? 0} pending task(s) dropped from Redis` : ""}, ` +
            `${r.runs_failed ?? 0} run(s) marked failed` +
            `${hard ? `, ${r.runs_canceled ?? 0} queued run(s) canceled` : ""}` +
            `${r.shutdown_sent ? ", restart signal sent" : ""}.` +
            `${(r.errors || []).length ? `\nWarnings: ${(r.errors || []).join("; ")}` : ""}`);
      setTimeout(load, hard ? 8000 : 2000);   // give the pool / container a moment, then re-check
    } catch (e: any) {
      alert(`Worker reset failed: ${e?.response?.data?.detail || e?.message || e}`);
    } finally {
      setResetting(false);
    }
  }, [load]);

  useEffect(() => {
    load();
  }, [load]);

  const counts = {
    ok: data?.checks.filter((c) => c.status === "ok").length || 0,
    warn: data?.checks.filter((c) => c.status === "warn").length || 0,
    down: data?.checks.filter((c) => c.status === "down").length || 0,
  };

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      {/* Header */}
      <PageHeader
        title="System Health"
        icon={HeartPulse}
        description="A live check of every moving part — API, worker, database, and models — with plain-English fixes."
        actions={
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => resetWorker(false)}
            disabled={resetting || loading}
            title="Kill the tasks the worker is currently executing (hung work dies, queued work continues). Data untouched."
            className="flex items-center gap-2 px-3 py-2 bg-white border border-amber-300 text-amber-700 text-sm font-semibold rounded-lg hover:bg-amber-50 disabled:opacity-50 transition-colors"
          >
            {resetting ? <Loader2 size={15} className="animate-spin" /> : <Boxes size={15} />}
            Reset worker
          </button>
          <button
            onClick={() => resetWorker(true)}
            disabled={resetting || loading}
            title="Total task nuke: drops ALL pending tasks from Redis, kills hung/running/scheduled ones, and restarts the worker process (Docker revives it with an empty queue). Ingested data is not touched."
            className="px-3 py-2 bg-white border border-red-300 text-red-700 text-sm font-semibold rounded-lg hover:bg-red-50 disabled:opacity-50 transition-colors"
          >
            Hard restart
          </button>
          <Button variant="secondary" icon={RefreshCw} busy={loading} onClick={load}>{loading ? "Checking…" : "Re-check"}</Button>
        </div>
        }
      />

      {/* API unreachable */}
      {err && (
        <div className="rounded-xl border border-red-200 bg-red-50 p-5">
          <div className="flex items-center gap-2 text-red-700 font-semibold">
            <XCircle size={18} /> Can&apos;t reach the API
          </div>
          <p className="text-sm text-red-600 mt-1">{err}</p>
          <p className="text-sm text-stone-600 mt-3">
            <Wrench size={14} className="inline mr-1.5 -mt-0.5" />
            Start the backend, then re-check:{" "}
            <code className="px-1.5 py-0.5 bg-white border border-red-200 rounded text-[12px]">
              docker compose up -d
            </code>
          </p>
        </div>
      )}

      {/* Overall banner */}
      {data && (
        <div className={`rounded-xl border p-5 ${OVERALL[data.overall].bg}`}>
          <div className="flex items-center justify-between gap-4 flex-wrap">
            <div>
              <div className={`text-lg font-bold ${OVERALL[data.overall].fg}`}>{OVERALL[data.overall].text}</div>
              <p className="text-sm text-stone-600 mt-0.5">{OVERALL[data.overall].sub}</p>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <span className="px-2.5 py-1 rounded-full bg-emerald-100 text-emerald-700 font-semibold">{counts.ok} healthy</span>
              {counts.warn > 0 && <span className="px-2.5 py-1 rounded-full bg-amber-100 text-amber-700 font-semibold">{counts.warn} warning</span>}
              {counts.down > 0 && <span className="px-2.5 py-1 rounded-full bg-red-100 text-red-700 font-semibold">{counts.down} down</span>}
            </div>
          </div>
        </div>
      )}

      {/* Initial loading skeleton */}
      {loading && !data && (
        <div className="space-y-3">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="h-20 rounded-xl border border-stone-200 bg-stone-50 animate-pulse" />
          ))}
        </div>
      )}

      {/* Checks */}
      {data && (
        <div className="space-y-3">
          {data.checks.map((c, i) => {
            const s = STATUS[c.status];
            const Icon = iconFor(c.name);
            return (
              <div key={i} className={`rounded-xl border bg-white p-4 ${s.ring}`}>
                <div className="flex items-start gap-3">
                  <div className="relative mt-0.5">
                    <div className="p-2 rounded-lg bg-stone-50 text-stone-500">
                      <Icon size={18} />
                    </div>
                    <span className={`absolute -top-1 -right-1 h-3 w-3 rounded-full ring-2 ring-white ${s.dot}`} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <h3 className="font-semibold text-stone-800">{c.name}</h3>
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${s.chip}`}>
                        {s.label}
                      </span>
                    </div>
                    <p className="text-sm text-stone-600 mt-1 break-words">{c.detail}</p>
                    {c.status !== "ok" && c.fix && (
                      <div className="mt-2.5 flex items-start gap-2 rounded-lg bg-stone-50 border border-stone-200 p-2.5">
                        <Wrench size={14} className="text-stone-400 mt-0.5 shrink-0" />
                        <p className="text-sm text-stone-700 whitespace-pre-wrap break-words">{c.fix}</p>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {last && (
        <p className="text-xs text-stone-400 text-center">Last checked at {last}</p>
      )}
    </div>
  );
}
