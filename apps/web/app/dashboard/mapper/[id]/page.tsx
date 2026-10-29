"use client";

/**
 * Mapper — the builder.
 *
 * One form for all three paradigms. The source panel changes (JSON / tables / a parsed document) and
 * so does the vocabulary — path vs table vs element, field vs column vs attribute — but a rule is
 * always the same two things: a NODE with an identity, or an EDGE between two endpoints, each
 * resolved one of four ways.
 *
 * The endpoint control is the point of the rewrite. Before it, an edge lived on the target node's
 * row as `parent` + `edge_type`, so it could only ever mean "my parent, renamed"; two flat arrays
 * that reference each other by id were unreachable, and the only thing that crossed that gap was the
 * walker guessing from an `<entity>_id` field and calling every result `REFERENCES`. Choosing
 * "by value" here is what makes `line.sku → Product.sku as OF_PRODUCT` something you declare.
 *
 * The Declared graph under the form is the shape these rules compile to (components/mapper/declaredShape.ts
 * mirrors the compiler), and the Verify panel checks that shape against an asset that was actually
 * ingested. There is deliberately no in-browser "materialized" preview of the data.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import { contextV2Api } from "@/lib/api/contextV2";
import {
  ArrowLeft, Save, Plus, Loader2, AlertTriangle, CheckCircle2, Play,
} from "lucide-react";
import {
  ChunkCfg, EdgeRule, EditorModel, KeyRow, Paradigm,
  chunkText, collectPaths, docFromLayout, docFromText, normalizeChunk, parseCSV, proposeFromTables, proposeRules, walk,
  type SqlTable,
} from "@/components/mapper/model";
import {
  ChunkingPanel, DOC_ELEMENTS, DocSource, DocumentMetadataPanel, EdgeForm, ElementsPanel, JsonSource,
  KindLegend, Label, NodeForm, PARADIGM_META, Panel, RuleList, TableSource, UploadBar,
  type ExistingType,
} from "@/components/mapper/panels";
import ConformancePanel from "@/components/mapper/conformance";
import KnowledgePanel from "@/components/mapper/knowledge";
import { SqlConnectBar, SqlSchemaSource, type SqlOrigin } from "@/components/mapper/sqlsource";
import DeclaredCanvas from "@/components/mapper/canvas";

const READERS = ["unstructured.llm", "unstructured.ner", "unstructured.regex", "unstructured.keyword",
  "unstructured.dictionary", "unstructured.attributes", "unstructured.generative_llm", "unstructured.python"];

const SAMPLE_MD =
  "## Onboarding\nNew hires complete IT setup on day one. Their manager assigns a buddy from the same team. " +
  "HR schedules a welcome session for the first Friday.\n\n## Benefits\nEmployees enroll in health coverage " +
  "within 30 days. The company matches retirement contributions up to six percent. Open enrollment runs every November.";

const emptyEditor = (): EditorModel => ({
  keyRows: [], nodeRules: [], edgeRules: [], attributeRules: [], reading: {},
});

/** Rows (objects keyed by column) back to CSV, quoting what CSV requires. */
function toCSV(rows: Record<string, any>[]): string {
  const cols = Array.from(new Set(rows.flatMap((r) => Object.keys(r || {}))));
  const cell = (v: any) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [cols.join(","), ...rows.map((r) => cols.map((c) => cell(r?.[c])).join(","))].join("\n");
}

/** A parsed sample document back to its text, section by section. */
function docText(sample: any): string {
  const doc = sample?.document || {};
  return (doc.sections || []).map((s: any) =>
    [s.heading ? `## ${s.heading}` : "", ...(s.blocks || []).map((b: any) => b.text || "")]
      .filter(Boolean).join("\n\n")).join("\n\n");
}

