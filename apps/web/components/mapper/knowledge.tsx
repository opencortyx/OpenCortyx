"use client";

/**
 * Knowledge — what this mapping SAYS the data means (knowledge/semantic on the server).
 *
 * The rules above describe how a source is read; this declares the business meaning: which entity
 * types live in which assets, the field (or fields) that identify a record, the properties exposed,
 * and the relationships to other types. Records stay in their tables - an entity is turned into
 * graph nodes only when "Publish to graph" is on. The Detector fills this in for detected mappings;
 * anything changed here is kept (curated) and saved as a new version.
 *
 * A correction tool, not a step: detection fills this in, and a person changes what it got wrong.
 */

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Eye, History, KeyRound, Link2, Loader2, Plus, RefreshCw, Save, ShieldCheck, Trash2 } from "lucide-react";
import Button from "@/components/ui/Button";
import {
  contextV2Api, KnowledgeEntity, KnowledgeRelationship, MappingHistory, MappingKnowledge, MappingPreview,
  MappingValidation,
} from "@/lib/api/contextV2";

const input = "w-full rounded-md border border-stone-200 px-2 py-1 text-sm";
const STATUSES = ["accepted", "suggested", "rejected"] as const;

function errorText(e: any): string {
  return e?.response?.data?.detail || e?.message || "Failed";
}

const emptyEntity = (): KnowledgeEntity => ({
  type: "", keys: [], kind: "entity", path: "", assets: [{ id: "", name: "", fields: {} }], properties: [],
  materialize: false, status: "accepted", confidence: 1, rows: 0,
});

/** "prop = field" lines <-> the fields map. */
const fieldsText = (f: Record<string, string>) => Object.entries(f || {}).map(([p, s]) => (p === s ? p : `${p} = ${s}`)).join("\n");
const parseFields = (t: string) => Object.fromEntries(t.split("\n").map((l) => l.trim()).filter(Boolean)
  .map((l) => { const [p, s] = l.split("=").map((x) => x.trim()); return [p, s || p]; }));

