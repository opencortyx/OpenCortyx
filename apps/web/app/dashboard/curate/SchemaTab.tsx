"use client";
import { useEffect, useState, useCallback, useRef } from "react";
import Link from "next/link";
import { contextV2Api } from "@/lib/api/contextV2";
import {
  Plus, Trash2, Pencil, Database, Link2, X, Save,
  RefreshCw, Search,
} from "lucide-react";
import Button from "@/components/ui/Button";

interface Attr { name: string; type?: string }
interface TypeRow {
  name: string; kind: string; description: string; status: string; source: string;
  domain?: string | null; attributes?: Attr[];
  allowed_source_types?: string[] | null; allowed_target_types?: string[] | null;
}
interface Instance { id: string; label: string; type: string; properties?: Record<string, any> }
interface GEdge { id?: string; source: string; target: string; label: string; relation?: string }
type Field = { name: string; type?: string; value: string };
// records the graph builds itself (ontology scaffold / date-compaction hubs) are not "things the
// user created by hand" — the curated lists below the Add forms show hand-made records only.
const _SCAFFOLD = (n: Instance) => !!(n.properties?.__concept || n.properties?.__template);

const PALETTE = ["#0d9488", "#10b981", "#6366f1", "#f59e0b", "#0ea5e9", "#8b5cf6", "#ec4899", "#ef4444", "#84cc16", "#f97316", "#14b8a6", "#64748b"];
const colorFor = (() => { const m: Record<string, string> = {}; let i = 0; return (t: string) => (m[t] ??= PALETTE[i++ % PALETTE.length]); })();
const inputFor = (t?: string) => (["int", "float", "number"].includes(t || "") ? "number" : t === "date" ? "date" : t === "datetime" ? "datetime-local" : "text");

