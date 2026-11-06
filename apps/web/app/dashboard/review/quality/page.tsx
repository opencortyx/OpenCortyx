"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { AlertCircle, CheckCircle2, Gauge, RefreshCw, Save, Split, ThumbsUp, Trash2, Upload } from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import ReviewTabs from "@/components/layout/ReviewTabs";
import Button from "@/components/ui/Button";
import { contextV2Api, type TypeResolution } from "@/lib/api/contextV2";
import {
  apiError, pct, qualityApi, type AuditItem, type Evaluation, type Label, type LabelImportItem, type Score, type TypeQuality,
} from "@/lib/api/governance";

/**
 * Is resolution right? For each type: the labels people gave ("same" / "different"), the engine
 * scored against them, and the false merges it makes - the expensive mistake. Change a setting and
 * score it BEFORE saving. Check automatic merges nobody has looked at: each answer is a label, and
 * "different" splits the pair. Any type an extractor, a mapping or a reader writes can be measured.
 */
export default function QualityPage() {
  const [types, setTypes] = useState<TypeQuality[]>([]);
  const [sel, setSel] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const say = useCallback((text: string, ok: boolean) => setMsg({ text, ok }), []);

  const load = useCallback(async () => {
    try {
      const t = await qualityApi.types();
      setTypes(t);
      setSel((s) => s || t[0]?.node_type || "");
    } catch (e) { say(apiError(e, "Could not load resolution quality."), false); }
  }, [say]);
  useEffect(() => { load(); }, [load]);

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-6 pb-20">
      <PageHeader title="Review" icon={Gauge}
        description="How good is matching records as the same thing? Labels, the score, and the automatic merges to check." />
      <ReviewTabs />

      {msg && (
        <div role="status" className={`p-3 rounded-lg text-sm flex items-center gap-2 ${msg.ok ? "bg-emerald-50 text-emerald-700 border border-emerald-100" : "bg-red-50 text-red-700 border border-red-100"}`}>
          {msg.ok ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />} {msg.text}
        </div>
      )}

      <section className="bg-white border border-stone-200 rounded-xl shadow-sm p-5" aria-label="Types">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-bold text-stone-700 uppercase tracking-wide">Types</h2>
          <Button variant="ghost" size="sm" icon={RefreshCw} onClick={load}>Refresh</Button>
        </div>
        {types.length === 0 ? (
          <p className="text-sm text-stone-400">No type has been matched or labelled yet. Matching starts once a type has identity fields: set them in <Link href={"/dashboard/schema" as any} className="text-primary hover:underline">the type&apos;s settings</Link>, or in the extractor that writes it. You can also import labelled pairs below.</p>
        ) : (
          <table className="w-full text-sm">
            <thead><tr className="text-left text-xs text-stone-500 border-b border-stone-100">
              <th className="py-1.5">Type</th><th>Labels</th><th>Same / different</th><th>Matches</th><th>Waiting for a person</th>
            </tr></thead>
            <tbody>
              {types.map((t) => (
                <tr key={t.node_type} className={`border-b border-stone-50 cursor-pointer ${sel === t.node_type ? "bg-emerald-50/50" : "hover:bg-stone-50"}`}
                    onClick={() => setSel(t.node_type)} aria-selected={sel === t.node_type}>
                  <td className="py-1.5 font-medium text-stone-800">{t.node_type}</td>
                  <td className="tabular-nums">{t.labels || 0}</td>
                  <td className="tabular-nums">{t.same || 0} / {t.different || 0}</td>
                  <td className="tabular-nums">{t.same_decisions || 0}</td>
                  <td className="tabular-nums">{t.awaiting_review || 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {sel && <TypeQualityPanel key={sel} type={sel} say={say} onChanged={load} />}
      <ImportLabels say={say} onDone={load} />
    </div>
  );
}

function TypeQualityPanel({ type, say, onChanged }: { type: string; say: (t: string, ok: boolean) => void; onChanged: () => void }) {
  const [evalr, setEval] = useState<Evaluation | null>(null);
  const [spec, setSpec] = useState<TypeResolution | null>(null);
  const [draft, setDraft] = useState<Partial<TypeResolution>>({});
  const [audit, setAudit] = useState<AuditItem[] | null>(null);
  const [labels, setLabels] = useState<Label[]>([]);
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    setBusy("load");
    try {
      const [e, r, l] = await Promise.all([qualityApi.evaluate(type), contextV2Api.getTypeResolution(type), qualityApi.labels(type, 100)]);
      setEval(e); setLabels(l);
      setSpec(r.resolution || null); setDraft({});
    } catch (e) { say(apiError(e, "Could not score this type."), false); }
    finally { setBusy(""); }
  }, [type, say]);
  useEffect(() => { load(); }, [load]);

  const changed = Object.keys(draft).length > 0;
  const tryIt = async () => {
    setBusy("try");
    try { setEval(await qualityApi.evaluate(type, draft)); }
    catch (e) { say(apiError(e, "Those settings are not valid."), false); }
    finally { setBusy(""); }
  };
  const saveIt = async () => {
    if (!spec && !draft.identify_by?.length) { say("Pick identity fields for this type first, in the type's settings (Mapper).", false); return; }
    setBusy("save");
    try {
      await contextV2Api.setTypeResolution(type, { ...(spec || {} as TypeResolution), ...draft } as TypeResolution);
      say(`${type}: settings saved. Use "Re-check existing data" on the type to apply them to what is already in.`, true);
      await load(); onChanged();
    } catch (e) { say(apiError(e, "Could not save."), false); }
    finally { setBusy(""); }
  };
  const loadAudit = async () => {
    setBusy("audit");
    try { setAudit(await qualityApi.audit(type, 10)); }
    catch (e) { say(apiError(e, "Could not load automatic merges."), false); }
    finally { setBusy(""); }
  };
  const answer = async (it: AuditItem, same: boolean) => {
    setBusy(`a:${it.decision_id}`);
    try {
      await qualityApi.label(it.a.id, it.b.id, same, true);
      setAudit((a) => (a || []).filter((x) => x.decision_id !== it.decision_id));
      say(same ? "Labelled the same thing." : "Labelled different, and split apart.", true);
      onChanged();
    } catch (e) { say(apiError(e, "Could not save the answer."), false); }
    finally { setBusy(""); }
  };
  const removeLabel = async (l: Label) => {
    try { await qualityApi.deleteLabel(l.id); setLabels((x) => x.filter((y) => y.id !== l.id)); onChanged(); }
    catch (e) { say(apiError(e, "Could not remove the label."), false); }
  };

  const eff = { ...(spec || {}), ...draft } as TypeResolution;
  const set = (patch: Partial<TypeResolution>) => setDraft((d) => ({ ...d, ...patch }));

  return (
    <section className="bg-white border border-stone-200 rounded-xl shadow-sm p-5 space-y-5" aria-label={`${type} quality`}>
      <h2 className="text-sm font-bold text-stone-700 uppercase tracking-wide">{type}</h2>
      {busy === "load" && <p className="text-sm text-stone-400 animate-pulse">Scoring…</p>}
      {evalr && (
        <div className="grid gap-4 md:grid-cols-2">
          <ScoreCard title={evalr.proposed ? "Now" : "Score on the labels"} s={evalr.current} />
          {evalr.proposed && <ScoreCard title="With your changes" s={evalr.proposed} compare={evalr.current} />}
        </div>
      )}

      <div className="border-t border-stone-100 pt-4 space-y-3">
        <h3 className="text-xs font-bold text-stone-500 uppercase">Try other settings - scored before anything is saved</h3>
        {!spec?.identify_by?.length && <p className="text-xs text-amber-700">This type has no identity fields of its own{evalr?.current.rule ? "; its records are matched by name" : ""}. Set them in <Link href={"/dashboard/schema" as any} className="underline">the type&apos;s settings</Link>.</p>}
        <div className="grid gap-3 sm:grid-cols-3 text-sm">
          <label className="space-y-1 text-xs text-stone-500"><span>Match</span>
            <select value={eff.scorer || "exact"} onChange={(e) => set({ scorer: e.target.value as any })} className="w-full border border-stone-200 rounded-lg px-2 py-1.5 bg-white text-sm">
              <option value="exact">Equal values</option><option value="similar">Close spellings too</option>
            </select></label>
          <label className="space-y-1 text-xs text-stone-500"><span>Join automatically at score</span>
            <input type="number" min={0} max={1} step={0.01} value={eff.auto ?? 0.95} onChange={(e) => set({ auto: Number(e.target.value) })}
              className="w-full border border-stone-200 rounded-lg px-2 py-1.5 text-sm" /></label>
          <label className="space-y-1 text-xs text-stone-500"><span>Ask a person from score</span>
            <input type="number" min={0} max={1} step={0.01} value={eff.review ?? 0.8} onChange={(e) => set({ review: Number(e.target.value) })}
              className="w-full border border-stone-200 rounded-lg px-2 py-1.5 text-sm" /></label>
          <label className="space-y-1 text-xs text-stone-500"><span>Also needs a second field to agree</span>
            <select value={eff.corroborate || "default"} onChange={(e) => set({ corroborate: e.target.value as any })} className="w-full border border-stone-200 rounded-lg px-2 py-1.5 bg-white text-sm">
              <option value="default">Workspace setting</option><option value="similar">When only similar</option>
              <option value="always">Always</option><option value="never">Never</option>
            </select></label>
          <label className="space-y-1 text-xs text-stone-500"><span>When other fields disagree</span>
            <select value={eff.conflict_review === null || eff.conflict_review === undefined ? "default" : eff.conflict_review ? "review" : "join"}
              onChange={(e) => set({ conflict_review: e.target.value === "default" ? null : e.target.value === "review" })}
              className="w-full border border-stone-200 rounded-lg px-2 py-1.5 bg-white text-sm">
              <option value="default">Workspace setting</option><option value="review">Ask a person</option><option value="join">Join and flag</option>
            </select></label>
          <label className="space-y-1 text-xs text-stone-500"><span>Must agree (comma-separated fields)</span>
            <input value={(eff.distinguish || []).join(", ")} onChange={(e) => set({ distinguish: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })}
              placeholder="e.g. date_of_birth, email" className="w-full border border-stone-200 rounded-lg px-2 py-1.5 text-sm" /></label>
          <label className="space-y-1 text-xs text-stone-500 sm:col-span-3"><span>Names from extractors</span>
            <select value={eff.name_scope || "workspace"} onChange={(e) => set({ name_scope: e.target.value as any })} className="w-full sm:w-1/2 border border-stone-200 rounded-lg px-2 py-1.5 bg-white text-sm">
              <option value="workspace">One record per name in the workspace (two files&apos; &quot;John Smith&quot; are one)</option>
              <option value="asset">One record per name per file, joined only by these rules</option>
            </select></label>
        </div>
        <div className="flex justify-end gap-2">
          {changed && <Button variant="ghost" size="sm" onClick={() => { setDraft({}); load(); }}>Discard</Button>}
          <Button variant="secondary" size="sm" icon={Gauge} busy={busy === "try"} disabled={!changed || !!busy} onClick={tryIt}>Score these settings</Button>
          <Button variant="primary" size="sm" icon={Save} busy={busy === "save"} disabled={!changed || !!busy} onClick={saveIt}>Save settings</Button>
        </div>
      </div>

      <div className="border-t border-stone-100 pt-4">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-bold text-stone-500 uppercase">Check automatic merges</h3>
          <Button variant="secondary" size="sm" busy={busy === "audit"} onClick={loadAudit}>{audit ? "Load more" : "Show 10 unchecked"}</Button>
        </div>
        <p className="text-xs text-stone-500 mt-1">False merges hide in merges nobody looked at. Each answer is a label; &quot;Different&quot; also splits the two.</p>
        {audit && audit.length === 0 && <p className="text-sm text-stone-400 mt-2">Every automatic merge of this type has been checked.</p>}
        <div className="mt-2 space-y-2">
          {(audit || []).map((it) => (
            <div key={it.decision_id} className="border border-stone-200 rounded-lg p-3 grid gap-3 md:grid-cols-[1fr_1fr_auto] items-start">
              <Side s={it.a} /><Side s={it.b} />
              <div className="flex md:flex-col gap-2">
                <Button variant="secondary" size="sm" icon={ThumbsUp} busy={busy === `a:${it.decision_id}`} onClick={() => answer(it, true)}>Same</Button>
                <Button variant="danger" size="sm" icon={Split} busy={busy === `a:${it.decision_id}`} onClick={() => answer(it, false)}>Different</Button>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="border-t border-stone-100 pt-4">
        <h3 className="text-xs font-bold text-stone-500 uppercase mb-2">Labels ({labels.length})</h3>
        {labels.length === 0 ? <p className="text-sm text-stone-400">No labels yet. Answer possible duplicates in Decisions, check automatic merges above, or import a labelled set.</p> : (
          <ul className="divide-y divide-stone-100 text-sm max-h-72 overflow-y-auto">
            {labels.map((l) => (
              <li key={l.id} className="flex items-center justify-between py-1.5 gap-2">
                <span className="min-w-0 truncate"><span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded border mr-2 ${l.same ? "bg-emerald-50 text-emerald-700 border-emerald-200" : "bg-red-50 text-red-700 border-red-200"}`}>{l.same ? "same" : "different"}</span>
                  {l.a.label} <span className="text-stone-400">·</span> {l.b.label} <span className="text-xs text-stone-400">({l.origin})</span></span>
                <button type="button" aria-label="Remove label" onClick={() => removeLabel(l)} className="p-1 text-stone-400 hover:text-red-600"><Trash2 size={14} /></button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function ScoreCard({ title, s, compare }: { title: string; s: Score; compare?: Score }) {
  const delta = (a?: number | null, b?: number | null) => (a === null || a === undefined || b === null || b === undefined || a === b) ? "" : a > b ? " ▲" : " ▼";
  if (!s.labels) return <div className="rounded-lg border border-stone-200 p-4 text-sm text-stone-500"><b>{title}</b><p className="mt-1">No labels for this type yet.</p></div>;
  return (
    <div className={`rounded-lg border p-4 text-sm ${s.false_merges ? "border-red-200 bg-red-50/40" : "border-emerald-200 bg-emerald-50/40"}`}>
      <div className="font-semibold text-stone-800 mb-2">{title}</div>
      <div className="grid grid-cols-3 gap-2">
        <Metric label="False merges" value={String(s.false_merges)} warn={s.false_merges > 0}
                hint={s.false_merges_at_write ? `${s.false_merges_at_write} made when written (same name)` : ""} />
        <Metric label="Precision" value={pct(s.precision) + delta(s.precision, compare?.precision)} />
        <Metric label="Recall" value={pct(s.recall) + delta(s.recall, compare?.recall)} />
        <Metric label="Found" value={`${s.true_merges} of ${s.pairs_same}`} />
        <Metric label="Kept apart correctly" value={`${s.correct_apart} of ${s.pairs_different}`} />
        <Metric label="For a person" value={String(s.review_pairs)} />
      </div>
      {s.note && <p className="text-xs text-stone-500 mt-2">{s.note}</p>}
      {s.false_merge_examples.length > 0 && (
        <details className="mt-2"><summary className="text-xs text-red-700 cursor-pointer">The false merges</summary>
          <ul className="text-xs mt-1 space-y-0.5">{s.false_merge_examples.map((e, i) => <li key={i}>{e.a} · {e.b}{e.at_write ? " (joined when written: same name)" : ""}</li>)}</ul></details>
      )}
      {s.missed_examples.length > 0 && (
        <details className="mt-1"><summary className="text-xs text-stone-600 cursor-pointer">The missed ones</summary>
          <ul className="text-xs mt-1 space-y-0.5">{s.missed_examples.map((e, i) => <li key={i}>{e.a} · {e.b}{e.in_review ? " (waits for a person)" : ""}</li>)}</ul></details>
      )}
    </div>
  );
}

function Metric({ label, value, warn, hint }: { label: string; value: string; warn?: boolean; hint?: string }) {
  return <div><div className="text-[11px] text-stone-500">{label}</div><div className={`font-semibold ${warn ? "text-red-700" : "text-stone-900"}`}>{value}</div>{hint && <div className="text-[10px] text-stone-400">{hint}</div>}</div>;
}

function Side({ s }: { s: AuditItem["a"] }) {
  const props = Object.entries(s.properties || {}).filter(([k]) => !k.startsWith("__")).slice(0, 8);
  return (
    <div className="text-sm min-w-0">
      <div className="font-semibold text-stone-800 truncate">{s.label || "(no name)"}</div>
      <dl className="text-xs text-stone-600 mt-1 space-y-0.5">{props.map(([k, v]) => <div key={k} className="truncate"><dt className="inline text-stone-400">{k}: </dt><dd className="inline">{String(v)}</dd></div>)}</dl>
    </div>
  );
}

/** A labelled set: CSV (type,a,b,same) or JSON (the API's items), for a benchmark or your own pairs. */
function ImportLabels({ say, onDone }: { say: (t: string, ok: boolean) => void; onDone: () => void }) {
  const [text, setText] = useState("");
  const [named, setNamed] = useState(false);
  const [busy, setBusy] = useState(false);
  const parsed = useMemo(() => parse(text), [text]);
  const go = async () => {
    if (!parsed.items.length) return;
    setBusy(true);
    try {
      const r = await qualityApi.importLabels(parsed.items, named);
      say(`${r.imported} labelled pair(s) imported${r.skipped ? `, ${r.skipped} skipped (the same record twice)` : ""}.`, true);
      setText(""); onDone();
    } catch (e) { say(apiError(e, "Could not import the labels."), false); }
    finally { setBusy(false); }
  };
  return (
    <section className="bg-white border border-stone-200 rounded-xl shadow-sm p-5 space-y-2" aria-label="Import labels">
      <h2 className="text-sm font-bold text-stone-700 uppercase tracking-wide">Import labelled pairs</h2>
      <p className="text-xs text-stone-500">One pair per line as <code>type,first name,second name,same</code> (same = yes/no), or the JSON the API takes. Public sets such as DBLP-ACM or Abt-Buy fit this shape.</p>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={5} aria-label="Labelled pairs"
        placeholder={"Person,Kristin Hannah,Kristen Hannah,yes\nPerson,Hans Andersen,Hans Anderson,no"}
        className="w-full border border-stone-200 rounded-lg px-3 py-2 text-xs font-mono" />
      {parsed.errors.length > 0 && <p className="text-xs text-red-600">{parsed.errors.slice(0, 3).join(" · ")}</p>}
      <div className="flex items-center justify-between">
        <label className="flex items-center gap-2 text-xs text-stone-600"><input type="checkbox" checked={named} onChange={(e) => setNamed(e.target.checked)} />The records are names an extractor finds (people, companies)</label>
        <Button variant="primary" size="sm" icon={Upload} busy={busy} disabled={!parsed.items.length || parsed.errors.length > 0 || busy} onClick={go}>
          Import {parsed.items.length || ""}
        </Button>
      </div>
    </section>
  );
}

function parse(text: string): { items: LabelImportItem[]; errors: string[] } {
  const t = text.trim();
  if (!t) return { items: [], errors: [] };
  if (t.startsWith("[") || t.startsWith("{")) {
    try {
      const v = JSON.parse(t);
      const items = Array.isArray(v) ? v : v.items;
      if (!Array.isArray(items)) return { items: [], errors: ["JSON must be a list of pairs, or {\"items\": [...]}"] };
      return { items, errors: [] };
    } catch { return { items: [], errors: ["That is not valid JSON."] }; }
  }
  const items: LabelImportItem[] = [];
  const errors: string[] = [];
  t.split(/\r?\n/).forEach((line, i) => {
    if (!line.trim() || /^type\s*,/i.test(line)) return;
    const cols = line.split(",").map((c) => c.trim());
    if (cols.length !== 4 || !cols[0] || !cols[1] || !cols[2]) { errors.push(`line ${i + 1}: needs type,first,second,same`); return; }
    const same = /^(y|yes|true|1|same)$/i.test(cols[3]) ? true : /^(n|no|false|0|different)$/i.test(cols[3]) ? false : null;
    if (same === null) { errors.push(`line ${i + 1}: "same" must be yes or no`); return; }
    items.push({ node_type: cols[0], a: { label: cols[1] }, b: { label: cols[2] }, same });
  });
  return { items, errors };
}
