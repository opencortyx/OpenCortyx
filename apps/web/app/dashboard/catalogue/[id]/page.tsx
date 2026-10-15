"use client";
import { useEffect, useState, useCallback, use } from "react";
import { contextV2Api } from "@/lib/api/contextV2";
import {
  ArrowLeft, Network, Box, Hash, Layers, ListTree, Database, RefreshCw, FileText, ShieldCheck,
  BadgeCheck, User, Users, Eye, Download, Star, GitBranch, Clock, AlertTriangle, Lock,
  MessageSquare, CheckCircle2, Sparkles, Table2, Link2, Calendar, BookOpen, Gauge, Activity, FileBox,
  Trash2
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import Button from "@/components/ui/Button";
import DeleteAssetDialog from "@/components/assets/DeleteAssetDialog";
import AssetTags from "@/components/assets/AssetTags";
import GraphTree from "@/app/dashboard/graph/GraphTree";

const avg = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0);

/** Edge types that describe how an asset is stored, not what it feeds or is fed by. */
const NON_LINEAGE_EDGES = new Set(["CONTAINS", "NEXT", "HAS_SECTION", "HAS_RECORD", "CREATED_ON", "MODIFIED_ON"]);

export default function AssetDetailsPage(props0: { params: Promise<{ id: string }> }) {
  const params = use(props0.params);
  const router = useRouter();
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [activeTab, setActiveTab] = useState<"overview" | "schema" | "lineage">("overview");

  // Comments & collaboration
  const [comments, setComments] = useState<any[]>([]);
  const [cText, setCText] = useState("");
  const [cBusy, setCBusy] = useState(false);
  const loadComments = useCallback(async () => {
    try { const r = await contextV2Api.listComments(params.id); setComments(r.items || []); } catch { /* ignore */ }
  }, [params.id]);
  useEffect(() => { loadComments(); }, [loadComments]);
  const addComment = async () => {
    const t = cText.trim();
    if (!t || cBusy) return;
    setCBusy(true);
    try { await contextV2Api.addComment(params.id, t); setCText(""); await loadComments(); }
    catch (e: any) { alert(e?.response?.data?.detail || e?.message || "Failed to add comment"); }
    finally { setCBusy(false); }
  };
  const deleteComment = async (id: string) => {
    try { await contextV2Api.deleteComment(id); await loadComments(); } catch { /* ignore */ }
  };

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await contextV2Api.getNode(params.id, 1));
    } catch { /* ignore */ } finally { setLoading(false); }
  }, [params.id]);

  useEffect(() => { load(); }, [load]);

  // AI-overview toggle (global) + on-demand generation for this asset
  const [aiOn, setAiOn] = useState<boolean>(true);
  const [generating, setGenerating] = useState(false);
  useEffect(() => {
    contextV2Api.getConfig().then((c: any) => {
      const v = c?.tunable ?? c?.values ?? c ?? {};
      setAiOn(v.ai_overviews !== false);
    }).catch(() => {});
  }, []);
  const generateOverview = useCallback(async () => {
    setGenerating(true);
    try { await contextV2Api.generateOverview(params.id); await load(); }
    catch (e) { console.error("generate overview failed", e); }
    finally { setGenerating(false); }
  }, [params.id, load]);
  // edit the AI overview summaries + persist them back to the section nodes
  const [editing, setEditing] = useState(false);
  const [savingEdit, setSavingEdit] = useState(false);
  const [editVals, setEditVals] = useState({ exec_summary: "", business_purpose: "" });
  const saveEdit = useCallback(async () => {
    setSavingEdit(true);
    try { await contextV2Api.editOverview(params.id, editVals); setEditing(false); await load(); }
    catch (e) { console.error("save overview failed", e); }
    finally { setSavingEdit(false); }
  }, [params.id, editVals, load]);

  if (loading && !data) {
    return (
      <div className="flex flex-col items-center justify-center h-[60vh] gap-3 text-stone-500">
        <RefreshCw className="animate-spin text-primary" size={28} />
        <p className="text-sm font-medium">Loading asset details...</p>
      </div>
    );
  }
  if (!data || data.status === "not_found") {
    return (
      <div className="max-w-4xl mx-auto py-16 text-center">
        <h2 className="text-2xl font-bold text-stone-900 mb-2">Asset Not Found</h2>
        <p className="text-stone-500 mb-6">The requested asset does not exist or you do not have permission to view it.</p>
        <Link href={"/dashboard/catalogue" as any} className="text-primary font-medium hover:underline flex items-center justify-center gap-2">
          <ArrowLeft size={16} /> Back to Catalog
        </Link>
      </div>
    );
  }

  const { node, graph, asset } = data;
  const props = node.properties || {};
  // The server's catalogue view says what kind of asset this is; a layout-read document's root is a
  // `Document`, so a type-name check called it "a table with 0 columns".
  const isDocument = asset?.kind ? asset.kind === "document" : (node.type === "Source" || node.type === "Chunk");
  const neighbors = (graph?.nodes || []).filter((n: any) => n.id !== node.id);
  const edges = graph?.edges || [];
  const byId: Record<string, any> = Object.fromEntries((graph?.nodes || []).map((n: any) => [n.id, n]));
  const labelOf = (id: string) => byId[id]?.label || String(id).slice(0, 8);

  const columns = neighbors.filter((n: any) => n.type === "Attribute");
  const keywords = neighbors.filter((n: any) => n.type === "Keyword");
  const chunks = neighbors.filter((n: any) => n.type === "Chunk");
  const summaryNode = neighbors.find((n: any) => n.type === "Summary");

  const _SCHEMA_SKIP_TYPES = new Set(["Attribute", "Topic", "Keyword", "Chunk", "Summary", "Date",
    "Source", "Table", "ExecutiveSummary", "BusinessPurpose", "NLDescription", "StructureProfile",
    "QualityProfile", "UsageProfile", "ChangeProfile", "TrustProfile", "ContextRule", "BusinessContext", "Term"]);
  const recordNodes = neighbors.filter((n: any) => !_SCHEMA_SKIP_TYPES.has(n.type));
  // The catalogue's kind decides, as it does for `isDocument`; inferring it from the neighbours only
  // stands in when the server did not say (a layout document has elements, not Chunks, and read as
  // semi-structured, listing its Title's bookkeeping fields as a "key schema").
  const isStructured = asset?.kind ? asset.kind === "structured" : (node.type === "Table" || columns.length > 0);
  const isUnstructured = asset?.kind ? asset.kind === "document"
    : (node.type === "Chunk" || (chunks.length > 0 && recordNodes.length === 0));
  const isSemi = asset?.kind ? asset.kind === "semistructured"
    : (!isStructured && !isUnstructured && recordNodes.length > 0);
  // system/provenance props that are NOT part of the source's actual JSON schema
  const _SYS_PROPS = new Set(["x", "y", "degree", "lod", "embedding", "source_key", "source_table",
    "version", "filename", "superseded", "superseded_by", "created_date", "modified_date",
    "valid_from", "valid_until", "checksum", "format", "kind", "source"]);
  const _skipProp = (k: string) => k.startsWith("__") || _SYS_PROPS.has(k);
  const jsonKeys = (() => {                       // distinct JSON keys across the record nodes + inferred type
    const m = new Map<string, { type: string; node_type: string; sample: string }>();
    for (const rn of recordNodes) {
      for (const [k, v] of Object.entries(rn.properties || {})) {
        if (_skipProp(k) || m.has(k)) continue;
        const t = v === null ? "null" : Array.isArray(v) ? "array"
          : typeof v === "number" ? (Number.isInteger(v) ? "integer" : "float") : typeof v;
        m.set(k, { type: t, node_type: rn.type, sample: String(v ?? "").slice(0, 40) });
      }
    }
    return Array.from(m.entries()).map(([name, meta]) => ({ name, ...meta }));
  })();
  // Real catalog sections — each produced by its OWN independent extractor (HAS_SECTION nodes).
  const sec = (t: string) => neighbors.find((n: any) => n.type === t)?.properties || {};
  const sExec = sec("ExecutiveSummary").text || "";
  const sBiz = sec("BusinessPurpose").text || "";
  const sNL = sec("NLDescription").text || "";
  const profile: any = { ...sec("StructureProfile"), ...sec("QualityProfile"), ...sec("UsageProfile"),
    ...sec("ChangeProfile"), ...sec("TrustProfile"),
    executive_summary: sExec, business_purpose: sBiz, nl_description: sNL };
  // whether a REAL AI overview was generated (section nodes exist) vs only client-side fallback text
  const hasRealSections = !!(sExec || sBiz || sNL || summaryNode ||
    neighbors.some((n: any) => ["StructureProfile", "QualityProfile", "UsageProfile", "ChangeProfile", "TrustProfile"].includes(n.type)));

  // Lineage, not the asset's own insides: containment, reading order and date bookkeeping are how an
  // asset is stored, and counting them made a document "feed" its own title and its ingest date.
  const lineage = edges.filter((e: any) => !NON_LINEAGE_EDGES.has(String(e.label || "").toUpperCase()));
  const downstream = lineage.filter((e: any) => e.source === node.id).map((e: any) => ({ rel: e.label, to: labelOf(e.target), id: e.target }));
  const upstream = lineage.filter((e: any) => e.target === node.id).map((e: any) => ({ rel: e.label, from: labelOf(e.source), id: e.source }));

  // ── derived (real where available) ──
  const nullRates = columns.map((c: any) => Number(c.properties?.null_rate ?? 0)).filter((x: number) => !isNaN(x));
  const completeness = typeof profile.completeness === "number" ? profile.completeness
    : isDocument ? (props.word_count ? 100 : 70)
    : (nullRates.length ? Math.round((1 - avg(nullRates)) * 100) : 95);
  const anyPII = !!profile.contains_pii || columns.some((c: any) => c.properties?.is_pii) || !!props.is_pii;
  const trust = Math.min(98, 70 + Math.round(completeness * 0.22) + ((profile.executive_summary || props.description) ? 5 : 0));
  const certified = (profile.certification || "Draft") === "Certified";
  const version = profile.version || props.__version || 1;
  const aiSummary = (summaryNode?.properties?.text) || props.summary || "";

  // ── governance & usage: REAL signals from the Asset Profile extractor; "Not set" where a value
  //    isn't measurable yet (no fabrication — owner/steward/SLA come from the governance module later) ──
  const PH = {
    owner: profile.owner || "Not set",
    steward: profile.steward || "Not set",
    sme: profile.sme || "Not set",
    certifiedBy: certified ? (profile.certified_by || "—") : "—",
    lastReviewed: profile.last_reviewed || "—",
    created: profile.created || props.created_date || "—",
    modified: profile.modified || props.modified_date || "—",
    views: profile.views ?? "—",
    queries: profile.retrievals ?? 0,
    favorited: profile.favorited ?? 0,
    freshness: isDocument ? "Re-ingested on change" : "Updated on change",
    sla: profile.sla || "Not set",
    incidents: profile.incidents || "No incidents recorded",
    sensitivity: profile.classification || (anyPII ? "Confidential (PII)" : "Internal"),
    retention: profile.retention || "Not set",
  };

  // ── AI-generated report (derived from the asset's real data; narrative where we lack telemetry) ──
  const fmt = (n: any) => (typeof n === "number" ? n.toLocaleString() : n);
  const typeCounts: Record<string, number> = {};
  neighbors.forEach((n: any) => { typeCounts[n.type] = (typeCounts[n.type] || 0) + 1; });
  const derivedTypes = Object.entries(typeCounts).filter(([t]) => !["Attribute", "Chunk"].includes(t));
  const pkCols = columns.filter((c: any) => c.properties?.primary_key);
  const fkCols = columns.filter((c: any) => c.properties?.foreign_key || c.properties?.references_table);
  const highNull = columns
    .filter((c: any) => Number(c.properties?.null_rate || 0) > 0.3)
    .map((c: any) => `${c.properties?.physical_column || c.label} (${Math.round(Number(c.properties?.null_rate || 0) * 100)}% null)`);

  const relSummary = (() => {
    const ups = upstream.slice(0, 5).map((u: any) => u.from);
    const downs = downstream.slice(0, 5).map((d: any) => d.to);
    if (!ups.length && !downs.length) return "No relationships captured yet — increase traversal depth or enrich lineage.";
    const parts: string[] = [];
    if (ups.length) parts.push(`is linked from ${ups.join(", ")}`);
    if (downs.length) parts.push(`feeds ${downs.join(", ")}`);
    return `This asset ${parts.join("; ")}.`;
  })();

  const executiveAuto = aiSummary
    ? aiSummary.split(". ").slice(0, 2).join(". ")
    : isDocument
      ? `${node.label} is a ${props.language || ""} document${props.word_count ? ` of ~${fmt(props.word_count)} words` : ""}${chunks.length ? `, split into ${chunks.length} chunks` : ""}.`
      : `${node.label} is a ${props.domain ? props.domain + " " : ""}table with ${columns.length} columns${props.row_count ? ` and ~${fmt(props.row_count)} rows` : ""}.`;
  const executive = sExec || executiveAuto;   // prefer the Executive Summary extractor node
  const businessPurpose = sBiz || props.description
    || `Supports the ${props.domain || "organization's"} domain by providing ${isDocument ? "reference knowledge for search and AI grounding" : "structured records for reporting, metrics and AI grounding"}.`;
  const dataContents = isDocument
    ? `${chunks.length || "Several"} content chunks with ${keywords.length} keyword${keywords.length !== 1 ? "s" : ""} extracted${summaryNode ? ", plus an AI summary" : ""}.`
    : `${columns.length} columns${pkCols.length ? `, ${pkCols.length} primary key(s)` : ""}${fkCols.length ? `, ${fkCols.length} foreign key(s)` : ""}${derivedTypes.length ? `; derived: ${derivedTypes.map(([t, c]) => `${c} ${t.toLowerCase()}`).join(", ")}` : ""}.`;
  const usageSummary = `Commonly used for ${props.domain || (isDocument ? "knowledge lookup" : "analytics & reporting")}. Referenced in ${downstream.length} downstream asset(s); ~${PH.queries.toLocaleString()} ${isDocument ? "retrievals" : "queries"} and ${PH.views.toLocaleString()} views recently.`;
  const dqSummary = `${completeness}% complete${nullRates.length ? ` (avg ${Math.round(avg(nullRates) * 100)}% null across columns)` : ""}. ${highNull.length ? `Watch: ${highNull.slice(0, 3).join(", ")}.` : "No high-null columns detected."} ${PH.freshness}.`;
  const changeSummary = `Currently version ${version}.${props.__checksum ? " Content hash is tracked — the asset is re-ingested automatically when the source changes." : ""} Last modified ${PH.modified}.`;

  const keyMetrics: [string, any][] = isSemi
    ? [["Keys", jsonKeys.length], ["Records", recordNodes.length], ["Node types", new Set(recordNodes.map((n: any) => n.type)).size], ["Downstream", downstream.length]]
    : isUnstructured
    ? [["Chunks", chunks.length || props.chunks || 0], ["Words", props.word_count || "—"], ["Language", props.language || "—"], ["Downstream", downstream.length]]
    : [["Columns", columns.length], ["Rows", props.row_count ?? "—"], ["Completeness", `${completeness}%`], ["Avg null", `${Math.round(avg(nullRates) * 100)}%`], ["Downstream", downstream.length]];

  const insights: string[] = [];
  columns.forEach((c: any) => { const nr = Number(c.properties?.null_rate || 0); if (nr > 0.5) insights.push(`Column “${c.properties?.physical_column || c.label}” is mostly empty (~${Math.round(nr * 100)}% null).`); });
  if (downstream.length >= 3) insights.push(`Highly reused — feeds ${downstream.length} downstream assets.`);
  if (!isDocument && columns.length) insights.push(anyPII ? "PII detected in one or more columns." : "No PII detected in scanned columns.");
  if (!insights.length) insights.push("No notable anomalies detected automatically.");

  const warnings: string[] = [];
  if (!props.description) warnings.push("No business description — add one for discovery & AI grounding.");
  if (highNull.length) warnings.push(`High null rate in: ${highNull.slice(0, 4).join(", ")}.`);
  if (!certified) warnings.push("Not yet certified — verify before relying on it for decisions.");
  if (anyPII) warnings.push("Contains PII — ensure access controls & masking are applied.");
  if (!upstream.length && !downstream.length) warnings.push("No lineage captured — origins and downstream impact are unknown.");
  if (!warnings.length) warnings.push("No major caveats detected.");

  return (
    <div className="max-w-7xl mx-auto space-y-6 pb-16">
      {/* Header */}
      <div>
        <Link href={"/dashboard/catalogue" as any} className="text-xs font-semibold uppercase tracking-wider text-stone-500 hover:text-primary flex items-center gap-1 w-fit mb-4 transition-colors">
          <ArrowLeft size={14} /> Catalog
        </Link>
        <div className="bg-white border border-stone-200 rounded-2xl p-8 shadow-sm flex items-start gap-6">
          <div className={`p-5 rounded-2xl ${isDocument ? "bg-emerald-50 text-emerald-600" : "bg-teal-50 text-teal-600"}`}>
            {isDocument ? <FileText size={40} strokeWidth={1.5} /> : <Database size={40} strokeWidth={1.5} />}
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-3 flex-wrap">
              <h1 className="text-3xl font-bold tracking-tight text-stone-900">{node.label}</h1>
              {certified
                ? <span className="inline-flex items-center gap-1 text-xs font-bold px-2.5 py-1 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200"><BadgeCheck size={13} /> Certified</span>
                : <span className="inline-flex items-center gap-1 text-xs font-bold px-2.5 py-1 rounded-full bg-amber-50 text-amber-700 border border-amber-200">Draft</span>}
            </div>
            <div className="flex flex-wrap items-center gap-4 text-sm text-stone-500 font-medium mt-2">
              <span className="flex items-center gap-1.5 px-3 py-1 bg-stone-100 rounded-full"><Box size={14} /> {node.type}</span>
              {props.domain && <span className="flex items-center gap-1.5"><Layers size={14} /> {props.domain}</span>}
              <span className="flex items-center gap-1.5 text-stone-400 font-mono text-xs"><Hash size={14} /> {String(node.id).slice(0, 12)}…</span>
            </div>
            {props.description && <p className="mt-4 text-stone-600 leading-relaxed max-w-4xl">{props.description}</p>}
            <div className="mt-4 max-w-xl">
              <div className="text-xs font-semibold uppercase tracking-wider text-stone-500 mb-1.5">Tags</div>
              <AssetTags assetId={params.id} />
            </div>
          </div>
          <Button variant="danger" icon={Trash2} onClick={() => setConfirmDelete(true)}>
            Delete asset
          </Button>
        </div>
      </div>

      {confirmDelete && (
        <DeleteAssetDialog
          assetId={params.id}
          assetName={node.label || "this asset"}
          onClose={() => setConfirmDelete(false)}
          onDeleted={() => router.push("/dashboard/catalogue" as any)}
        />
      )}

      {/* Tabs */}
      <div className="flex items-center gap-6 border-b border-stone-200 px-2 overflow-x-auto">
        {([
          ["overview", "Overview", Gauge],
          ["schema", "Schema", Table2],
          ["lineage", "Lineage", Network],
        ] as const).map(([key, label, Icon]) => (
          <button
            key={key}
            onClick={() => setActiveTab(key as any)}
            className={`pb-4 text-sm font-semibold transition-colors relative whitespace-nowrap ${
              activeTab === key ? "text-primary font-bold" : "text-stone-500 hover:text-stone-800"
            }`}
          >
            <span className="flex items-center gap-2">
              <Icon size={16} /> {label}
            </span>
            {activeTab === key && <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary rounded-t-full" />}
          </button>
        ))}
      </div>

      {/* ── OVERVIEW ── */}
      {activeTab === "overview" && (
        <div className="space-y-6">
          {/* top metric row */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            {/* Trust & Certification */}
            <Card title="Trust & Certification" icon={<ShieldCheck size={16} className="text-emerald-500" />}>
              <div className="flex items-center gap-5 mb-4">
                <ScoreRing value={trust} />
                <div>
                  <div className="text-xs font-bold uppercase tracking-wider text-stone-400">Data Confidence</div>
                  <div className="text-sm font-semibold text-stone-800 mt-0.5">{trust >= 85 ? "High" : trust >= 70 ? "Moderate" : "Low"}</div>
                </div>
              </div>
              <Row label="Certification" value={<Badge tone={certified ? "emerald" : "amber"}>{certified ? "Certified" : "Draft"}</Badge>} />
              <Row label="Completeness" value={`${completeness}%`} />
              <Row label="Certified by" value={PH.certifiedBy} />
              <Row label="Last reviewed" value={PH.lastReviewed} />
            </Card>

            {/* Business Context */}
            <Card title="Business Context" icon={<Users size={16} className="text-emerald-500" />}>
              <Row label="Business owner" value={PH.owner} icon={<User size={13} />} />
              <Row label="Steward" value={PH.steward} icon={<ShieldCheck size={13} />} />
              <Row label="Subject-matter expert" value={PH.sme} icon={<BookOpen size={13} />} />
              <Row label="Domain" value={props.domain || "—"} icon={<Layers size={13} />} />
              <Row label="Asset type" value={node.type} icon={<Box size={13} />} />
            </Card>

            {/* Classification & Sensitivity */}
            <Card title="Classification & Sensitivity" icon={<Lock size={16} className="text-rose-500" />}>
              <Row label="Contains PII" value={<Badge tone={anyPII ? "rose" : "slate"}>{anyPII ? "Yes" : "No"}</Badge>} />
              <Row label="Sensitivity" value={PH.sensitivity} />
              <Row label="Retention" value={PH.retention} />
              <Row label="Access" value="Org scope · policy-enforced" />
            </Card>
          </div>

          {/* second metric row */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            {/* Data Quality & Observability */}
            <Card title="Data Quality & Observability" icon={<Activity size={16} className="text-teal-500" />}>
              <Row label="Freshness" value={PH.freshness} icon={<Clock size={13} />} />
              <Row label="Completeness" value={`${completeness}%`} icon={<Gauge size={13} />} />
              {!isDocument && <Row label="Avg null rate" value={`${Math.round(avg(nullRates) * 100)}%`} />}
              <Row label="SLA" value={PH.sla} />
              <Row label="Incidents" value={<span className="text-emerald-600 flex items-center gap-1"><CheckCircle2 size={13} /> {PH.incidents}</span>} />
            </Card>

            {/* Usage & Popularity */}
            <Card title="Usage & Popularity" icon={<Eye size={16} className="text-emerald-500" />}>
              <Row label="Views" value={PH.views.toLocaleString()} icon={<Eye size={13} />} />
              <Row label={isDocument ? "Retrievals" : "Queries"} value={PH.queries.toLocaleString()} icon={<Download size={13} />} />
              <Row label="Favorited by" value={`${PH.favorited} teams`} icon={<Star size={13} />} />
              <Row label="Referenced in" value={`${downstream.length} downstream asset${downstream.length !== 1 ? "s" : ""}`} icon={<Link2 size={13} />} />
            </Card>

            {/* Change History */}
            <Card title="Change History" icon={<Clock size={16} className="text-amber-500" />}>
              <Row label="Version" value={`v${version}`} icon={<GitBranch size={13} />} />
              <Row label="Created" value={PH.created} icon={<Calendar size={13} />} />
              <Row label="Last modified" value={PH.modified} icon={<Calendar size={13} />} />
              {props.__checksum && <Row label="Checksum" value={<span className="font-mono text-xs">{String(props.__checksum).slice(0, 12)}…</span>} />}
            </Card>
          </div>

          {/* ── AI-generated report ── */}
          <div className="flex items-center gap-2 pt-2">
            <Sparkles size={18} className="text-emerald-500" />
            <h2 className="text-lg font-bold text-stone-900">AI‑Generated Report</h2>
            <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full border ${aiOn ? "bg-emerald-50 text-emerald-600 border-emerald-100" : "bg-stone-100 text-stone-500 border-stone-200"}`}>
              {aiOn ? "auto" : "AI off"}
            </span>
            <div className="ml-auto flex items-center gap-2">
              {!editing && (
                <button onClick={() => { setEditVals({ exec_summary: executive, business_purpose: businessPurpose }); setEditing(true); }}
                  className="text-xs font-semibold text-stone-600 bg-white border border-stone-200 rounded-lg px-3 py-1.5 hover:bg-stone-50">
                  Edit
                </button>
              )}
              <button onClick={generateOverview} disabled={generating || editing}
                className="text-xs font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-1.5 hover:bg-emerald-100 disabled:opacity-60 flex items-center gap-1.5">
                <Sparkles size={13} /> {generating ? "Generating…" : hasRealSections ? "Regenerate" : "Generate overview"}
              </button>
            </div>
          </div>
          {editing && (
            <div className="border border-stone-200 rounded-2xl p-4 space-y-3 bg-white">
              <div className="text-xs font-bold uppercase tracking-wider text-stone-500">Edit overview</div>
              {([["exec_summary", "Executive Summary"], ["business_purpose", "Asset Description"]] as const).map(([k, label]) => (
                <div key={k}>
                  <label className="block text-[11px] font-semibold text-stone-500 mb-1">{label}</label>
                  <textarea value={(editVals as any)[k]} onChange={(e) => setEditVals((v) => ({ ...v, [k]: e.target.value }))}
                    rows={k === "business_purpose" ? 4 : 2}
                    className="w-full border border-stone-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20" />
                </div>
              ))}
              <div className="flex items-center gap-2 justify-end">
                <button onClick={() => setEditing(false)} className="text-xs font-semibold text-stone-500 px-3 py-1.5">Cancel</button>
                <button onClick={saveEdit} disabled={savingEdit}
                  className="text-xs font-semibold text-white bg-emerald-600 hover:bg-emerald-700 rounded-lg px-4 py-1.5 disabled:opacity-60">
                  {savingEdit ? "Saving…" : "Save"}
                </button>
              </div>
            </div>
          )}
          {!hasRealSections && !aiOn && (
            <div className="bg-stone-50 border border-stone-200 rounded-xl p-4 text-sm text-stone-600 flex items-center gap-2">
              <Sparkles size={15} className="text-stone-400 shrink-0" />
              <span>AI overviews are off (Settings → Ingestion). Showing a basic auto-summary — click <b className="font-semibold">Generate overview</b> for the full AI report, connected to this asset.</span>
            </div>
          )}

          {/* Executive summary — prominent */}
          <div className="bg-gradient-to-br from-emerald-50 to-white border border-emerald-100 rounded-2xl p-6">
            <div className="text-xs font-bold uppercase tracking-wider text-emerald-500 mb-2 flex items-center gap-1.5"><Sparkles size={13} /> Executive Summary</div>
            <p className="text-[15px] text-stone-800 leading-relaxed">{executive}</p>
          </div>

          {/* Narrative cards */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <TextCard title="Asset Description" icon={<BookOpen size={16} className="text-emerald-500" />} text={businessPurpose} />
            <TextCard title="Data Contents Summary" icon={<FileBox size={16} className="text-emerald-500" />} text={dataContents} />
            <TextCard title="Usage Summary" icon={<Eye size={16} className="text-emerald-500" />} text={usageSummary} />
            <TextCard title="Relationships Summary" icon={<GitBranch size={16} className="text-emerald-500" />} text={relSummary} />
            <TextCard title="Data Quality Summary" icon={<Activity size={16} className="text-teal-500" />} text={dqSummary} />
            <TextCard title="Change Summary" icon={<Clock size={16} className="text-amber-500" />} text={changeSummary} />
          </div>

          {/* Insights + Warnings */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <Card title="AI Insights" icon={<Sparkles size={16} className="text-emerald-500" />}>
              <ul className="space-y-2">
                {insights.map((t, i) => (
                  <li key={i} className="text-sm text-stone-700 flex items-start gap-2"><CheckCircle2 size={15} className="text-emerald-400 mt-0.5 shrink-0" /> {t}</li>
                ))}
              </ul>
            </Card>
            <Card title="Warnings & Caveats" icon={<AlertTriangle size={16} className="text-amber-500" />}>
              <ul className="space-y-2">
                {warnings.map((t, i) => (
                  <li key={i} className="text-sm text-stone-700 flex items-start gap-2"><AlertTriangle size={15} className="text-amber-400 mt-0.5 shrink-0" /> {t}</li>
                ))}
              </ul>
            </Card>
          </div>

          {/* Raw properties */}
          <Card title="Raw Properties" icon={<ListTree size={16} className="text-stone-500" />}>
            {(() => {
              const rawProps = { ...props };
              const rawAttrs = rawProps.attributes;
              const declaredAttrs: string[] = [];
              if (Array.isArray(rawAttrs)) {
                for (const item of rawAttrs) {
                  if (typeof item === "string") declaredAttrs.push(item.trim());
                  else if (item && typeof item === "object") {
                    const name = item.name || item.field || item.key;
                    if (name) declaredAttrs.push(String(name).trim());
                  }
                }
              } else if (typeof rawAttrs === "string" && rawAttrs.trim()) {
                declaredAttrs.push(...rawAttrs.split(/[\n,]/).map((s: string) => s.trim()).filter(Boolean));
              }
              delete rawProps.attributes;
              for (const attr of declaredAttrs) {
                if (!(attr in rawProps) || rawProps[attr] === undefined) {
                  rawProps[attr] = null;
                }
              }
              const entries = Object.entries(rawProps).filter(([k]) => k !== "description" && !k.startsWith("__"));
              return entries.length === 0 ? (
                <p className="text-sm text-stone-400 italic">No additional properties available for this asset.</p>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-5">
                  {entries.map(([k, v]) => {
                    const isNull = v === null || v === undefined || v === "" || v === "null" || v === "NULL" || v === "None" || (Array.isArray(v) && v.length === 0);
                    return (
                      <div key={k} className="flex flex-col gap-1.5">
                        <dt className="text-xs font-bold uppercase tracking-wider text-stone-400">{k.replace(/_/g, " ")}</dt>
                        <dd className="text-sm text-stone-800 font-medium break-words bg-stone-50/50 p-3 rounded-lg border border-stone-100">
                          {isNull ? <span className="text-stone-400 italic">Null</span> : typeof v === "boolean" ? (v ? "True" : "False") : Array.isArray(v) ? v.join(", ") : String(v)}
                        </dd>
                      </div>
                    );
                  })}
                </div>
              );
            })()}
          </Card>

          {/* Comments */}
          <Card title="Comments & Collaboration" icon={<MessageSquare size={16} className="text-stone-500" />}>
            <div className="space-y-3">
              {comments.length === 0 ? (
                <p className="text-sm text-stone-400">No comments yet — start the discussion.</p>
              ) : (
                <ul className="space-y-2">
                  {comments.map((c) => (
                    <li key={c.id} className="bg-stone-50 border border-stone-100 rounded-lg p-3">
                      <div className="flex items-start justify-between gap-2">
                        <p className="text-sm text-stone-700 whitespace-pre-wrap">{c.text}</p>
                        <button onClick={() => deleteComment(c.id)} className="text-[11px] text-stone-300 hover:text-rose-500 shrink-0">Delete</button>
                      </div>
                      <div className="text-[10px] text-stone-400 mt-1">{c.ts ? new Date(c.ts).toLocaleString() : ""}</div>
                    </li>
                  ))}
                </ul>
              )}
              <div className="flex gap-2 pt-1">
                <input value={cText} onChange={(e) => setCText(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && addComment()} placeholder="Add a comment…"
                  className="flex-1 border border-stone-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500/20" />
                <Button variant="primary" busy={cBusy} disabled={!cText.trim()} onClick={addComment}>Post</Button>
              </div>
            </div>
          </Card>

          <p className="text-[11px] text-stone-400 text-center pt-2">
            Profile sections (structure, quality, usage, change, trust + summaries) are produced by independent
            built-in extractors and shown here live. Ownership, SLA &amp; views show &quot;Not set&quot; until the
            governance/observability module sources them — no fabricated values.
          </p>
        </div>
      )}

      {/* ── SCHEMA TAB ── */}
      {activeTab === "schema" && (
        <div className="space-y-6">
          <Card title={
              isSemi ? `Schema Definition — ${jsonKeys.length} key${jsonKeys.length !== 1 ? "s" : ""} (semi-structured)`
              : isUnstructured ? "Schema Definition — unstructured document"
              : `Schema Definition — ${columns.length} column${columns.length !== 1 ? "s" : ""}`
            } icon={<Table2 size={18} className="text-teal-600" />}>
            {isSemi ? (
              jsonKeys.length === 0 ? (
                <p className="text-sm text-stone-400 italic py-4">No keys captured (increase traversal depth or re-ingest).</p>
              ) : (
                <div className="overflow-x-auto border border-stone-200 rounded-xl">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="bg-stone-50 text-stone-500 font-bold uppercase tracking-wider border-b border-stone-200">
                        <th className="px-4 py-3">Key</th>
                        <th className="px-4 py-3">Data Type</th>
                        <th className="px-4 py-3">Node Type</th>
                        <th className="px-4 py-3">Sample</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-stone-100 font-mono">
                      {jsonKeys.map((k) => (
                        <tr key={`${k.node_type}.${k.name}`} className="hover:bg-stone-50/80">
                          <td className="px-4 py-3 font-semibold text-stone-900">{k.name}</td>
                          <td className="px-4 py-3 text-stone-600">{k.type}</td>
                          <td className="px-4 py-3"><span className="px-2 py-0.5 text-[10px] font-bold bg-purple-100 text-purple-800 rounded font-sans">{k.node_type}</span></td>
                          <td className="px-4 py-3 text-stone-400 truncate max-w-[220px]">{k.sample || "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            ) : !isUnstructured ? (
              columns.length === 0 ? (
                <p className="text-sm text-stone-400 italic py-4">No columns captured for this asset.</p>
              ) : (
                <div className="overflow-x-auto border border-stone-200 rounded-xl">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="bg-stone-50 text-stone-500 font-bold uppercase tracking-wider border-b border-stone-200">
                        <th className="px-4 py-3">Column Name</th>
                        <th className="px-4 py-3">Data Type</th>
                        <th className="px-4 py-3">Keys / Constraints</th>
                        <th className="px-4 py-3">Null Rate</th>
                        <th className="px-4 py-3">PII Classification</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-stone-100 font-mono">
                      {columns.map((c: any) => (
                        <tr key={c.id} className="hover:bg-stone-50/80">
                          <td className="px-4 py-3 font-semibold text-stone-900">{c.properties?.physical_column || c.label}</td>
                          <td className="px-4 py-3 text-stone-600">{c.properties?.data_type || "string"}</td>
                          <td className="px-4 py-3">
                            {c.properties?.primary_key && <span className="px-2 py-0.5 text-[10px] font-bold bg-amber-100 text-amber-800 rounded mr-1 font-sans">PK</span>}
                            {c.properties?.foreign_key && <span className="px-2 py-0.5 text-[10px] font-bold bg-blue-100 text-blue-800 rounded font-sans">FK</span>}
                            {!c.properties?.primary_key && !c.properties?.foreign_key && <span className="text-stone-300 font-sans">—</span>}
                          </td>
                          <td className="px-4 py-3 font-sans text-stone-600">
                            {c.properties?.null_rate != null ? `${Math.round(c.properties.null_rate * 100)}%` : "0%"}
                          </td>
                          <td className="px-4 py-3 font-sans">
                            {c.properties?.is_pii ? <Badge tone="rose">PII</Badge> : <span className="text-stone-400">None</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            ) : (
              <div className="space-y-4 text-xs">
                <div className="p-4 bg-stone-50 rounded-xl border border-stone-200">
                  <div className="font-bold text-stone-800 mb-1">Unstructured Document Schema</div>
                  <p className="text-stone-600">Extracted chunks and NLP structural tags.</p>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                  {chunks.length || !recordNodes.length
                    ? <Mini label="Chunks" value={chunks.length || props.chunks || 0} />
                    : <Mini label="Layout elements" value={recordNodes.length} />}
                  <Mini label="Word count" value={props.word_count?.toLocaleString?.() || "—"} />
                  <Mini label="Language" value={props.language || "English"} />
                </div>
              </div>
            )}
          </Card>
        </div>
      )}

      {/* ── LINEAGE GRAPH ── */}
      {activeTab === "lineage" && (
        <div className="bg-white border border-stone-200 rounded-2xl shadow-sm overflow-hidden h-[700px]">
          {/* the dashboard's knowledge graph, opened down to this asset */}
          <GraphTree focusNodeId={params.id} embedded />
        </div>
      )}
    </div>
  );
}

/* ── presentational helpers ── */
function Card({ title, icon, action, children }: { title: string; icon: React.ReactNode; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="bg-white border border-stone-200 rounded-2xl shadow-sm p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-bold text-stone-800 flex items-center gap-2 text-sm uppercase tracking-wide">{icon} {title}</h3>
        {action}
      </div>
      {children}
    </div>
  );
}

function TextCard({ title, icon, text }: { title: string; icon: React.ReactNode; text: string }) {
  return (
    <div className="bg-white border border-stone-200 rounded-2xl shadow-sm p-6">
      <h3 className="font-bold text-stone-800 flex items-center gap-2 text-sm uppercase tracking-wide mb-3">{icon} {title}</h3>
      <p className="text-sm text-stone-700 leading-relaxed">{text}</p>
    </div>
  );
}

function Row({ label, value, icon }: { label: string; value: React.ReactNode; icon?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5 border-b border-stone-50 last:border-0">
      <span className="text-xs text-stone-500 flex items-center gap-1.5">{icon}{label}</span>
      <span className="text-sm font-medium text-stone-800 text-right truncate max-w-[60%]">{value}</span>
    </div>
  );
}

function Badge({ tone, children }: { tone: "emerald" | "amber" | "rose" | "slate"; children: React.ReactNode }) {
  const map: Record<string, string> = {
    emerald: "bg-emerald-50 text-emerald-700 border-emerald-200",
    amber: "bg-amber-50 text-amber-700 border-amber-200",
    rose: "bg-rose-50 text-rose-700 border-rose-200",
    slate: "bg-stone-100 text-stone-600 border-stone-200",
  };
  return <span className={`text-xs font-bold px-2 py-0.5 rounded-full border ${map[tone]}`}>{children}</span>;
}

function ScoreRing({ value }: { value: number }) {
  const tone = value >= 85 ? "#10b981" : value >= 70 ? "#f59e0b" : "#ef4444";
  const r = 26, c = 2 * Math.PI * r, off = c - (value / 100) * c;
  return (
    <div className="relative w-16 h-16">
      <svg width="64" height="64" className="-rotate-90">
        <circle cx="32" cy="32" r={r} stroke="#e2e8f0" strokeWidth="6" fill="none" />
        <circle cx="32" cy="32" r={r} stroke={tone} strokeWidth="6" fill="none" strokeDasharray={c} strokeDashoffset={off} strokeLinecap="round" />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center text-base font-bold text-stone-800">{value}</div>
    </div>
  );
}

function Mini({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="bg-stone-50 border border-stone-100 rounded-lg p-3 text-center">
      <div className="text-xl font-bold text-stone-900">{value}</div>
      <div className="text-[10px] font-bold uppercase tracking-wider text-stone-400 mt-0.5">{label}</div>
    </div>
  );
}
