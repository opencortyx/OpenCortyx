"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, Coins, RefreshCw, Save, Trash2 } from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import { contextV2Api, type CostStatus } from "@/lib/api/contextV2";
import { apiError, costApi, pct, tokens, usd, type RunCost, type UsageReport } from "@/lib/api/governance";

/**
 * What the models cost: this month against the budget, by use (answers, each extractor, summaries),
 * by model, and per 1,000 records for each ingest run. Every number is from the usage ledger; the
 * "measured" share says how much of it the provider or the tokenizer counted rather than estimated.
 */
export default function CostPage() {
  const [status, setStatus] = useState<CostStatus | null>(null);
  const [report, setReport] = useState<UsageReport | null>(null);
  const [runs, setRuns] = useState<RunCost[]>([]);
  const [days, setDays] = useState(30);
  const [form, setForm] = useState({ monthly_token_budget: 0, alert_threshold: 0.8, hard_cap: false, usd_per_1k_tokens: 0 });
  const [busy, setBusy] = useState<"" | "save" | "cache" | "load">("load");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [role, setRole] = useState("");

  const load = useCallback(async () => {
    setBusy("load");
    try {
      const [s, r, rs] = await Promise.all([contextV2Api.cost(), costApi.usage(days), costApi.runs(50)]);
      setStatus(s); setReport(r); setRuns(rs);
      setForm({ monthly_token_budget: s.monthly_token_budget, alert_threshold: s.alert_threshold,
                hard_cap: s.hard_cap, usd_per_1k_tokens: s.usd_per_1k_tokens });
    } catch (e) { setMsg({ text: apiError(e, "Could not load usage."), ok: false }); }
    finally { setBusy(""); }
  }, [days]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { contextV2Api.authMe().then((r) => setRole(r.role)).catch(() => {}); }, []);

  const save = async () => {
    if (form.monthly_token_budget < 0 || form.alert_threshold < 0 || form.alert_threshold > 1) {
      setMsg({ text: "The budget cannot be negative, and the alert is a share between 0 and 1.", ok: false });
      return;
    }
    setBusy("save");
    try {
      await contextV2Api.setBudget(form as any);
      setMsg({ text: "Budget saved. It applies to answers and to reading files.", ok: true });
      await load();
    } catch (e) { setMsg({ text: apiError(e, "Could not save the budget."), ok: false }); }
    finally { setBusy(""); }
  };
  const clearCache = async () => {
    if (!confirm("Forget every cached extraction answer? The next time a record is read, the model is paid again.")) return;
    setBusy("cache");
    try {
      const r = await costApi.clearCache();
      setMsg({ text: `${r.deleted} cached answer(s) removed.`, ok: true });
    } catch (e) { setMsg({ text: apiError(e, "Could not clear the cache."), ok: false }); }
    finally { setBusy(""); }
  };

  const canEdit = role === "admin" || role === "editor";
  const used = status && status.monthly_token_budget ? Math.min(1, status.spent_tokens / status.monthly_token_budget) : 0;

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-6 pb-20">
      <PageHeader title="Cost & usage" icon={Coins}
        description="What the models cost - this month against the budget, by use, and per 1,000 records read."
        actions={<Button variant="secondary" size="sm" icon={RefreshCw} busy={busy === "load"} onClick={load}>Refresh</Button>} />

      {msg && (
        <div role="status" className={`p-3 rounded-lg text-sm flex items-center gap-2 ${msg.ok ? "bg-emerald-50 text-emerald-700 border border-emerald-100" : "bg-red-50 text-red-700 border border-red-100"}`}>
          {msg.ok ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />} {msg.text}
        </div>
      )}

      {status && (
        <section className="bg-white border border-stone-200 rounded-xl shadow-sm p-5 space-y-4" aria-label="This month">
          <div className="grid gap-4 sm:grid-cols-4">
            <Stat label="Paid tokens this month" value={tokens(status.spent_tokens)}
                  sub={status.monthly_token_budget ? `of ${tokens(status.monthly_token_budget)}` : "no budget set"} />
            <Stat label="Cost this month" value={usd(status.spent_usd)} sub={status.spent_usd === null ? "set prices on the Models page" : ""} />
            <Stat label="All model calls" value={status.calls.toLocaleString()} sub={`${tokens(status.total_tokens)} tokens, free models included`} />
            <Stat label="Measured, not estimated" value={pct(status.measured_share)}
                  sub={`${status.cached_calls.toLocaleString()} answered from cache`} />
          </div>
          {status.monthly_token_budget > 0 && (
            <div>
              <div className="h-2 rounded-full bg-stone-100 overflow-hidden" role="progressbar" aria-valuenow={Math.round(used * 100)} aria-valuemin={0} aria-valuemax={100}>
                <div className={`h-full ${status.over ? "bg-red-500" : status.alert ? "bg-amber-500" : "bg-emerald-500"}`} style={{ width: `${used * 100}%` }} />
              </div>
              {status.over && <p className="text-xs text-red-600 mt-1">The budget is spent.{status.hard_cap ? " Paid model calls are refused until next month or until it is raised; runs that read files pause and can be resumed." : ""}</p>}
              {!status.over && status.alert && <p className="text-xs text-amber-700 mt-1">Past the alert threshold.</p>}
            </div>
          )}

          {canEdit && (
            <div className="border-t border-stone-100 pt-4 grid gap-3 sm:grid-cols-5 items-end">
              <Field label="Monthly budget (paid tokens, 0 = none)">
                <input type="number" min={0} step={10000} value={form.monthly_token_budget}
                  onChange={(e) => setForm({ ...form, monthly_token_budget: Math.max(0, Number(e.target.value) || 0) })}
                  className="w-full border border-stone-200 rounded-lg px-2 py-1.5 text-sm" />
              </Field>
              <Field label="Alert at (share of budget)">
                <input type="number" min={0} max={1} step={0.05} value={form.alert_threshold}
                  onChange={(e) => setForm({ ...form, alert_threshold: Number(e.target.value) })}
                  className="w-full border border-stone-200 rounded-lg px-2 py-1.5 text-sm" />
              </Field>
              <Field label="$ per 1,000 tokens (when a model has no price)">
                <input type="number" min={0} step={0.001} value={form.usd_per_1k_tokens}
                  onChange={(e) => setForm({ ...form, usd_per_1k_tokens: Math.max(0, Number(e.target.value) || 0) })}
                  className="w-full border border-stone-200 rounded-lg px-2 py-1.5 text-sm" />
              </Field>
              <label className="flex items-center gap-2 text-sm text-stone-700 pb-2">
                <input type="checkbox" checked={form.hard_cap} onChange={(e) => setForm({ ...form, hard_cap: e.target.checked })} />
                Stop paid calls at the budget
              </label>
              <Button variant="primary" size="sm" icon={Save} busy={busy === "save"} disabled={!!busy} onClick={save}>Save budget</Button>
            </div>
          )}
        </section>
      )}

      {report && (
        <section className="bg-white border border-stone-200 rounded-xl shadow-sm p-5" aria-label="By use">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-bold text-stone-700 uppercase tracking-wide">By use</h2>
            <label className="text-xs text-stone-500 flex items-center gap-2">Last
              <select value={days} onChange={(e) => setDays(Number(e.target.value))} className="border border-stone-200 rounded px-2 py-1 bg-white">
                {[7, 30, 90, 365].map((d) => <option key={d} value={d}>{d} days</option>)}
              </select>
            </label>
          </div>
          {report.by_purpose.length === 0 ? <p className="text-sm text-stone-400">No model calls in this period.</p> : (
            <Table head={["Use", "Calls", "Tokens in", "Tokens out", "Cost", "Measured"]}
              rows={report.by_purpose.map((r) => [purposeLabel(r.purpose), r.calls.toLocaleString(), tokens(r.tokens_in),
                                                  tokens(r.tokens_out), usd(r.cost_usd), pct(r.measured_share)])} />
          )}
          {report.by_model.length > 0 && (
            <div className="mt-5">
              <h3 className="text-xs font-bold text-stone-500 uppercase mb-2">By model</h3>
              <Table head={["Model", "Calls", "Tokens", "Cost"]}
                rows={report.by_model.map((r) => [`${r.model || "?"}${r.provider ? ` (${r.provider})` : ""}`, r.calls.toLocaleString(),
                                                  tokens(r.tokens), usd(r.cost_usd)])} />
            </div>
          )}
        </section>
      )}

      <section className="bg-white border border-stone-200 rounded-xl shadow-sm p-5" aria-label="Runs">
        <h2 className="text-sm font-bold text-stone-700 uppercase tracking-wide mb-3">Reading files: cost per 1,000 records</h2>
        {runs.filter((r) => r.calls > 0).length === 0 ? (
          <p className="text-sm text-stone-400">No run has called a model yet. Reading without extractors costs nothing.</p>
        ) : (
          <Table head={["File", "Records", "Calls", "Tokens", "Cost", "Tokens / 1k records", "Cost / 1k records"]}
            rows={runs.filter((r) => r.calls > 0).map((r) => [r.asset || r.run_id.slice(0, 8), r.records?.toLocaleString() ?? "—",
              r.calls.toLocaleString(), tokens(r.tokens), usd(r.cost_usd), tokens(r.tokens_per_1k_records), usd(r.cost_usd_per_1k_records)])} />
        )}
        {role === "admin" && (
          <div className="mt-4 flex items-center justify-between gap-3 border-t border-stone-100 pt-3">
            <p className="text-xs text-stone-500">Extraction answers are cached: reading an unchanged record again costs nothing. Clear the cache after changing a model in place.</p>
            <Button variant="secondary" size="sm" icon={Trash2} busy={busy === "cache"} disabled={!!busy} onClick={clearCache}>Clear cache</Button>
          </div>
        )}
      </section>
    </div>
  );
}

function purposeLabel(p: string): string {
  if (p.startsWith("extract:")) return `Extractor: ${p.slice(8)}`;
  if (p.startsWith("preview:")) return `Preview: ${p.slice(8)}`;
  return ({ answer: "Answers", "answer.relevance": "Answers: relevance check", "answer.grounding": "Answers: grounding check",
            other: "Other" } as Record<string, string>)[p] || p.replace(/^opencortyx\./, "").replace(/_/g, " ");
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div>
      <div className="text-xs text-stone-500">{label}</div>
      <div className="text-xl font-semibold text-stone-900">{value}</div>
      {sub && <div className="text-[11px] text-stone-400">{sub}</div>}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <label className="block text-xs text-stone-500 space-y-1"><span>{label}</span>{children}</label>;
}

function Table({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead><tr className="text-left text-xs text-stone-500 border-b border-stone-100">{head.map((h) => <th key={h} className="py-1.5 pr-3 font-semibold">{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i} className="border-b border-stone-50">{r.map((c, j) => <td key={j} className={`py-1.5 pr-3 ${j ? "tabular-nums text-stone-700" : "text-stone-800"}`}>{c}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}
