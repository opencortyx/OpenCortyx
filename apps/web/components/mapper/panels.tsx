"use client";

/**
 * Presentational pieces of the unified Mapper.
 *
 * House style throughout: warm-paper + stone neutrals, evergreen `primary`, honey `accent` — the
 * same tokens every other dashboard page uses. The only colour these panels add is SEMANTIC: each
 * of the four ways an endpoint can be found gets its own hue, because "how is this end resolved?"
 * is the question the edge form exists to answer and it has to read at a glance.
 */

import { useMemo } from "react";
import {
  Braces, Table as TableIcon, FileText, Boxes, GitBranch, Link2, Hash, AlertTriangle,
  CheckCircle2, Trash2, Plus, Upload, Sparkles,
} from "lucide-react";
import Button from "@/components/ui/Button";
import {
  ChunkCfg, EdgeRule, EditorModel, GNode, KeyRow, Materialized, Paradigm, RefKind,
  chunkText, fieldsOf, valueOf, walk,
} from "./model";

/* ─────────────────────────────────────────────────── the four reference kinds */

export const KIND_LABEL: Record<RefKind, string> = {
  self: "this row", context: "in context", join: "by value", position: "by position",
};
export const KIND_BLURB: Record<RefKind, string> = {
  self: "the node this rule already produced",
  context: "the nearest enclosing node",
  join: "a value matched against another type's key",
  position: "the same index in a sibling array",
};
export const KIND_STYLE: Record<RefKind, string> = {
  self: "text-stone-600 bg-stone-100 border-stone-200",
  context: "text-teal-700 bg-teal-50 border-teal-200",
  join: "text-amber-700 bg-amber-50 border-amber-200",
  position: "text-lime-700 bg-lime-50 border-lime-200",
};
export const KIND_SOLID: Record<RefKind, string> = {
  self: "bg-stone-600 text-white border-stone-600",
  context: "bg-teal-700 text-white border-teal-700",
  join: "bg-amber-600 text-white border-amber-600",
  position: "bg-lime-700 text-white border-lime-700",
};

/** Which endpoint modes make sense per paradigm — and WHY the others do not. */
export const KINDS_FOR: Record<Paradigm, Partial<Record<RefKind, string | true>>> = {
  semistructured: { self: true, context: true, join: true, position: true },
  structured: {
    self: true, join: true,
    context: "tables do not nest — a parent is reached by key, not by containment",
    position: "rows have no ordinal correspondence — use a key",
  },
  unstructured: {
    self: true, context: true, position: true,
    join: "here a value match IS entity resolution — an extracted name against the identity index",
  },
};

export function KindLegend({ paradigm }: { paradigm: Paradigm }) {
  const kinds = KINDS_FOR[paradigm];
  return (
    <div className="flex flex-wrap gap-2">
      {(["self", "context", "join", "position"] as RefKind[]).map((k) => {
        const on = !!kinds[k];
        const note = typeof kinds[k] === "string" ? (kinds[k] as string) : null;
        return (
          <span key={k}
            className={`inline-flex items-baseline gap-2 text-xs px-2.5 py-1.5 rounded-lg border bg-white
                        ${on ? "border-stone-200" : "border-stone-200 opacity-60"}`}>
            <b className={`text-[11px] font-semibold uppercase tracking-wide ${KIND_STYLE[k].split(" ")[0]}`}>
              {KIND_LABEL[k]}
            </b>
            <span className="text-stone-500">{note || (on ? KIND_BLURB[k] : "not applicable here")}</span>
          </span>
        );
      })}
    </div>
  );
}

/* ─────────────────────────────────────────────────────────── layout helpers */

export function Panel({ title, count, children, actions }: {
  title: string; count?: string; children: React.ReactNode; actions?: React.ReactNode;
}) {
  return (
    <section className="bg-white border border-stone-200 rounded-xl overflow-hidden">
      <header className="flex items-center gap-2 px-3.5 py-2.5 border-b border-stone-200 bg-stone-50/70">
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">{title}</h2>
        <div className="ml-auto flex items-center gap-2">
          {count && <span className="text-[11px] text-stone-500 tabular-nums">{count}</span>}
          {actions}
        </div>
      </header>
      {children}
    </section>
  );
}

export const Label = ({ children }: { children: React.ReactNode }) => (
  <span className="block text-[10.5px] font-semibold uppercase tracking-wider text-stone-500 mb-1.5">{children}</span>
);
export const Hint = ({ children }: { children: React.ReactNode }) => (
  <p className="text-[11.5px] text-stone-500 mt-1.5 leading-relaxed">{children}</p>
);
const inputCls =
  "w-full px-2.5 py-2 rounded-lg border border-stone-200 bg-white text-sm text-stone-800 " +
  "focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary/40";
export const monoCls = "font-mono text-[12.5px]";

export function Chip({ on, onClick, children, tone = "primary" }: {
  on: boolean; onClick: () => void; children: React.ReactNode; tone?: "primary" | "id";
}) {
  const active = tone === "id"
    ? "border-amber-400 bg-amber-50 text-amber-800"
    : "border-primary/50 bg-primary/10 text-primary";
  return (
    <button type="button" onClick={onClick} aria-pressed={on}
      className={`font-mono text-[11.5px] px-2.5 py-1.5 rounded-md border transition-colors
                  ${on ? `border-solid ${active} font-medium` : "border-dashed border-stone-300 bg-white text-stone-500 hover:border-stone-400"}`}>
      {children}
    </button>
  );
}

/* ───────────────────────────────────────────────────────────── source panels */

