"use client";

import { useCallback, useEffect, useState } from "react";
import { contextV2Api, type TypeResolution } from "@/lib/api/contextV2";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import {
  Network, Database, Link2, Power, Pencil, Trash2, RefreshCw, Search, Save, X,
  CheckCircle2, AlertCircle, GitMerge,
} from "lucide-react";

interface Attr { name: string; type?: string }
interface TypeRow {
  name: string; kind: string; description: string; status: string; source: string;
  domain?: string | null; attributes?: Attr[];
  allowed_source_types?: string[] | null; allowed_target_types?: string[] | null;
  resolved?: boolean;
}

export default function SchemaPage() {
  const [types, setTypes] = useState<TypeRow[]>([]);
  const [domains, setDomains] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [draft, setDraft] = useState<any>(null);
  const [toast, setToast] = useState<{ text: string; ok: boolean } | null>(null);
  const flash = (text: string, ok = true) => { setToast({ text, ok }); setTimeout(() => setToast(null), 3000); };

  const load = useCallback(async () => {
    try {
      const d: any = await contextV2Api.ontology();
      setTypes(d.types || []);
      setDomains(Array.from(new Set([...(d.person_domains || []), ...(d.org_domains || [])])));
    } catch { /* noop */ }
  }, []);
  useEffect(() => { (async () => { setLoading(true); await load(); setLoading(false); })(); }, [load]);

  const isOn = (t: TypeRow) => t.status !== "disabled";
  // Framework internals (source="system": Chunk, Asset, …) stay out — this is the authored ontology.
  const curated = types.filter((t) => t.source !== "system");
  const matches = (t: TypeRow) => !q.trim() || t.name.toLowerCase().includes(q.trim().toLowerCase());
  const nodeList = curated.filter((t) => (t.kind === "node" || t.kind === "entity") && matches(t)).sort((a, b) => a.name.localeCompare(b.name));
  const relList = curated.filter((t) => t.kind === "edge" && matches(t)).sort((a, b) => a.name.localeCompare(b.name));
  const onCount = curated.filter(isOn).length;

  const toggleType = async (t: TypeRow) => {
    const next = t.status === "active" ? "disabled" : "active";
    setTypes((ts) => ts.map((x) => (x.name === t.name && x.kind === t.kind ? { ...x, status: next } : x)));
    try { await contextV2Api.updateTypeStatus(t.name, t.kind, next); flash(`${t.name} ${next === "active" ? "on" : "off"} — graphs & extraction updated`); }
    catch { setTypes((ts) => ts.map((x) => (x.name === t.name && x.kind === t.kind ? { ...x, status: t.status } : x))); flash("Could not change status", false); }
  };
  const setBulk = async (rows: TypeRow[], next: "active" | "disabled") => {
    const keys = new Set(rows.map((r) => r.kind + " " + r.name));
    setTypes((ts) => ts.map((x) => (keys.has(x.kind + " " + x.name) ? { ...x, status: next } : x)));
    try { await Promise.all(rows.map((r) => contextV2Api.updateTypeStatus(r.name, r.kind, next))); flash(`${rows.length} type(s) ${next === "active" ? "on" : "off"}`); }
    catch { await load(); flash("Some types could not be updated", false); }
  };
  const delType = async (t: TypeRow) => {
    if (t.source === "system") { flash("Framework types can't be deleted", false); return; }
    if (!confirm(`Delete the ${t.kind === "edge" ? "relationship" : "node"} type “${t.name}”?`)) return;
    try { const r: any = await contextV2Api.deleteType(t.name, t.kind); if (r?.status === "deleted") { await load(); flash(`Deleted ${t.name}`); } else flash("Could not delete", false); }
    catch { flash("Delete failed", false); }
  };
  const editDraft = (t: TypeRow) => setDraft({
    editing: true, kind: t.kind === "edge" ? "edge" : "node", name: t.name,
    description: t.description || "", domain: t.domain || domains[0] || "",
    attrs: t.attributes || [], sources: (t.allowed_source_types || []).join(", "),
    targets: (t.allowed_target_types || []).join(", "),
  });
  const saveType = async () => {
    if (!draft?.name?.trim()) return;
    const spec: any = { name: draft.name.trim(), kind: draft.kind, description: draft.description, domain: draft.domain };
    if (draft.kind === "edge") {
      spec.allowed_source_types = draft.sources.split(",").map((s: string) => s.trim()).filter(Boolean);
      spec.allowed_target_types = draft.targets.split(",").map((s: string) => s.trim()).filter(Boolean);
    } else spec.fields = (draft.attrs || []).filter((a: Attr) => a.name.trim());
    try { await contextV2Api.registerType(spec); setDraft(null); await load(); flash(`Saved ${draft.name}`); }
    catch (e: any) { flash(e?.response?.data?.detail || "Save failed", false); }
  };

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      <PageHeader
        title="Nodes & Relationships"
        icon={Network}
        description="The whole ontology in one place — switch any node or relationship on/off (off = hidden from the graph and skipped by extraction), or edit it. No LLM."
      />

      {toast && (
        <div className={`rounded-lg border px-4 py-3 text-sm flex items-center gap-2 ${toast.ok ? "bg-emerald-50 border-emerald-200 text-emerald-700" : "bg-red-50 border-red-200 text-red-700"}`}>
          {toast.ok ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />} {toast.text}
        </div>
      )}

      <div className="border border-stone-200 rounded-xl bg-white">
        <div className="px-4 py-3 border-b border-stone-100 flex flex-wrap items-center gap-3">
          <div className="relative">
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-stone-400" />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search types…"
              className="w-48 pl-8 pr-2 py-1.5 text-xs border border-stone-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-teal-500/20" />
          </div>
          <span className="text-[11px] text-stone-400"><b className="text-emerald-600">{onCount} on</b> / {curated.length}</span>
          <div className="ml-auto flex items-center gap-1.5">
            <button onClick={() => setBulk(curated, "active")} className="text-[11px] font-semibold text-stone-500 hover:text-emerald-600">Enable all</button>
            <span className="text-stone-300">·</span>
            <button onClick={() => setBulk(curated, "disabled")} className="text-[11px] font-semibold text-stone-500 hover:text-red-600">Disable all</button>
            <button onClick={() => { load(); flash("Refreshed"); }} title="Refresh" className="ml-1 text-stone-400 hover:text-teal-600"><RefreshCw size={13} /></button>
          </div>
        </div>

        {loading ? <p className="p-8 text-center text-sm text-stone-400">Loading ontology…</p> : (
          <div className="px-4 py-4 space-y-5">
            {/* No "New node type" / "New relationship type" here. A type is not declared on this
                page — it is declared by a MAPPING, which says what a source becomes, and published
                into the registry from there. Offering a second way to mint a bare type produced
                vocabulary that no mapping emits and nothing ever fills, which then shows up in every
                type picker as a choice that cannot work. This page governs the types that exist:
                review them, describe them, enable or disable them. */}
            {draft && <TypeEditor draft={draft} setDraft={setDraft} domains={domains} onSave={saveType} onClose={() => setDraft(null)}
                                  flash={flash} onResolutionSaved={load} />}

            <div>
              <div className="flex items-center gap-2 mb-1.5">
                <Database size={13} className="text-stone-400" />
                <span className="text-[11px] font-bold uppercase tracking-wider text-stone-500">Nodes</span>
                <span className="text-[11px] text-stone-400">{nodeList.length}</span>
              </div>
              {nodeList.length === 0 ? <p className="text-xs text-stone-400 italic">No node types{q ? " match your search" : " yet"}.</p> : (
                <div className="flex flex-wrap gap-1.5">
                  {nodeList.map((t) => <TypeChip key={t.kind + t.name} t={t} onToggle={() => toggleType(t)} onEdit={() => editDraft(t)} onDelete={() => delType(t)} />)}
                </div>
              )}
            </div>

            <div>
              <div className="flex items-center gap-2 mb-1.5">
                <Link2 size={13} className="text-stone-400" />
                <span className="text-[11px] font-bold uppercase tracking-wider text-stone-500">Relationships</span>
                <span className="text-[11px] text-stone-400">{relList.length}</span>
              </div>
              {relList.length === 0 ? <p className="text-xs text-stone-400 italic">No relationship types{q ? " match your search" : " yet"}.</p> : (
                <div className="flex flex-wrap gap-1.5">
                  {relList.map((t) => <TypeChip key={t.kind + t.name} t={t} edge onToggle={() => toggleType(t)} onEdit={() => editDraft(t)} onDelete={() => delType(t)} />)}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function TypeChip({ t, edge, onToggle, onEdit, onDelete }: { t: TypeRow; edge?: boolean; onToggle: () => void; onEdit: () => void; onDelete: () => void }) {
  const off = t.status === "disabled";
  const base = edge ? "bg-emerald-50 border-emerald-100 text-emerald-700 font-mono" : "bg-stone-50 border-stone-200 text-stone-700";
  return (
    <span className={`group inline-flex items-center gap-1.5 text-[11px] pl-1 pr-2 py-1 rounded border ${off ? "bg-stone-100 border-stone-200 text-stone-400" : base}`}>
      <button onClick={onToggle} aria-pressed={!off} aria-label={`${off ? "Enable" : "Disable"} ${t.name}`}
        title={off ? "Disabled — click to enable (show on the graph)" : "Enabled — click to disable (hide from the graph)"}
        className={`shrink-0 w-[18px] h-[18px] flex items-center justify-center rounded-full transition-colors ${off ? "bg-stone-200 text-stone-400 hover:bg-emerald-100 hover:text-emerald-600" : "bg-emerald-500 text-white hover:bg-emerald-600"}`}>
        <Power size={10} />
      </button>
      <span className={off ? "line-through" : ""}>{t.name}</span>
      {t.resolved && <span title={`${t.name} records are matched across assets (Resolution is set)`}
        className="text-teal-600" aria-label="Resolution set"><GitMerge size={10} aria-hidden="true" /></span>}
      {/* Named, and never fully invisible. These were opacity-0 until HOVER with no accessible name:
          a screen reader announced "button", a keyboard user tabbed onto something they could not
          see, and on a touch screen — which has no hover — they did not exist at all. */}
      <button onClick={onEdit} aria-label={`Edit ${t.name}`} title={`Edit ${t.name}`}
        className="opacity-40 group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-teal-500 rounded text-stone-400 hover:text-teal-600"><Pencil size={10} aria-hidden="true" /></button>
      {t.source !== "system" && <button onClick={onDelete} aria-label={`Delete ${t.name}`} title={`Delete ${t.name}`}
        className="opacity-40 group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-red-500 rounded text-stone-400 hover:text-red-500"><Trash2 size={10} aria-hidden="true" /></button>}
    </span>
  );
}

function TypeEditor({ draft, setDraft, domains, onSave, onClose, flash, onResolutionSaved }: any) {
  const set = (patch: any) => setDraft({ ...draft, ...patch });
  return (
    <div className="bg-stone-50 border border-stone-200 rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="font-bold text-stone-800 text-sm">{draft.editing ? "Edit" : "New"} {draft.kind === "edge" ? "relationship" : "node"} type</h4>
        <button onClick={onClose} className="text-stone-400 hover:text-stone-700"><X size={15} /></button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        <input value={draft.name} disabled={draft.editing} onChange={(e) => set({ name: draft.kind === "edge" ? e.target.value.toUpperCase().replace(/\s+/g, "_") : e.target.value })}
          placeholder={draft.kind === "edge" ? "REL_NAME (e.g. WORKS_FOR)" : "Type name (e.g. Vehicle)"}
          className="border border-stone-200 rounded-lg px-3 py-2 text-sm disabled:bg-stone-100 disabled:text-stone-400" />
        <select value={draft.domain} onChange={(e) => set({ domain: e.target.value })} className="border border-stone-200 rounded-lg px-3 py-2 text-sm bg-white">
          <option value="">— (custom / no domain)</option>
          {domains.map((d: string) => <option key={d} value={d}>{d}</option>)}
        </select>
      </div>
      <input value={draft.description} onChange={(e) => set({ description: e.target.value })} placeholder="Description" className="w-full border border-stone-200 rounded-lg px-3 py-2 text-sm" />
      {draft.kind !== "edge" ? (
        <div>
          <div className="flex items-center justify-between mb-1"><span className="text-xs font-bold text-stone-500">Attributes (name · type)</span><button onClick={() => set({ attrs: [...draft.attrs, { name: "", type: "string" }] })} className="text-[11px] text-teal-600 font-semibold">+ attribute</button></div>
          {(draft.attrs || []).map((a: Attr, i: number) => (
            <div key={i} className="flex gap-2 mb-1.5">
              <input value={a.name} onChange={(e) => { const arr = [...draft.attrs]; arr[i] = { ...arr[i], name: e.target.value }; set({ attrs: arr }); }} placeholder="name" className="flex-1 border border-stone-200 rounded px-2 py-1 text-xs font-mono" />
              <select value={a.type || "string"} onChange={(e) => { const arr = [...draft.attrs]; arr[i] = { ...arr[i], type: e.target.value }; set({ attrs: arr }); }} className="w-28 border border-stone-200 rounded px-2 py-1 text-xs bg-white">
                {["string", "text", "int", "float", "bool", "date", "datetime", "string[]"].map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
              <button onClick={() => set({ attrs: draft.attrs.filter((_: any, j: number) => j !== i) })} className="text-stone-300 hover:text-red-500"><Trash2 size={13} /></button>
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          <input value={draft.sources} onChange={(e) => set({ sources: e.target.value })} placeholder="From types (comma-sep)" className="border border-stone-200 rounded-lg px-3 py-2 text-sm" />
          <input value={draft.targets} onChange={(e) => set({ targets: e.target.value })} placeholder="To types (comma-sep)" className="border border-stone-200 rounded-lg px-3 py-2 text-sm" />
        </div>
      )}
      <div className="flex justify-end"><Button variant="primary" size="sm" icon={Save} disabled={!draft.name.trim()} onClick={onSave}>Save type</Button></div>
      {draft.editing && draft.kind !== "edge" && (
        <ResolutionPanel key={draft.name} name={draft.name} flash={flash} onSaved={onResolutionSaved} />
      )}
    </div>
  );
}

const NO_RESOLUTION: TypeResolution = {
  identify_by: [], compare: [], ignore: [], enabled: true, scorer: "exact", auto: 0.95, review: 0.8,
};

/**
 * When two nodes of this type are the same thing, for any type. A type an extractor creates can also
 * be set in that extractor's Resolution section; a mapping's record types (Lead, Project, ...) are
 * set only here. The choices are the attributes real nodes of the type carry.
 */
function ResolutionPanel({ name, flash, onSaved }: { name: string; flash: (t: string, ok?: boolean) => void; onSaved: () => void }) {
  const [spec, setSpec] = useState<TypeResolution | null>(null);
  const [attrs, setAttrs] = useState<string[]>([]);
  const [busy, setBusy] = useState<"" | "save" | "run">("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    contextV2Api.getTypeResolution(name).then((r) => {
      setSpec({ ...NO_RESOLUTION, ...(r.resolution || {}) });
      setAttrs(r.attributes || []);
      setSaved(!!r.resolution?.identify_by?.length);
    }).catch(() => setSpec({ ...NO_RESOLUTION }));
  }, [name]);

  if (!spec) return <p className="text-xs text-stone-400 border-t border-stone-200 pt-3">Loading resolution…</p>;
  const set = (patch: Partial<TypeResolution>) => { setSpec({ ...spec, ...patch }); setSaved(false); };
  const toggle = (list: string[], v: string) => list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
  // fields already chosen stay offered even when the sample no longer shows them
  const fields = Array.from(new Set([...spec.identify_by, ...attrs]));
  const others = fields.filter((f) => !spec.identify_by.includes(f));

  const save = async () => {
    setBusy("save");
    try {
      await contextV2Api.setTypeResolution(name, spec);
      setSaved(true);
      onSaved();
      flash(spec.identify_by.length ? `${name}: new assets are now matched by ${spec.identify_by.join(" + ")}`
                                    : `${name}: records are no longer matched`);
    } catch (e: any) {
      const d = e?.response?.data?.detail;
      flash(Array.isArray(d) ? d[0]?.msg : d || "Could not save resolution", false);
    } finally { setBusy(""); }
  };
  const runAgain = async () => {
    setBusy("run");
    try {
      const r = await contextV2Api.resolveTypeAgain(name);
      flash(r.assets ? `Re-checking ${r.assets} asset(s) with ${name} records. Results appear in the Review Queue.`
                     : `No assets hold ${name} records yet`);
    } catch { flash("Could not queue the re-check", false); } finally { setBusy(""); }
  };

  return (
    <div className="border-t border-stone-200 pt-3 space-y-3">
      <div>
        <h5 className="text-xs font-bold text-stone-700 flex items-center gap-1.5"><GitMerge size={13} className="text-teal-600" /> Resolution</h5>
        <p className="text-[11.5px] text-stone-500 mt-0.5">
          When are two {name} records the same thing? Pick the fields that identify one. After each asset
          is ingested, its {name} records are matched with the ones you already have: matches are linked,
          missing values added, and different values flagged in the Review Queue. Nothing is overwritten.
        </p>
      </div>

      <div>
        <span className="text-[11px] font-bold text-stone-500">Identified by</span>
        <div className="flex flex-wrap gap-1.5 mt-1">
          {fields.map((f) => {
            const on = spec.identify_by.includes(f);
            return (
              <button key={f} type="button" aria-pressed={on}
                onClick={() => set({ identify_by: toggle(spec.identify_by, f), ignore: spec.ignore.filter((x) => x !== f) })}
                className={`px-2 py-0.5 rounded border text-[11px] font-mono ${on ? "bg-teal-600 border-teal-600 text-white" : "bg-white border-stone-200 text-stone-600 hover:border-teal-400"}`}>
                {f}
              </button>
            );
          })}
          {!fields.length && <span className="text-[11px] text-stone-400">No {name} records yet, so there are no fields to choose from. Ingest some first.</span>}
        </div>
        <p className="text-[11px] text-stone-400 mt-1">
          {spec.identify_by.length
            ? <>Two records are the same when their <b>{spec.identify_by.join(" + ")}</b> {spec.identify_by.length > 1 ? "match together" : "matches"}.</>
            : <>None picked: {name} records are never matched.</>}
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3 text-[11.5px] text-stone-600">
        <label className="flex items-center gap-1.5">Scorer
          <select value={spec.scorer} onChange={(e) => set({ scorer: e.target.value as TypeResolution["scorer"] })}
            className="px-2 py-1 rounded-md border border-stone-200 bg-white">
            <option value="exact">Exact (same values)</option>
            <option value="similar">Similar (close spellings)</option>
          </select>
        </label>
        {spec.scorer === "similar" ? (
          <>
            <label className="flex items-center gap-1.5">Apply automatically at
              <input type="number" min={0} max={1} step={0.05} value={spec.auto}
                onChange={(e) => set({ auto: Number(e.target.value) })} className="w-16 px-2 py-1 rounded-md border border-stone-200" />
            </label>
            <label className="flex items-center gap-1.5">Send for review at
              <input type="number" min={0} max={1} step={0.05} value={spec.review}
                onChange={(e) => set({ review: Number(e.target.value) })} className="w-16 px-2 py-1 rounded-md border border-stone-200" />
            </label>
          </>
        ) : <span className="text-stone-400">An exact match is applied automatically.</span>}
      </div>

      {others.length > 0 && (
        <div>
          <span className="text-[11px] font-bold text-stone-500">Never compare</span>
          <div className="flex flex-wrap gap-1.5 mt-1">
            {others.map((f) => {
              const on = spec.ignore.includes(f);
              return (
                <button key={f} type="button" aria-pressed={on} onClick={() => set({ ignore: toggle(spec.ignore, f) })}
                  className={`px-2 py-0.5 rounded border text-[11px] font-mono ${on ? "bg-stone-600 border-stone-600 text-white line-through" : "bg-white border-stone-200 text-stone-600 hover:border-stone-400"}`}>
                  {f}
                </button>
              );
            })}
          </div>
          <p className="text-[11px] text-stone-400 mt-1">Every other field is compared on a match. Pick fields whose differences do not matter, such as notes or timestamps.</p>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-end gap-2">
        {saved && spec.identify_by.length > 0 && (
          <Button variant="secondary" size="sm" icon={RefreshCw} busy={busy === "run"} disabled={!!busy} onClick={runAgain}>
            Re-check existing data
          </Button>
        )}
        <Button variant="primary" size="sm" icon={Save} busy={busy === "save"} disabled={!!busy || saved} onClick={save}>
          {saved ? "Saved" : "Save resolution"}
        </Button>
      </div>
    </div>
  );
}