export default function KnowledgePanel({ mappingId, payload, onSaved }: {
  mappingId: string; payload: any; onSaved: (rec: any) => void;
}) {
  const [k, setK] = useState<MappingKnowledge>({ entities: [], relationships: [] });
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [check, setCheck] = useState<MappingValidation | null>(null);
  const [preview, setPreview] = useState<MappingPreview | null>(null);
  const [history, setHistory] = useState<MappingHistory | null>(null);

  useEffect(() => {
    const cur = payload?.knowledge || { entities: [], relationships: [] };
    setK({ entities: cur.entities || [], relationships: cur.relationships || [], version: cur.version, curated: cur.curated });
    setDirty(false);
  }, [payload]);

  const edit = (next: MappingKnowledge) => { setK(next); setDirty(true); setCheck(null); setPreview(null); };
  const setEntity = (i: number, patch: Partial<KnowledgeEntity>) =>
    edit({ ...k, entities: k.entities.map((e, j) => (j === i ? { ...e, ...patch } : e)) });
  const setRel = (i: number, patch: Partial<KnowledgeRelationship>) =>
    edit({ ...k, relationships: k.relationships.map((r, j) => (j === i ? { ...r, ...patch } : r)) });
  const typeNames = useMemo(() => k.entities.map((e) => e.type).filter(Boolean), [k.entities]);

  const run = async (what: string, fn: () => Promise<void>) => {
    setBusy(what); setMsg(null);
    try { await fn(); } catch (e) { setMsg({ ok: false, text: errorText(e) }); } finally { setBusy(""); }
  };

  const save = () => run("save", async () => {
    const rec = await contextV2Api.updateSchemaMapping(mappingId, { payload: { ...(payload || {}), knowledge: k } });
    onSaved(rec);
    setMsg({ ok: true, text: `Saved as version ${rec?.payload?.knowledge?.version ?? ""}.` });
  });
  const validate = () => run("validate", async () => setCheck(await contextV2Api.validateMapping(mappingId)));
  const showPreview = () => run("preview", async () => setPreview(await contextV2Api.previewMapping(mappingId)));
  const showHistory = () => run("history", async () => setHistory(await contextV2Api.mappingHistory(mappingId)));
  const restore = (v: MappingKnowledge) => { edit({ ...v }); setHistory(null); };
  const redetect = () => {
    if (!confirm("Re-detect this mapping from the data as it is now? Your edits are replaced by what the Detector finds "
                 + "(the current version stays under Versions); \"Publish to graph\" is kept.")) return;
    run("redetect", async () => {
      const rec = await contextV2Api.redetectMapping(mappingId);
      onSaved(rec);
      setMsg({ ok: true, text: `Re-detected as version ${rec?.payload?.knowledge?.version ?? ""}.` });
    });
  };

  return (
    <div className="space-y-4 p-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <p className="flex-1 min-w-[260px] text-[12.5px] text-stone-500">
          What the data means: entity types, the field that identifies each record, and how types refer to each other.
          Records stay in their tables; questions read them through these types. {k.version ? `Version ${k.version}${k.curated ? " · edited" : " · detected"}.` : ""}
        </p>
        <Button variant="secondary" size="sm" icon={ShieldCheck} busy={busy === "validate"} disabled={dirty} onClick={validate}
          title={dirty ? "Save first" : "Check identifiers, references and joins against the data"}>Validate</Button>
        <Button variant="secondary" size="sm" icon={Eye} busy={busy === "preview"} disabled={dirty} onClick={showPreview}>Preview</Button>
        <Button variant="secondary" size="sm" icon={History} busy={busy === "history"} onClick={showHistory}>Versions</Button>
        <Button variant="secondary" size="sm" icon={RefreshCw} busy={busy === "redetect"} disabled={dirty} onClick={redetect}
          title={dirty ? "Save or discard your edits first" : "Read this mapping's data again and take what the Detector finds now"}>
          Re-detect this mapping
        </Button>
        <Button variant="primary" size="sm" icon={Save} busy={busy === "save"} disabled={!dirty} onClick={save}>Save knowledge</Button>
      </div>
      {msg && <p className={`text-sm ${msg.ok ? "text-emerald-700" : "text-red-600"}`}>{msg.text}</p>}

      {k.entities.map((e, i) => (
        <div key={i} className="rounded-lg border border-stone-200 p-3 space-y-2">
          <div className="grid gap-2 md:grid-cols-[1.2fr_1.2fr_1fr_auto_auto]">
            <label className="text-xs text-stone-500">Entity type
              <input className={input} value={e.type} onChange={(ev) => setEntity(i, { type: ev.target.value })} placeholder="Customer" />
            </label>
            <label className="text-xs text-stone-500 flex flex-col"><span className="flex items-center gap-1"><KeyRound size={11} /> Identifier field(s)</span>
              <input className={input} value={e.keys.join(", ")} placeholder="customer_id (several: a, b)"
                onChange={(ev) => setEntity(i, { keys: ev.target.value.split(",").map((s) => s.trim()).filter(Boolean),
                                                  kind: ev.target.value.trim() ? "entity" : "event" })} />
            </label>
            <label className="text-xs text-stone-500">Record path
              <input className={input} value={e.path} onChange={(ev) => setEntity(i, { path: ev.target.value })} placeholder="rows, $.project or $.customers[*]" />
            </label>
            <label className="text-xs text-stone-500">Status
              <select className={input} value={e.status} onChange={(ev) => setEntity(i, { status: ev.target.value as any })}>
                {STATUSES.map((s) => <option key={s}>{s}</option>)}
              </select>
            </label>
            <label className="text-xs text-stone-500 flex flex-col items-start"
              title="Put each record in the knowledge graph as a node (one per identifier). Off: records stay in their tables and are read with SQL.">
              Publish to graph
              <input type="checkbox" className="mt-2" checked={e.materialize} onChange={(ev) => setEntity(i, { materialize: ev.target.checked })} />
            </label>
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            <div className="space-y-1">
              <div className="text-xs text-stone-500">Sources ({e.assets.length}) - asset name, and its &ldquo;property = field&rdquo; lines</div>
              {e.assets.slice(0, 3).map((a, j) => (
                <div key={j} className="grid grid-cols-[1fr_1.4fr] gap-2">
                  <input className={input} value={a.name} placeholder="asset name"
                    onChange={(ev) => setEntity(i, { assets: e.assets.map((x, n) => (n === j ? { ...x, name: ev.target.value, id: "" } : x)) })} />
                  <textarea className={`${input} font-mono text-[12px]`} rows={Math.min(6, Object.keys(a.fields || {}).length + 1)}
                    value={fieldsText(a.fields)} placeholder={"customer_id\nemail = contact.email"}
                    onChange={(ev) => setEntity(i, { assets: e.assets.map((x, n) => (n === j ? { ...x, fields: parseFields(ev.target.value) } : x)) })} />
                </div>
              ))}
              {e.assets.length > 3 && (
                <p className="text-xs text-stone-400">…and {e.assets.length - 3} more with the same fields
                  ({e.assets.slice(3, 8).map((a) => a.name).join(", ")}{e.assets.length > 8 ? ", …" : ""}).</p>
              )}
              <button className="text-xs text-teal-700 hover:underline"
                onClick={() => setEntity(i, { assets: [...e.assets, { id: "", name: "", fields: { ...(e.assets[0]?.fields || {}) } }] })}>+ source</button>
            </div>
            <div>
              <div className="text-xs text-stone-500 mb-1">Properties</div>
              <div className="flex flex-wrap gap-1">
                {e.properties.map((p) => (
                  <span key={p.name} title={`${p.data_type}, ${p.role}`}
                    className={`rounded px-1.5 py-0.5 text-xs border ${e.keys.includes(p.name) ? "border-stone-900" : "border-stone-200"}`}>
                    {p.name}<span className="text-stone-400"> · {p.role}</span>
                  </span>
                ))}
                {!e.properties.length && <span className="text-xs text-stone-400">Read from the fields when validated.</span>}
              </div>
              {e.confidence < 1 && <p className="text-xs text-stone-400 mt-1">Detected with confidence {Math.round(e.confidence * 100)}%.</p>}
              {e.kind === "entity" && e.properties.length > 0 && (
                <label className="mt-2 block text-xs text-stone-500"
                  title="The field a person would call a record by. Published records are labelled by it, and names read from text are matched against it.">
                  Named by
                  <select className={input} value={e.label || ""} onChange={(ev) => setEntity(i, { label: ev.target.value })}>
                    <option value="">(its identifier)</option>
                    {e.properties.filter((p) => ["text", "key"].includes(p.role)).map((p) => <option key={p.name}>{p.name}</option>)}
                  </select>
                </label>
              )}
            </div>
          </div>

          <button className="text-xs text-red-600 hover:underline flex items-center gap-1"
            onClick={() => edit({ ...k, entities: k.entities.filter((_, j) => j !== i) })}><Trash2 size={12} /> Remove entity</button>
        </div>
      ))}
      <button className="text-sm text-teal-700 hover:underline flex items-center gap-1"
        onClick={() => edit({ ...k, entities: [...k.entities, emptyEntity()] })}><Plus size={14} /> Add entity</button>

      <div className="rounded-lg border border-stone-200 p-3 space-y-2">
        <h4 className="text-sm font-semibold flex items-center gap-2"><Link2 size={14} /> Relationships</h4>
        {k.relationships.map((r, i) => (
          <div key={i} className="grid gap-2 items-end grid-cols-2 sm:grid-cols-3 rounded-md border border-stone-100 p-2">
            <label className="text-xs text-stone-500">Name<input className={input} value={r.name} onChange={(ev) => setRel(i, { name: ev.target.value.toUpperCase() })} /></label>
            <label className="text-xs text-stone-500">From type<input className={input} list="kn-types" value={r.from_type} onChange={(ev) => setRel(i, { from_type: ev.target.value })} /></label>
            <label className="text-xs text-stone-500">Field<input className={input} value={r.from_field} onChange={(ev) => setRel(i, { from_field: ev.target.value })} /></label>
            <label className="text-xs text-stone-500">To type<input className={input} value={r.to_type} onChange={(ev) => setRel(i, { to_type: ev.target.value })} /></label>
            <label className="text-xs text-stone-500">Its field<input className={input} value={r.to_field} onChange={(ev) => setRel(i, { to_field: ev.target.value })} /></label>
            <div className="flex items-center gap-2">
              <span className="text-xs text-stone-400 whitespace-nowrap" title={r.origin === "inferred" ? `from values: ${Math.round(r.coverage * 100)}% match` : "declared"}>
                {r.origin}{r.origin === "inferred" ? ` ${Math.round(r.coverage * 100)}%` : ""}
              </span>
              <select className={input} value={r.status} onChange={(ev) => setRel(i, { status: ev.target.value as any })}>
                {STATUSES.map((s) => <option key={s}>{s}</option>)}
              </select>
              <button aria-label="Remove relationship" className="text-stone-400 hover:text-red-600"
                onClick={() => edit({ ...k, relationships: k.relationships.filter((_, j) => j !== i) })}><Trash2 size={14} /></button>
            </div>
          </div>
        ))}
        <datalist id="kn-types">{typeNames.map((t) => <option key={t} value={t} />)}</datalist>
        <button className="text-sm text-teal-700 hover:underline flex items-center gap-1"
          onClick={() => edit({ ...k, relationships: [...k.relationships, { name: "", from_type: typeNames[0] || "", from_field: "", to_type: "",
                                                                            to_field: "", origin: "declared", coverage: 0, status: "accepted" }] })}>
          <Plus size={14} /> Add relationship
        </button>
      </div>

      {check && (
        <div className={`rounded-lg border p-3 text-sm ${check.valid ? "border-emerald-200 bg-emerald-50" : "border-red-200 bg-red-50"}`}>
          <div className="font-semibold flex items-center gap-2">
            {check.valid ? <CheckCircle2 size={14} className="text-emerald-600" /> : <AlertTriangle size={14} className="text-red-600" />}
            {check.valid ? "Valid" : "Not valid"} · {check.issues.length} issue{check.issues.length === 1 ? "" : "s"}
          </div>
          <ul className="mt-1 space-y-0.5">
            {check.issues.map((x, i) => (
              <li key={i} className={x.level === "error" ? "text-red-700" : "text-amber-700"}>
                <b>{x.where}</b>: {x.text}{x.examples?.length ? ` (e.g. ${x.examples.join(", ")})` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}

      {preview && (
        <div className="space-y-3">
          {preview.entities.map((p) => (
            <div key={p.type} className="rounded-lg border border-stone-200 overflow-x-auto">
              <div className="px-3 py-1.5 text-xs bg-stone-50 border-b border-stone-100">
                <b>{p.type}</b> · {p.records} records · from {p.sources.slice(0, 4).join(", ")}{p.sources.length > 4 ? "…" : ""}
              </div>
              <Rows rows={p.rows} />
            </div>
          ))}
          {preview.relationships.map((r) => (
            <div key={r.name + r.from} className="rounded-lg border border-stone-200 overflow-x-auto">
              <div className="px-3 py-1.5 text-xs bg-stone-50 border-b border-stone-100">
                <b>{r.from}</b> -{r.name}→ <b>{r.to}</b> · {r.origin}
              </div>
              <Rows rows={r.rows} />
            </div>
          ))}
        </div>
      )}

      {history && (
        <div className="rounded-lg border border-stone-200 p-3 text-sm">
          <div className="font-semibold mb-1">Versions</div>
          {history.versions.length === 0 && <p className="text-stone-400">No versions yet.</p>}
          <ul className="space-y-1">
            {history.versions.map((v) => (
              <li key={`${v.version}-${v.at}`} className="flex items-center gap-2">
                <span className="font-mono">v{v.version}</span>
                <span className="text-stone-400 text-xs">{v.at ? new Date(v.at).toLocaleString() : ""}</span>
                <span className="text-xs text-stone-500">{v.knowledge?.entities?.length || 0} entities, {v.knowledge?.relationships?.length || 0} relationships</span>
                {v.current ? <span className="text-xs text-emerald-700">current</span>
                  : <button className="text-xs text-teal-700 hover:underline" onClick={() => restore(v.knowledge)}>restore</button>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {busy && !["save", "validate", "preview", "history", "redetect"].includes(busy) && <Loader2 className="animate-spin" size={14} />}
    </div>
  );
}

function Rows({ rows }: { rows: Record<string, any>[] }) {
  if (!rows?.length) return <p className="px-3 py-2 text-xs text-stone-400">No rows.</p>;
  const cols = Object.keys(rows[0]);
  return (
    <table className="min-w-full text-xs">
      <thead><tr>{cols.map((c) => <th key={c} className="px-2 py-1 text-left font-medium text-stone-500">{c}</th>)}</tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} className="border-t border-stone-100">
            {cols.map((c) => <td key={c} className="px-2 py-1 whitespace-nowrap">{r[c] == null ? "" : String(r[c])}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