export default function SchemaTab({ flash }: { flash: (t: string, ok?: boolean) => void }) {
  const [types, setTypes] = useState<TypeRow[]>([]);
  const [domains, setDomains] = useState<string[]>([]);
  const [instances, setInstances] = useState<Instance[]>([]);
  const [edges, setEdges] = useState<GEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState<"node" | "rel">("node");
  const [typeQ, setTypeQ] = useState("");           // search across the created nodes & relationships lists
  const [draft, setDraft] = useState<any>(null);   // type-definition editor (relationship edit)

  // node-instance form
  const [nType, setNType] = useState("");
  const [nLabel, setNLabel] = useState("");
  const [nDesc, setNDesc] = useState("");
  const [nFields, setNFields] = useState<Field[]>([]);
  const [busy, setBusy] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);   // editing an existing graph node
  // relationship-instance form
  const [relSrc, setRelSrc] = useState<Instance | null>(null);
  const [relTgt, setRelTgt] = useState<Instance | null>(null);
  const [relType, setRelType] = useState("RELATES_TO");

  const nodeTypes = types.filter((t) => t.kind === "node");
  const edgeTypes = types.filter((t) => t.kind === "edge");
  const isOn = (t: TypeRow) => t.status !== "disabled";
  // "Add node / relationship" pickers only offer types that are switched ON (you can still type a
  // custom one). Switching a type off up-front keeps these lists focused.
  const activeNodeTypes = nodeTypes.filter(isOn);
  const activeEdgeTypes = edgeTypes.filter(isOn);

  const q = typeQ.trim().toLowerCase();
  const labelOf = (id: string) => instances.find((n) => n.id === id)?.label || String(id).slice(0, 8);
  const NOISE_NODES = new Set(["Date"]);            // per-day compaction hubs — infra, not content
  const AUTO_EDGES = new Set(["CREATED_ON", "MODIFIED_ON"]);   // links to those Date hubs — noise
  const byNewest = (a: Instance, b: Instance) =>
    String(b.properties?.created_date || "").localeCompare(String(a.properties?.created_date || ""));
  const nodeRecords = instances.filter((n) => !NOISE_NODES.has(n.type))
    .filter((n) => !q || (n.label || "").toLowerCase().includes(q) || (n.type || "").toLowerCase().includes(q))
    .sort(byNewest);
  const relRecords = edges
    .filter((e) => !AUTO_EDGES.has(e.label))
    .filter((e) => !q || (e.label || "").toLowerCase().includes(q)
      || labelOf(e.source).toLowerCase().includes(q) || labelOf(e.target).toLowerCase().includes(q))
    .slice().reverse();

  const load = useCallback(async () => {
    try {
      const d = await contextV2Api.ontology();
      setTypes(d.types || []);
      const ds = [...(d.person_domains || []), ...(d.org_domains || [])];
      setDomains(Array.from(new Set(ds)));
    } catch { /* noop */ }
  }, []);
  const loadInstances = useCallback(async () => {
    try {
      const g = await contextV2Api.graph(2000);
      setInstances(g.nodes || []);
      setEdges(g.edges || []);
    } catch { /* noop */ }
  }, []);
  useEffect(() => { (async () => { setLoading(true); await Promise.all([load(), loadInstances()]); setLoading(false); })(); }, [load, loadInstances]);

  // Type combobox: pick an existing node type (→ seed its attribute fields) OR type your own (custom).
  const chooseType = (name: string) => {
    setEditingId(null);
    setNType(name);
    const t = nodeTypes.find((x) => x.name === name);
    setNFields(t ? (t.attributes || []).map((a) => ({ name: a.name, type: a.type, value: "" })) : []);
  };

  const _INTERNAL = new Set(["description", "attributes", "domain", "version", "created_date", "modified_date"]);
  const _inferType = (v: any) => (typeof v === "number" ? (Number.isInteger(v) ? "int" : "float") : typeof v === "boolean" ? "bool" : "string");

  // Edit an existing graph node IN THE SAME create form — pre-fill its type, name, description and ALL
  // attributes (the type's schema attrs + the node's own props) with their CURRENT values.
  const editInstance = (n: Instance) => {
    setCreating("node");
    setEditingId(n.id);
    setNType(n.type);
    setNLabel(n.label || "");
    const props = n.properties || {};
    setNDesc(typeof props.description === "string" ? props.description : "");
    const fields: Field[] = [];
    const seen = new Set<string>();
    for (const a of (nodeTypes.find((t) => t.name === n.type)?.attributes || [])) {
      const v = props[a.name];
      fields.push({ name: a.name, type: a.type, value: v == null ? "" : String(v) });
      seen.add(a.name);
    }
    for (const [k, v] of Object.entries(props)) {
      if (seen.has(k) || k.startsWith("__") || _INTERNAL.has(k)) continue;
      fields.push({ name: k, type: _inferType(v), value: typeof v === "boolean" ? String(v) : String(v ?? "") });
    }
    setNFields(fields);
    if (typeof window !== "undefined") window.scrollTo({ top: 0, behavior: "smooth" });
  };
  const cancelEdit = () => { setEditingId(null); setNType(""); setNLabel(""); setNDesc(""); setNFields([]); };

  // Open a node TYPE from the catalog in this form for value-editing: load its concept node (or the
  // first node of that type) with current values; if none exists yet, prefill the type's attributes.
  const editNodeType = (t: TypeRow) => {
    const inst = instances.find((n) => n.type === t.name && n.properties?.__concept) || instances.find((n) => n.type === t.name);
    if (inst) { editInstance(inst); return; }
    setCreating("node"); setEditingId(null); setNType(t.name); setNLabel(t.name); setNDesc(t.description || "");
    setNFields((t.attributes || []).map((a) => ({ name: a.name, type: a.type, value: "" })));
    if (typeof window !== "undefined") window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const createNodeInstance = async () => {
    if (!nType || !nLabel.trim()) { flash("Pick a type and enter a name", false); return; }
    const props: Record<string, any> = {};
    if (nDesc.trim()) props.description = nDesc.trim();
    for (const f of nFields) {
      if (!f.name.trim() || f.value === "") continue;
      const t = f.type;
      props[f.name] = (t === "int" || t === "float" || t === "number") ? Number(f.value)
        : t === "bool" ? f.value === "true"
        : f.value;
    }
    // keep the type's attribute schema (name · type) in sync with what's shown — so adding/renaming an
    // attribute here also updates the type definition. Values go onto the node instance.
    const fields = nFields.filter((f) => f.name.trim()).map((f) => ({ name: f.name.trim(), type: f.type || "string" }));
    setBusy(true);
    try {
      await contextV2Api.registerType({ name: nType, kind: "node", description: "", fields });
      if (editingId) {
        await contextV2Api.updateNode(editingId, { type: nType, label: nLabel.trim(), properties: props });
        flash(`Updated “${nLabel.trim()}”`);
        cancelEdit();
      } else {
        await contextV2Api.createNode(nType, nLabel.trim(), props);
        flash(`Created ${nType} “${nLabel.trim()}” — now on the graph`);
        setNLabel(""); setNDesc(""); setNFields((fs) => fs.map((f) => ({ ...f, value: "" })));
      }
      await load(); await loadInstances();
    } catch (e: any) { flash(e?.response?.data?.detail || "Save failed", false); }
    finally { setBusy(false); }
  };

  const createRelInstance = async () => {
    if (!relSrc || !relTgt) { flash("Pick a source and target node", false); return; }
    setBusy(true);
    try {
      await contextV2Api.createEdge(relSrc.id, relTgt.id, relType);
      flash(`Linked ${relSrc.label} —${relType}→ ${relTgt.label}`);
      setRelSrc(null); setRelTgt(null);
      await Promise.all([load(), loadInstances()]);
    } catch (e: any) { flash(e?.response?.data?.detail || "Link failed", false); }
    finally { setBusy(false); }
  };

  const delInstance = async (n: Instance) => {
    if (!confirm(`Delete node “${n.label}” from the graph?`)) return;
    try { await contextV2Api.deleteNode(n.id); await loadInstances(); flash(`Deleted ${n.label}`); }
    catch { flash("Delete failed", false); }
  };
  const delEdge = async (e: GEdge) => {
    if (!e.id) return;
    if (!confirm(`Remove the relationship ${labelOf(e.source)} —${e.label}→ ${labelOf(e.target)}?`)) return;
    try { await contextV2Api.deleteEdge(e.id); await loadInstances(); flash("Relationship removed"); }
    catch { flash("Delete failed", false); }
  };

  // ── type-definition CRUD (secondary) ──
  const saveType = async () => {
    if (!draft?.name?.trim()) return;
    const spec: any = { name: draft.name.trim(), kind: draft.kind, description: draft.description, domain: draft.domain };
    if (draft.kind === "edge") { spec.allowed_source_types = draft.sources.split(",").map((s: string) => s.trim()).filter(Boolean); spec.allowed_target_types = draft.targets.split(",").map((s: string) => s.trim()).filter(Boolean); }
    else spec.fields = (draft.attrs || []).filter((a: Attr) => a.name.trim());
    try { await contextV2Api.registerType(spec); setDraft(null); await load(); flash(`${draft.editing ? "Updated" : "Added"} ${draft.kind} type “${spec.name}”`); }
    catch (e: any) { flash(e?.response?.data?.detail || "Save failed", false); }
  };
  const editTypeDraft = (t: TypeRow) => setDraft({
    editing: true, kind: t.kind, name: t.name, description: t.description || "",
    domain: t.domain || domains[0] || "", attrs: t.attributes || [],
    sources: (t.allowed_source_types || []).join(", "), targets: (t.allowed_target_types || []).join(", "),
  });
  const delType = async (t: TypeRow) => {
    if (t.source === "system") { flash("Framework types can't be deleted", false); return; }
    if (!confirm(`Delete the ${t.kind} TYPE “${t.name}” from the schema?`)) return;
    try { const r = await contextV2Api.deleteType(t.name, t.kind); if (r?.status === "deleted") { await load(); flash(`Deleted type ${t.name}`); } else flash("Could not delete", false); }
    catch { flash("Delete failed", false); }
  };

  if (loading) return <div className="text-center py-12 text-stone-400 text-sm">Loading schema…</div>;

  return (
    <div className="space-y-5">
      <p className="text-sm text-stone-500">
        Build your graph by hand: pick a type, fill the values, and the node/relationship is created for real and shows up on the
        {" "}<Link href="/dashboard/graph" className="text-teal-600 font-medium hover:underline">Lineage Graph</Link>.
      </p>

      {/* ── Create panel ── */}
      <div className="bg-white border border-stone-200 rounded-xl shadow-sm">
        <div className="flex border-b border-stone-100">
          {([["node", "Add node", Database], ["rel", "Add relationship", Link2]] as const).map(([k, label, Icon]) => (
            <button key={k} onClick={() => setCreating(k)}
              className={`px-4 py-2.5 text-sm font-semibold flex items-center gap-1.5 border-b-2 -mb-px ${creating === k ? "border-teal-500 text-teal-700" : "border-transparent text-stone-500 hover:text-stone-700"}`}>
              <Icon size={15} /> {label}
            </button>
          ))}
        </div>

        {creating === "node" ? (
          <div className="p-4 space-y-3">
            {editingId && (
              <div className="flex items-center justify-between gap-2 text-xs bg-teal-50 border border-teal-100 rounded-lg px-3 py-2 text-teal-800">
                <span>Editing an existing node — its attributes and current values are loaded below.</span>
                <button onClick={cancelEdit} className="font-semibold hover:underline shrink-0">Cancel</button>
              </div>
            )}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label className="text-xs font-bold text-stone-500">Type</label>
                <input list="node-types" value={nType} onChange={(e) => chooseType(e.target.value)}
                  placeholder="Pick a type or type your own…" className="w-full border border-stone-200 rounded-lg px-3 py-2 text-sm bg-white mt-1" />
                <datalist id="node-types">{activeNodeTypes.map((t) => <option key={t.name} value={t.name}>{t.domain ? `${t.domain}` : "custom"}</option>)}</datalist>
              </div>
              <div>
                <label className="text-xs font-bold text-stone-500">Name / label</label>
                <input value={nLabel} onChange={(e) => setNLabel(e.target.value)} placeholder="e.g. John Doe, Tesla Model 3" className="w-full border border-stone-200 rounded-lg px-3 py-2 text-sm mt-1" />
              </div>
            </div>
            <div>
              <label className="text-xs font-bold text-stone-500">Description</label>
              <textarea value={nDesc} onChange={(e) => setNDesc(e.target.value)} rows={2}
                placeholder="What this record is, in plain language…"
                className="w-full border border-stone-200 rounded-lg px-3 py-2 text-sm mt-1 resize-none" />
            </div>
            {nType && (
              <div>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-bold text-stone-500">Attribute values</span>
                  <button onClick={() => setNFields([...nFields, { name: "", type: "string", value: "" }])} className="text-[11px] text-teal-600 font-semibold">+ property</button>
                </div>
                {nFields.length === 0 ? <p className="text-[11px] text-stone-400 italic">This type has no attributes — add a property or just name the node.</p> : (
                  <div className="space-y-1.5">
                    {nFields.map((f, i) => (
                      <div key={i} className="flex items-center gap-1.5">
                        <input value={f.name} onChange={(e) => { const a = [...nFields]; a[i] = { ...a[i], name: e.target.value }; setNFields(a); }} placeholder="attribute name" className="w-36 border border-stone-200 rounded px-2 py-1 text-xs font-mono" />
                        <select value={f.type || "string"} onChange={(e) => { const a = [...nFields]; a[i] = { ...a[i], type: e.target.value, value: "" }; setNFields(a); }} className="w-24 border border-stone-200 rounded px-2 py-1 text-xs bg-white" title="data type">
                          {["string", "text", "int", "float", "bool", "date", "datetime"].map((t) => <option key={t} value={t}>{t}</option>)}
                        </select>
                        {f.type === "bool" ? (
                          <select value={f.value} onChange={(e) => { const a = [...nFields]; a[i] = { ...a[i], value: e.target.value }; setNFields(a); }} className="flex-1 border border-stone-200 rounded px-2 py-1 text-xs bg-white">
                            <option value="">—</option><option value="true">true</option><option value="false">false</option>
                          </select>
                        ) : (
                          <input type={inputFor(f.type)} value={f.value} onChange={(e) => { const a = [...nFields]; a[i] = { ...a[i], value: e.target.value }; setNFields(a); }} placeholder="value" className="flex-1 border border-stone-200 rounded px-2 py-1 text-xs" />
                        )}
                        <button onClick={() => setNFields(nFields.filter((_, j) => j !== i))} className="text-stone-300 hover:text-red-500"><Trash2 size={12} /></button>
                      </div>
                    ))}
                  </div>
                )}
                <p className="text-[10px] text-stone-400 mt-1.5">Created &amp; Modified dates and version are added automatically.</p>
              </div>
            )}
            <div className="flex justify-end items-center gap-2">
              {editingId && <button onClick={cancelEdit} className="px-3 py-2 text-sm font-semibold text-stone-500 hover:text-stone-800">Cancel</button>}
              <button onClick={createNodeInstance} disabled={busy || !nType || !nLabel.trim()} className="flex items-center gap-1.5 px-4 py-2 bg-teal-600 text-white text-sm font-semibold rounded-lg hover:bg-teal-700 disabled:opacity-50">
                {editingId ? <><Save size={15} /> Save changes</> : <><Plus size={15} /> Create node</>}
              </button>
            </div>
          </div>
        ) : (
          <div className="p-4 space-y-3">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
              <NodePicker label="Source node" value={relSrc} onChange={setRelSrc} />
              <div>
                <label className="text-xs font-bold text-stone-500">Relationship</label>
                <input list="rel-types" value={relType} onChange={(e) => setRelType(e.target.value.toUpperCase().replace(/\s+/g, "_"))}
                  placeholder="Pick or type a custom one…" className="w-full border border-stone-200 rounded-lg px-3 py-2 text-sm bg-white mt-1 font-mono" />
                <datalist id="rel-types">{activeEdgeTypes.map((t) => <option key={t.name} value={t.name} />)}</datalist>
              </div>
              <NodePicker label="Target node" value={relTgt} onChange={setRelTgt} />
            </div>
            <div className="flex justify-end">
              <Button variant="primary" icon={Link2} disabled={busy || !relSrc || !relTgt} onClick={createRelInstance}>Create relationship</Button>
            </div>
          </div>
        )}
      </div>

      {/* ── Created list — exactly the glossary pattern: the form is above, and what it creates lands in
             the list right below. Follows the active tab: node tab → your Nodes; relationship tab →
             your Relationships. Enable/disable + graph visibility live on the Nodes & Relationships page. ── */}
      <div className="border border-stone-200 rounded-xl bg-white">
        <div className="px-5 py-3 border-b border-stone-100 flex flex-wrap items-center gap-3">
          <h3 className="text-sm font-bold text-stone-700 flex items-center gap-1.5">
            {creating === "node" ? <><Database size={15} className="text-teal-600" /> Nodes</> : <><Link2 size={15} className="text-teal-600" /> Relationships</>}
            <span className="text-[11px] font-normal text-stone-400">{(creating === "node" ? nodeRecords : relRecords).length}</span>
          </h3>
          <div className="relative">
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-stone-400" />
            <input value={typeQ} onChange={(e) => setTypeQ(e.target.value)} placeholder="Search…"
              className="w-40 pl-8 pr-2 py-1.5 text-xs border border-stone-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-teal-500/20" />
          </div>
          <div className="ml-auto flex items-center gap-2">
            <Link href="/dashboard/schema" className="text-[11px] font-semibold text-teal-600 hover:underline">Manage &amp; toggle →</Link>
            <button onClick={() => { load(); loadInstances(); flash("Refreshed"); }} title="Refresh" className="text-stone-400 hover:text-teal-600"><RefreshCw size={13} /></button>
          </div>
        </div>

        {creating === "rel" && draft && <div className="px-4 pt-4"><TypeEditor draft={draft} setDraft={setDraft} domains={domains} onSave={saveType} onClose={() => setDraft(null)} /></div>}

        {creating === "node" ? (
          nodeRecords.length === 0 ? <p className="px-5 py-6 text-sm text-stone-400 text-center">No nodes {typeQ ? "match your search" : "yet — create one above and it lands here"}.</p> : (
            <div className="divide-y divide-stone-100 max-h-[28rem] overflow-y-auto">
              {nodeRecords.map((n) => (
                <div key={n.id} className="group px-5 py-2.5 flex items-center justify-between gap-3">
                  <div className="min-w-0 flex items-center gap-2">
                    <span className="text-sm font-semibold text-stone-800 truncate max-w-[280px]">{n.label || "(unnamed)"}</span>
                    <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-stone-100 text-stone-500 shrink-0">{n.type}</span>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <button onClick={() => editInstance(n)} title="Edit" className="p-1 text-stone-300 group-hover:text-teal-600 rounded"><Pencil size={13} /></button>
                    <button onClick={() => delInstance(n)} title="Delete" className="p-1 text-stone-300 group-hover:text-red-500 rounded"><Trash2 size={13} /></button>
                  </div>
                </div>
              ))}
            </div>
          )
        ) : (
          relRecords.length === 0 ? <p className="px-5 py-6 text-sm text-stone-400 text-center">No relationships {typeQ ? "match your search" : "yet — link two nodes above and it lands here"}.</p> : (
            <div className="divide-y divide-stone-100 max-h-[28rem] overflow-y-auto">
              {relRecords.map((e, i) => (
                <div key={e.id || i} className="group px-5 py-2.5 flex items-center justify-between gap-3">
                  <div className="min-w-0 flex items-center gap-2 text-sm truncate">
                    <span className="font-medium text-stone-700 truncate max-w-[160px]">{labelOf(e.source)}</span>
                    <span className="text-[10px] font-mono font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 shrink-0">{e.label}</span>
                    <span className="font-medium text-stone-700 truncate max-w-[160px]">{labelOf(e.target)}</span>
                  </div>
                  {e.id && <button onClick={() => delEdge(e)} title="Remove" className="p-1 text-stone-300 group-hover:text-red-500 rounded shrink-0"><Trash2 size={13} /></button>}
                </div>
              ))}
            </div>
          )
        )}
      </div>
    </div>
  );
}

