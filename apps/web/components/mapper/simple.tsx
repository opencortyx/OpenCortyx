"use client";

/**
 * What this data is — the Mapper in plain sentences.
 *
 * Every choice is a dropdown built from the data itself (`POST /schema-mappings/shape`): the records
 * a file holds, and for each field whether it is filled everywhere and never repeats within a file
 * (an identifier) or is a short text a person would call a record by (a name). A choice the data
 * does not support is not offered, so a mapping cannot say "identified by a list of conversations".
 * A preview of the first records is always on screen; one Save writes it and checks it.
 *
 * The rule editor (node and edge rules, the graph shaped by hand) stays under Advanced.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Link2, Loader2, Plus, RefreshCw, Save, Trash2, X } from "lucide-react";
import Button from "@/components/ui/Button";
import {
  contextV2Api, KnowledgeEntity, KnowledgeRelationship, MappingAsset, MappingKnowledge, MappingValidation,
  SemanticLayer, ShapeSource,
} from "@/lib/api/contextV2";

const select = "rounded-md border border-stone-300 bg-white px-2 py-1 text-sm font-medium text-stone-900 " +
  "focus:outline-none focus:ring-2 focus:ring-primary/30";

function errorText(e: any): string {
  return e?.response?.data?.detail || e?.message || "Failed";
}

/** How a record source reads in a sentence. */
function recordLabel(s: ShapeSource): string {
  if (!s.path) return s.kind === "structured" ? "each row" : "each record";
  const key = s.path.replace(/^\$\./, "").replace(/(\[\*\])+$/, "");
  if (s.path.endsWith("[*][*]")) return `each item of each list in "${key}"`;
  return s.path.endsWith("[*]") ? `each item in "${key}"` : `the "${key}" object in each record`;
}

/** "rows", "$" and "" all mean each record of the file. */
const normPath = (p?: string) => (["rows", "row", "$"].includes((p || "").trim().toLowerCase()) ? "" : (p || "").trim());

type Field = ShapeSource["fields"][number];

/** What a type's files hold at its record path, judged across all of them: a field identifies only if
 *  it identifies in every file, and names only if it names in every file that has it. */
interface Measured {
  loading: boolean;
  paths: ShapeSource[];          // the record sources on offer (one per path, from the first file that has it)
  fields: Field[];
  records: number;
  rows: Record<string, any>[];
  errors: string[];
  perAsset: Record<string, ShapeSource>;
}

function measure(e: KnowledgeEntity, shapes: Record<string, ShapeSource[]>): Measured {
  const path = normPath(e.path);
  const ids = (e.assets || []).map((a) => a.id).filter(Boolean);
  const paths: ShapeSource[] = [];
  const perAsset: Record<string, ShapeSource> = {};
  for (const a of ids) {
    for (const s of shapes[a] || []) {
      if (!paths.some((p) => p.path === s.path)) paths.push(s);
      if (s.path === path) perAsset[a] = s;
    }
  }
  const srcs = Object.values(perAsset);
  const byName = new Map<string, Field>();
  for (const s of srcs) {
    for (const f of s.fields) {
      const cur = byName.get(f.name);
      byName.set(f.name, cur ? { ...cur, filled: cur.filled + f.filled, distinct: cur.distinct + f.distinct,
        repeats: cur.repeats + f.repeats, words: Math.max(cur.words, f.words),
        identifier: cur.identifier && f.identifier, nameable: cur.nameable && f.nameable } : { ...f });
    }
  }
  // a field missing from some file cannot identify those files' records
  const fields = Array.from(byName.values()).map((f) =>
    srcs.every((s) => s.fields.some((x) => x.name === f.name)) ? f : { ...f, identifier: false });
  return {
    loading: ids.some((a) => !(a in shapes)),
    paths, fields, perAsset,
    records: srcs.reduce((n, s) => n + (s.records || 0), 0),
    rows: srcs.flatMap((s) => s.rows).slice(0, 3),
    errors: srcs.filter((s) => s.error).map((s) => `${s.asset_name}: ${s.error}`),
  };
}

const FILES_SHOWN = 6;

/** A field's role from what was measured: the choices made here, then the data. */
function roleOf(f: Field, key: string, refs: Set<string>): string {
  if (f.name === key) return "key";
  if (refs.has(f.name)) return "reference";
  if (f.data_type === "date") return "time";
  if (f.data_type === "number") return "measure";
  if (f.words >= 8) return "prose";
  return "text";
}