export function JsonSource({ data }: { data: any }) {
  const html = useMemo(() => {
    const esc = (s: string) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]!));
    return esc(JSON.stringify(data, null, 2) || "")
      .replace(/"([^"]+)"(\s*:)/g, '<span class="text-primary">"$1"</span>$2')
      .replace(/:\s("(?:[^"\\]|\\.)*")/g, ': <span class="text-lime-700">$1</span>')
      .replace(/:\s(-?\d+(?:\.\d+)?)/g, ': <span class="text-amber-700">$1</span>');
  }, [data]);
  return <pre className="m-0 px-3.5 py-3 text-[11.5px] leading-relaxed font-mono overflow-auto max-h-[440px]"
               dangerouslySetInnerHTML={{ __html: html }} />;
}

const colRole = (table: string, name: string, sample: any) => {
  const n = name.toLowerCase(), sing = table.replace(/s$/, "");
  if (n === "id" || n === `${sing}_id`) return "identity";
  if (n.endsWith("_id")) return "fk";
  if (typeof sample === "number") return "measure";
  if (typeof sample === "string" && sample.length > 60) return "text";
  return "dimension";
};
const ROLE_TONE: Record<string, string> = {
  identity: "text-amber-700 bg-amber-50", fk: "text-teal-700 bg-teal-50",
  measure: "text-lime-700 bg-lime-50", text: "text-primary bg-primary/10",
  dimension: "text-stone-600 bg-stone-100",
};