/* Async node search-picker for building relationships between real instances. */
function NodePicker({ label, value, onChange }: { label: string; value: Instance | null; onChange: (n: Instance | null) => void }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [hits, setHits] = useState<Instance[]>([]);
  const timer = useRef<any>(null);
  useEffect(() => {
    if (!q.trim()) { setHits([]); return; }
    clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      try { const r: any = await contextV2Api.search(q.trim()); setHits((r.results || []).slice(0, 8)); } catch { setHits([]); }
    }, 250);
    return () => clearTimeout(timer.current);
  }, [q]);
  return (
    <div className="relative">
      <label className="text-xs font-bold text-stone-500">{label}</label>
      {value ? (
        <div className="flex items-center gap-2 border border-stone-200 rounded-lg px-3 py-2 text-sm mt-1 bg-stone-50">
          <span className="truncate flex-1">{value.label} <span className="text-stone-400 text-xs">· {value.type}</span></span>
          <button onClick={() => onChange(null)} className="text-stone-400 hover:text-stone-700"><X size={14} /></button>
        </div>
      ) : (
        <>
          <input value={q} onChange={(e) => { setQ(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)} placeholder="Search a node…" className="w-full border border-stone-200 rounded-lg px-3 py-2 text-sm mt-1" />
          {open && hits.length > 0 && (
            <ul className="absolute z-10 mt-1 w-full bg-white border border-stone-200 rounded-lg shadow-lg max-h-48 overflow-y-auto">
              {hits.map((h) => (
                <li key={h.id}><button onClick={() => { onChange(h); setQ(""); setOpen(false); }} className="w-full text-left px-3 py-2 text-sm hover:bg-stone-50 truncate">{h.label} <span className="text-stone-400 text-xs">· {h.type}</span></button></li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

/* Type-definition editor (kept for growing the schema). */
function TypeEditor({ draft, setDraft, domains, onSave, onClose }: any) {
  const set = (patch: any) => setDraft({ ...draft, ...patch });
  return (
    <div className="bg-stone-50 border border-stone-200 rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h4 className="font-bold text-stone-800 text-sm">{draft.editing ? "Edit" : "New"} {draft.kind === "edge" ? "relationship" : draft.kind} type</h4>
        <button onClick={onClose} className="text-stone-400 hover:text-stone-700"><X size={15} /></button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        <input value={draft.name} disabled={draft.editing} onChange={(e) => set({ name: e.target.value })} placeholder={draft.kind === "node" ? "Type name (e.g. Vehicle)" : "REL_NAME"} className="border border-stone-200 rounded-lg px-3 py-2 text-sm disabled:bg-stone-100 disabled:text-stone-400" />
        <select value={draft.domain} onChange={(e) => set({ domain: e.target.value })} className="border border-stone-200 rounded-lg px-3 py-2 text-sm bg-white">{domains.map((d: string) => <option key={d} value={d}>{d}</option>)}</select>
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
      <div className="flex justify-end"><Button variant="primary" size="sm" icon={Save} disabled={!draft.name.trim()} onClick={onSave}>{draft.editing ? "Save" : "Add"} type</Button></div>
    </div>
  );
}