export default function SimpleMapping({ mappingId, paradigm, payload, status, onSaved }: {
  mappingId: string;
  paradigm: string;
  payload: any;
  /** the mapping's own status: a draft detection proposed is put in use by a person's save */
  status?: string;
  onSaved: (rec: any) => void;
}) {
  const [entities, setEntities] = useState<KnowledgeEntity[]>([]);
  const [rels, setRels] = useState<KnowledgeRelationship[]>([]);
  const [assets, setAssets] = useState<MappingAsset[]>([]);
  const [shapes, setShapes] = useState<Record<string, ShapeSource[]>>({});     // asset id -> its sources
  const [layer, setLayer] = useState<SemanticLayer | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [check, setCheck] = useState<MappingValidation | null>(null);
  const [showAll, setShowAll] = useState<Record<number, boolean>>({});
  const [orig, setOrig] = useState<string[]>([]);        // each entity's type name as loaded

  useEffect(() => {
    const k = payload?.knowledge || {};
    const ents: KnowledgeEntity[] = (k.entities || []).length ? k.entities : [{
      type: "", keys: [], kind: "entity", path: "", assets: [], properties: [], materialize: false,
      status: "accepted", confidence: 1, rows: 0,
    }];
    setEntities(ents.map((e: KnowledgeEntity) => ({ ...e })));
    setOrig(ents.map((e: KnowledgeEntity) => e.type));
    setRels((k.relationships || []).map((r: KnowledgeRelationship) => ({ ...r })));
    setDirty(false); setCheck(null);
  }, [payload]);

  // Every table and JSON file: a detected type can read either, whatever the mapping's kind.
  useEffect(() => {
    contextV2Api.mappingAssets().then(setAssets).catch(() => setAssets([]));
    contextV2Api.knowledgeLayer().then(setLayer).catch(() => setLayer(null));
  }, []);
  // the mapping's own kind of file first
  const own = paradigm === "semistructured" ? "semistructured" : "structured";
  const kinds = [own, own === "structured" ? "semistructured" : "structured"];

  // Measure every asset the entities read, once each.
  const assetIds = useMemo(() => Array.from(new Set(entities.flatMap((e) => (e.assets || []).map((a) => a.id)).filter(Boolean))),
    [entities]);
  const asked = useRef(new Set<string>());
  useEffect(() => {
    const missing = assetIds.filter((a) => !(a in shapes) && !asked.current.has(a));
    if (!missing.length) return;
    missing.forEach((a) => asked.current.add(a));
    contextV2Api.mappingShape(missing).then((srcs) => {
      setShapes((cur) => {
        const next = { ...cur };
        for (const a of missing) next[a] = srcs.filter((s) => s.asset_id === a);
        return next;
      });
    }).catch(() => setShapes((cur) => ({ ...cur, ...Object.fromEntries(missing.map((a) => [a, []])) })));
  }, [assetIds, shapes]);

  const edit = (i: number, patch: Partial<KnowledgeEntity>) => {
    setEntities((es) => es.map((e, j) => (j === i ? { ...e, ...patch } : e)));
    setDirty(true); setMsg(null); setCheck(null);
  };

  const save = async () => {
    setBusy("save"); setMsg(null);
    try {
      // a renamed type keeps its links: here and in every other mapping that names it
      const renames = new Map(entities.map((e, i) => [orig[i], e.type.trim()] as [string, string])
        .filter(([a, b]) => a && b && a !== b));
      const rn = (t: string) => renames.get(t) || t;
      // and a link into a type joins on that type's identifier as it is now
      const keyOf = new Map<string, string>([
        ...(layer?.types || []).filter((t) => t.keys?.length).map((t) => [t.name, t.keys[0]] as [string, string]),
        ...entities.filter((e) => e.type.trim() && e.keys[0]).map((e) => [e.type.trim(), e.keys[0]] as [string, string]),
      ]);
      const relink = (rs: KnowledgeRelationship[]) => rs.map((r) => {
        const to = rn(r.to_type);
        return { ...r, from_type: rn(r.from_type), to_type: to, to_field: keyOf.get(to) || r.to_field };
      });
      const knowledge: MappingKnowledge = {
        entities: entities.filter((e) => e.type.trim()).map((e) => {
          const m = measure(e, shapes);
          const key = e.keys[0] || "";
          const refs = new Set(rels.filter((r) => r.from_type === e.type).map((r) => r.from_field));
          const read = !m.loading && m.fields.length > 0;
          return {
            ...e,
            type: e.type.trim(),
            path: normPath(e.path),
            kind: key ? "entity" : "event",
            // saving here is a person's decision: what is not ignored is accepted
            status: e.status === "rejected" ? "rejected" : "accepted",
            materialize: !!key && e.materialize,               // events stay rows (the box shows it off)
            assets: (e.assets || []).map((a) => {
              const s = m.perAsset[a.id];
              return s ? { ...a, fields: Object.fromEntries(s.fields.map((f) => [f.name, f.source])) } : a;
            }),
            properties: read ? m.fields.map((f) => ({ name: f.name, data_type: f.data_type, role: roleOf(f, key, refs),
                                                      distinct: f.distinct, words: f.words }))
                             : e.properties,
            rows: read ? m.records : e.rows,
            confidence: 1,
          };
        }),
        relationships: relink(rels),
      };
      const rec = await contextV2Api.updateSchemaMapping(mappingId, {
        payload: { ...(payload || {}), knowledge }, ...(status === "draft" ? { status: "active" } : {}) });
      if (renames.size) {
        const others = (await contextV2Api.listSchemaMappings()).items.filter((m) => String(m.id) !== mappingId);
        for (const m of others) {
          const k = (m.payload as any)?.knowledge;
          if (!k?.relationships?.some((r: KnowledgeRelationship) => renames.has(r.from_type) || renames.has(r.to_type))) continue;
          await contextV2Api.updateSchemaMapping(String(m.id), {
            payload: { ...(m.payload as any), knowledge: { ...k, relationships: relink(k.relationships) } } });
        }
      }
      onSaved(rec);
      setDirty(false);
      const v = await contextV2Api.validateMapping(mappingId).catch(() => null);
      setCheck(v);
      setMsg({ ok: !v || v.valid, text: !v || v.valid ? "Saved and checked against the data." : "Saved - the data disagrees with something below." });
    } catch (e) {
      setMsg({ ok: false, text: errorText(e) });
    } finally { setBusy(""); }
  };

  const redetect = async () => {
    if (!confirm("Read this data again and take what detection finds now? Your edits are replaced; the current version stays under Advanced > Versions.")) return;
    setBusy("redetect"); setMsg(null);
    try {
      const rec = await contextV2Api.redetectMapping(mappingId);
      onSaved(rec);
      setShapes({}); asked.current.clear();
      setMsg({ ok: true, text: "Detected again from the data." });
    } catch (e) {
      setMsg({ ok: false, text: errorText(e) });
    } finally { setBusy(""); }
  };

  const otherTypes = (layer?.types || []).filter((t) => t.keys?.length);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex-1" />
        <Button variant="secondary" size="sm" icon={RefreshCw} busy={busy === "redetect"} onClick={redetect}
                title="Read the data again and take what detection finds now">Re-detect</Button>
        <Button variant="primary" size="sm" icon={Save} busy={busy === "save"} disabled={!dirty} onClick={save}>
          {dirty ? "Save" : "Saved"}
        </Button>
      </div>
      {msg && (
        <p className={`flex items-center gap-1.5 text-sm ${msg.ok ? "text-emerald-700" : "text-red-700"}`}>
          {msg.ok ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />} {msg.text}
        </p>
      )}
      {check && !check.valid && (
        <ul className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 space-y-0.5">
          {check.issues.map((x, i) => <li key={i}><b>{x.where}</b>: {x.text}{x.examples?.length ? ` (e.g. ${x.examples.join(", ")})` : ""}</li>)}
        </ul>
      )}

      {entities.map((e, i) => {
        const files = e.assets || [];
        const has = files.length > 0;
        const m = measure(e, shapes);
        const path = normPath(e.path);
        const ids = m.fields.filter((f) => f.identifier);
        const names = m.fields.filter((f) => f.nameable);
        const key = e.keys[0] || "";
        const ignored = e.status === "rejected";
        const cols = [key, e.label || "", ...m.fields.map((f) => f.name)].filter((c, k, a) => c && a.indexOf(c) === k).slice(0, 6);
        const myRels = rels.filter((r) => r.from_type === e.type);
        const open = !!showAll[i];
        const addable = assets.filter((a) => !files.some((f) => f.id === a.id));
        const addFile = (id: string) => {
          const a = assets.find((x) => x.id === id);
          if (a) edit(i, { assets: [...files, { id: a.id, name: a.name, fields: {} }] });
        };
        const filePicker = (cls: string, first: string) => (
          <select className={cls} aria-label="Add a file" value="" onChange={(ev) => addFile(ev.target.value)}>
            <option value="">{first}</option>
            {kinds.map((k) => (
              <optgroup key={k} label={k === "structured" ? "Tables" : "JSON and XML"}>
                {addable.filter((a) => a.kind === k).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </optgroup>))}
          </select>
        );
        return (
          <div key={i} className={`rounded-xl border p-4 space-y-3 ${ignored ? "border-stone-200 bg-stone-50 opacity-70" : "border-stone-200 bg-white"}`}>
            <div className="text-[15px] leading-9 text-stone-700">
              {has ? (<>
                Each{" "}
                <select className={select} aria-label="Records" value={path}
                  onChange={(ev) => edit(i, { path: ev.target.value, keys: [], label: "" })}>
                  {!m.paths.some((s) => s.path === path) && <option value={path}>{m.loading ? "reading the files…" : path || "row"}</option>}
                  {m.paths.map((s) => <option key={s.path} value={s.path}>{recordLabel(s).replace(/^each /, "")}</option>)}
                </select>
                {" "}in {files.length === 1 ? "this file" : `these ${files.length} files`}
                {m.records ? <span className="text-stone-400"> ({m.records.toLocaleString()} records)</span> : null}
                {" "}is a{" "}
              </>) : (<>
                Records in {filePicker(select, "choose a file…")} are each a{" "}
              </>)}
              <input className={`${select} w-48`} aria-label="Type name" value={e.type} placeholder="Customer"
                onChange={(ev) => edit(i, { type: ev.target.value })} />
              {has && (<>
                <br />
                identified by{" "}
                <select className={select} aria-label="Identifier" value={key}
                  onChange={(ev) => edit(i, { keys: ev.target.value ? [ev.target.value] : [], kind: ev.target.value ? "entity" : "event" })}>
                  <option value="">nothing - these are events, not things</option>
                  {key && !ids.some((f) => f.name === key) && (
                    <option value={key}>{key}{m.loading ? "" : " (not unique or not filled in every file)"}</option>)}
                  {ids.map((f) => <option key={f.name} value={f.name}>{f.name} - unique in every file</option>)}
                </select>
                {key && (<>
                  {" "}and called by{" "}
                  <select className={select} aria-label="Named by" value={e.label || ""}
                    onChange={(ev) => edit(i, { label: ev.target.value })}>
                    <option value="">its identifier</option>
                    {e.label && e.label !== key && !names.some((f) => f.name === e.label) && <option value={e.label}>{e.label}</option>}
                    {names.filter((f) => f.name !== key).map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}
                  </select>
                </>)}
              </>)}
            </div>

            {has && (
              <div className="flex flex-wrap items-center gap-1.5 text-xs">
                {(open ? files : files.slice(0, FILES_SHOWN)).map((f) => (
                  <span key={f.id} className="inline-flex items-center gap-1 rounded-full border border-stone-200 bg-stone-50 px-2 py-0.5 text-stone-600">
                    {f.name || f.id.slice(0, 8)}
                    <button aria-label={`Stop reading ${f.name}`} className="text-stone-400 hover:text-red-600"
                      onClick={() => edit(i, { assets: files.filter((x) => x.id !== f.id) })}><X size={11} /></button>
                  </span>
                ))}
                {files.length > FILES_SHOWN && (
                  <button className="text-primary hover:underline" onClick={() => setShowAll((x) => ({ ...x, [i]: !open }))}>
                    {open ? "show fewer" : `+${files.length - FILES_SHOWN} more`}
                  </button>
                )}
                {filePicker("rounded-md border border-dashed border-stone-300 bg-white px-1.5 py-0.5 text-xs text-stone-500", "+ add a file")}
              </div>
            )}

            {has && (
              <div className="flex flex-wrap items-center gap-4 text-sm">
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input type="checkbox" checked={!ignored} onChange={(ev) => edit(i, { status: ev.target.checked ? "accepted" : "rejected" })} />
                  Use this
                </label>
                <label className={`flex items-center gap-1.5 ${key ? "cursor-pointer" : "opacity-50"}`}
                  title={key ? "One graph node per record, linked to the records it refers to" : "Needs an identifier"}>
                  <input type="checkbox" disabled={!key} checked={!!key && e.materialize}
                    onChange={(ev) => edit(i, { materialize: ev.target.checked })} />
                  Add its records to the knowledge graph
                </label>
                {entities.length > 1 && (
                  <button className="ml-auto text-xs text-red-600 hover:underline flex items-center gap-1"
                    onClick={() => { setEntities((es) => es.filter((_, j) => j !== i)); setDirty(true); }}>
                    <Trash2 size={12} /> Remove
                  </button>
                )}
              </div>
            )}

            {m.errors.length > 0 && <p className="text-sm text-red-700">Could not be read - {m.errors.slice(0, 3).join("; ")}</p>}
            {m.rows.length > 0 && (
              <div className="overflow-x-auto rounded-lg border border-stone-100">
                <table className="min-w-full text-xs">
                  <thead className="bg-stone-50">
                    <tr>{cols.map((c) => (
                      <th key={c} className="px-2 py-1 text-left font-medium text-stone-500 whitespace-nowrap">
                        {c}{c === key ? " · identifier" : c === e.label ? " · name" : ""}
                      </th>))}</tr>
                  </thead>
                  <tbody>
                    {m.rows.map((r, k) => (
                      <tr key={k} className="border-t border-stone-100">
                        {cols.map((c) => <td key={c} className="px-2 py-1 whitespace-nowrap max-w-[260px] truncate">{r[c] == null ? "" : String(r[c])}</td>)}
                      </tr>))}
                  </tbody>
                </table>
              </div>
            )}

            {e.type && m.fields.length > 0 && (
              <div className="space-y-1">
                {myRels.map((r, k) => (
                  <div key={k} className="flex items-center gap-2 text-sm text-stone-700">
                    <Link2 size={13} className="text-stone-400" />
                    Each <b>{r.from_type}</b> refers to a <b>{r.to_type}</b> through <code className="text-xs">{r.from_field}</code>
                    {r.origin === "inferred" && <span className="text-xs text-stone-400">({Math.round(r.coverage * 100)}% match)</span>}
                    <button aria-label="Remove link" className="text-stone-400 hover:text-red-600"
                      onClick={() => { setRels((rs) => rs.filter((x) => x !== r)); setDirty(true); }}><X size={13} /></button>
                  </div>
                ))}
                <AddLink fields={m.fields.map((f) => f.name).filter((f) => f !== key)}
                  types={otherTypes.filter((t) => t.name !== e.type)}
                  onAdd={(field, target) => {
                    const t = otherTypes.find((x) => x.name === target);
                    if (!t) return;
                    setRels((rs) => [...rs, { name: target.toUpperCase().replace(/[^A-Z0-9]+/g, "_"), from_type: e.type,
                      from_field: field, to_type: target, to_field: t.keys[0], origin: "declared", coverage: 0, status: "accepted" }]);
                    setDirty(true);
                  }} />
              </div>
            )}
          </div>
        );
      })}

      <button className="text-sm text-primary hover:underline flex items-center gap-1"
        onClick={() => { setEntities((es) => [...es, { type: "", keys: [], kind: "entity", path: "", assets: [], properties: [],
          materialize: false, status: "accepted", confidence: 1, rows: 0 }]); setDirty(true); }}>
        <Plus size={14} /> Describe another kind of record
      </button>
      {busy && <Loader2 className="animate-spin text-stone-400" size={14} />}
    </div>
  );
}

/** "Each … refers to a [type] through [field]" - a link the data did not show, declared. */
function AddLink({ fields, types, onAdd }: {
  fields: string[]; types: { name: string }[]; onAdd: (field: string, target: string) => void;
}) {
  const [field, setField] = useState("");
  const [target, setTarget] = useState("");
  if (!fields.length || !types.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-sm text-stone-500">
      <Plus size={13} /> Refers to a
      <select className={select} value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Linked type">
        <option value="">type…</option>
        {types.map((t) => <option key={t.name}>{t.name}</option>)}
      </select>
      through
      <select className={select} value={field} onChange={(e) => setField(e.target.value)} aria-label="Field that refers">
        <option value="">field…</option>
        {fields.map((f) => <option key={f}>{f}</option>)}
      </select>
      <button className="text-primary disabled:text-stone-300" disabled={!field || !target}
        onClick={() => { onAdd(field, target); setField(""); setTarget(""); }}>Add link</button>
    </div>
  );
}