export function TableSource({ tables }: { tables: Record<string, any[]> }) {
  return (
    <div className="overflow-auto max-h-[440px]">
      {Object.entries(tables).map(([t, rows]) => {
        if (!Array.isArray(rows) || !rows.length) return null;
        const cols = Object.keys(rows[0]);
        return (
          <div key={t} className="px-3.5 py-3 border-b border-stone-100 last:border-b-0">
            <h3 className="font-mono text-[12px] font-semibold text-stone-800 mb-2">
              {t}<span className="ml-2 font-normal text-stone-500">{rows.length} rows</span>
            </h3>
            <div className="overflow-x-auto">
              <table className="w-full border-collapse font-mono text-[11px]">
                <thead>
                  <tr>{cols.map((c) => (
                    <th key={c} className="text-left font-semibold text-stone-500 border-b border-stone-200 pb-1.5 pr-3 whitespace-nowrap">
                      {c}
                      <span className={`ml-1.5 px-1 py-px rounded text-[8.5px] font-sans font-bold tracking-wide ${ROLE_TONE[colRole(t, c, rows[0][c])]}`}>
                        {colRole(t, c, rows[0][c]).slice(0, 3).toUpperCase()}
                      </span>
                    </th>))}</tr>
                </thead>
                <tbody>
                  {rows.slice(0, 6).map((r, i) => (
                    <tr key={i}>{cols.map((c) => (
                      <td key={c} className="py-1 pr-3 border-b border-stone-100 text-stone-600 whitespace-nowrap tabular-nums">
                        {String(r[c] ?? "")}
                      </td>))}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}
    </div>
  );
}

const EKIND_TONE: Record<string, string> = {
  heading: "text-teal-700 bg-teal-50", paragraph: "text-stone-600 bg-stone-100",
  table: "text-lime-700 bg-lime-50", figure: "text-primary bg-primary/10", picture: "text-primary bg-primary/10",
  list_item: "text-sky-700 bg-sky-50", caption: "text-amber-700 bg-amber-50", footnote: "text-amber-700 bg-amber-50",
  page_header: "text-violet-700 bg-violet-50", page_footer: "text-violet-700 bg-violet-50",
};
const headingTag = (level?: number) => (level === 0 ? "title" : level ? `heading ${level}` : "heading");

export function DocSource({ doc, elementOn }: { doc: any; elementOn: (kind: string) => boolean }) {
  const d = doc?.document || {};
  return (
    <div className="px-3.5 py-3 overflow-auto max-h-[440px]">
      <div className="font-semibold text-sm text-stone-800">{d.title}</div>
      <div className="font-mono text-[11.5px] text-stone-500 mb-3">
        {d.doc_id} · {d.pages} page{d.pages === 1 ? "" : "s"}{d.revised ? ` · revised ${d.revised}` : ""}
      </div>
      {(d.sections || []).map((s: any, i: number) => (
        <div key={i} className="border-l-2 border-stone-200 pl-3 mb-3"
             style={{ marginLeft: `${Math.max(0, (s.level ?? 1) - 1) * 14}px` }}>
          <h4 className="text-[12.5px] font-semibold text-stone-800 mb-1.5">
            <span className={`text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded mr-2 ${EKIND_TONE.heading}`}>{headingTag(s.level)}</span>
            {s.heading}
          </h4>
          {(s.blocks || []).map((b: any, j: number) => (
            <div key={j} className={`flex gap-2 items-start mb-1.5 text-[11.5px] text-stone-600 ${elementOn(b.kind) ? "" : "opacity-40"}`}>
              <span className={`text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded whitespace-nowrap mt-px ${EKIND_TONE[b.kind] || EKIND_TONE.paragraph}`}>
                {b.kind}
              </span>
              <span>{String(b.text || "").slice(0, 110)}{String(b.text || "").length > 110 ? "…" : ""}</span>
            </div>
          ))}
        </div>
      ))}
      {!!(d.page_furniture || []).length && (
        <>
          <Label>Page headers &amp; footers</Label>
          {d.page_furniture.map((b: any, j: number) => (
            <div key={j} className="flex gap-2 items-start mb-1.5 text-[11.5px] text-stone-600">
              <span className={`text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded whitespace-nowrap mt-px ${EKIND_TONE[b.kind]}`}>
                {b.kind === "page_header" ? "header" : "footer"} · p{b.page}
              </span>
              <span>{b.text}</span>
            </div>
          ))}
        </>
      )}
      {!!(doc?.mentions || []).length && (
        <>
          <Label>Extracted mentions</Label>
          {doc.mentions.map((m: any, i: number) => (
            <div key={i} className="font-mono text-[11.5px] text-stone-500 py-0.5">
              {m.entity} <span className="opacity-70">({m.kind})</span> → {m.in_section}
            </div>
          ))}
        </>
      )}
    </div>
  );
}

/* ──────────────────────────────────────────────────────────────── rule list */

export function RuleList({ rows, edges, sel, onSelect, disabledFor }: {
  rows: KeyRow[]; edges: EdgeRule[];
  sel: { kind: "node" | "edge"; index: number };
  onSelect: (s: { kind: "node" | "edge"; index: number }) => void;
  disabledFor?: (r: KeyRow) => string | null;
}) {
  const nodeIdx = rows.map((r, i) => ({ r, i })).filter((x) => x.r.kind === "node");
  return (
    <div className="flex flex-col">
      {nodeIdx.map(({ r, i }) => {
        const why = disabledFor?.(r) || null;
        const active = sel.kind === "node" && sel.index === i;
        return (
          <button key={`n${i}`} type="button" onClick={() => onSelect({ kind: "node", index: i })}
            className={`grid grid-cols-[auto_1fr_auto] items-center gap-2.5 w-full text-left px-3.5 py-2.5
                        border-b border-stone-100 last:border-b-0 transition-colors
                        ${active ? "bg-accent" : "hover:bg-stone-50"} ${why ? "opacity-50" : ""}`}>
            <Boxes size={14} className="text-primary shrink-0" />
            <span className="min-w-0">
              <span className="font-semibold text-[13.5px] text-stone-800">{r.node_type || "(unnamed)"}</span>
              <span className="block font-mono text-[11.5px] text-stone-500 break-all">
                {r.path || "«record root»"}
                {r.attachToNode ? ` · attach → ${r.attachToNode}` : ""}
              </span>
            </span>
            <span className={`text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded whitespace-nowrap
                             ${why ? "text-stone-500 bg-stone-100" : r.edge_type ? KIND_STYLE.context : KIND_STYLE.self}`}>
              {why || (r.edge_type ? "node + edge" : "node")}
            </span>
          </button>
        );
      })}
      {edges.map((e, i) => {
        const kind: RefKind = e.fromKey || e.toKey ? "join" : "context";
        const active = sel.kind === "edge" && sel.index === i;
        return (
          <button key={`e${i}`} type="button" onClick={() => onSelect({ kind: "edge", index: i })}
            className={`grid grid-cols-[auto_1fr_auto] items-center gap-2.5 w-full text-left px-3.5 py-2.5
                        border-b border-stone-100 last:border-b-0 transition-colors
                        ${active ? "bg-accent" : "hover:bg-stone-50"}`}>
            {kind === "join" ? <Link2 size={14} className="text-amber-600 shrink-0" />
                             : <GitBranch size={14} className="text-teal-600 shrink-0" />}
            <span className="min-w-0">
              <span className="font-semibold text-[13.5px] text-stone-800">{e.edgeLabel || "(unnamed)"}</span>
              <span className="block text-[11.5px] text-stone-500 break-all">
                {e.fromNode} → {e.toNode}{e.toKey ? ` · on ${e.toKey}` : ""}
              </span>
            </span>
            <span className={`text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded border whitespace-nowrap ${KIND_STYLE[kind]}`}>
              {KIND_LABEL[kind]}
            </span>
          </button>
        );
      })}
      {!nodeIdx.length && !edges.length && (
        <p className="px-3.5 py-6 text-sm text-stone-500">
          No rules yet. Load a sample and choose <b>Propose rules</b>, or add a node.
        </p>
      )}
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────── forms */

export function NodeForm({ row, rows, paradigm, sample, paths, readers, onPatch, onDelete, disabledNote }: {
  row: KeyRow; rows: KeyRow[]; paradigm: Paradigm; sample: any; paths: string[];
  readers?: string[];
  onPatch: (p: Partial<KeyRow>, attrPatch?: { path: string; patch: Partial<KeyRow> }) => void;
  onDelete: () => void;
  disabledNote?: string | null;
}) {
  const prefix = row.path ? `${row.path}.` : "";
  const attrs = rows.filter((r) => r.kind === "attribute" && r.path.startsWith(prefix) &&
    !r.path.slice(prefix.length).includes(".") && !r.path.slice(prefix.length).includes("[]") &&
    r.path.slice(prefix.length).length > 0);
  const fields = fieldsOf(sample);
  const known = new Set(attrs.map((a) => a.path.slice(prefix.length)));
  const unused = fields.filter((f) => f !== "$value" && !known.has(f));
  const pkTrap = paradigm === "structured"
    ? attrs.find((a) => (a.role === "id" || a.becomes === "id") && /_id$/i.test(a.path.slice(prefix.length)))
    : null;
  // Only a table's key identifies its rows; in JSON an `id` field is an attribute like any other.
  const keyed = (a: KeyRow) => paradigm !== "semistructured" && (a.role === "id" || a.becomes === "id");
  const unitLabel = paradigm === "structured" ? "Source table" : paradigm === "unstructured" ? "Element path" : "Source path";
  const fieldWord = paradigm === "structured" ? "column" : paradigm === "unstructured" ? "attribute" : "field";

  return (
    <div className="p-3.5">
      <div className="mb-3.5">
        <Label>Node label</Label>
        <input className={inputCls} value={row.node_type || ""}
          onChange={(e) => onPatch({ node_type: e.target.value, auto_type: false })} />
        <Hint>The type this rule creates — the label, the ontology entry, and the name every edge uses to reach it.</Hint>
      </div>

      {disabledNote && (
        <div className="mb-3.5 px-3 py-2.5 rounded-lg border border-amber-300 bg-amber-50 text-[12px] text-amber-800">
          <b className="font-semibold">{disabledNote}</b>
        </div>
      )}

      <div className="mb-3.5">
        <Label>{unitLabel}</Label>
        <select className={`${inputCls} ${monoCls}`} value={row.path}
          onChange={(e) => onPatch({ path: e.target.value })}>
          {Array.from(new Set([row.path].concat(paths))).map((p) => (
            <option key={p} value={p}>{p || "«record root»"}</option>
          ))}
        </select>
        <Hint>
          {paradigm === "structured" ? "One node per row."
            : paradigm === "unstructured" ? "One node per parsed element that passes the filter."
            : <>One node per match. <code className="font-mono">[]</code> means every element of that array.</>}
        </Hint>
      </div>

      {paradigm === "unstructured" && readers && (
        <div className="mb-3.5">
          <Label>Read with</Label>
          <select className={`${inputCls} ${monoCls}`} value={(row as any).reader || ""}
            onChange={(e) => onPatch({ ...(({ reader: e.target.value || undefined }) as any) })}>
            <option value="">(none — store as text)</option>
            {readers.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
      )}

      {/* A JSON node's id is generated from where it sits; its id fields are plain attributes, and
          whether two records are the same thing is the Resolution Engine's call. */}
      {paradigm !== "semistructured" && <div className="mb-3.5">
        <Label>Identity — {attrs.filter((a) => a.role === "id" || a.becomes === "id").length} selected</Label>
        <div className="flex flex-wrap gap-1.5">
          {attrs.map((a) => {
            const f = a.path.slice(prefix.length);
            const on = a.role === "id" || a.becomes === "id";
            return (
              <Chip key={a.path} on={on} tone="id"
                onClick={() => onPatch({}, { path: a.path, patch: on ? { role: "metadata", becomes: "label" } : { role: "id", becomes: "id" } })}>
                {f}
              </Chip>
            );
          })}
          {!attrs.length && <span className="text-[11.5px] text-stone-500">no scalar {fieldWord}s under this path</span>}
        </div>
        <Hint>
          Deterministic id = type + these {fieldWord}s. Pick more than one for a composite key.
          Leave empty and the node is identified by where it sits.
        </Hint>
      </div>}

      {paradigm !== "semistructured" && row.path.endsWith("[]") && (
        <label className="flex items-center gap-2 mb-3.5 text-[12px] text-stone-700">
          <input type="checkbox" className="accent-[hsl(var(--primary))]" checked={row.id_scope === "parent"}
            onChange={(e) => onPatch({ id_scope: e.target.checked ? "parent" : undefined })} />
          id is unique only <b className="font-semibold">within its parent</b>
          <span className="text-stone-500">(turn “1” exists in every conversation)</span>
        </label>
      )}

      {pkTrap && (
        <div className="mb-3.5 px-3 py-2.5 rounded-lg border border-amber-300 bg-amber-50 text-[12px] text-amber-800 leading-relaxed">
          <b className="font-semibold">{pkTrap.path.slice(prefix.length)} is this table’s own key.</b>{" "}
          A column ending in <code className="font-mono">_id</code> reads as a foreign key by default — which drops it
          from the properties and produces a factless duplicate. Marking it as identity here is what stops that.
        </div>
      )}

      <div className="mb-3.5">
        <Label>Attributes — {attrs.filter((a) => !keyed(a) && a.becomes !== "ignore").length} kept</Label>
        <div className="flex flex-wrap gap-1.5">
          {attrs.filter((a) => !keyed(a)).map((a) => {
            const f = a.path.slice(prefix.length);
            const on = a.becomes !== "ignore" && a.role !== "skip";
            return (
              <Chip key={a.path} on={on}
                onClick={() => onPatch({}, { path: a.path, patch: on ? { becomes: "ignore", role: "skip" } : { becomes: "label", role: "metadata" } })}>
                {f}
              </Chip>
            );
          })}
        </div>
      </div>

      <div className={`px-3 py-2.5 rounded-lg border text-[12px] ${unused.length
        ? "border-stone-200 bg-stone-50 text-stone-500" : "border-lime-300 bg-lime-50 text-lime-800"}`}>
        {unused.length
          ? <><b className="font-semibold text-stone-700">Unused {fieldWord}s ({unused.length})</b> {unused.join(", ")}</>
          : <><CheckCircle2 size={13} className="inline mr-1 -mt-0.5" /><b className="font-semibold">All {fieldWord}s used</b> — nothing here is being dropped</>}
      </div>

      <div className="flex items-center gap-2 pt-3.5 mt-3 border-t border-stone-200">
        <Button variant="ghost" size="sm" onClick={onDelete} icon={Trash2}>Delete rule</Button>
      </div>
    </div>
  );
}

function Endpoint({ side, edge, types, fieldsHere, targetFields, paradigm, onPatch }: {
  side: "from" | "to"; edge: EdgeRule; types: string[];
  fieldsHere: string[]; targetFields: (t: string) => string[];
  paradigm: Paradigm; onPatch: (p: Partial<EdgeRule>) => void;
}) {
  const kinds = KINDS_FOR[paradigm];
  const isFrom = side === "from";
  const nodeType = isFrom ? edge.fromNode : edge.toNode;
  const col = isFrom ? edge.fromKey : edge.toKey;
  const on = isFrom ? edge.fromOn : edge.toOn;
  const kind: RefKind = col ? "join" : "self";

  const setKind = (k: RefKind) => {
    if (k === "join") {
      onPatch(isFrom ? { fromKey: fieldsHere[0] || "id", fromOn: "" } : { toKey: fieldsHere[0] || "id", toOn: "" });
    } else {
      onPatch(isFrom ? { fromKey: "", fromOn: "" } : { toKey: "", toOn: "" });
    }
  };

  return (
    <div className={`rounded-lg border border-stone-200 bg-stone-50/70 p-3 border-l-[3px] ${isFrom ? "border-l-primary" : "border-l-teal-600"}`}>
      <Label>{isFrom ? "From" : "To"}</Label>
      <div className="grid grid-cols-4 gap-1 mb-2.5">
        {(["self", "context", "join", "position"] as RefKind[]).map((k) => {
          const enabled = !!kinds[k];
          const note = typeof kinds[k] === "string" ? (kinds[k] as string) : undefined;
          return (
            <button key={k} type="button" disabled={!enabled || k === "context" || k === "position"}
              title={note || (k === "context" || k === "position" ? "drawn on the node rule itself" : "")}
              aria-pressed={kind === k} onClick={() => setKind(k)}
              className={`text-[11px] font-semibold py-1.5 rounded-md border transition-colors
                          ${kind === k ? KIND_SOLID[k] : "bg-white text-stone-500 border-stone-200 hover:border-stone-300"}
                          ${!enabled || k === "context" || k === "position" ? "opacity-40 cursor-not-allowed" : ""}`}>
              {KIND_LABEL[k]}
            </button>
          );
        })}
      </div>
      <select className={`${inputCls} ${monoCls}`} value={nodeType}
        onChange={(e) => onPatch(isFrom ? { fromNode: e.target.value } : { toNode: e.target.value })}>
        {Array.from(new Set([nodeType].concat(types))).filter(Boolean).map((t) => <option key={t} value={t}>{t}</option>)}
      </select>
      {kind === "join" && (
        <div className="mt-2 grid grid-cols-2 gap-2">
          <div>
            <Label>read column</Label>
            <select className={`${inputCls} ${monoCls}`} value={col || ""}
              onChange={(e) => onPatch(isFrom ? { fromKey: e.target.value } : { toKey: e.target.value })}>
              {Array.from(new Set([col || ""].concat(fieldsHere))).filter(Boolean).map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </div>
          <div>
            <Label>matched on</Label>
            <select className={`${inputCls} ${monoCls}`} value={on || ""}
              onChange={(e) => onPatch(isFrom ? { fromOn: e.target.value } : { toOn: e.target.value })}>
              <option value="">(that type’s key)</option>
              {targetFields(nodeType).map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </div>
        </div>
      )}
      {kind === "self" && <Hint>The node this rule’s path already produced.</Hint>}
      {kind === "join" && paradigm === "unstructured" && (
        <Hint>This is entity resolution: the extracted value is matched against the identity index.</Hint>
      )}
    </div>
  );
}

export function EdgeForm({ edge, rows, paradigm, sample, onPatch, onDelete }: {
  edge: EdgeRule; rows: KeyRow[]; paradigm: Paradigm; sample: any;
  onPatch: (p: Partial<EdgeRule>) => void; onDelete: () => void;
}) {
  const nodeRows = rows.filter((r) => r.kind === "node" && r.node_type);
  const types = nodeRows.map((r) => r.node_type!);
  const anchorType = edge.readFrom || edge.fromNode;
  const anchor = nodeRows.find((r) => r.node_type === anchorType) || nodeRows[0];
  const anchorPrefix = anchor?.path ? `${anchor.path}.` : "";
  const fieldsHere = rows.filter((r) => r.kind === "attribute" && anchor && r.path.startsWith(anchorPrefix) &&
      !r.path.slice(anchorPrefix.length).includes(".") && r.path.slice(anchorPrefix.length).length > 0)
    .map((r) => r.path.slice(anchorPrefix.length));
  const targetFields = (t: string) => {
    const n = nodeRows.find((r) => r.node_type === t);
    if (!n) return [];
    const p = n.path ? `${n.path}.` : "";
    return rows.filter((r) => r.kind === "attribute" && r.path.startsWith(p) &&
        !r.path.slice(p.length).includes(".") && r.path.slice(p.length).length > 0)
      .map((r) => r.path.slice(p.length));
  };
  const isJoin = !!(edge.fromKey || edge.toKey);

  return (
    <div className="p-3.5">
      <div className="mb-3.5">
        <Label>Edge label</Label>
        <input className={inputCls} value={edge.edgeLabel}
          onChange={(e) => onPatch({ edgeLabel: e.target.value.toUpperCase() })} />
        <Hint>
          {paradigm === "structured"
            ? <>Today a foreign key becomes <code className="font-mono">BELONGS_TO</code> or <code className="font-mono">REFERENCES</code>. Here you name it.</>
            : <>You name the relationship. Before this, every value-matched edge was called <code className="font-mono">REFERENCES</code>.</>}
        </Hint>
      </div>

      <div className="mb-3.5">
        <Label>Read from</Label>
        <select className={`${inputCls} ${monoCls}`} value={edge.readFrom || edge.fromNode}
          onChange={(e) => onPatch({ readFrom: e.target.value })}>
          {types.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <Hint>One edge attempt per {paradigm === "structured" ? "row" : "match"} of that rule’s path.</Hint>
      </div>

      <div className="grid grid-cols-2 gap-2.5 mb-3.5">
        <Endpoint side="from" edge={edge} types={types} fieldsHere={fieldsHere}
          targetFields={targetFields} paradigm={paradigm} onPatch={onPatch} />
        <Endpoint side="to" edge={edge} types={types} fieldsHere={fieldsHere}
          targetFields={targetFields} paradigm={paradigm} onPatch={onPatch} />
      </div>

      {!isJoin && (
        <div className="mb-3.5 px-3 py-2.5 rounded-lg border border-stone-200 bg-stone-50 text-[12px] text-stone-600 leading-relaxed">
          Both ends are <b>this row</b>, so this is a <b>containment</b> edge — it follows the nesting and is drawn
          from the child rule’s parent. Set one end to <b>by value</b> to relate two branches that are not nested.
        </div>
      )}

      <div className="mb-3.5">
        <Label>Edge properties — {(edge.props || []).length} selected</Label>
        <div className="flex flex-wrap gap-1.5">
          {fieldsHere.map((f) => {
            const on = (edge.props || []).some((p) => p.from_field === f);
            return (
              <Chip key={f} on={on} onClick={() => onPatch({
                props: on ? (edge.props || []).filter((p) => p.from_field !== f)
                          : [...(edge.props || []), { prop: f, from_field: f }],
              })}>{f}</Chip>
            );
          })}
        </div>
        <Hint>Facts that belong to the relationship, not to either end.</Hint>
      </div>

      <div className="flex items-center gap-2 pt-3.5 border-t border-stone-200">
        <Button variant="ghost" size="sm" onClick={onDelete} icon={Trash2}>Delete rule</Button>
      </div>
    </div>
  );
}

/* ──────────────────────────────────────────────────────────── graph preview */

export function GraphPreview({ g }: { g: Materialized }) {
  const order = Array.from(new Set(g.nodes.map((n) => n.type)));
  if (!order.length) {
    return <div className="px-3.5 py-10 text-center text-sm text-stone-500">
      No nodes yet — add a rule, or load a sample to see what this mapping produces.
    </div>;
  }
  const colW = 168, W = Math.max(600, order.length * colW);
  const perCol = order.map((t) => g.nodes.filter((n) => n.type === t).length);
  const H = Math.max(200, Math.max(...perCol) * 58 + 60);
  const pos = new Map<string, { x: number; y: number }>();
  const seen: Record<number, number> = {};
  g.nodes.forEach((n) => {
    const c = order.indexOf(n.type);
    seen[c] = seen[c] || 0;
    pos.set(n.id, { x: (W / order.length) * c + W / order.length / 2, y: 44 + (H - 76) * ((seen[c] + 0.5) / perCol[c]) });
    seen[c]++;
  });
  const HUE = ["hsl(160 45% 34%)", "hsl(190 45% 34%)", "hsl(35 60% 38%)", "hsl(96 40% 32%)", "hsl(330 35% 42%)", "hsl(215 40% 40%)"];
  return (
    <div className="p-1.5 overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="block w-full h-auto min-w-[560px]" role="img"
           aria-label="Graph produced by the current rules">
        <defs><marker id="mp-ah" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0,0 L8,4 L0,8 z" fill="#a8a29e" /></marker></defs>
        {order.map((t, i) => (
          <text key={t} x={(W / order.length) * i + W / order.length / 2} y={20} textAnchor="middle"
                fontSize="10" fontWeight="600" letterSpacing=".4" fill={HUE[i % HUE.length]}
                style={{ textTransform: "uppercase" }}>{t}</text>
        ))}
        {g.edges.map((e, i) => {
          const a = pos.get(e.source), b = pos.get(e.target);
          if (!a || !b) return null;
          const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2 - Math.min(34, Math.abs(a.x - b.x) / 5 + 8);
          return (
            <g key={i}>
              <path d={`M${a.x},${a.y} Q${mx},${my} ${b.x},${b.y}`} fill="none" stroke="#d6d3d1"
                    strokeWidth={1.4} markerEnd="url(#mp-ah)" />
              <text x={mx} y={my - 3} textAnchor="middle" fontSize="9" fontWeight="600" fill="#78716c">{e.type}</text>
            </g>
          );
        })}
        {g.nodes.map((n) => {
          const p = pos.get(n.id)!;
          const hue = HUE[order.indexOf(n.type) % HUE.length];
          const label = String(n.props.name || n.props.title || n.props.heading || n.key).slice(0, 15);
          const w = Math.max(76, label.length * 7.1 + 20);
          return (
            <g key={n.id}>
              <rect x={p.x - w / 2} y={p.y - 13} width={w} height={26} rx={6} fill="#fff" stroke={hue} strokeWidth={1.2} />
              <text x={p.x} y={p.y + 4} textAnchor="middle" fontSize="11" fontFamily="ui-monospace, monospace" fill={hue}>{label}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/* ───────────────────────────────── unstructured: elements + model assignment */

export interface ElementDef {
  key: string; title: string; blurb: string; rule: string;
  /** The Model Registry role that serves this element — only where a registered model does the work. */
  role?: string;
  /** What actually does this work when no registry model is involved (the layout reader). */
  builtin?: string;
  /** Said instead of "add a model" when nothing reads this role yet. */
  unwired?: string;
}
export const DOC_ELEMENTS: ElementDef[] = [
  { key: "layout", title: "Section & Layout Structure", rule: "Section",
    blurb: "Reads headings, reading order and page regions so sections become nodes.",
    builtin: "Layout reader for the format" },
  { key: "chunks", title: "Text Paragraph Chunks", role: "embedding", rule: "Chunk",
    blurb: "Turns each chunk into a vector so the chunk nodes are searchable." },
  { key: "tables", title: "Embedded Tables & Grids", rule: "Table",
    blurb: "Recovers table structure (cells, headers, spans) into table nodes.",
    builtin: "Layout reader (table cells)" },
  { key: "figures", title: "Images, Figures & Charts", rule: "Figure",
    blurb: "Describes figures and charts so image nodes carry real captions.",
    unwired: "Figures are kept as image nodes without captions — no captioning model is used yet." },
];
const ROLE_TITLE: Record<string, string> = { embedding: "Embedding model" };

export function ElementsPanel({ state, models, onToggle }: {
  state: Record<string, boolean>;
  models: Record<string, string | null>;
  onToggle: (key: string) => void;
}) {
  return (
    <div className="grid gap-3 p-3.5" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))" }}>
      {DOC_ELEMENTS.map((e) => {
        const on = state[e.key] !== false;
        const model = e.role ? models[e.role] || null : null;
        return (
          <div key={e.key} className={`border border-stone-200 rounded-lg p-3 bg-stone-50/70 flex flex-col gap-2 ${on ? "" : "opacity-60"}`}>
            <div className="flex items-start gap-2.5">
              <div className="min-w-0">
                <h4 className="text-[13px] font-semibold text-stone-800">{e.title}</h4>
                <p className="text-[11.5px] text-stone-500 mt-0.5">{e.blurb}</p>
              </div>
              <button type="button" role="switch" aria-checked={on} aria-label={`Toggle ${e.title}`}
                onClick={() => onToggle(e.key)}
                className={`ml-auto shrink-0 relative w-[38px] h-[21px] rounded-full border transition-colors
                            ${on ? "bg-primary border-primary" : "bg-stone-200 border-stone-300"}`}>
                <span className={`absolute top-[2px] w-[15px] h-[15px] rounded-full bg-white shadow transition-all
                                  ${on ? "left-[19px]" : "left-[2px]"}`} />
              </button>
            </div>
            {e.role && (
              <div className="text-[11px] text-stone-500">
                <b className="text-stone-700 font-semibold">{ROLE_TITLE[e.role] || e.role}</b> (role: <code className="font-mono">{e.role}</code>)
              </div>
            )}
            <div className={`text-[11.5px] px-2.5 py-1.5 rounded-md border ${e.builtin || model
              ? "text-lime-800 bg-lime-50 border-lime-300" : "text-amber-800 bg-amber-50 border-amber-300"}`}>
              {e.builtin ? <>Built in — <span className="font-mono">{e.builtin}</span>, in the ingest workers</>
                : model ? <>Registry default — <span className="font-mono">{model}</span></>
                : e.unwired ? <>{e.unwired}</>
                : <>No {(ROLE_TITLE[e.role || ""] || "model").toLowerCase()} registered. <a href="/dashboard/models" className="underline underline-offset-2">Add one</a></>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

const STRATEGIES: { k: ChunkCfg["strategy"]; n: string; d: string; tag?: string }[] = [
  { k: "sliding", n: "Sliding window", d: "Fixed windows of target size with the overlap carried forward." },
  { k: "layout", n: "Layout", tag: "per format", d: "Read by the reader for the file's format: titles, sections, 2D table grids, pictures, and NEXT reading flow." },
];

export function ChunkingPanel({ cfg, sample, onCfg, onSample }: {
  cfg: ChunkCfg; sample: string; onCfg: (c: Partial<ChunkCfg>) => void; onSample: (s: string) => void;
}) {
  const chunks = useMemo(() => chunkText(sample, cfg), [sample, cfg]);
  // `!` was a lie: a mapping saved with a strategy this list does not carry made `strat` undefined
  // and `strat.d` below crashed the entire page, not just this panel.
  const strat = STRATEGIES.find((s) => s.k === cfg.strategy) ?? STRATEGIES[0];
  return (
    <div className="grid lg:grid-cols-2">
      <div className="p-3.5 lg:border-r border-b lg:border-b-0 border-stone-200">
        <p className="text-[12.5px] text-stone-500 mb-3">
          How the document is split, and the node it produces. Any pipeline bound to this mapping reads documents this way.
        </p>
        <div className="mb-3.5">
          <Label>Strategy</Label>
          <div className="flex flex-wrap gap-1.5">
            {STRATEGIES.map((s) => (
              <button key={s.k} type="button" aria-pressed={s.k === cfg.strategy}
                onClick={() => onCfg({ strategy: s.k })}
                className={`text-[12px] px-2.5 py-1.5 rounded-md border transition-colors
                            ${s.k === cfg.strategy ? "border-primary/50 bg-primary/10 text-primary font-semibold"
                                                   : "bg-white text-stone-500 border-stone-200 hover:border-stone-300"}`}>
                {s.n}{s.tag && <span className="ml-1.5 text-[9px] uppercase tracking-wide opacity-70">· {s.tag}</span>}
              </button>
            ))}
          </div>
          <Hint>{strat.d}</Hint>
        </div>
        <div className="mb-3.5">
          <Label>Reading mode</Label>
          <div className="grid gap-2">
            {([["save", "Save chunks", "Persist chunk text nodes + everything extracted from them."],
               ["extract", "Extract only", "Compute chunks to extract, but do not save chunk nodes — keep only the extracted nodes (entities, claims, topics)."]] as const)
              .map(([k, t, d]) => (
                <button key={k} type="button" aria-pressed={cfg.mode === k} onClick={() => onCfg({ mode: k })}
                  className={`flex gap-2.5 items-start text-left px-3 py-2.5 rounded-lg border transition-colors
                              ${cfg.mode === k ? "border-primary/50 bg-primary/10" : "border-stone-200 bg-white hover:border-stone-300"}`}>
                  <span className={`w-3.5 h-3.5 rounded-full border-2 mt-0.5 shrink-0
                                    ${cfg.mode === k ? "border-primary bg-primary ring-2 ring-inset ring-white" : "border-stone-300"}`} />
                  <span>
                    <b className="block text-[12.5px] font-semibold text-stone-800">{t}</b>
                    <span className="text-[11.5px] text-stone-500">{d}</span>
                  </span>
                </button>
              ))}
          </div>
        </div>
        {cfg.strategy === "layout" ? (
          <div className="rounded-lg border border-teal-200 bg-teal-50/60 p-3 text-[12px] text-teal-900 leading-relaxed mb-3">
            <div className="font-semibold text-teal-800 mb-1 flex items-center gap-1.5">
              <Sparkles className="w-3.5 h-3.5 text-teal-600" />
              Layout reading active
            </div>
            Extracts native layout blocks dynamically (<b>SectionHeader</b>, <b>Paragraph</b>, <b>Table</b> 2D grids, <b>Picture</b>) and connects them via hierarchical containment and sequential <b>NEXT</b> reading order edges.
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label>Target size · {cfg.size} ch</Label>
              <input type="range" min={200} max={3000} step={100} value={cfg.size} className="w-full accent-[hsl(var(--primary))]"
                onChange={(e) => onCfg({ size: Number(e.target.value) })} />
            </div>
            <div>
              <Label>Overlap · {cfg.overlap} ch</Label>
              <input type="range" min={0} max={600} step={25} value={cfg.overlap} className="w-full accent-[hsl(var(--primary))]"
                onChange={(e) => onCfg({ overlap: Number(e.target.value) })} />
            </div>
          </div>
        )}
        <div className="mt-3.5">
          <Label>Chunk node</Label>
          <input className={`${inputCls} ${monoCls}`} value={cfg.node} onChange={(e) => onCfg({ node: e.target.value })} />
          <Hint>Name the node each chunk becomes.</Hint>
        </div>
      </div>
      <div className="p-3.5 bg-stone-50/70">
        <Label>Sample text — previewed in-browser</Label>
        <textarea value={sample} onChange={(e) => onSample(e.target.value)}
          className={`${inputCls} ${monoCls} min-h-[110px] resize-y`} />
        <div className="mt-3 space-y-2 max-h-[320px] overflow-auto">
          {chunks.map((c, i) => (
            <div key={i} className="relative">
              <div className="border border-stone-200 rounded-lg bg-white px-3 py-2">
                <div className="flex gap-2 items-baseline flex-wrap text-[11px] text-stone-500 mb-1">
                  <b className="text-primary font-semibold">{cfg.strategy === "layout" ? `Block #${i + 1}` : `Chunk #${i + 1}`}</b>
                  {c.label && (
                    <span className="font-mono bg-teal-50 text-teal-700 border border-teal-200 px-1.5 py-0.5 rounded font-semibold text-[10px]">
                      {c.label}
                    </span>
                  )}
                  {c.heading && <span className="font-mono bg-primary/10 text-primary px-1.5 py-px rounded">heading={c.heading}</span>}
                  <span>{c.text.length} ch</span>
                </div>
                <p className="text-[11.5px] text-stone-600 leading-relaxed m-0">
                  {c.text.slice(0, 190)}{c.text.length > 190 ? "…" : ""}
                </p>
              </div>
              {cfg.strategy === "layout" && i < chunks.length - 1 && (
                <div className="flex items-center justify-center my-1 text-[10px] font-mono text-stone-400 gap-1">
                  <span>↓ NEXT</span>
                </div>
              )}
            </div>
          ))}
          {!chunks.length && <p className="text-[12px] text-stone-500">Paste or upload a document to preview its chunks.</p>}
        </div>
      </div>
    </div>
  );
}

/* ───────────────────────────────────────────────────────────────── upload */

export function UploadBar({ paradigm, docs, pick, onPick, onFile, msg, onPropose }: {
  paradigm: Paradigm; docs: { id: string; name: string }[]; pick: string;
  onPick: (id: string) => void; onFile: (f: File) => void; msg: string;
  onPropose: () => void;
}) {
  const accept = paradigm === "structured" ? ".csv,text/csv"
    : paradigm === "unstructured" ? ".pdf,.docx,.pptx,.html,.htm,.md,.txt,.markdown,text/plain"
    : ".json,application/json";
  const what = paradigm === "structured" ? "a CSV table"
    : paradigm === "unstructured" ? "a document (PDF, Word, PowerPoint, HTML, Markdown or text)" : "a JSON payload";
  return (
    <div className="flex flex-wrap gap-2 items-center px-3.5 py-3 bg-stone-50/70 border-t border-stone-200">
      <p className="basis-full text-[11.5px] text-stone-500 m-0">
        Verify this mapping against real data. Upload {what}, or pick a sample already loaded.
      </p>
      <label className="text-[12.5px] font-semibold cursor-pointer px-3 py-1.5 rounded-lg border border-dashed
                        border-stone-300 bg-white text-stone-700 hover:border-primary hover:text-primary transition-colors">
        <Upload size={14} className="inline mr-1.5 -mt-0.5" />Upload
        <input type="file" accept={accept} className="hidden"
          onChange={(e) => { const f = e.target.files?.[0]; if (f) onFile(f); }} />
      </label>
      {docs.length > 0 && (
        <select className={`${inputCls} ${monoCls} max-w-[260px] w-auto`} value={pick} onChange={(e) => onPick(e.target.value)}>
          {docs.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
      )}
      <Button variant="secondary" size="sm" icon={Sparkles} onClick={onPropose}>Propose rules</Button>
      {msg && <span className="text-[11.5px] text-stone-500">{msg}</span>}
    </div>
  );
}

/* ──────────────────────────────────────────────────────────────── stats bar */

export function StatsBar({ g, edgeRules }: { g: Materialized; edgeRules: EdgeRule[] }) {
  const joins = edgeRules.filter((e) => e.fromKey || e.toKey).length;
  return (
    <div className="flex flex-wrap gap-5 px-3.5 py-2.5 border-t border-stone-200 bg-stone-50/70 text-[12px] text-stone-500">
      <span><b className="text-stone-800 font-semibold tabular-nums">{g.nodes.length}</b> nodes</span>
      <span><b className="text-stone-800 font-semibold tabular-nums">{g.edges.length}</b> edges</span>
      <span><b className="text-stone-800 font-semibold tabular-nums">{new Set(g.edges.map((e) => e.type)).size}</b> edge types</span>
      <span><b className="text-stone-800 font-semibold tabular-nums">{joins}</b> value-matched rules</span>
      <span className={g.unresolved.length ? "text-amber-700" : ""}>
        {g.unresolved.length > 0 && <AlertTriangle size={12} className="inline mr-1 -mt-0.5" />}
        <b className="font-semibold tabular-nums">{g.unresolved.length}</b> unresolved
      </span>
    </div>
  );
}

export const PARADIGM_META: Record<Paradigm, { label: string; sub: string; icon: any }> = {
  semistructured: { label: "Semi-structured", sub: "JSON, APIs, events", icon: Braces },
  structured: { label: "Structured", sub: "SQL tables, CSV, warehouses", icon: TableIcon },
  unstructured: { label: "Unstructured", sub: "PDFs, docs, prose", icon: FileText },
};
