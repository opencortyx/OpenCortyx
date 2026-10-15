"use client";

import { useCallback, useEffect, useState } from "react";
import { contextV2Api } from "@/lib/api/contextV2";
import {
  FlaskConical, Play, Plus, Trash2, Loader2, CheckCircle2, XCircle,
  ThumbsUp, ThumbsDown, ChevronDown,
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";

type Case = { id: string; question: string; expect_keywords: string[]; should_abstain: boolean };
type Cite = { type?: string; score?: number; label?: string; text?: string };
type Result = {
  id: string; question: string; pass: boolean; abstained?: boolean; grounded?: boolean;
  keyword_hit?: boolean; citations?: number; trust_score?: number; reason?: string; should_abstain?: boolean;
  answer?: string; cites?: Cite[];
};
type RunSummary = {
  cases: number; passed: number; pass_rate: number; groundedness: number;
  abstain_accuracy: number | null; results: Result[];
};
type Feedback = {
  up: number; down: number; total: number; satisfaction: number | null;
  recent: { rating: string; query?: string; correction?: string; actor?: string; at?: string }[];
};

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);

export default function EvalPage() {
  const [cases, setCases] = useState<Case[]>([]);
  const [q, setQ] = useState("");
  const [kw, setKw] = useState("");
  const [abstain, setAbstain] = useState(false);
  const [adding, setAdding] = useState(false);
  const [running, setRunning] = useState(false);
  const [run, setRun] = useState<RunSummary | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [fb, setFb] = useState<Feedback | null>(null);

  const load = useCallback(async () => {
    try { setCases((await contextV2Api.evalCases())?.items || []); } catch { /* noop */ }
    try { setFb(await contextV2Api.feedback()); } catch { /* noop */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  const add = async () => {
    if (!q.trim()) return;
    setAdding(true);
    try {
      await contextV2Api.addEvalCase(
        q.trim(),
        kw.split(",").map((s) => s.trim()).filter(Boolean),
        abstain);
      setQ(""); setKw(""); setAbstain(false);
      await load();
    } finally { setAdding(false); }
  };
  const del = async (id: string) => {
    try { await contextV2Api.deleteEvalCase(id); setCases((c) => c.filter((x) => x.id !== id)); } catch { /* noop */ }
  };
  // Aggregate partial results into the live summary (mirrors the server's summarize_results).
  const summarize = (cs: Case[], rs: Result[]): RunSummary => {
    const passed = rs.filter((r) => r.pass).length;
    const grounded = rs.filter((r) => r.grounded).length;
    // Rates over the cases RUN so far: dividing by every case scored the ones still queued as
    // failures, so a suite of all-passing cases showed "50%" halfway through.
    const abstainTotal = rs.filter((r) => r.should_abstain).length;
    const abstainOk = rs.filter((r) => r.should_abstain && r.pass).length;
    const total = rs.length || 1;
    const answerable = rs.filter((r) => !r.should_abstain).length || 1;
    return {
      cases: cs.length, passed, pass_rate: passed / total, groundedness: grounded / answerable,
      abstain_accuracy: abstainTotal ? abstainOk / abstainTotal : null, results: rs,
    };
  };

  // Run case-by-case so we can show real progress + each case's retrieved answer/citations.
  const runSuite = async () => {
    setRunning(true); setRun(null); setExpanded(null);
    setProgress({ done: 0, total: cases.length });
    const results: Result[] = [];
    for (const c of cases) {
      try { results.push(await contextV2Api.runEvalCase(c.id)); }
      catch { results.push({ id: c.id, question: c.question, pass: false, reason: "error" }); }
      setProgress({ done: results.length, total: cases.length });
      setRun(summarize(cases, [...results]));   // live-updating summary as each case lands
    }
    setRunning(false);
  };

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      <PageHeader
        title="Evaluations"
        icon={FlaskConical}
        description="The no-hallucination proof: run gold questions through the real answer pipeline and measure groundedness, abstention, and accuracy."
      />

      {/* Run summary */}
      <div className="bg-white border border-stone-200 rounded-xl shadow-sm p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold text-stone-800">Gold suite</h2>
          <Button variant="primary" icon={Play} busy={running} disabled={cases.length === 0} onClick={runSuite}>Run suite</Button>
        </div>
        {running && progress && (
          <div className="mb-4">
            <div className="flex items-center justify-between text-xs text-stone-500 mb-1.5">
              <span>Running case {Math.min(progress.done + 1, progress.total)} of {progress.total} through the real answer pipeline…</span>
              <span className="font-semibold text-stone-700">{progress.done}/{progress.total}</span>
            </div>
            <div className="h-2 w-full bg-stone-100 rounded-full overflow-hidden">
              <div className="h-full bg-primary rounded-full transition-all duration-300"
                style={{ width: `${progress.total ? (progress.done / progress.total) * 100 : 0}%` }} />
            </div>
          </div>
        )}
        {run ? (
          <>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
              <Stat label="Pass rate" value={pct(run.pass_rate)} good={run.pass_rate >= 0.9} />
              <Stat label="Groundedness" value={pct(run.groundedness)} good={run.groundedness >= 0.9} />
              <Stat label="Abstain accuracy" value={pct(run.abstain_accuracy)} good={(run.abstain_accuracy ?? 1) >= 0.9} />
              <Stat label="Cases" value={`${run.passed}/${run.cases}`} />
            </div>
            <ul className="divide-y divide-stone-100 border border-stone-100 rounded-lg max-h-[28rem] overflow-y-auto">
              {run.results.map((r) => {
                const open = expanded === r.id;
                const hasDetail = !!(r.answer || (r.cites && r.cites.length));
                return (
                  <li key={r.id} className="text-sm">
                    <button onClick={() => hasDetail && setExpanded(open ? null : r.id)}
                      className={`w-full px-4 py-2.5 flex items-center gap-3 text-left ${hasDetail ? "hover:bg-stone-50 cursor-pointer" : "cursor-default"}`}>
                      {r.pass ? <CheckCircle2 size={16} className="text-emerald-500 shrink-0" /> : <XCircle size={16} className="text-red-500 shrink-0" />}
                      <span className="flex-1 min-w-0 truncate text-stone-700">{r.question}</span>
                      {typeof r.citations === "number" && <span className="text-[11px] text-stone-400 shrink-0">{r.citations} retrieved</span>}
                      {!r.pass && r.reason && <span className="text-[11px] text-red-500 shrink-0">{r.reason}</span>}
                      {hasDetail && <ChevronDown size={14} className={`text-stone-400 shrink-0 transition-transform ${open ? "rotate-180" : ""}`} />}
                    </button>
                    {open && (
                      <div className="px-4 pb-3 pt-1 space-y-3 bg-stone-50/60">
                        <div>
                          <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400 mb-1">Answer it gave</div>
                          <p className="text-sm text-stone-700 whitespace-pre-wrap">{r.answer || <span className="italic text-stone-400">— abstained / no answer —</span>}</p>
                        </div>
                        {r.cites && r.cites.length > 0 && (
                          <div>
                            <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400 mb-1">Retrieved citations ({r.cites.length})</div>
                            <ul className="space-y-1.5">
                              {r.cites.map((c, i) => (
                                <li key={i} className="bg-white border border-stone-100 rounded-md p-2">
                                  <div className="flex items-center gap-2 mb-0.5">
                                    <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-teal-50 text-teal-700">{c.type || "node"}</span>
                                    {c.label && <span className="text-[11px] text-stone-500 truncate">{c.label}</span>}
                                    {typeof c.score === "number" && <span className="ml-auto text-[10px] text-stone-400">score {c.score.toFixed(3)}</span>}
                                  </div>
                                  {c.text && <p className="text-[12px] text-stone-600 line-clamp-3">{c.text}</p>}
                                </li>
                              ))}
                            </ul>
                          </div>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        ) : (
          !running && <p className="text-sm text-stone-400">{cases.length === 0 ? "Add a gold case below, then run the suite." : "Run the suite to score every gold case."}</p>
        )}
      </div>

      {/* Add + list cases */}
      <div className="bg-white border border-stone-200 rounded-xl shadow-sm p-5 space-y-4">
        <h2 className="font-semibold text-stone-800">Gold cases</h2>
        <input value={q} onChange={(e) => setQ(e.target.value)} className="w-full border border-stone-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20" placeholder="Question (e.g. What is our total revenue for Q3?)" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <input value={kw} onChange={(e) => setKw(e.target.value)} disabled={abstain} className="w-full border border-stone-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20 disabled:opacity-50" placeholder="Expected keywords (comma-separated)" />
          <label className="flex items-center gap-2 text-sm text-stone-600">
            <input type="checkbox" checked={abstain} onChange={(e) => setAbstain(e.target.checked)} />
            Should abstain (no good answer exists)
          </label>
        </div>
        <Button variant="primary" icon={Plus} busy={adding} disabled={!q.trim()} onClick={add}>Add case</Button>
        <ul className="divide-y divide-stone-100">
          {cases.length === 0 ? (
            <li className="py-4 text-sm text-stone-400 text-center">No gold cases yet.</li>
          ) : cases.map((c) => (
            <li key={c.id} className="py-2.5 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <span className="text-sm text-stone-800">{c.question}</span>
                <div className="flex flex-wrap gap-1 mt-1">
                  {c.should_abstain && <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700">should abstain</span>}
                  {c.expect_keywords.map((k) => <span key={k} className="text-[10px] px-1.5 py-0.5 rounded bg-stone-100 text-stone-500">{k}</span>)}
                </div>
              </div>
              <button onClick={() => del(c.id)} className="p-1.5 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-md shrink-0"><Trash2 size={15} /></button>
            </li>
          ))}
        </ul>
      </div>

      {/* Feedback loop */}
      <div className="bg-white border border-stone-200 rounded-xl shadow-sm p-5">
        <h2 className="font-semibold text-stone-800 mb-3">Feedback loop</h2>
        {!fb || fb.total === 0 ? (
          <p className="text-sm text-stone-400">No feedback yet. 👍/👎 from users &amp; agents (via <code>opencortyx_feedback</code>) lands here to tune ranking.</p>
        ) : (
          <>
            <div className="flex items-center gap-4 mb-3">
              <span className="inline-flex items-center gap-1.5 text-sm text-emerald-600 font-semibold"><ThumbsUp size={15} /> {fb.up}</span>
              <span className="inline-flex items-center gap-1.5 text-sm text-red-600 font-semibold"><ThumbsDown size={15} /> {fb.down}</span>
              <span className="text-sm text-stone-500">satisfaction {pct(fb.satisfaction)}</span>
            </div>
            <ul className="divide-y divide-stone-100 max-h-60 overflow-y-auto">
              {fb.recent.filter((r) => r.correction).map((r, i) => (
                <li key={i} className="py-2 text-sm">
                  <span className={r.rating === "up" ? "text-emerald-600" : "text-red-600"}>{r.rating === "up" ? "👍" : "👎"}</span>{" "}
                  <span className="text-stone-700">{r.query}</span>
                  {r.correction && <div className="text-xs text-stone-500 mt-0.5">↳ correction: {r.correction}</div>}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return (
    <div className="bg-stone-50 rounded-lg p-3">
      <div className="text-xs text-stone-500">{label}</div>
      <div className={`text-2xl font-bold mt-0.5 ${good === undefined ? "text-stone-900" : good ? "text-emerald-600" : "text-amber-600"}`}>{value}</div>
    </div>
  );
}
