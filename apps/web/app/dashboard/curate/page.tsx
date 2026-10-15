"use client";

import { useEffect, useState } from "react";
import { contextV2Api } from "@/lib/api/contextV2";
import {
  PenSquare, Trash2, Plus, BookOpen, Tags, Loader2,
  CheckCircle2, AlertCircle, Clock, Library, Scale,
  ChevronRight, ChevronDown, Compass, Network,
} from "lucide-react";
import { GlossaryInput, SynonymInput, RuleInput } from "@/lib/api/contextV2";
import PageHeader from "@/components/layout/PageHeader";
import SchemaTab from "./SchemaTab";

type Hit = { id: string; label: string; type: string };
type Tab = "schema" | "resources";

const TABS: { id: Tab; label: string; icon: any }[] = [
  { id: "schema", label: "Schema", icon: Network },
  { id: "resources", label: "Resources", icon: Library },
];

export default function CuratePage() {
  const [tab, setTab] = useState<Tab>("schema");
  const [toast, setToast] = useState<{ text: string; ok: boolean } | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const flash = (text: string, ok = true) => {
    setToast({ text, ok }); setTimeout(() => setToast(null), 3500);
    if (ok) setRefreshKey((k) => k + 1);
  };

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      <PageHeader
        title="Curation"
        icon={PenSquare}
        description="Hand-curate the catalog with no LLM — manage schema, resources, and link assets."
      />

      <div className="inline-flex bg-stone-100 rounded-lg p-1 text-sm">
        {TABS.map((t) => (
          <button key={t.id} onClick={() => setTab(t.id)}
            className={`px-4 py-1.5 rounded-md font-medium flex items-center gap-1.5 ${tab === t.id ? "bg-white shadow-sm text-stone-900" : "text-stone-500 hover:text-stone-700"}`}>
            <t.icon size={14} /> {t.label}
          </button>
        ))}
      </div>

      {toast && (
        <div className={`rounded-lg border px-4 py-3 text-sm flex items-center gap-2 ${toast.ok ? "bg-emerald-50 border-emerald-200 text-emerald-700" : "bg-red-50 border-red-200 text-red-700"}`}>
          {toast.ok ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />} {toast.text}
        </div>
      )}


      {tab === "schema" && <SchemaTab flash={flash} />}
      {tab === "resources" && <Resources flash={flash} />}

      <RecentActivity refreshKey={refreshKey} />
    </div>
  );
}

const card = "bg-white border border-stone-200 rounded-xl shadow-sm p-5 space-y-4";
const input = "w-full border border-stone-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20";
const btn = "px-4 py-2 bg-primary text-primary-foreground text-sm font-semibold rounded-lg hover:bg-primary/90 disabled:opacity-50 flex items-center gap-2";



/* ── shared governance/provenance block (collapsed by default — no friction) ── */
type Meta = { scope?: string; status?: string; owner?: string; source?: string; valid_from?: string; valid_until?: string };
function AdvancedMeta({ meta, setMeta, showDates }: { meta: Meta; setMeta: (m: Meta) => void; showDates?: boolean }) {
  const [open, setOpen] = useState(false);
  const set = (k: keyof Meta, v: string) => setMeta({ ...meta, [k]: v });
  return (
    <div className="border-t border-stone-100 pt-3">
      <button type="button" onClick={() => setOpen((o) => !o)} className="text-xs font-semibold text-stone-500 hover:text-stone-700 flex items-center gap-1">
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />} Advanced (optional)
      </button>
      {open && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3">
          <Labeled label="Scope">
            <select value={meta.scope || ""} onChange={(e) => set("scope", e.target.value)} className={input}>
              <option value="">— (attached node only)</option>
              <option value="global">Global (org-wide)</option>
              <option value="node">Specific node(s)</option>
            </select>
          </Labeled>
          <Labeled label="Status">
            <select value={meta.status || ""} onChange={(e) => set("status", e.target.value)} className={input}>
              <option value="">—</option>
              <option value="approved">Approved</option>
              <option value="draft">Draft</option>
              <option value="deprecated">Deprecated</option>
            </select>
          </Labeled>
          <Labeled label="Owner / steward"><input value={meta.owner || ""} onChange={(e) => set("owner", e.target.value)} className={input} placeholder="e.g. Data Governance team" /></Labeled>
          <Labeled label="Source / authority"><input value={meta.source || ""} onChange={(e) => set("source", e.target.value)} className={input} placeholder="e.g. Finance policy v3" /></Labeled>
          {showDates && <Labeled label="Valid from"><input type="date" value={meta.valid_from || ""} onChange={(e) => set("valid_from", e.target.value)} className={input} /></Labeled>}
          {showDates && <Labeled label="Valid until"><input type="date" value={meta.valid_until || ""} onChange={(e) => set("valid_until", e.target.value)} className={input} /></Labeled>}
        </div>
      )}
    </div>
  );
}
function Labeled({ label, children }: { label: string; children: React.ReactNode }) {
  return <div><label className="block text-xs font-bold text-stone-500 uppercase tracking-wider mb-1.5">{label}</label>{children}</div>;
}
const cleanMeta = (m: Meta): Meta => Object.fromEntries(Object.entries(m).filter(([, v]) => v && String(v).trim())) as Meta;



