"use client";

// The Extractor editor. ONE authoring surface for every extractor — after the built-in migration a
// built-in IS an ordinary extractor, so editing one is the SAME screen as authoring a custom one.
//
// Layout: section outline · the form · a sticky test-run pane. Every control here writes the same
// state it wrote in the old single-column Studio (see useExtractorDraft.ts); only placement changed:
//   - node / attribute authoring moved from "Live output" into "What it produces",
//   - the preview graph, metrics, warnings and YAML moved into the right-hand pane,
//   - "Resolution" sets how each emitted node type is matched after ingest; guardrail phrases
//     have their own section.
import Link from "next/link";
import { useEffect, useState } from "react";
import { Code, Hexagon, Database, Trash2, X, Save, Activity, ChevronRight } from "lucide-react";
import Button from "@/components/ui/Button";
import type { ExtractorDraft } from "./useExtractorDraft";
import { DEPENDENT_MECHANISMS, INDEPENDENT_MECHANISMS, MECH_META, epLabel, extractorBadge, RELATE_MODES } from "./extractorVocab";
import { Section, Choice, Field, inputCls, codeCls, chipCls } from "./ui";

// Plain titles for relationship decision modes
const MODE_TITLE: Record<string, string> = {
  "relate.llm": "Ask the model", "relate.fixed": "Every pair", "relate.regex": "Named together",
};

// Plain labels for the per-attribute tools. The `m` values are the engine's (d.ATTR_TOOLS).
const TOOL_LABEL: Record<string, string> = {
  llm: "AI prompt", regex: "Pattern", copy: "Copy a field", constant: "Fixed value", source: "From the node",
};
// Plain labels for what the preview reports filled a field.
const MECH_LABEL: Record<string, string> = {
  llm: "AI prompt", ner: "Named entities", regex: "Pattern", dictionary: "Dictionary", keyword: "Keywords",
  copy: "Copy a field", python: "Python", source: "From the node", "per-attribute": "Per-field", constant: "Fixed value",
};

const NODE_COLORS = ["#6b57e0", "#0e9e82", "#2b6fd6", "#c07d10", "#993556", "#3d8a2e"];

/** The display model the old renderPreview computed, unchanged: authored attributes first (so a
 *  just-added one shows instantly), values from the live preview matched by key. */
function previewModel(d: ExtractorDraft, p: any) {
  const declaredFor = (type: string) => (d.emitMode === "node" ? ((d.emitNodes.find((s: any) => s.type === type)?.attributes) || []) : []);
  const placeholder = (k: string) => ({ key: k, mechanism: d.attrOverrides[k]?.mechanism || "llm", value: "", filled: false, model: d.attrOverrides[k]?.model });
  const mergeAttrs = (type: string, pv: any[]) => {
    const byKey: Record<string, any> = {}; (pv || []).forEach((a: any) => byKey[a.key] = a);
    return Array.from(new Set([...(pv || []).map((a: any) => a.key), ...declaredFor(type)])).map(k => byKey[k] || placeholder(k));
  };
  let displayNodes = (p.nodes || []);
  if (d.emitMode === "node" && !displayNodes.length && d.emitNodes.length)
    displayNodes = d.emitNodes.map((s: any) => ({ type: s.type, label: s.type, new: true, attributes: [] }));
  const srcByKey: Record<string, any> = {}; (p.source_attributes || []).forEach((a: any) => srcByKey[a.key] = a);
  const displaySource = Array.from(new Set([...(p.source_attributes || []).map((a: any) => a.key), ...d.emitAttrs.map((a: any) => a.key)])).map(k => srcByKey[k] || placeholder(k));

  const allA = [...displaySource, ...displayNodes.flatMap((n: any) => mergeAttrs(n.type, n.attributes || []))];
  const filled = allA.filter((a: any) => a.filled).length;
  const llmCalls = allA.filter((a: any) => a.mechanism === "llm" || a.mechanism === "ner").length;
  const cost = (llmCalls * 0.12).toFixed(2);   // rough est. per 1k docs (balanced model)
  const existingAttrs = (d.nodeAttrsByType[d.enrichType] || []).filter((k: string) => !displaySource.some((a: any) => a.key === k));
  return { mergeAttrs, displayNodes, displaySource, allA, filled, cost, existingAttrs };
}

/** One field in "What it produces": a table row that expands into its editor. */
function AttrRow({ d, nodeType, a }: { d: ExtractorDraft; nodeType: string; a: any }) {
  const key = a.key;
  const okey = nodeType + "::" + key;
  const isOpen = d.openAttr === okey;
  const ov = d.attrOverrides[key] || {};
  // `a.mechanism` arrives as "source" from the preview for a from_source field — see the old Studio.
  const fromSrc = d.isFromSource(nodeType, key);
  const tool = fromSrc ? "source"
    : (ov.mechanism || (["llm", "regex", "copy", "constant"].includes(a.mechanism) ? a.mechanism : "llm"));
  const { readsNodeType, nodeFields, boundInPipeline } = d;
  return (
    <div className="border-t border-stone-100">
      <button type="button" onClick={() => d.setOpenAttr(isOpen ? "" : okey)} aria-expanded={isOpen}
        className="w-full grid grid-cols-[14px_minmax(80px,1fr)_minmax(0,1.4fr)_auto] gap-3 items-center px-3 py-2 text-left text-[13px] hover:bg-stone-50">
        <ChevronRight size={12} className={`text-stone-400 transition-transform ${isOpen ? "rotate-90" : ""}`} />
        <span className="font-mono text-[12.5px] text-stone-700 truncate" title={key}>{key}</span>
        <span className={`truncate ${a.filled ? "font-medium text-stone-800" : "text-stone-400 italic"}`}>{a.filled ? String(a.value) : "empty on sample"}</span>
        <span className={`px-1.5 py-0.5 rounded text-[11px] font-semibold whitespace-nowrap ${d.mechBadge(a.mechanism)}`} title={a.mechanism}>{MECH_LABEL[a.mechanism] || a.mechanism}</span>
      </button>
      {isOpen && (
        <div className="bg-stone-50 border-t border-dashed border-stone-200 pl-9 pr-3 py-3 space-y-3">
          <div className="flex items-end gap-2">
            <Field label="Field name" className="flex-1">
              <input defaultValue={key} onBlur={e => d.renameAttr(nodeType, key, e.target.value.trim())} className={`${inputCls} font-mono text-xs`} />
            </Field>
            <button type="button" onClick={() => d.removeAttr(key)} className="p-2 text-stone-400 hover:text-rose-600 rounded-lg hover:bg-rose-50" title="Remove field" aria-label={`Remove ${key}`}><Trash2 size={15} /></button>
          </div>
          <div>
            <span className="block text-xs font-semibold text-stone-600 mb-1">How it&rsquo;s filled</span>
            <div className="flex flex-wrap gap-1.5">
              {d.ATTR_TOOLS.map(t => (
                <button key={t.m} type="button" aria-pressed={tool === t.m} onClick={() => {
                    if (t.m === "source") { d.setFromSource(nodeType, key, true); return; }
                    if (fromSrc) d.setFromSource(nodeType, key, false);
                    d.setAttrOv(key, { mechanism: t.m });
                  }}
                  className={`text-xs font-semibold rounded-md px-2.5 py-1 border transition-colors ${tool === t.m ? "border-emerald-500 bg-emerald-50 text-emerald-800" : "border-stone-200 bg-white text-stone-600 hover:border-stone-300"}`}>{TOOL_LABEL[t.m] || t.label}</button>
              ))}
            </div>
          </div>
          {tool === "source" && (
            <p className="text-xs leading-relaxed text-stone-600 bg-white border border-stone-200 rounded-lg p-2.5">
              Taken from{" "}
              {boundInPipeline
                ? <>whichever node the <b>pipeline step</b> binds this to</>
                : <>the <span className="font-mono">{readsNodeType}</span> this runs over</>}
              {" "}&mdash; its own <code className="font-mono">{key}</code> field, when it has one.
              Nothing to fill in: same field name, no prompt, no model, no cost.
              {!boundInPipeline && nodeFields.length > 0 && (
                <> That node carries{" "}
                  <span className="font-mono">{nodeFields.slice(0, 8).join(", ")}</span>
                  {nodeFields.length > 8 ? ` and ${nodeFields.length - 8} more` : ""}
                  {nodeFields.includes(key)
                    ? <> &mdash; including <span className="font-mono">{key}</span>, so this will resolve.</>
                    : <> &mdash; but <b>not <span className="font-mono">{key}</span></b>, so nothing will be
                        taken and the model&rsquo;s answer stands.</>}
                </>
              )}
              <br />
              The model is still <em>asked</em> for it, and its answer is used only when the node has
              nothing: bound to a <span className="font-mono">Turn</span> the speaker is already a
              field there, but over a <span className="font-mono">Chunk</span> with several people
              talking the text is the only evidence there is.
            </p>
          )}
          {tool === "llm" && (<>
            <textarea value={ov.prompt || ""} onChange={e => d.setAttrOv(key, { prompt: e.target.value })} placeholder={`Prompt to fill "${key}". Use {text} / {label}.`} className={`${inputCls} font-mono text-xs h-16 resize-y`} />
            <Field label="Model">
              <select value={ov.model || ""} onChange={e => d.setAttrOv(key, { model: e.target.value })} className={`${inputCls} text-xs`}>
                <option value="">Org default</option>
                {d.genModels.map(m => <option key={m.id} value={m.id}>{m.name} · {m.provider}/{m.model_id}</option>)}
              </select>
            </Field>
          </>)}
          {tool === "regex" && (
            <input value={ov.pattern || ""} onChange={e => d.setAttrOv(key, { pattern: e.target.value })} placeholder="Pattern, e.g. (?P<v>\d+)" className={`${inputCls} font-mono text-xs`} />
          )}
          {tool === "copy" && (<>
            <input list={`fields-${okey}`} value={ov.input || ""} onChange={e => d.setAttrOv(key, { input: e.target.value })}
              placeholder={boundInPipeline ? "Field name — the pipeline step decides which node" : `Field on ${readsNodeType}`}
              className={`${inputCls} font-mono text-xs`} />
            <datalist id={`fields-${okey}`}>{nodeFields.map(f => <option key={f} value={f} />)}</datalist>
            <p className="text-xs leading-relaxed text-stone-500">
              {boundInPipeline
                ? <>Reads a field off whichever node the <b>pipeline step</b> binds this to
                    (<span className="ui">Input: structure node(s) &amp; attributes</span>). Pick a
                    node in <b>Add fields to</b> to see its real field names here.</>
                : <>Reads a field off the <span className="font-mono">{readsNodeType}</span> this runs
                    over{nodeFields.length ? <> — it currently carries{" "}
                    <span className="font-mono">{nodeFields.slice(0, 8).join(", ")}</span>
                    {nodeFields.length > 8 ? ` and ${nodeFields.length - 8} more` : ""}.</> : <>, which has no
                    fields in the graph yet.</>}</>}
            </p>
            <p className="text-xs leading-relaxed text-amber-800 bg-amber-50 border border-amber-200 rounded-lg p-2.5">
              <b>Careful:</b> if the model also returns a field of this name, the model&rsquo;s value
              wins — copy resolves against the record first. Use <b>From the node</b> when the node
              must be the authority.
            </p>
          </>)}
          {tool === "constant" && (
            <input value={ov.value || ""} onChange={e => d.setAttrOv(key, { value: e.target.value })} placeholder="Same value on every node" className={`${inputCls} text-xs`} />
          )}
        </div>
      )}
    </div>
  );
}

