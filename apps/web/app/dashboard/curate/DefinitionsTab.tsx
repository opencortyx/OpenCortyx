"use client";

import { useEffect, useState } from "react";
import { Loader2, Plus, Trash2, Sigma, Filter, BadgeCheck } from "lucide-react";
import { contextV2Api, DefinitionsList } from "@/lib/api/contextV2";

// Data definitions: agreed meanings the assistant's SQL uses instead of guessing. Each one is run
// against the data before it is saved, so a typo is an error here, not a wrong number later.

type Kind = "Metric" | "Segment" | "VerifiedQuery";

const EMPTY_METRIC = { name: "", table: "", expression: "", description: "", filters: "", segment: "", unit: "" };
const EMPTY_SEGMENT = { name: "", table: "", condition: "", description: "" };

function errorText(e: any): string {
  return e?.response?.data?.detail || e?.message || "Not saved";
}

export default function DefinitionsTab({ flash }: { flash: (text: string, ok?: boolean) => void }) {
  const [defs, setDefs] = useState<DefinitionsList | null>(null);
  const [busy, setBusy] = useState(false);
  const [metric, setMetric] = useState({ ...EMPTY_METRIC });
  const [segment, setSegment] = useState({ ...EMPTY_SEGMENT });

  const load = () => contextV2Api.listDefinitions().then(setDefs).catch(() => setDefs({ Metric: [], Segment: [], VerifiedQuery: [] }));
  useEffect(() => { load(); }, []);

  const save = async (kind: Kind) => {
    setBusy(true);
    try {
      if (kind === "Metric") {
        const r = await contextV2Api.addMetric({ ...metric, segment: metric.segment || null });
        flash(`Metric saved - current value ${r.value ?? "n/a"}`);
        setMetric({ ...EMPTY_METRIC });
      } else if (kind === "Segment") {
        const r = await contextV2Api.addSegment(segment);
        flash(`Segment saved - ${r.rows_matched} rows match it`);
        setSegment({ ...EMPTY_SEGMENT });
      }
      await load();
    } catch (e) {
      flash(errorText(e), false);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    try { await contextV2Api.deleteDefinition(id); flash("Deleted"); await load(); }
    catch (e) { flash(errorText(e), false); }
  };

  const input = "w-full rounded-md border border-stone-200 px-2.5 py-1.5 text-sm";
  const List = ({ kind, render }: { kind: Kind; render: (d: any) => React.ReactNode }) => (
    <ul className="divide-y divide-stone-100 text-sm">
      {(defs?.[kind] || []).map((d: any) => (
        <li key={d.id} className="py-2 flex items-start gap-2">
          <div className="flex-1 min-w-0">{render(d)}</div>
          <button onClick={() => remove(d.id)} className="text-stone-400 hover:text-red-600" aria-label="Delete">
            <Trash2 size={14} />
          </button>
        </li>
      ))}
      {defs && (defs[kind] || []).length === 0 && <li className="py-2 text-stone-400">None yet.</li>}
    </ul>
  );

  if (!defs) return <div className="flex items-center gap-2 text-stone-500"><Loader2 className="animate-spin" size={16} /> Loading…</div>;

  return (
    <div className="grid gap-6 lg:grid-cols-3">
      <section className="rounded-lg border border-stone-200 bg-white p-4 space-y-3">
        <h3 className="font-semibold flex items-center gap-2"><Sigma size={16} /> Metrics</h3>
        <p className="text-xs text-stone-500">One agreed expression, e.g. <code>sum(fee)</code> on <code>licences</code>. Questions that name it use it as written.</p>
        <List kind="Metric" render={(d) => (<>
          <div className="font-medium">{d.name}</div>
          <code className="text-xs text-stone-600">{d.expression}</code> <span className="text-xs text-stone-400">on {d.table}{d.filters ? ` where ${d.filters}` : ""}</span>
        </>)} />
        <div className="space-y-2 pt-2 border-t border-stone-100">
          <input className={input} placeholder="Name (e.g. Revenue)" value={metric.name} onChange={(e) => setMetric({ ...metric, name: e.target.value })} />
          <input className={input} placeholder="Table (e.g. licences)" value={metric.table} onChange={(e) => setMetric({ ...metric, table: e.target.value })} />
          <input className={input} placeholder="Expression (e.g. sum(fee))" value={metric.expression} onChange={(e) => setMetric({ ...metric, expression: e.target.value })} />
          <input className={input} placeholder="Filter, optional (e.g. status = 'ACTIVE')" value={metric.filters} onChange={(e) => setMetric({ ...metric, filters: e.target.value })} />
          <input className={input} placeholder="Segment, optional" value={metric.segment} onChange={(e) => setMetric({ ...metric, segment: e.target.value })} />
          <input className={input} placeholder="Description" value={metric.description} onChange={(e) => setMetric({ ...metric, description: e.target.value })} />
          <button disabled={busy || !metric.name || !metric.table || !metric.expression} onClick={() => save("Metric")}
            className="inline-flex items-center gap-1.5 rounded-md bg-stone-900 px-3 py-1.5 text-sm text-white disabled:opacity-40">
            <Plus size={14} /> Check and save
          </button>
        </div>
      </section>

      <section className="rounded-lg border border-stone-200 bg-white p-4 space-y-3">
        <h3 className="font-semibold flex items-center gap-2"><Filter size={16} /> Segments</h3>
        <p className="text-xs text-stone-500">A named filter, e.g. &ldquo;active licence&rdquo; = <code>status = &apos;ACTIVE&apos;</code>. Metrics can be scoped to one.</p>
        <List kind="Segment" render={(d) => (<>
          <div className="font-medium">{d.name}</div>
          <code className="text-xs text-stone-600">{d.condition}</code> <span className="text-xs text-stone-400">on {d.table} · {d.rows_matched} rows</span>
        </>)} />
        <div className="space-y-2 pt-2 border-t border-stone-100">
          <input className={input} placeholder="Name" value={segment.name} onChange={(e) => setSegment({ ...segment, name: e.target.value })} />
          <input className={input} placeholder="Table" value={segment.table} onChange={(e) => setSegment({ ...segment, table: e.target.value })} />
          <input className={input} placeholder="Condition (SQL)" value={segment.condition} onChange={(e) => setSegment({ ...segment, condition: e.target.value })} />
          <input className={input} placeholder="Description" value={segment.description} onChange={(e) => setSegment({ ...segment, description: e.target.value })} />
          <button disabled={busy || !segment.name || !segment.table || !segment.condition} onClick={() => save("Segment")}
            className="inline-flex items-center gap-1.5 rounded-md bg-stone-900 px-3 py-1.5 text-sm text-white disabled:opacity-40">
            <Plus size={14} /> Check and save
          </button>
        </div>
      </section>

      <section className="rounded-lg border border-stone-200 bg-white p-4 space-y-3">
        <h3 className="font-semibold flex items-center gap-2"><BadgeCheck size={16} /> Verified queries</h3>
        <p className="text-xs text-stone-500">Questions with SQL someone checked; the assistant learns from the closest ones. Save one from an answer in the AI Assistant.</p>
        <List kind="VerifiedQuery" render={(d) => (<>
          <div className="font-medium">{d.question}</div>
          <code className="text-xs text-stone-600 break-all">{d.sql}</code>
        </>)} />
      </section>
    </div>
  );
}