/* ── tab: Resources — glossary + synonyms + business rules in one place ────── */
function Resources({ flash }: { flash: (t: string, ok?: boolean) => void }) {
  const [sub, setSub] = useState<"glossary" | "synonyms" | "rules" | "context">("glossary");
  const SUBS: { id: typeof sub; label: string; icon: any }[] = [
    { id: "glossary", label: "Glossary", icon: BookOpen },
    { id: "synonyms", label: "Synonyms", icon: Tags },
    { id: "rules", label: "Business rules", icon: Scale },
    { id: "context", label: "Business context", icon: Compass },
  ];
  return (
    <div className="space-y-4">
      <div className="inline-flex bg-stone-100 rounded-lg p-1 text-xs">
        {SUBS.map((s) => (
          <button key={s.id} onClick={() => setSub(s.id)}
            className={`px-3 py-1.5 rounded-md font-medium flex items-center gap-1.5 ${sub === s.id ? "bg-white shadow-sm text-stone-900" : "text-stone-500 hover:text-stone-700"}`}>
            <s.icon size={13} /> {s.label}
          </button>
        ))}
      </div>
      {sub === "glossary" && <Glossary flash={flash} />}
      {sub === "synonyms" && <Synonyms flash={flash} />}
      {sub === "rules" && <Rules flash={flash} />}
      {sub === "context" && <BusinessContextTab flash={flash} />}
    </div>
  );
}

const ALIAS_KINDS = [
  { id: "", label: "Alias (generic)" }, { id: "acronym", label: "Acronym" },
  { id: "abbreviation", label: "Abbreviation" }, { id: "former_name", label: "Former / deprecated name" },
  { id: "variant", label: "Common variant / misspelling" }, { id: "translation", label: "Translation" },
];
const RULE_KINDS = [
  { id: "", label: "General note" }, { id: "definition", label: "Definition / assumption" },
  { id: "calculation", label: "Calculation" }, { id: "policy", label: "Policy / governance" },
  { id: "quality", label: "Data quality" }, { id: "freshness", label: "Freshness / SLA" },
];
const SEVERITIES = [
  { id: "info", label: "Info (FYI)" }, { id: "should", label: "Should" }, { id: "must", label: "Must (hard rule)" },
];

function Glossary({ flash }: { flash: (t: string, ok?: boolean) => void }) {
  const [terms, setTerms] = useState<Hit[]>([]);
  const [term, setTerm] = useState("");
  const [def, setDef] = useState("");
  const [targets, setTargets] = useState("");
  const [examples, setExamples] = useState("");
  const [meta, setMeta] = useState<Meta>({});
  const [busy, setBusy] = useState(false);

  const load = async () => { try { setTerms((await contextV2Api.search("", "Term"))?.results || []); } catch { /* noop */ } };
  useEffect(() => { load(); }, []);

  const add = async () => {
    if (!term.trim() || !def.trim()) { flash("Term and definition are required.", false); return; }
    setBusy(true);
    try {
      const spec: GlossaryInput = {
        term: term.trim(), definition: def.trim(),
        targets: targets.split(/[\n,]/).map((s) => s.trim()).filter(Boolean),
        examples: examples.split(/[\n,]/).map((s) => s.trim()).filter(Boolean), ...cleanMeta(meta),
      };
      await contextV2Api.addGlossary(spec);
      setTerm(""); setDef(""); setTargets(""); setExamples(""); setMeta({}); await load(); flash("Term added.");
    } catch (e: any) { flash(e?.response?.data?.detail || "Failed.", false); }
    finally { setBusy(false); }
  };
  const remove = async (id: string) => {
    try { await contextV2Api.deleteNode(id); setTerms((t) => t.filter((x) => x.id !== id)); }
    catch { flash("Delete failed.", false); }
  };

  return (
    <div className="space-y-4">
      <div className={card}>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <input value={term} onChange={(e) => setTerm(e.target.value)} className={input} placeholder="Term (e.g. ARR)" />
          <input value={def} onChange={(e) => setDef(e.target.value)} className={input + " md:col-span-2"} placeholder="Definition" />
        </div>
        <input value={targets} onChange={(e) => setTargets(e.target.value)} className={input} placeholder="Target entities to link (optional, e.g. School, Phone, or UUIDs)" />
        <input value={examples} onChange={(e) => setExamples(e.target.value)} className={input} placeholder="Examples (optional, comma-separated)" />
        <AdvancedMeta meta={meta} setMeta={setMeta} showDates />
        <button onClick={add} disabled={busy} className={btn}>{busy ? <Loader2 size={16} className="animate-spin" /> : <Plus size={16} />} Add term</button>
        <p className="text-xs text-stone-400">Embedded on save (instantly searchable / answerable). Attach it to any node(s) to link it in the graph.</p>
      </div>
      <div className="bg-white border border-stone-200 rounded-xl shadow-sm divide-y divide-stone-100">
        {terms.length === 0 ? <p className="p-6 text-sm text-stone-400 text-center">No glossary terms yet.</p> : terms.map((t) => (
          <div key={t.id} className="px-5 py-3 flex items-center justify-between gap-3">
            <span className="text-sm font-semibold text-stone-800 flex items-center gap-2"><BookOpen size={14} className="text-primary" /> {t.label}</span>
            <button onClick={() => remove(t.id)} className="p-1.5 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-md"><Trash2 size={15} /></button>
          </div>
        ))}
      </div>
    </div>
  );
}