export default function MapperBuilderPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();

  const [rec, setRec] = useState<any>(null);
  const [ed, setEd] = useState<EditorModel>(emptyEditor());
  const [paradigm, setParadigm] = useState<Paradigm>("semistructured");
  const [name, setName] = useState("");
  const [sample, setSample] = useState<any>(null);
  const [docs, setDocs] = useState<{ id: string; name: string }[]>([]);
  const [pick, setPick] = useState("");
  const [uploadMsg, setUploadMsg] = useState("");
  const [chunkSample, setChunkSample] = useState(SAMPLE_MD);
  const [models, setModels] = useState<Record<string, string | null>>({});
  const [sel, setSel] = useState<{ kind: "node" | "edge"; index: number }>({ kind: "node", index: 0 });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");
  const [dirty, setDirty] = useState(false);
  const [running, setRunning] = useState(false);
  // A live SQL schema is a THIRD kind of source: not a JSON payload and not rows, but tables whose
  // keys are already known. Kept apart from `sample` so nothing here is ever mistaken for data.
  const [sqlTables, setSqlTables] = useState<SqlTable[] | null>(null);
  const [sqlSource, setSqlSource] = useState("");
  const [sqlOrigin, setSqlOrigin] = useState<SqlOrigin | null>(null);

  /* ─────────────────────────────────────────────────────────────── load */

  useEffect(() => {
    (async () => {
      setLoading(true); setErr("");
      try {
        const m = await contextV2Api.getSchemaMapping(String(id));
        setRec(m);
        setName(m.name);
        setParadigm((m.paradigm as Paradigm) || "semistructured");
        const editor: EditorModel = { ...emptyEditor(), ...((m.payload as any)?.editor || {}) };
        editor.keyRows = editor.keyRows || [];
        editor.edgeRules = editor.edgeRules || [];
        setEd(editor);
        const s = (m.payload as any)?.sample;
        if (m.paradigm === "unstructured") {
          setSample(s && s.document ? s : docFromText("sample.md", SAMPLE_MD));
        } else if (s) {
          setSample(s);
          setDocs([{ id: "saved", name: "sample saved with this mapping" }]);
          setPick("saved");
        }
      } catch (e: any) {
        setErr(e?.response?.data?.detail || e?.message || "Could not load this mapping");
      } finally { setLoading(false); }
    })();
  }, [id]);

  // model registry — what actually reads each document element
  useEffect(() => {
    if (paradigm !== "unstructured") return;
    (async () => {
      try {
        const res: any = await (contextV2Api as any).listModels?.();
        const byRole: Record<string, string | null> = {};
        for (const el of DOC_ELEMENTS) {
          if (!el.role) continue;              // built-in (the layout reader) or not model-served
          const hit = (res?.items || []).find((m: any) => m.role === el.role && (m.default || m.is_default));
          byRole[el.role] = hit ? (hit.model_id || hit.name) : null;
        }
        setModels(byRole);
      } catch { setModels({}); }
    })();
  }, [paradigm]);

  // node types already in the graph: a join edge may name one as its foreign key's target
  const [existing, setExisting] = useState<ExistingType[]>([]);
  useEffect(() => {
    if (paradigm === "unstructured") return;
    (async () => {
      try {
        const res: any = await contextV2Api.ontology();
        setExisting((res?.types || [])
          .filter((t: any) => t.kind === "node" || t.kind === "entity")
          .map((t: any) => ({
            name: String(t.name),
            attributes: (t.attributes || []).map((a: any) => String(typeof a === "string" ? a : a?.name || "")).filter(Boolean),
          })));
      } catch { setExisting([]); }
    })();
  }, [paradigm]);

  /* ───────────────────────────────────────────────────── derived state */

  const patchEditor = (p: Partial<EditorModel>) => { setDirty(true); setEd((e) => ({ ...e, ...p, __touched: true })); };

  const chunk: ChunkCfg = normalizeChunk(ed.chunking);
  const chunkCount = useMemo(() => chunkText(chunkSample, chunk).length, [chunkSample, chunk]);
  const elements: Record<string, boolean> = ed.elements || {};
  const elementOn = (key: string) => elements[key] !== false;
  const kindForBlock = (k: string) => (k === "table" ? "tables" : k === "figure" ? "figures" : "chunks");

  /** A node rule is inert when its document element is switched off, or when chunks are not saved. */
  const disabledFor = useCallback((r: KeyRow): string | null => {
    if (paradigm !== "unstructured") return null;
    const el = DOC_ELEMENTS.find((e) => e.rule === r.node_type);
    if (el && !elementOn(el.key)) return "element off";
    if (r.node_type === chunk.node && chunk.mode === "extract") return "not saved";
    return null;
  }, [paradigm, elements, chunk.mode, chunk.node]);

  const paths = useMemo(() => collectPaths(sample ?? {}), [sample]);
  const nodeIndexes = ed.keyRows.map((r, i) => ({ r, i })).filter((x) => x.r.kind === "node");
  const currentNode = sel.kind === "node" ? ed.keyRows[sel.index] : null;
  const currentEdge = sel.kind === "edge" ? ed.edgeRules[sel.index] : null;
  const nodeSample = useMemo(() => {
    if (!currentNode) return null;
    const hits = walk(currentNode.path, sample ?? {});
    return hits.length ? hits[0].value : null;
  }, [currentNode, sample]);

  /* ───────────────────────────────────────────────────────── mutations */

  const patchRow = (index: number, patch: Partial<KeyRow>, attrPatch?: { path: string; patch: Partial<KeyRow> }) => {
    setDirty(true);
    setEd((e) => {
      const rows = e.keyRows.slice();
      const old = rows[index];
      rows[index] = { ...old, ...patch };
      // renaming a node's path moves its attribute rows with it — otherwise they silently detach
      if (patch.path !== undefined && patch.path !== old.path) {
        const from = old.path ? `${old.path}.` : "";
        const to = patch.path ? `${patch.path}.` : "";
        for (let i = 0; i < rows.length; i++) {
          if (i !== index && rows[i].kind === "attribute" && from && rows[i].path.startsWith(from)) {
            rows[i] = { ...rows[i], path: to + rows[i].path.slice(from.length) };
          }
        }
      }
      if (attrPatch) {
        const j = rows.findIndex((r) => r.kind === "attribute" && r.path === attrPatch.path);
        if (j >= 0) rows[j] = { ...rows[j], ...attrPatch.patch };
      }
      // a renamed node type has to follow into every edge that names it
      let edges = e.edgeRules;
      if (patch.node_type && old.node_type && patch.node_type !== old.node_type) {
        edges = edges.map((x) => ({
          ...x,
          fromNode: x.fromNode === old.node_type ? patch.node_type! : x.fromNode,
          toNode: x.toNode === old.node_type ? patch.node_type! : x.toNode,
          readFrom: x.readFrom === old.node_type ? patch.node_type! : x.readFrom,
        }));
      }
      return { ...e, keyRows: rows, edgeRules: edges, __touched: true };
    });
  };

  const patchEdge = (index: number, patch: Partial<EdgeRule>) => {
    setDirty(true);
    setEd((e) => {
      const edges = e.edgeRules.slice();
      edges[index] = { ...edges[index], ...patch };
      return { ...e, edgeRules: edges, __touched: true };
    });
  };

  const addNode = () => {
    const path = paths.find((p) => p.endsWith("[]")) || "";
    const row: KeyRow = { path, kind: "node", type: path.endsWith("[]") ? "item" : "object",
      node_type: "NewType", decision: "node", becomes: "node" };
    setDirty(true);
    setEd((e) => ({ ...e, keyRows: [...e.keyRows, row], __touched: true }));
    setSel({ kind: "node", index: ed.keyRows.length });
  };

  const addEdge = () => {
    const types = nodeIndexes.map((x) => x.r.node_type).filter(Boolean) as string[];
    const edge: EdgeRule = {
      id: `e-${Date.now()}`,
      fromNode: types[0] || "", toNode: types[1] || types[0] || "",
      edgeLabel: "NEW_EDGE", direction: "out", props: [],
    };
    setDirty(true);
    setEd((e) => ({ ...e, edgeRules: [...e.edgeRules, edge], __touched: true }));
    setSel({ kind: "edge", index: ed.edgeRules.length });
  };

  const removeNode = (index: number) => {
    setDirty(true);
    setEd((e) => ({ ...e, keyRows: e.keyRows.filter((_, i) => i !== index), __touched: true }));
    setSel({ kind: "node", index: 0 });
  };
  const removeEdge = (index: number) => {
    setDirty(true);
    setEd((e) => ({ ...e, edgeRules: e.edgeRules.filter((_, i) => i !== index), __touched: true }));
    setSel({ kind: "node", index: 0 });
  };

  /* ──────────────────────────────────────────────────── upload + propose */

  const onFile = (f: File) => {
    // A PDF, Word file and the like is read by its layout reader on the server, as ingestion reads it:
    // the preview then shows its real title, sections, lists, tables and page headers/footers.
    if (paradigm === "unstructured" && !/\.(md|markdown|txt)$/i.test(f.name)) {
      setUploadMsg(`reading ${f.name}…`);
      contextV2Api.readLayout(f).then((r) => {
        setSample(docFromLayout(f.name, r.elements));
        setChunkSample(r.elements.map((e) => e.text).join("\n\n").slice(0, 6000));
        setDocs((d) => [...d.filter((x) => x.id !== f.name), { id: f.name, name: f.name }]);
        setPick(f.name);
        setDirty(true);
        const furniture = r.elements.filter((e) => e.label === "page_header" || e.label === "page_footer").length;
        setUploadMsg(r.engine !== "fallback"
          ? `read ${f.name} (${r.engine} reader): ${r.elements.length} elements, ${furniture} page header/footer(s)`
          : `read ${f.name} as plain text (${r.fallback_reason || "its reader failed"}): structure may be missing`);
      }).catch((e: any) => setUploadMsg(`could not read that file — ${e?.response?.data?.detail || e?.message || "unsupported"}`));
      return;
    }
    const rd = new FileReader();
    rd.onload = () => {
      try {
        const text = String(rd.result);
        if (paradigm === "semistructured") {
          setSample(JSON.parse(text));
        } else if (paradigm === "structured") {
          const rows = parseCSV(text);
          const t = f.name.replace(/\.[^.]+$/, "").replace(/[^a-z0-9_]/gi, "_").toLowerCase();
          setSample((s: any) => ({ ...(s || {}), [t]: rows }));
        } else {
          setChunkSample(text.slice(0, 6000));
          setSample(docFromText(f.name, text));
        }
        setDocs((d) => [...d.filter((x) => x.id !== f.name), { id: f.name, name: f.name }]);
        setPick(f.name);
        setDirty(true);
        setUploadMsg(`loaded ${f.name}`);
      } catch (e: any) {
        setUploadMsg(`could not read that file — ${e?.message || "unsupported"}`);
      }
    };
    rd.readAsText(f);
  };

  /** Dragging from one box onto another IS the declaration — the edge form then names it. */
  const connectTypes = (fromType: string, toType: string) => {
    const edge: EdgeRule = {
      id: `e-${Date.now()}`, fromNode: fromType, toNode: toType,
      edgeLabel: `HAS_${toType.replace(/[^A-Za-z0-9]/g, "").toUpperCase()}`,
      readFrom: fromType, direction: "out", props: [],
    };
    setDirty(true);
    setEd((e) => ({ ...e, edgeRules: [...e.edgeRules, edge], __touched: true }));
    setSel({ kind: "edge", index: ed.edgeRules.length });
  };

  /** Rules from a live schema — keys come from the database, not from a column's name. */
  const proposeFromSchema = () => {
    if (!sqlTables?.length) return;
    const { keyRows, edgeRules } = proposeFromTables(sqlTables);
    setDirty(true);
    setEd((e) => ({ ...e, keyRows, edgeRules, __touched: true }));
    setSel({ kind: "node", index: 0 });
    const fks = edgeRules.length;
    setUploadMsg(`${keyRows.filter((r) => r.kind === "node").length} tables mapped, ` +
      `${fks} foreign key${fks === 1 ? "" : "s"} became named edges — review before saving`);
  };

  const propose = () => {
    if (!sample) { setUploadMsg("load a sample first"); return; }
    const { keyRows, edgeRules } = proposeRules(sample);
    if (!keyRows.length) { setUploadMsg("nothing to propose from that sample"); return; }
    setDirty(true);
    setEd((e) => ({ ...e, keyRows, edgeRules, __touched: true }));
    setSel({ kind: "node", index: 0 });
    setUploadMsg(`proposed ${keyRows.filter((r) => r.kind === "node").length} node rules and ${edgeRules.length} edges — review before saving`);
  };

  /* ───────────────────────────────────────────────────────────── save */

  const save = async () => {
    setSaving(true); setErr(""); setOk("");
    try {
      const editor: EditorModel = { ...ed, __touched: true };
      if (paradigm === "unstructured") { editor.chunking = chunk; editor.elements = elements; editor.metadata = ed.metadata || {}; }
      const payload: any = { ...(rec?.payload || {}), editor };
      if (sample) payload.sample = sample;
      const saved = await contextV2Api.updateSchemaMapping(String(id), { name, payload });
      setRec(saved);
      setDirty(false);
      const compiled = (saved.payload as any)?.structures?.[0]?.edge_rules || [];
      setOk("Saved." +
            (compiled.length ? ` ${compiled.length} value-matched edge rule${compiled.length === 1 ? "" : "s"} compiled.` : ""));
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || "Save failed");
    } finally { setSaving(false); }
  };

  /** Run the sample through the REAL pipeline, bound to this mapping. */
  // Each kind of mapping runs through the ingest path that READS that kind of source. Every sample
  // used to go to the JSON endpoint: a table mapping's rows were walked as a JSON document, a document
  // mapping's parse as records, and a live database — the source the rules came from — had no way
  // to run at all ("Load a sample first").
  const runSample = async () => {
    const live = paradigm === "structured" && !!sqlOrigin && !!sqlTables?.length;
    if (!sample && !live) { setErr("Load a sample first."); return; }
    setRunning(true); setErr(""); setOk("");
    try {
      const stamp = `mapper-${name.replace(/\W+/g, "-").slice(0, 24)}-${Date.now()}`;
      let res: any;
      if (live) {
        res = await contextV2Api.ingestSql({
          ...sqlOrigin!, table_mappings: Object.fromEntries(sqlTables!.map((t) => [t.name, String(id)])) });
      } else if (paradigm === "structured") {
        const runs = [];
        for (const [table, rows] of Object.entries(sample as Record<string, any[]>)) {
          const file = new File([toCSV(rows || [])], `${table}.csv`, { type: "text/csv" });
          runs.push(await contextV2Api.verifyTabularAgainstMappings(file, [String(id)]));
        }
        res = runs[0] || {};
      } else if (paradigm === "unstructured") {
        const file = new File([docText(sample)], `${stamp}.md`, { type: "text/markdown" });
        res = await contextV2Api.uploadDocumentWithPipeline(file, { pipeline: [
          { id: "stage_0", type: "chunking", action: "chunking", source: "document",
            config: { mapping_id: String(id) } }] });
      } else {
        res = await contextV2Api.ingestJsonInline(sample, `${stamp}.json`, { mapping_ids: [String(id)] });
      }
      setOk(`Ingestion dispatched — run ${res.run_id || res.task_id}. Watch it on the Progress page.`);
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || "Could not start the ingestion");
    } finally { setRunning(false); }
  };

  /* ────────────────────────────────────────────────────────────── view */

  if (loading) {
    return <div className="p-6 flex items-center gap-2 text-sm text-stone-500">
      <Loader2 size={16} className="animate-spin" /> loading mapping…
    </div>;
  }

  const meta = PARADIGM_META[paradigm];
  const derived = !!ed.__derived && !ed.__touched;
  const cards = ed.__structures || 0;

  return (
    <div className="p-6 max-w-[1500px] mx-auto">
      <PageHeader
        title={name || "Mapping"}
        icon={meta.icon}
        description={`${meta.label} — ${meta.sub}. Declare the nodes and edges this source becomes.`}
        actions={
          <div className="flex items-center gap-2">
            <Link href="/dashboard/mapper">
              <Button variant="ghost" size="sm" icon={ArrowLeft}>All mappings</Button>
            </Link>
            <Button variant="secondary" size="sm" icon={Play} busy={running} onClick={runSample}>
              Run sample
            </Button>
            <Button variant="primary" size="sm" icon={Save} busy={saving} onClick={save}
              disabled={!dirty && !!rec}>
              {dirty ? "Save changes" : "Saved"}
            </Button>
          </div>
        }
      />

      <div className="flex flex-wrap items-center gap-3 mb-4">
        <input value={name} onChange={(e) => { setName(e.target.value); setDirty(true); }}
          className="px-3 py-2 rounded-lg border border-stone-200 bg-white text-sm w-80
                     focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary/40"
          aria-label="Mapping name" />
        <span className="text-[12px] text-stone-500">
          {meta.label} · {ed.keyRows.filter((r) => r.kind === "node").length} node rules · {ed.edgeRules.length} edge rules
        </span>
      </div>

      {err && (
        <div className="mb-4 flex items-start gap-2 px-3.5 py-3 rounded-xl border border-red-200 bg-red-50 text-sm text-red-800">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" /><div>{err}</div>
        </div>
      )}
      {ok && (
        <div className="mb-4 flex items-start gap-2 px-3.5 py-3 rounded-xl border border-lime-300 bg-lime-50 text-sm text-lime-800">
          <CheckCircle2 size={16} className="mt-0.5 shrink-0" /><div>{ok}</div>
        </div>
      )}
      {derived && (
        <div className="mb-4 px-3.5 py-3 rounded-xl border border-amber-300 bg-amber-50 text-[12.5px] text-amber-900">
          <b className="font-semibold">These rules were derived from the compiled mapping.</b>{" "}
          {cards > 1
            ? <>It was stored as <b>{cards} structure cards</b>, shown here merged into one. Nothing changes until you
               save — and saving writes the merged card, which can differ from the {cards} it replaces.</>
            : <>Nothing changes until you save.</>}
        </div>
      )}

      {paradigm === "unstructured" && (
        <div className="space-y-4 mb-4">
          <Panel title="Extractable document elements & model assignment"
                 count={`${DOC_ELEMENTS.filter((e) => elementOn(e.key)).length} / ${DOC_ELEMENTS.length} on`}>
            <p className="px-3.5 pt-3 text-[12.5px] text-stone-500">
              Turn each element on or off and see the model that reads it. The choices come from your Model Registry.
            </p>
            <ElementsPanel state={elements} models={models}
              onToggle={(k) => patchEditor({ elements: { ...elements, [k]: !elementOn(k) } })} />
          </Panel>
          <Panel title="Chunking & reading rules" count={`→ ${chunkCount} chunks`}>
            <ChunkingPanel cfg={chunk} sample={chunkSample}
              onCfg={(c) => patchEditor({ chunking: { ...chunk, ...c } })}
              onSample={setChunkSample} />
          </Panel>
          <Panel title="Document metadata & links"
                 count={ed.metadata?.header_references === false ? "links off" : "links on"}>
            <DocumentMetadataPanel rules={ed.metadata || {}} onRules={(r) => patchEditor({ metadata: r })} />
          </Panel>
        </div>
      )}

      <div className="mb-4"><KindLegend paradigm={paradigm} /></div>

      <div className="grid xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1fr)_minmax(0,1.15fr)] lg:grid-cols-2 gap-4 items-start">
        <Panel title={paradigm === "structured" ? "Source tables" : paradigm === "unstructured" ? "Parsed document" : "Source sample"}
               count={sample ? undefined : "no sample"}>
          {paradigm === "structured" && sqlTables ? (
            <SqlSchemaSource tables={sqlTables} source={sqlSource} proposed={ed.keyRows.length > 0}
              onReconnect={() => { setSqlTables(null); setSqlSource(""); setSqlOrigin(null); }}
              onPropose={proposeFromSchema} />
          ) : !sample ? (
            <p className="px-3.5 py-6 text-sm text-stone-500">
              {paradigm === "structured"
                ? "No source yet. Connect a database below to read its real keys, or upload a CSV."
                : "No sample loaded. Upload one below to see what this mapping produces."}
            </p>
          ) : paradigm === "structured" ? <TableSource tables={sample} />
            : paradigm === "unstructured" ? <DocSource doc={sample} elementOn={(k) => elementOn(kindForBlock(k))} />
            : <JsonSource data={sample} />}
          {paradigm === "structured" && !sqlTables && (
            <SqlConnectBar onTables={(t, label, origin) => { setSqlTables(t); setSqlSource(label); setSqlOrigin(origin); }} />
          )}
          {!(paradigm === "structured" && sqlTables) && (
            <UploadBar paradigm={paradigm} docs={docs} pick={pick} onPick={setPick}
              onFile={onFile} msg={uploadMsg} onPropose={propose} />
          )}
        </Panel>

        <Panel title="Rules" count={`${nodeIndexes.length} nodes, ${ed.edgeRules.length} edges`}>
          <RuleList rows={ed.keyRows} edges={ed.edgeRules} sel={sel} onSelect={setSel} disabledFor={disabledFor} />
          <div className="flex gap-2 px-3.5 py-3 border-t border-stone-200 bg-stone-50/70">
            <Button variant="secondary" size="sm" icon={Plus} onClick={addNode}>Add node</Button>
            <Button variant="secondary" size="sm" icon={Plus} onClick={addEdge}>Add edge</Button>
          </div>
        </Panel>

        <section className="bg-white border border-stone-300 rounded-xl overflow-hidden shadow-sm">
          <header className="flex items-center gap-2 px-3.5 py-2.5 border-b border-stone-200 bg-stone-50/70">
            <h2 className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
              {sel.kind === "node" ? "Edit node" : "Edit edge"}
            </h2>
            <span className="ml-auto text-[11px] text-stone-500">{sel.kind} rule</span>
          </header>
          {currentNode ? (
            <NodeForm row={currentNode} rows={ed.keyRows} paradigm={paradigm} sample={nodeSample}
              paths={paths} readers={paradigm === "unstructured" ? READERS : undefined}
              disabledNote={disabledFor(currentNode)
                ? `${currentNode.node_type} produces nothing right now — ${disabledFor(currentNode)}.`
                : null}
              onPatch={(p, a) => patchRow(sel.index, p, a)}
              onDelete={() => removeNode(sel.index)} />
          ) : currentEdge ? (
            <EdgeForm edge={currentEdge} rows={ed.keyRows} paradigm={paradigm} sample={sample} existing={existing}
              onPatch={(p) => patchEdge(sel.index, p)}
              onDelete={() => removeEdge(sel.index)} />
          ) : (
            <p className="px-3.5 py-6 text-sm text-stone-500">Select a rule, or add one.</p>
          )}
        </section>
      </div>

      <div className="mt-4">
        <Panel title="Knowledge" count="entity types, identifiers, relationships">
          <KnowledgePanel mappingId={String(id)} payload={rec?.payload} onSaved={setRec} />
        </Panel>
      </div>

      <div className="mt-4">
        <Panel title="Verify against an ingested asset" count="declared vs produced">
          <ConformancePanel mappingId={String(id)} />
        </Panel>
      </div>

      <div className="mt-4">
        <Panel title="Declared graph" count="your rules">
          <DeclaredCanvas
            rows={ed.keyRows} edges={ed.edgeRules} positions={ed.canvas || {}} sel={sel}
            onSelect={setSel}
            onMove={(next) => { setDirty(true); setEd((e) => ({ ...e, canvas: next, __touched: true })); }}
            onConnect={connectTypes}
            onDeleteEdge={removeEdge} />
        </Panel>
      </div>
    </div>
  );
}