function AttrHead() {
  return (
    <div className="grid grid-cols-[14px_minmax(80px,1fr)_minmax(0,1.4fr)_auto] gap-3 px-3 pt-2 pb-1 text-[10.5px] font-semibold uppercase tracking-wider text-stone-400">
      <span /><span>Field</span><span>Sample value</span><span>Filled by</span>
    </div>
  );
}

/** A live graph of the emit — renders actual connected edges with arrowheads and badges. */
function PreviewGraph({ p }: { p: any }) {
  const nodes = (p.nodes || []);
  if (!nodes.length) return null;
  const W = 400, H = Math.max(220, 180 + Math.max(0, nodes.length - 3) * 22);
  const cx = W / 2, cy = H / 2, R = Math.min(cx, cy) - 50;
  const pos: number[][] = nodes.map((_: any, i: number) => {
    const a = (2 * Math.PI * i) / nodes.length - Math.PI / 2;
    return [cx + R * Math.cos(a), cy + R * Math.sin(a)];
  });

  const at: Record<string, number[]> = {
    source: [cx, cy],
    Source: [cx, cy],
    Sample: [cx, cy],
    "the node it read": [cx, cy],
  };
  nodes.forEach((n: any, i: number) => {
    const pt = pos[i];
    if (n.id) at[n.id] = pt;
    at[`node_${i}`] = pt;
    at[`${n.label} (${n.type})`] = pt;
    if (n.rec_index !== undefined) {
      at[`${n.type}_${n.rec_index}`] = pt;
    }
    if (!at[n.label]) at[n.label] = pt;
    if (!at[n.type]) at[n.type] = pt;
  });

  const resolvePoint = (ep: any, epId: string | undefined, recIdx: number | undefined) => {
    if (epId && at[epId]) return at[epId];
    if (ep && at[ep]) return at[ep];
    if (!ep) return null;
    const s = String(ep).trim();
    if (["source", "sample", "the node it read"].includes(s.toLowerCase())) return [cx, cy];
    if (recIdx !== undefined && at[`${s}_${recIdx}`]) return at[`${s}_${recIdx}`];
    const found = nodes.find((n: any) =>
      n.id === s ||
      `${n.label} (${n.type})` === s ||
      n.label === s ||
      n.type === s ||
      `${n.type} (created node)` === s
    );
    return found ? at[found.id || `node_${nodes.indexOf(found)}`] : null;
  };

  const edges = (p.edges || []).map((e: any, i: number) => {
    const pA = resolvePoint(e.from, e.from_id, e.rec_index);
    const pB = resolvePoint(e.to, e.to_id, e.rec_index);
    return { ...e, pA, pB, key: "e" + i };
  }).filter((e: any) => e.pA && e.pB && (e.pA[0] !== e.pB[0] || e.pA[1] !== e.pB[1]));

  return (
    <div className="bg-stone-50 border border-stone-100 rounded-lg overflow-hidden">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Preview of the nodes and relationships this extractor produces">
        <defs>
          <marker id="preview-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M 0 1.5 L 8 5 L 0 8.5 z" fill="#6366f1" />
          </marker>
        </defs>

        {/* Real configured edges with arrowheads */}
        {edges.map((e: any) => {
          const { pA, pB, key } = e;
          const dx = pB[0] - pA[0], dy = pB[1] - pA[1];
          const dist = Math.sqrt(dx * dx + dy * dy);
          const offset = dist > 45 ? 22 : 0;
          const x2 = pB[0] - (dist > 0 ? (dx / dist) * offset : 0);
          const y2 = pB[1] - (dist > 0 ? (dy / dist) * offset : 0);
          const mx = (pA[0] + x2) / 2, my = (pA[1] + y2) / 2;
          const relLabel = String(e.type || e.rel || "(relationship)").trim();
          const badgeW = Math.max(48, Math.min(120, relLabel.length * 6 + 16));
          const displayLabel = relLabel.length > 18 ? relLabel.slice(0, 16) + "…" : relLabel;

          return (
            <g key={key}>
              <line x1={pA[0]} y1={pA[1]} x2={x2} y2={y2} stroke="#6366f1" strokeWidth={1.5} markerEnd="url(#preview-arrow)" />
              <rect x={mx - badgeW / 2} y={my - 8} width={badgeW} height={16} rx={8} fill="#ffffff" stroke="#cbd5e1" strokeWidth={1} />
              <text x={mx} y={my + 3.5} textAnchor="middle" fontSize={8} fontFamily="monospace" fill="#4f46e5" fontWeight={700}>
                {displayLabel}
              </text>
            </g>
          );
        })}

        {/* Source anchor node in the center */}
        <g>
          <rect x={cx - 42} y={cy - 16} width={84} height={32} rx={8} fill="#f1f5f9" stroke="#64748b" strokeWidth={1.5} />
          <text x={cx} y={cy + 4} textAnchor="middle" fontSize={11} fontWeight={600} fill="#1e293b">Source</text>
        </g>

        {/* Emitted graph nodes */}
        {nodes.map((n: any, i: number) => {
          const [x, y] = pos[i], c = NODE_COLORS[i % NODE_COLORS.length];
          const short = (n.label || n.type || "Node").length > 15 ? (n.label || n.type).slice(0, 14) + "…" : (n.label || n.type);
          const w = Math.max(64, short.length * 7 + 18);
          return (
            <g key={"n" + i}>
              <rect x={x - w / 2} y={y - 18} width={w} height={36} rx={8} fill="#fff" stroke={c} strokeWidth={1.5} />
              <text x={x} y={y - 2} textAnchor="middle" fontSize={11} fontWeight={600} fill={c}>{short}</text>
              <text x={x} y={y + 11} textAnchor="middle" fontSize={8} fontFamily="monospace" fill={c} opacity={0.7}>
                {n.type} ({n.attributes?.length || 0})
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

type Check = { level: "ok" | "warn" | "error" | "idle"; note?: string };
const DOT: Record<Check["level"], string> = { ok: "bg-emerald-500", warn: "bg-amber-500", error: "bg-rose-500", idle: "bg-stone-300" };

export default function ExtractorStudio({ d }: { d: ExtractorDraft }) {
  // Which edges have their "Only when…" row opened while still empty (UI state only).
  const [whenOpen, setWhenOpen] = useState<Set<number>>(new Set());

  // Unsaved changes = the spec exactly as it would be saved, compared with the spec when the editor
  // opened. Read-only: it only decides what the save bar says and whether closing asks first.
  const specNow = JSON.stringify(d.buildSpec());
  const [baseline] = useState(specNow);
  const dirty = specNow !== baseline;
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  useEffect(() => {
    if (!confirmDiscard) return;
    const t = setTimeout(() => setConfirmDiscard(false), 4000);
    return () => clearTimeout(t);
  }, [confirmDiscard]);
  useEffect(() => {
    if (!dirty) return;
    const h = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);
  const requestClose = () => {
    if (dirty && !confirmDiscard) { setConfirmDiscard(true); return; }
    d.setIsAdding(false);
  };
  const p = d.preview || { nodes: [], edges: [], source_attributes: [] };
  const pm = previewModel(d, p);
  const { mechRef, mechPrompt, editingBuiltin, sourceMode } = d;

  const updateEdge = (i: number, patch: Record<string, any>) => {
    // Immutable update in place of mutating with Object.assign to protect against frozen state objects
    const arr = d.emitEdges.map((edge: any, idx: number) =>
      idx === i ? { ...edge, ...patch } : edge
    );
    d.setEmitEdges(arr);
  };
  const warnCount = (d.preview?.warnings || []).length + (d.previewErr ? 1 : 0);
  // Section checks for the outline and the save bar. They flag problems; they never block a save
  // the old page allowed (only a missing name, which it already refused).
  const seedBad = mechRef === "seed.literal" && (() => { try { return !Array.isArray(JSON.parse(d.mechSeedItems)); } catch { return true; } })();
  const checks: [string, string, Check][] = [
    ["s-name", "Name", d.extName.trim() ? { level: "ok" } : { level: "error", note: "Needs a name to save" }],
    ["s-input", "What it reads", { level: "ok" }],
    ["s-logic", "How it works",
      seedBad ? { level: "error", note: "Fixed records aren't a valid JSON list — it will seed nothing" }
      : mechRef === "unstructured.python" && !d.mechCode.trim() ? { level: "warn", note: "No extract() code yet" }
      : mechRef === "unstructured.regex" && !d.mechPattern.trim() ? { level: "warn", note: "No pattern yet" }
      : { level: "ok" }],
    ["s-output", "What it produces",
      // Errors outrank warnings — a test-run warning must not hide a node type with no name.
      d.emitNodes.some((n: any) => !String(n.type || "").trim()) ? { level: "error", note: "A node type has no name" }
      : warnCount ? { level: "warn", note: `Test run reported ${warnCount} issue${warnCount > 1 ? "s" : ""}` }
      // Runtime prompts (pipeline.answer / pipeline.memory) run at answer or session-save time and produce no nodes by design.
      : (!mechRef.startsWith("pipeline.") && !d.emitNodes.length && !d.emitAttrs.length && !d.emitAttrsRest.length && !d.emitEdges.length) ? { level: "warn", note: "Produces nothing yet" }
      : { level: "ok" }],
    ["s-edges", "Relationships",
      d.emitEdges.some((e: any) => !String(e.type ?? e.rel ?? "").trim() && e.mode !== "relate.llm") ? { level: "error", note: "A relationship has no type" }
      : d.emitEdges.some((e: any) => (e.from === "match_on" && (!e.fromMatchNode || !e.fromMatchField)) || (e.to === "match_on" && (!e.toMatchNode || !e.toMatchField)))
        ? { level: "warn", note: "A matched node is missing its type or field" }
      : d.emitEdges.length ? { level: "ok" } : { level: "idle" }],
    ["s-resolution", "Resolution",
      d.emitNodes.some((n: any) => String(n.id || "").trim()) ? { level: "ok" } : { level: "idle" }],
  ];
  // The test-run status already has its own place in the save bar, so it is not repeated as a problem there.
  const problems = checks.filter(([id, , c]) => (c.level === "error" || c.level === "warn") && !(id === "s-output" && c.level === "warn" && warnCount));

  const renderEndpointOptions = (currentVal?: string) => {
    const isCustom = currentVal && !["emitted", "source", "match_on"].includes(currentVal) && !d.emitNodes.some((n: any) => n.type === currentVal);
    return (
      <>
        <option value="source">the node it read (source node)</option>
        {d.emitNodes.map((n: any) => (
          <option key={n.type} value={n.type}>{n.type} (created node)</option>
        ))}
        {isCustom && <option value={currentVal}>{currentVal} (node)</option>}
        <option value="emitted">{d.emitNodes.length > 0 ? "Default emitted node" : "the new node"}</option>
        <option value="match_on">an existing node, found by a field</option>
      </>
    );
  };

  return (
    <div className="space-y-4">
      {/* header */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-xs text-stone-400">Extractors / {editingBuiltin ? "Built-in" : d.activeTab === "built-in" ? "Built-in" : "Your extractors"}</div>
          <h2 className="font-mono text-lg font-semibold text-stone-800 flex flex-wrap items-center gap-2 [overflow-wrap:anywhere]">
            {d.extName ? d.extName.replace(/^builtin:/, "") : "New extractor"}
            {editingBuiltin && <span className={`${chipCls} bg-stone-100 text-stone-500`}>Built-in</span>}
          </h2>
        </div>
        <button type="button" onClick={requestClose} className="p-2 rounded-lg text-stone-400 hover:text-stone-700 hover:bg-stone-100" aria-label={confirmDiscard ? "Discard changes and close" : "Close editor"}>
          {confirmDiscard ? <span className="text-xs font-semibold text-rose-600 px-1">Discard changes?</span> : <X size={18} />}
        </button>
      </div>

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px] 2xl:grid-cols-[170px_minmax(0,1fr)_400px] items-start">
        {/* outline */}
        <nav className="hidden 2xl:block sticky top-0 space-y-0.5" aria-label="Editor sections">
          {checks.map(([id, label, c]) => (
            <a key={id} href={`#${id}`} title={c.note} className="flex items-start gap-2.5 px-2.5 py-1.5 rounded-lg text-[13px] text-stone-600 hover:bg-white hover:text-stone-900">
              <span className={`w-2 h-2 mt-1.5 rounded-full shrink-0 ${DOT[c.level]}`} />
              <span className="min-w-0">
                <span className="block">{label}</span>
                {c.note && <span className={`block text-[11px] leading-snug ${c.level === "error" ? "text-rose-600" : "text-amber-700"}`}>{c.note}</span>}
              </span>
            </a>
          ))}
        </nav>

        {/* form */}
        <div className="space-y-4 min-w-0">
          {/* Name — a built-in's name is its identity in the catalog (pipelines reference it). */}
          <section id="s-name" className="scroll-mt-4 bg-white border border-stone-200 rounded-xl px-5 py-4">
            <Field label="Extractor name"
              hint={editingBuiltin ? <>Built-in: pipelines refer to it by this name, so the name and its mechanism (<code>{mechRef || "—"}</code>) stay fixed. Prompts, models, per-field tools, produced nodes/relationships and review gating are all yours to change.</> : undefined}>
              <input value={d.extName} onChange={e => d.setExtName(e.target.value)} readOnly={editingBuiltin}
                className={`${inputCls} font-mono ${editingBuiltin ? "bg-stone-100 text-stone-500 cursor-not-allowed" : ""}`}
                placeholder="e.g. topic_from_summaries" />
            </Field>
          </section>

          {/* What it reads */}
          <Section id="s-input" lane="input" title="What it reads">
            <div className="grid sm:grid-cols-2 gap-2">
              <Choice on={sourceMode === "dependent"} title="Nodes already in the graph" desc="Runs over existing nodes" tech="bound · source: select"
                onClick={() => { d.setSourceMode("dependent"); d.setMechRef(DEPENDENT_MECHANISMS[0]); }} />
              <Choice on={sourceMode === "independent"} title="Nothing — it creates its own records" desc="Self-seeded, runs once" tech="independent · source: null"
                onClick={() => { d.setSourceMode("independent"); d.setMechRef(INDEPENDENT_MECHANISMS[0]); }} />
            </div>
            <p className="text-[12.5px] text-stone-500 mt-3 leading-relaxed max-w-[70ch]">
              {sourceMode === "dependent"
                ? <>You pick <b>which</b> nodes and fields it reads when you add it as a step in a <Link href="/dashboard/pipelines" className="underline font-semibold text-teal-700">pipeline</Link> — so one extractor works across every pipeline&rsquo;s vocabulary. No node types to name here.</>
                : <>Source is <code>null</code>. The mechanism pulls from an external connector, a fixed list, or a generative prompt and produces standalone nodes.</>}
            </p>
          </Section>

          {/* How it works */}
          <Section id="s-logic" lane="logic" title="How it works" aside={<code className="text-[11px] text-stone-400">{mechRef}</code>}>
            {editingBuiltin ? (
              <>
                {/* A built-in's `ref` is owned by the catalog (seed_builtin_templates re-applies it on every
                    ingest), so the tiles are shown read-only with the one it runs highlighted. */}
                {d.BUILTIN_SHAPE(mechRef, mechPrompt) ? (
                  <div className="grid sm:grid-cols-3 gap-2">
                    {[
                      { title: "Attributes", label: "Each field its own tool", desc: "Every field picks its own method" },
                      { title: "Coded · Python", label: "Python code", desc: "One extract() function" },
                      { title: "Prompt", label: "One prompt", desc: "A single call fills the records" },
                    ].map((o) => (
                      <Choice key={o.title} on={o.title === d.BUILTIN_SHAPE(mechRef, mechPrompt)} title={o.label} desc={o.desc}
                        disabled locked icon={o.title === "Coded · Python" ? <Code size={12} /> : undefined} />
                    ))}
                  </div>
                ) : (
                  <div className="flex items-center gap-2">
                    <span className={`${chipCls} ${extractorBadge({ mechanism: { ref: mechRef, model_capable: d.mechModelCapable } }).cls}`}>
                      {extractorBadge({ mechanism: { ref: mechRef, model_capable: d.mechModelCapable } }).label}
                    </span>
                    <code className="text-[11px] text-stone-400">{mechRef}</code>
                  </div>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-3 text-[12.5px] text-stone-600 bg-stone-50 border border-stone-200 rounded-lg p-3">
                  <p className="flex-1 min-w-[240px] leading-relaxed">
                    <b className="text-stone-800">Built-in — mechanism locked</b> (<code className="text-[11px]">{mechRef}</code>).
                    Everything else here is yours to change: the prompt, the model, each field&apos;s own tool,
                    and the nodes and relationships it produces. Those edits survive re-seeding; the
                    mechanism would not. To run a different mechanism, duplicate it.
                  </p>
                  <Button size="sm" onClick={d.duplicateAsCustom}>Duplicate as custom</Button>
                </div>
              </>
            ) : sourceMode === "independent" ? (
              <Field label="Where the records come from">
                <select value={mechRef} onChange={e => d.setMechRef(e.target.value)} className={inputCls}>
                  {INDEPENDENT_MECHANISMS.map((r) => <option key={r} value={r}>{MECH_META[r]?.label || r}{MECH_META[r]?.hint ? ` — ${MECH_META[r].hint}` : ""}</option>)}
                </select>
              </Field>
            ) : (
              <div className="grid sm:grid-cols-3 gap-2">
                <Choice on={mechRef !== "unstructured.python" && !mechPrompt.trim()} title="Each field its own tool" desc="Every field picks its own method below"
                  onClick={() => { d.setMechRef("unstructured.llm"); d.setMechPrompt(""); }} />
                <Choice on={mechRef === "unstructured.python"} title="Python code" desc="You write one extract() function" icon={<Code size={12} />}
                  onClick={() => d.setMechRef("unstructured.python")} />
                <Choice on={mechRef !== "unstructured.python" && !!mechPrompt.trim()} title="One prompt" desc="A single model call fills every field"
                  onClick={() => { d.setMechRef("unstructured.llm"); if (!mechPrompt.trim()) d.setMechPrompt(d.autoPrompt()); }} />
              </div>
            )}

            {/* ── the loaded mechanism's own knobs (same conditions as before) ── */}
            <div className="space-y-3 mt-3">
              {mechRef === "unstructured.attributes" && (
                <p className="text-[12.5px] text-stone-600 leading-relaxed bg-emerald-50/60 border border-emerald-100 rounded-lg p-3">
                  <b className="text-emerald-800">No single prompt.</b> Every field this extractor writes is made on its
                  own — its own tool (AI prompt / pattern / copy / fixed value), its own prompt and its own model — so one field can be
                  retuned or switched off without touching the rest. Edit them under <a href="#s-output" className="font-semibold underline">What it produces</a>.
                </p>
              )}
              {mechRef === "seed.literal" && (
                <Field label="Fixed records — the items it seeds" hint="A JSON list. Each object is one record → one produced node, with its keys as fields. No model involved.">
                  <textarea value={d.mechSeedItems} onChange={e => d.setMechSeedItems(e.target.value)} spellCheck={false} className={`${codeCls} h-40`} />
                </Field>
              )}
              {mechRef === "unstructured.keyword" && (
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs font-semibold text-stone-600">Top N terms</span>
                  <input type="number" min={1} max={50} value={d.mechTopN} onChange={e => d.setMechTopN(Number(e.target.value))} className={`${inputCls} w-20`} />
                  <span className="text-xs text-stone-400">Picking a model below switches it from frequency to the model.</span>
                </div>
              )}
              {mechRef === "unstructured.regex" && (
                <Field label="Pattern">
                  <input value={d.mechPattern} onChange={e => d.setMechPattern(e.target.value)} placeholder="Pattern, e.g. (?P<v>\d{4}-\d{2}-\d{2})" className={`${inputCls} font-mono text-xs`} />
                </Field>
              )}
              {mechRef === "unstructured.dictionary" && (
                <div className="grid sm:grid-cols-2 gap-3">
                  <Field label="Term list name"><input value={d.mechGazetteer} onChange={e => d.setMechGazetteer(e.target.value)} placeholder="Gazetteer name" className={`${inputCls} font-mono text-xs`} /></Field>
                  <Field label="Terms"><textarea value={d.mechTerms} onChange={e => d.setMechTerms(e.target.value)} placeholder="Terms, comma- or newline-separated" className={`${inputCls} font-mono text-xs h-20 resize-y`} /></Field>
                </div>
              )}
              {mechRef !== "unstructured.python" && mechRef !== "unstructured.attributes" && mechRef !== "seed.literal" && (
                <Field label={<span>Prompt {!mechPrompt.trim() && <span className="font-normal text-stone-400">— blank = built from the fields below</span>}</span>}
                  hint={<>Keep placeholders like {"{text}"}, {"{label}"} or {"{table_name}"} intact — they are filled from the node this runs over.</>}>
                  <textarea value={mechPrompt} onChange={e => d.setMechPrompt(e.target.value)} spellCheck={false}
                    placeholder={d.autoPrompt()} className={`${inputCls} font-mono text-xs h-40 leading-relaxed resize-y`} />
                </Field>
              )}
              {/* Model — not for `unstructured.attributes`: each field resolves its own model. */}
              {mechRef !== "seed.literal" && mechRef !== "unstructured.regex" && mechRef !== "unstructured.attributes" && (
                <Field label={<><span>Model</span><a href="/dashboard/models" className="text-[11px] font-semibold text-emerald-700 hover:underline">+ Register a model</a></>}
                  hint="The extractor's default. Any single field can use a different one in its own row.">
                  <select value={d.mechModel} onChange={e => d.setMechModel(e.target.value)} className={inputCls}>
                    <option value="">Org default generation model</option>
                    {d.genModels.map((m) => <option key={m.id} value={m.id}>{m.name} · {m.provider}/{m.model_id}{m.is_default ? " (default)" : ""}</option>)}
                  </select>
                </Field>
              )}
              {mechRef === "unstructured.python" && (
                <div className="space-y-2.5">
                  <Field label="extract(inputs) → list of records">
                    <textarea value={d.mechCode} onChange={e => d.setMechCode(e.target.value)} spellCheck={false} className={`${codeCls} h-72`}
                      placeholder={"def extract(inputs, cortex):\n    text = inputs.get('text') or ''\n    # cortex is the SDK handle (model-capable only): cortex.generate(prompt, json=True)\n    return [{'phrase': text[:20], 'kind': 'relative'}]"} />
                  </Field>
                  <label className="flex items-start gap-2 cursor-pointer">
                    <input type="checkbox" checked={d.mechModelCapable} onChange={e => d.setMechModelCapable(e.target.checked)} className="mt-1 accent-emerald-600" />
                    <span className="text-[12.5px] text-stone-600 leading-relaxed"><b className="text-stone-800">Let this code call the model</b> — passes the <code>cortex</code> SDK handle so <code>extract(inputs, cortex)</code> can call <code>cortex.generate(prompt, json=True)</code> (routed through your Model Registry) to e.g. detect phrases, then normalize in code. Off = deterministic, no model.</span>
                  </label>
                  <p className="text-xs text-stone-400 leading-relaxed">
                    Define <code>extract(inputs) -&gt; list[dict]</code>. <code>inputs</code> holds the node’s <b>text</b> + every field you bind to this step in the pipeline; each returned dict is one record whose keys become the produced node’s fields (define them in <b>What it produces</b>). Allowed imports: re, json, datetime, math, dateparser, parsedatetime, dateutil…
                  </p>
                </div>
              )}
            </div>
          </Section>

          {/* What it produces — node + attribute authoring (was "Live output") */}
          <Section id="s-output" lane="output" title="What it produces"
            aside={
              // These pick what you are EDITING, not an exclusive mode — an extractor may do both.
              <div className="inline-flex bg-stone-100 rounded-lg p-0.5" role="group" aria-label="Edit">
                {[{ m: "source", title: "Add fields to the node it reads" }, { m: "node", title: "Create new nodes" }].map(o => (
                  <button key={o.m} type="button" aria-pressed={d.emitMode === o.m} onClick={() => d.setEmitMode(o.m as "source" | "node")}
                    className={`px-3 py-1.5 rounded-md text-[12.5px] font-semibold transition-colors ${d.emitMode === o.m ? "bg-white text-stone-900 shadow-sm" : "text-stone-500 hover:text-stone-800"}`}>{o.title}</button>
                ))}
              </div>
            }>
            <p className="text-[12.5px] text-stone-500 mb-3">{d.emitMode === "source"
              ? "Adds fields onto the existing node it runs over. Each field picks its own tool, prompt and model — click a row to change it."
              : "Creates a new node (connect it under Relationships). Each field picks its own tool, prompt and model — click a row to change it."}</p>

            <div className="space-y-3">
              {d.emitMode === "node" && (
                <>
                  {d.emitNodes.length === 0 && <p className="text-sm text-stone-400 py-1">Add a node type to start creating.</p>}
                  {d.emitNodes.map((spec: any, si: number) => {
                    const inst = (p.nodes || []).find((n: any) => n.type === spec.type);
                    const attrs = pm.mergeAttrs(spec.type, inst?.attributes || []);
                    return (
                      <div key={si} className="border border-stone-200 rounded-lg overflow-hidden">
                        <div className="flex flex-wrap items-center gap-2 px-3 py-2 bg-stone-50 border-b border-stone-100 text-xs text-stone-500">
                          <Hexagon size={13} className="text-rose-500 shrink-0" />
                          <span>Node type</span>
                          <input value={spec.type} onChange={e => d.setNode(si, { type: e.target.value })} className={`${inputCls} w-32 text-xs font-semibold text-rose-700`} placeholder="Type" />
                          <span>identified by</span>
                          <input value={spec.id || ""} onChange={e => d.setNode(si, { id: e.target.value })} className={`${inputCls} w-28 text-xs font-mono`} placeholder="name" />
                          <label className="flex items-center gap-1.5 font-semibold text-stone-600 cursor-pointer"
                            title="Records that name the same thing merge into ONE node (entity resolution / one hub across the corpus). Off = every record is its own node. Works for any type — not just entities.">
                            <input type="checkbox" checked={!!spec.dedup} onChange={e => d.setNode(si, { dedup: e.target.checked })} className="accent-emerald-600" />
                            Merge duplicates
                          </label>
                          {inst && <span className="text-stone-500 truncate">→ {inst.label}</span>}
                          <button type="button" onClick={() => d.removeNode(si)} className="ml-auto p-1 text-stone-400 hover:text-rose-600" title="Remove node" aria-label={`Remove ${spec.type}`}><Trash2 size={14} /></button>
                        </div>
                        {attrs.length > 0 && <AttrHead />}
                        {attrs.map((a: any) => <AttrRow key={a.key} d={d} nodeType={spec.type} a={a} />)}
                        <button type="button" onClick={() => d.addAttrToNode(spec.type)} className="w-full text-left text-[12.5px] font-semibold text-emerald-700 border-t border-dashed border-stone-200 px-3 py-2 hover:bg-emerald-50">+ Add a field to {spec.type || "this node"}</button>
                      </div>
                    );
                  })}
                  <Button size="sm" onClick={d.addNode}>+ Node type</Button>
                </>
              )}

              {/* Shown whenever there ARE source attributes, not only in enrich mode. */}
              {(d.emitMode === "source" || d.emitAttrs.length > 0) && (
                <div className="space-y-2">
                  {d.emitMode === "node" && d.emitAttrs.length > 0 && (
                    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2.5">
                      <p className="flex-1 min-w-[240px] text-[12.5px] text-amber-900 leading-relaxed">
                        <b>This extractor also adds fields to the node it reads.</b> Both will run. If you meant the
                        records to become nodes only, clear these &mdash; otherwise every run also stamps{" "}
                        <span className="font-mono">{d.emitAttrs.map((a: any) => a.key).join(", ")}</span>{" "}
                        onto the node it read, taking the values from the <b>first record only</b>.
                      </p>
                      <button type="button" onClick={() => { d.setEmitAttrs([]); d.setEmitAttrsRest([]); }}
                        className="text-xs font-semibold text-amber-900 bg-white border border-amber-300 rounded-md px-2.5 py-1 hover:bg-amber-100">
                        Clear these fields
                      </button>
                    </div>
                  )}
                  <div className="border border-stone-200 rounded-lg overflow-hidden">
                    <div className="flex flex-wrap items-center gap-2 px-3 py-2 bg-stone-50 border-b border-stone-100 text-xs text-stone-500">
                      <Database size={13} className="text-teal-600 shrink-0" />
                      <span>Add fields to</span>
                      <select value={d.enrichType} onChange={e => d.setEnrichType(e.target.value)} className={`${inputCls} w-auto text-xs font-medium text-teal-800`}>
                        <option value="">Any node (set in pipeline)</option>
                        {d.availableNodes.map(n => <option key={n} value={n}>{n}</option>)}
                      </select>
                      <span className={`${chipCls} ml-auto bg-teal-50 text-teal-700`}>existing node</span>
                    </div>
                    {(pm.existingAttrs.length > 0 || pm.displaySource.length > 0) && <AttrHead />}
                    {pm.existingAttrs.map((k: string) => (
                      <div key={k} className="grid grid-cols-[14px_minmax(80px,1fr)_minmax(0,1.4fr)_auto] gap-3 items-center px-3 py-2 border-t border-stone-100 text-[13px] opacity-70">
                        <span />
                        <span className="font-mono text-[12.5px] text-stone-500 truncate" title={k}>{k}</span>
                        <span className="text-stone-400 italic truncate">kept as is</span>
                        <span className="px-1.5 py-0.5 rounded text-[11px] font-semibold bg-stone-100 text-stone-500">Already there</span>
                      </div>
                    ))}
                    {pm.displaySource.map((a: any) => <AttrRow key={a.key} d={d} nodeType="__source" a={a} />)}
                    <button type="button" onClick={() => d.addAttrToNode("__source")} className="w-full text-left text-[12.5px] font-semibold text-teal-700 border-t border-dashed border-stone-200 px-3 py-2 hover:bg-teal-50">+ Add a field to {d.enrichType || "the node"}</button>
                  </div>
                </div>
              )}
            </div>
          </Section>

          {/* Relationships */}
          <Section id="s-edges" lane="rel" title="Relationships to draw"
            aside={<Button size="sm" onClick={() => d.setEmitEdges([...d.emitEdges, { type: "HAS_TOPIC", from: "source", to: "emitted" }])}>+ Relationship</Button>}>
            <p className="text-[12.5px] text-stone-500 mb-3 max-w-[70ch]">Connect the node it read, a node it creates, or an existing node found by one of its fields. A relationship can use a field you just added.</p>
            <div className="space-y-2">
              {d.emitEdges.length === 0 && <p className="text-sm text-stone-400">No relationships.</p>}
              {d.emitEdges.map((e: any, i: number) => {
                const fromLabel = typeof e.from === "string" ? (e.from === "source" ? "the node it read" : e.from) : epLabel(e.from);
                const toLabel = typeof e.to === "string" ? (e.to === "emitted" ? (d.emitNodes.length > 0 ? "Default emitted node" : "the new node") : e.to) : epLabel(e.to);
                const edgeRel = String(e.type || e.rel || "RELATION");

                return (
                <div key={i} className="border border-stone-200 rounded-lg px-3 py-2.5 space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <select value={typeof e.from === "string" ? e.from : "source"} onChange={ev => updateEdge(i, { from: ev.target.value })} className={`${inputCls} w-auto text-xs`} aria-label="From">{renderEndpointOptions(typeof e.from === "string" ? e.from : undefined)}</select>
                    {/* Read either spelling, write `type`, drop `rel` so the shape converges on save. */}
                    <div className="relative flex items-center">
                      <input value={e.type ?? e.rel ?? ""} aria-label="Relationship type"
                        onChange={ev => {
                          const val = ev.target.value;
                          const arr = d.emitEdges.map((edge: any, idx: number) =>
                            idx === i ? { ...edge, type: val } : edge
                          );
                          d.setEmitEdges(arr);
                        }}
                        className={`${inputCls} w-52 text-xs font-mono font-bold text-indigo-700 text-center`}
                        placeholder={e.mode === "relate.llm" ? "LLM generated (or $field)" : "HAS_TOPIC"} />
                    </div>
                    <select value={typeof e.to === "string" ? e.to : "emitted"} onChange={ev => updateEdge(i, { to: ev.target.value })} className={`${inputCls} w-auto text-xs`} aria-label="To">{renderEndpointOptions(typeof e.to === "string" ? e.to : undefined)}</select>
                    <button type="button" onClick={() => { d.setEmitEdges(d.emitEdges.filter((_: any, j: number) => j !== i)); setWhenOpen(new Set()); }}
                      className="ml-auto p-1 text-stone-400 hover:text-rose-600" title="Remove relationship" aria-label="Remove relationship"><Trash2 size={14} /></button>
                  </div>
                  {(["from", "to"] as const).filter(side => e[side] === "match_on").map(side => (
                    <div key={side} className="flex flex-wrap items-center gap-2 pl-3 border-l-2 border-teal-300 text-xs text-stone-500">
                      <span className="font-semibold text-teal-700">{side === "from" ? "From" : "To"}:</span>
                      <select value={e[`${side}MatchNode`]} onChange={ev => updateEdge(i, { [`${side}MatchNode`]: ev.target.value })} className={`${inputCls} w-auto text-xs`}>
                        <option value="">Node type…</option>
                        {(d.availableNodes || []).map(n => <option key={n} value={n}>{n}</option>)}
                      </select>
                      <span>where</span>
                      <input value={e[`${side}MatchField`]} onChange={ev => updateEdge(i, { [`${side}MatchField`]: ev.target.value })} className={`${inputCls} w-24 text-xs font-mono`} placeholder="field" />
                      <span>=</span>
                      <input value={e[`${side}MatchValue`]} onChange={ev => updateEdge(i, { [`${side}MatchValue`]: ev.target.value })} className={`${inputCls} w-28 text-xs font-mono`} placeholder="$var" />
                    </div>
                  ))}
                  {e.when || whenOpen.has(i) ? (
                    <div className="flex items-center gap-2 pl-3 border-l-2 border-stone-200 text-xs text-stone-500">
                      <span className="font-semibold">Only when</span>
                      <input value={e.when || ""} onChange={ev => { if (!whenOpen.has(i)) setWhenOpen(new Set(whenOpen).add(i)); updateEdge(i, { when: ev.target.value }); }} className={`${inputCls} flex-1 text-xs font-mono`} placeholder="Optional condition (e.g. $parent != null)" />
                    </div>
                  ) : (
                    <button type="button" onClick={() => setWhenOpen(new Set(whenOpen).add(i))} className="text-xs font-semibold text-emerald-700 hover:underline">+ Only when…</button>
                  )}

                  {/* Per-edge Connection rule / LLM guidance */}
                  <div className="pt-2 border-t border-stone-100 space-y-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-400">Connection rule:</span>
                      <div className="inline-flex rounded-lg border border-stone-200 p-0.5 bg-stone-50">
                        <button
                          type="button"
                          onClick={() => updateEdge(i, { mode: "relate.fixed" })}
                          className={`px-2.5 py-1 text-xs font-medium rounded-md transition-colors ${(!e.mode || e.mode === "relate.fixed") ? "bg-white text-stone-900 shadow-sm font-semibold" : "text-stone-500 hover:text-stone-800"}`}
                        >
                          Every pair (fixed)
                        </button>
                        <button
                          type="button"
                          onClick={() => updateEdge(i, { mode: "relate.llm" })}
                          className={`px-2.5 py-1 text-xs font-medium rounded-md transition-colors ${e.mode === "relate.llm" ? "bg-emerald-50 text-emerald-800 border border-emerald-300 shadow-sm font-semibold" : "text-stone-500 hover:text-stone-800"}`}
                        >
                          Ask the model (LLM)
                        </button>
                        <button
                          type="button"
                          onClick={() => updateEdge(i, { mode: "relate.regex" })}
                          className={`px-2.5 py-1 text-xs font-medium rounded-md transition-colors ${e.mode === "relate.regex" ? "bg-white text-stone-900 shadow-sm font-semibold" : "text-stone-500 hover:text-stone-800"}`}
                        >
                          Named together (text)
                        </button>
                      </div>
                      {edgeRel.startsWith("$") ? (
                        <span className="text-[11px] font-mono text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">
                          Dynamic edge type from {edgeRel}
                        </span>
                      ) : (e.mode === "relate.llm" && !String(e.type || e.rel || "").trim()) ? (
                        <span className="text-[11px] font-mono text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">
                          ✨ Output of LLM will be the edge itself
                        </span>
                      ) : null}
                    </div>

                    {/* When "Ask the model" is selected for this specific edge */}
                    {e.mode === "relate.llm" && (
                      <div className="p-3 rounded-lg bg-emerald-50/50 border border-emerald-200/80 space-y-2.5">
                        <div className="flex flex-wrap items-center justify-between gap-1 text-xs">
                          <span className="font-semibold text-emerald-900">
                            ✨ Output of LLM will be the edge itself
                          </span>
                          <span className="text-[11px] text-emerald-700 font-mono">
                            Input to model: Root node ({sourceMode === "dependent" ? "the node it read" : "context"}) + {fromLabel} + {toLabel}
                          </span>
                        </div>
                        <p className="text-[11.5px] text-emerald-800 leading-normal">
                          The model extracts or generates the relationship edge connecting <b>{fromLabel}</b> and <b>{toLabel}</b> from the context text (e.g. &ldquo;is capital of&rdquo;, &ldquo;acquired by&rdquo;, or &ldquo;diagnosed with&rdquo;). You can also set a dynamic field like <code className="font-bold font-mono">$predicate</code> in the relationship box above.
                        </p>
                        <Field label="Instructions for the model (optional)">
                          <textarea
                            value={e.prompt || ""}
                            onChange={ev => updateEdge(i, { prompt: ev.target.value })}
                            className={`${inputCls} h-16 resize-y bg-white text-xs font-mono`}
                            placeholder={`Tell the model how to extract the relationship between ${fromLabel} and ${toLabel}. e.g. "Extract the exact predicate or relationship phrase connecting them from the text." Leave blank for automatic extraction.`}
                          />
                        </Field>
                        <Field
                          label={<><span>Model</span><a href="/dashboard/models" className="text-[11px] font-semibold text-emerald-700 hover:underline">+ Register a model</a></>}
                          hint="Entities are referenced to the model by index, so duplicate names never collide."
                        >
                          <select value={e.model || ""} onChange={ev => updateEdge(i, { model: ev.target.value })} className={`${inputCls} bg-white text-xs`}>
                            <option value="">Org default generation model</option>
                            {(d.genModels || []).map((m: any) => (
                              <option key={m.id} value={m.id}>
                                {m.name} · {m.provider}/{m.model_id}{m.is_default ? " (default)" : ""}
                              </option>
                            ))}
                          </select>
                        </Field>
                      </div>
                    )}

                    {/* When "Named together" is selected */}
                    {e.mode === "relate.regex" && (
                      <p className="text-[11.5px] text-stone-500 pl-1">
                        Draws an edge between <b className="text-stone-700">{fromLabel}</b> and <b className="text-stone-700">{toLabel}</b> only when both labels appear together in the text of the node it read.
                      </p>
                    )}
                  </div>
                </div>
                );
              })}
            </div>

            {/* Attribute merges the per-field rows cannot express — shown so nothing is invisible,
                saved back exactly as authored. */}
            {d.emitAttrsRest.length > 0 && (
              <div className="mt-3 bg-indigo-50/50 border border-indigo-100 rounded-lg p-3">
                <p className="text-[10.5px] font-semibold uppercase tracking-wider text-indigo-700 mb-1">Advanced field rules · kept as authored</p>
                {d.emitAttrsRest.map((a: any, i: number) => (
                  <div key={i} className="text-xs font-mono text-stone-600 truncate" title={JSON.stringify(a)}>
                    {typeof a.set === "string"
                      ? <>set <b>every field the model returns</b> ({a.set}) on {epLabel(a.on)}</>
                      : <>set {Object.keys(a.set || {}).join(", ") || "—"} on {epLabel(a.on)}</>}
                  </div>
                ))}
              </div>
            )}
          </Section>

          {/* Resolution: how each node type this extractor emits is matched after the asset is ingested */}
          <Section id="s-resolution" lane="safety" title="Resolution">
            <ResolutionSettings d={d} />
          </Section>

          {d.extName.includes("answer.generate") && (
            <Section id="s-guardrails" lane="safety" title="Guardrails">
              <div className="space-y-2">
                <p className="text-xs font-semibold text-stone-700">Guardrail phrase library (abstention)</p>
                <p className="text-xs text-stone-500">If the model’s reply contains any of these phrases it’s treated as “I don’t know” (abstain), even if it dressed up the non-answer. Comma- or newline-separated.</p>
                <div className="grid sm:grid-cols-2 gap-3">
                  <Field label="Non-answer phrases">
                    <textarea value={d.guardNonAnswer} onChange={e => d.setGuardNonAnswer(e.target.value)} className={`${inputCls} h-20 text-xs font-mono`} placeholder="i don't know, not mentioned, no data" />
                  </Field>
                  <Field label={<span>Refusal / deflection phrases <span className="font-normal text-stone-400">(match at any length)</span></span>}>
                    <textarea value={d.guardRefusal} onChange={e => d.setGuardRefusal(e.target.value)} className={`${inputCls} h-20 text-xs font-mono`} placeholder="as an ai, i can't assist, respect your privacy" />
                  </Field>
                </div>
              </div>
            </Section>
          )}
        </div>

        {/* test-run pane */}
        <aside className="space-y-3 xl:sticky xl:top-0 xl:max-h-[calc(100dvh-9rem)] xl:overflow-y-auto" aria-label="Test run">
          <div className="bg-white border border-stone-200 rounded-xl p-4">
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-[13px] font-semibold text-stone-800 flex items-center gap-1.5"><Activity size={14} className="text-emerald-600" /> Try it on a sample</h3>
              <span className="text-[11px] text-stone-400">{d.previewing ? <span className="animate-pulse">running…</span> : "updates as you edit"}</span>
            </div>
            <textarea value={d.sampleText} onChange={e => d.setSampleText(e.target.value)} placeholder="Paste a representative line of your data…"
              className={`${inputCls} h-24 text-xs font-mono leading-relaxed resize-y`} />
            <div className="flex items-center justify-between gap-2 mt-2">
              <span className="text-[11.5px] text-stone-400">Read-only dry-run — nothing is written to the graph.</span>
              <Button size="sm" onClick={d.runPreview} disabled={d.previewing}>{d.previewing ? "Previewing…" : "Update"}</Button>
            </div>
          </div>

          <div className="bg-white border border-stone-200 rounded-xl p-4 space-y-3">
            <h3 className="text-[13px] font-semibold text-stone-800">Result</h3>
            <div className="grid grid-cols-4 gap-px bg-stone-200 rounded-lg overflow-hidden">
              {[["Nodes", pm.displayNodes.length], ["Fields", `${pm.filled}/${pm.allA.length}`], ["Est $/1k", `$${pm.cost}`]].map(([k, v]: any, i) => (
                <div key={i} className="bg-white px-2.5 py-2">
                  <div className="text-[10px] font-semibold uppercase tracking-wider text-stone-400 truncate">{k}</div>
                  <div className="text-base font-semibold text-stone-800 tabular-nums">{v}</div>
                </div>
              ))}
            </div>
            {d.previewErr && <p className="text-xs text-rose-700 bg-rose-50 border border-rose-100 rounded-lg p-2.5">{d.previewErr}</p>}
            {(d.preview?.warnings || []).length > 0 && (
              <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 space-y-1">
                {(d.preview.warnings as string[]).map((wn, i) => (
                  <p key={i} className="text-xs text-rose-800 leading-relaxed">{wn}</p>
                ))}
              </div>
            )}
            {p.error && <p className="text-xs text-amber-800 bg-amber-50 border border-amber-100 rounded-lg p-2.5">Mechanism note: {p.error}</p>}
            <PreviewGraph p={p} />
            {!pm.displayNodes.length && !pm.allA.length && <p className="text-xs text-stone-400">Nothing produced yet — add a node type or a field.</p>}
          </div>

          <details className="bg-white border border-stone-200 rounded-xl overflow-hidden group">
            <summary className="px-4 py-2.5 text-[13px] font-semibold text-stone-700 cursor-pointer flex items-center gap-2 list-none">
              <Code size={14} /> YAML this compiles to <ChevronRight size={13} className="ml-auto text-stone-400 group-open:rotate-90 transition-transform" />
            </summary>
            <div className="px-3 pb-3 overflow-x-auto"><pre className="text-[11px] leading-relaxed text-emerald-300 font-mono bg-stone-900 rounded-lg p-3">{d.generateYaml()}</pre></div>
          </details>
        </aside>
      </div>

      {/* save bar — sticks to the bottom of the scrolling dashboard pane */}
      <div className="sticky bottom-0 z-10 -mx-3 sm:-mx-4 lg:-mx-6 px-3 sm:px-4 lg:px-6 py-3 bg-white/90 backdrop-blur border-t border-stone-200 flex flex-wrap items-center gap-3">
        <span className="text-[13px] text-stone-600 flex flex-wrap items-center gap-x-2 gap-y-1 min-w-0">
          <span className={`w-2 h-2 rounded-full ${dirty ? "bg-amber-500" : "bg-stone-300"}`} />
          <b className="font-semibold">{dirty ? "Unsaved changes" : "No changes"}</b>
          <span className="text-stone-400">· {d.previewing ? "test run in progress…" : d.previewErr ? "test run failed — see Result" : warnCount ? `test run reported ${warnCount} issue${warnCount > 1 ? "s" : ""}` : d.preview ? "test run OK" : "no test run yet"}</span>
          {problems.length > 0 && (
            <a href={`#${problems[0][0]}`} className={`${problems.some(([, , c]) => c.level === "error") ? "text-rose-600" : "text-amber-700"} hover:underline`}>
              · {problems[0][2].note}{problems.length > 1 ? ` (+${problems.length - 1} more)` : ""}
            </a>
          )}
        </span>
        <span className="flex-1" />
        <Button variant="ghost" onClick={requestClose} className={confirmDiscard ? "!text-rose-600" : ""}>{confirmDiscard ? "Discard changes?" : "Cancel"}</Button>
        <Button variant="primary" icon={Save} onClick={d.handleSaveExtraction} disabled={!d.extName}>{editingBuiltin ? "Save changes" : "Save to Registry"}</Button>
      </div>
    </div>
  );
}

const RELATION_LABELS: [string, string][] = [
  ["SAME", "Link records that are the same"], ["SUPPORTS", "Confirm matching values"],
  ["EXTENDS", "Add missing values"], ["CONTRADICTS", "Flag differing values for review"],
];
const ALL_RELATIONS = RELATION_LABELS.map(([r]) => r);

/** Per emitted node type: what identifies it, which attributes are compared, which outcomes apply,
 *  how a match is scored, and the gate. Saved on the node (`id` and `resolve`), then on its type. */
function ResolutionSettings({ d }: { d: any }) {
  const nodes = (d.emitNodes || []).map((n: any, i: number) => ({ n, i })).filter(({ n }: any) => n.type);
  if (!nodes.length) {
    return <p className="text-[12.5px] text-stone-500">Resolution applies to the node types this extractor creates. Add a node type under <b>What it produces</b> to set it up.</p>;
  }
  return (
    <div className="space-y-4">
      <p className="text-[12.5px] text-stone-500">After an asset is ingested, each new node is matched with your existing data by the fields that identify it. What happens is shown in the <a href="/dashboard/review" className="text-emerald-700 font-semibold underline">Review Queue</a>: applied matches under Auto-resolution, the rest under Conflicts.</p>
      {nodes.map(({ n, i }: any) => {
        const r = n.resolve && typeof n.resolve === "object" ? n.resolve : {};
        const set = (patch: any) => d.setNode(i, { resolve: { ...r, ...patch } });
        const idKeys = String(n.id || "").split(",").map((s: string) => s.trim()).filter(Boolean);
        const attrs: string[] = Array.from(new Set<string>((n.attributes || []).map((a: any) => typeof a === "string" ? a : a?.name).filter(Boolean)))
          .filter((a) => !idKeys.includes(a));
        const ignore: string[] = r.ignore || [];
        const relations: string[] = r.relations || ALL_RELATIONS;
        const scorer = r.scorer === "similar" ? "similar" : "exact";
        const auto = typeof r.auto === "number" ? r.auto : 0.95, review = typeof r.review === "number" ? r.review : 0.8;
        return (
          <div key={i} className="border border-stone-200 rounded-lg p-3 space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-xs text-stone-500">
              <span className="font-semibold text-rose-700 text-sm">{n.type}</span>
              <span>identified by</span>
              <input value={n.id || ""} onChange={e => d.setNode(i, { id: e.target.value })} aria-label={`${n.type} identified by`}
                className="px-2 py-1 rounded-md border border-stone-200 text-xs font-mono w-44" placeholder="e.g. customer_no" />
              {!idKeys.length && <span className="text-amber-700">Not resolved until you name the identifying fields.</span>}
            </div>
            {idKeys.length > 0 && (
              <>
                <div className="flex flex-wrap items-center gap-1.5 text-xs">
                  <span className="font-semibold text-stone-600 mr-1">Compare</span>
                  {attrs.length === 0 && <span className="text-stone-400">all fields of matched records</span>}
                  {attrs.map((a) => {
                    const on = !ignore.includes(a);
                    return (
                      <button key={a} type="button" aria-pressed={on}
                        onClick={() => set({ ignore: on ? [...ignore, a] : ignore.filter((k) => k !== a) })}
                        className={`px-2 py-0.5 rounded-full border font-mono ${on ? "border-emerald-300 bg-emerald-50 text-emerald-800" : "border-stone-200 text-stone-400 line-through"}`}>{a}</button>
                    );
                  })}
                </div>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-stone-600">
                  {RELATION_LABELS.map(([rel, label]) => (
                    <label key={rel} className="flex items-center gap-1.5 cursor-pointer">
                      <input type="checkbox" checked={relations.includes(rel)} className="accent-emerald-600"
                        onChange={e => set({ relations: e.target.checked ? [...relations, rel] : relations.filter((x) => x !== rel) })} />
                      {label}
                    </label>
                  ))}
                </div>
                <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-stone-600">
                  <label className="flex items-center gap-1.5">Scorer
                    <select value={scorer} onChange={e => set({ scorer: e.target.value })} className="px-2 py-1 rounded-md border border-stone-200 bg-white">
                      <option value="exact">Exact (same identity)</option>
                      <option value="similar">Similar (close spellings)</option>
                    </select>
                  </label>
                  {scorer === "similar" && (
                    <>
                      <label className="flex items-center gap-1.5" title="A match scoring at least this is applied automatically">Apply automatically at
                        <input type="number" min={0} max={1} step={0.05} value={auto} aria-label="Apply automatically at"
                          onChange={e => set({ auto: Math.min(1, Math.max(0, Number(e.target.value))), review: Math.min(review, Number(e.target.value)) })}
                          className="w-16 px-2 py-1 rounded-md border border-stone-200 font-mono" />
                      </label>
                      <label className="flex items-center gap-1.5" title="A match scoring at least this waits for your decision; lower is not a match">Send for review at
                        <input type="number" min={0} max={1} step={0.05} value={review} aria-label="Send for review at"
                          onChange={e => set({ review: Math.min(auto, Math.max(0, Number(e.target.value))) })}
                          className="w-16 px-2 py-1 rounded-md border border-stone-200 font-mono" />
                      </label>
                    </>
                  )}
                  {scorer === "exact" && <span className="text-stone-400">An exact match scores 1.0 and is applied automatically.</span>}
                </div>
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