function Synonyms({ flash }: { flash: (t: string, ok?: boolean) => void }) {
  const [synonyms, setSynonyms] = useState<Hit[]>([]);
  const [term, setTerm] = useState("");
  const [target, setTarget] = useState("");
  const [kind, setKind] = useState("");
  const [meta, setMeta] = useState<Meta>({});
  const [busy, setBusy] = useState(false);

  const load = async () => { try { setSynonyms((await contextV2Api.search("", "Synonym"))?.results || []); } catch { /* noop */ } };
  useEffect(() => { load(); }, []);

  const add = async () => {
    if (!term.trim()) { flash("Enter a synonym / alias.", false); return; }
    setBusy(true);
    try {
      const tgts = target.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
      const spec: SynonymInput = { term: term.trim(), targets: tgts, kind: kind || undefined, ...cleanMeta(meta) };
      await contextV2Api.addSynonym(spec);
      setTerm(""); setTarget(""); setKind(""); setMeta({}); await load();
      flash(`Alias "${spec.term}" saved.`);
    } catch (e: any) { flash(e?.response?.data?.detail || "Failed.", false); }
    finally { setBusy(false); }
  };

  const remove = async (id: string) => {
    try { await contextV2Api.deleteNode(id); setSynonyms((s) => s.filter((x) => x.id !== id)); }
    catch { flash("Delete failed.", false); }
  };

  return (
    <div className="space-y-4">
      <div className={card}>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div className="md:col-span-2">
            <label className="block text-xs font-bold text-stone-500 uppercase tracking-wider mb-1.5">Synonym / alias</label>
            <input value={term} onChange={(e) => setTerm(e.target.value)} className={input} placeholder="e.g. “revenue” for the SALES_FACT table" />
          </div>
          <Labeled label="Alias kind">
            <select value={kind} onChange={(e) => setKind(e.target.value)} className={input}>
              {ALIAS_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
            </select>
          </Labeled>
        </div>
        <input value={target} onChange={(e) => setTarget(e.target.value)} className={input} placeholder="Target entity to link (optional, e.g. Phone, School, or UUID)" />
        <AdvancedMeta meta={meta} setMeta={setMeta} />
        <button onClick={add} disabled={busy} className={btn}>{busy ? <Loader2 size={16} className="animate-spin" /> : <Plus size={16} />} Add synonym</button>
        <p className="text-xs text-stone-400">Aliases are embedded, so search &amp; the assistant find the node by its alias too. A <b>former / deprecated name</b> is flagged so the assistant can warn instead of silently resolving.</p>
      </div>
      <div className="bg-white border border-stone-200 rounded-xl shadow-sm divide-y divide-stone-100">
        {synonyms.length === 0 ? <p className="p-6 text-sm text-stone-400 text-center">No synonyms yet.</p> : synonyms.map((s) => (
          <div key={s.id} className="px-5 py-3 flex items-center justify-between gap-3">
            <span className="text-sm font-semibold text-stone-800 flex items-center gap-2"><Tags size={14} className="text-primary" /> {s.label}</span>
            <button onClick={() => remove(s.id)} className="p-1.5 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-md"><Trash2 size={15} /></button>
          </div>
        ))}
      </div>
    </div>
  );
}

function Rules({ flash }: { flash: (t: string, ok?: boolean) => void }) {
  const [rules, setRules] = useState<Hit[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [targets, setTargets] = useState("");
  const [kind, setKind] = useState("");
  const [severity, setSeverity] = useState("info");
  const [meta, setMeta] = useState<Meta>({});
  const [busy, setBusy] = useState(false);

  const load = async () => { try { setRules((await contextV2Api.search("", "ContextRule"))?.results || []); } catch { /* noop */ } };
  useEffect(() => { load(); }, []);

  const add = async () => {
    if (!title.trim() || !content.trim()) { flash("Title and content are required.", false); return; }
    setBusy(true);
    try {
      const spec: RuleInput = {
        title: title.trim(), content: content.trim(),
        targets: targets.split(/[\n,]/).map((s) => s.trim()).filter(Boolean),
        kind: kind || undefined, severity, ...cleanMeta(meta),
      };
      await contextV2Api.addRule(spec);
      setTitle(""); setContent(""); setTargets(""); setKind(""); setSeverity("info"); setMeta({}); await load(); flash("Rule added.");
    } catch (e: any) { flash(e?.response?.data?.detail || "Failed.", false); }
    finally { setBusy(false); }
  };
  const remove = async (id: string) => {
    try { await contextV2Api.deleteNode(id); setRules((r) => r.filter((x) => x.id !== id)); }
    catch { flash("Delete failed.", false); }
  };

  return (
    <div className="space-y-4">
      <div className={card}>
        <input value={title} onChange={(e) => setTitle(e.target.value)} className={input} placeholder="Rule title (e.g. Fiscal year starts in April)" />
        <textarea value={content} onChange={(e) => setContent(e.target.value)} className={input + " h-24 resize-y"} placeholder="The rule, in plain language…" />
        <input value={targets} onChange={(e) => setTargets(e.target.value)} className={input} placeholder="Target entities to link (optional, e.g. School, Phone, or UUIDs)" />
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Labeled label="Rule kind">
            <select value={kind} onChange={(e) => setKind(e.target.value)} className={input}>
              {RULE_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
            </select>
          </Labeled>
          <Labeled label="Severity">
            <select value={severity} onChange={(e) => setSeverity(e.target.value)} className={input}>
              {SEVERITIES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
            </select>
          </Labeled>
        </div>
        <AdvancedMeta meta={meta} setMeta={setMeta} showDates />
        <button onClick={add} disabled={busy} className={btn}>{busy ? <Loader2 size={16} className="animate-spin" /> : <Plus size={16} />} Add rule</button>
        <p className="text-xs text-stone-400">A <b>Must</b> rule is weighted as a hard constraint; an expiry date (Advanced) lets a rule go stale on its own.</p>
      </div>
      <div className="bg-white border border-stone-200 rounded-xl shadow-sm divide-y divide-stone-100">
        {rules.length === 0 ? <p className="p-6 text-sm text-stone-400 text-center">No business rules yet.</p> : rules.map((r) => (
          <div key={r.id} className="px-5 py-3 flex items-center justify-between gap-3">
            <span className="text-sm font-semibold text-stone-800 flex items-center gap-2"><Scale size={14} className="text-primary" /> {r.label}</span>
            <button onClick={() => remove(r.id)} className="p-1.5 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-md"><Trash2 size={15} /></button>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── org/collection-level business context — always-on framing for the assistant ── */
function BusinessContextTab({ flash }: { flash: (t: string, ok?: boolean) => void }) {
  const [items, setItems] = useState<import("@/lib/api/contextV2").BusinessContextEntry[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [alwaysOn, setAlwaysOn] = useState(true);
  const [meta, setMeta] = useState<Meta>({});
  const [busy, setBusy] = useState(false);

  const load = async () => { try { setItems((await contextV2Api.listBusinessContext())?.items || []); } catch { /* noop */ } };
  useEffect(() => { load(); }, []);

  const add = async () => {
    if (!title.trim() || !content.trim()) { flash("Title and context are required.", false); return; }
    setBusy(true);
    try {
      await contextV2Api.addBusinessContext({
        title: title.trim(), content: content.trim(), always_on: alwaysOn,
        owner: meta.owner || undefined, source: meta.source || undefined,
      });
      setTitle(""); setContent(""); setAlwaysOn(true); setMeta({}); await load(); flash("Business context added.");
    } catch (e: any) { flash(e?.response?.data?.detail || "Failed.", false); }
    finally { setBusy(false); }
  };
  const remove = async (id: string) => {
    try { await contextV2Api.deleteNode(id); setItems((x) => x.filter((i) => i.id !== id)); }
    catch { flash("Delete failed.", false); }
  };

  return (
    <div className="space-y-4">
      <div className={card}>
        <p className="text-xs text-stone-500 -mb-1">Describe what the organization is and cares about — mission, domain, standing assumptions, governance philosophy. <b>Always-on</b> items frame every answer (toggle the behavior in Settings → Retrieval).</p>
        <input value={title} onChange={(e) => setTitle(e.target.value)} className={input} placeholder="Title (e.g. Mission, Domain overview, Standing assumptions)" />
        <textarea value={content} onChange={(e) => setContent(e.target.value)} className={input + " h-28 resize-y"} placeholder="The context, in plain language…" />
        <label className="flex items-center gap-2 text-sm text-stone-700">
          <input type="checkbox" checked={alwaysOn} onChange={(e) => setAlwaysOn(e.target.checked)} className="rounded border-stone-300" />
          Always-on (prepend to every answer as a frame)
        </label>
        <AdvancedMeta meta={meta} setMeta={setMeta} />
        <button onClick={add} disabled={busy} className={btn}>{busy ? <Loader2 size={16} className="animate-spin" /> : <Plus size={16} />} Add context</button>
      </div>
      <div className="bg-white border border-stone-200 rounded-xl shadow-sm divide-y divide-stone-100">
        {items.length === 0 ? <p className="p-6 text-sm text-stone-400 text-center">No business context yet.</p> : items.map((i) => (
          <div key={i.id} className="px-5 py-3 flex items-start justify-between gap-3">
            <div className="min-w-0">
              <span className="text-sm font-semibold text-stone-800 flex items-center gap-2">
                <Compass size={14} className="text-primary" /> {i.title}
                {i.always_on && <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200">Always-on</span>}
              </span>
              <p className="text-xs text-stone-500 mt-0.5 line-clamp-2">{i.content}</p>
            </div>
            <button onClick={() => remove(i.id)} className="p-1.5 text-stone-400 hover:text-red-600 hover:bg-red-50 rounded-md shrink-0"><Trash2 size={15} /></button>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ── audit trail: who edited what, when (writes go to the AuditLog table) ──── */
const ACTION_LABEL: Record<string, string> = {
  "curate/create_node": "Created node", "curate/update_node": "Edited",
  "curate/delete_node": "Deleted node", "curate/create_edge": "Linked",
  "curate/delete_edge": "Unlinked", "curate/glossary": "Glossary term",
  "curate/rule": "Business rule", "curate/synonym": "Synonym",
  "curate/relationship": "Relationship",
};

function RecentActivity({ refreshKey }: { refreshKey: number }) {
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const r = await contextV2Api.auditLogs(60);
        setItems((r?.items || []).filter((x: any) => String(x.action || "").startsWith("curate/")));
      } catch { setItems([]); }
      finally { setLoading(false); }
    })();
  }, [refreshKey]);

  return (
    <div className="bg-white border border-stone-200 rounded-xl shadow-sm">
      <div className="px-5 py-3 border-b border-stone-100 flex items-center gap-2">
        <Clock size={15} className="text-stone-400" />
        <h2 className="font-semibold text-stone-800 text-sm">Recent edits</h2>
      </div>
      {loading ? (
        <p className="p-5 text-xs text-stone-400">Loading…</p>
      ) : items.length === 0 ? (
        <p className="p-5 text-xs text-stone-400">No manual edits yet — every change you make here is logged with who &amp; when.</p>
      ) : (
        <ul className="divide-y divide-stone-100 max-h-80 overflow-y-auto">
          {items.map((it) => {
            const d = it.details || {};
            const target = d.label || d.term || d.title || d.relation || d.type ||
              (d.source && d.target ? `${String(d.source).slice(0, 8)} → ${String(d.target).slice(0, 8)}` : "");
            return (
              <li key={it.id} className="px-5 py-2.5 flex items-center justify-between gap-3 text-sm">
                <div className="min-w-0 truncate">
                  <span className="font-medium text-stone-800">{ACTION_LABEL[it.action] || it.action}</span>
                  {target && <span className="text-stone-500"> · {target}</span>}
                </div>
                <span className="text-[11px] text-stone-400 shrink-0">
                  {it.actor ? `${it.actor_name || String(it.actor).slice(0, 8)} · ` : ""}
                  {it.created_at ? new Date(it.created_at).toLocaleString() : ""}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
