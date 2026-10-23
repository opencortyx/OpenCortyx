/**
 * The Mapper's shared model: the editor rule shapes, a client-side materializer that mirrors the
 * backend walker, a chunker, and rule auto-proposal.
 *
 * The materializer exists so the builder can show what a mapping PRODUCES while it is being written,
 * against the author's own sample, without a round trip. It is deliberately a mirror and not a
 * re-implementation: the same four ways of naming a node that `ingest_json` resolves —
 *
 *    self      the node this rule's path already produced
 *    context   an enclosing node of that type
 *    join      a field's VALUE matched against another type's key   (the new one)
 *    position  the same ordinal in a sibling array
 *
 * — and the same path vocabulary the walker uses (`lines[]`, `order.customer`, `""` for the record
 * root), NOT JSONPath. Anything the preview shows that the walker would not do is a bug in here.
 */

/* ────────────────────────────────────────────────────────────── editor model */

export type Paradigm = "semistructured" | "structured" | "unstructured";

export type RefKind = "self" | "context" | "join" | "position";

export interface KeyRow {
  path: string;
  kind: "node" | "attribute";
  type?: string;
  sample?: string;
  node_type?: string;
  decision?: string;
  becomes?: string;
  role?: string;
  edge_type?: string;
  edge_dir?: "in" | "out";
  attachToNode?: string;
  attr_name?: string;
  dict_mode?: boolean;
  key_attr?: string;
  id_scope?: "parent";
  pair_with?: string;
  auto_type?: boolean;
  emit?: string;
}

export interface EdgeRule {
  id: string;
  fromNode: string;
  toNode: string;
  edgeLabel: string;
  direction?: "in" | "out";
  cardinality?: string;
  /** Present ⇒ this is a JOIN edge: a value match, not containment. */
  fromKey?: string;
  fromOn?: string;
  toKey?: string;
  toOn?: string;
  readFrom?: string;
  props?: { prop: string; from_field: string }[];
}

export interface NodeRule { id: string; nodeLabel: string; sourcePath: string; attachedAttributes?: any[]; }
export interface AttributeRule { id: string; sourcePath: string; attachToNode: string; propertyName: string; dataType?: string; }

export interface EditorModel {
  status?: string;
  keyRows: KeyRow[];
  nodeRules: NodeRule[];
  edgeRules: EdgeRule[];
  attributeRules: AttributeRule[];
  edgeAttributeRules?: any[];
  compositeKeyRules?: any[];
  /** where the author arranged each node type on the declared-graph canvas */
  canvas?: Record<string, { x: number; y: number }>;
  /** unstructured only */
  chunking?: ChunkCfg;
  elements?: Record<string, boolean>;
  metadata?: DocMetadataRules;
  tableRules?: any[];

  reading?: Record<string, any>;
  discriminatorRules?: any[];
  arrayFilterRules?: any[];
  zipCorrelations?: any[];
  patternRules?: any[];
  junctionRules?: any[];
  citationRules?: any[];
  __derived?: boolean;
  __touched?: boolean;
  __structures?: number;
}

/**
 * The retired document builder wrote a different chunking dialect: `markdown_heading` for the
 * strategy, `chunk_size`, `save_chunks`, `node_type`. The Mapper edits `markdown` / `size` / `mode`
 * / `node`, and read the saved object raw — so opening one of those mappings put an unknown strategy
 * into `ChunkingPanel`, whose `STRATEGIES.find(...)!` then returned undefined and took the WHOLE
 * mapper page down with "Cannot read properties of undefined (reading 'd')".
 *
 * Normalising here rather than at the call site means the panel, the live chunk preview and the save
 * all agree about what the mapping says.
 */
export function normalizeChunk(raw: any): ChunkCfg {
  const r = raw || {};
  const s = String(r.strategy || "").toLowerCase();
  // No strategy saved means layout, as it does to ingestion (`step1.get("strategy") or "layout"`):
  // showing windows here, and then saving them, quietly turned a new mapping's documents into
  // fixed-size chunks with no titles, sections, lists or tables.
  const strategy: ChunkCfg["strategy"] =
    !s || s.startsWith("layout") || s.includes("docling") ? "layout" : "sliding";
  const size = Number(r.size ?? r.chunk_size ?? r.target_size ?? 800);
  const overlap = Number(r.overlap ?? 80);
  return {
    strategy,
    mode: r.mode === "extract" || r.save_chunks === false ? "extract" : "save",
    size: Number.isFinite(size) && size > 0 ? size : 800,
    overlap: Number.isFinite(overlap) && overlap >= 0 ? overlap : 80,
    node: String(r.node || r.node_type || "Chunk"),
    custom: r.custom === true,
  };
}

export interface ChunkCfg {
  strategy: "sliding" | "layout";
  mode: "save" | "extract";
  size: number;
  overlap: number;
  node: string;
  /** Layout reading: `size` is this mapping's paragraph split. Unset, the workspace's Chunk size applies. */
  custom?: boolean;
}

/** What a document mapping does with the header block a document opens with. */
export interface DocMetadataRules {
  /** Link documents the header names ("Obsoletes: 8113"). On unless turned off. */
  header_references?: boolean;
  /** Header field → the name it is stored under; a reference's relation is renamed with it. */
  rename?: Record<string, string>;
  /** Header fields not stored (and not linked). */
  drop?: string[];
}

/* ─────────────────────────────────────────────── paths (the WALKER's grammar) */

/** `orders[].items[].sku` → [{key:"orders",arr:true},{key:"items",arr:true},{key:"sku"}] */
export function segs(path: string) {
  const out: { key: string; arr: boolean }[] = [];
  for (const part of String(path || "").split(".")) {
    if (!part) continue;
    const arr = part.endsWith("[]");
    out.push({ key: arr ? part.slice(0, -2) : part, arr });
  }
  return out;
}

export interface Hit { trail: string[]; value: any }

/** schema.ROOT_PATH_ALIASES — kept identical, or a rule authored at the root matches nothing. */
export const ROOT_PATH_ALIASES: readonly string[] = [
  "payload_root", "payload_root[]", "$root", "__root__", "root_payload",
];

/**
 * schema.canonical_path: fold the editor's root sentinel, as a whole path or as a `<alias>.` prefix.
 *
 * The editor prefixes every key of a top-level-array sample with `payload_root[]`, while the walker —
 * which iterates the array and walks each element on its own — never sees that segment. Reading the
 * sentinel literally is why the preview matched nothing at all: it looked inside each record for a key
 * named `payload_root`. The declared canvas uses this same definition (declaredShape.ts re-exports it).
 */
export function canonicalPath(path: string | null | undefined): string {
  const p = String(path ?? "").trim();
  if (ROOT_PATH_ALIASES.includes(p)) return "";
  for (const alias of ROOT_PATH_ALIASES) {
    if (p.startsWith(`${alias}.`)) return p.slice(alias.length + 1);
  }
  return p;
}

/**
 * Does `key` match the single-segment glob `pattern`? Mirrors ingest_json._glob_to_re exactly: `*` is any
 * run of characters that is not a dot, an opening bracket or a closing bracket.
 *
 * Written without a RegExp. A key containing a dot or a bracket could only match if that character fell
 * inside a `*`, which the pattern forbids — so such a key never matches, and everything else is an
 * ordinary glob decided by its literal parts appearing in order.
 */
export function segmentMatches(pattern: string, key: string): boolean {
  if (!pattern.includes("*")) return pattern === key;
  if (key.includes(".") || key.includes("[") || key.includes("]")) return false;
  const parts = pattern.split("*");
  const head = parts[0];
  const tail = parts[parts.length - 1];
  if (key.length < head.length + tail.length || !key.startsWith(head) || !key.endsWith(tail)) return false;
  let at = head.length;
  const end = key.length - tail.length;
  for (let k = 1; k < parts.length - 1; k++) {
    const found = key.indexOf(parts[k], at);
    if (found < 0 || found + parts[k].length > end) return false;
    at = found + parts[k].length;
  }
  return true;
}

/** A `**` segment spans zero or more segments, as in ingest_json (`**.turns[]` finds turns at any depth). */
const ANY_DEPTH = "**";
/** How deep a walk may descend — a guard against a pathological payload, never a limit a mapping reaches. */
const MAX_WALK_DEPTH = 64;

/**
 * Every value at `path`, each with the concrete trail that located it.
 *
 * The trail carries the REAL key that matched, never the pattern. That is load-bearing: `byTrail` is
 * keyed on it, so a trail of `session_*[0]` would be the same for every session and each turn would
 * attach to whichever conversation happened to be indexed last.
 */
export function walk(path: string, data: any): Hit[] {
  const S = segs(canonicalPath(path));
  const hits: Hit[] = [];
  const rec = (i: number, v: any, trail: string[], depth: number) => {
    if (depth > MAX_WALK_DEPTH) return;
    if (i === S.length) { hits.push({ trail, value: v }); return; }
    const s = S[i];
    if (v == null || typeof v !== "object") return;
    if (s.key === ANY_DEPTH) {
      rec(i + 1, v, trail, depth + 1);                     // zero segments
      if (Array.isArray(v)) return;                        // a list is entered from its own key, below
      for (const [k, x] of Object.entries(v)) {
        if (Array.isArray(x)) x.forEach((el, n) => rec(i, el, trail.concat(`${k}[${n}]`), depth + 1));
        else rec(i, x, trail.concat(k), depth + 1);
      }
      return;
    }
    if (Array.isArray(v)) return;                          // a named key is never looked up on a list
    const keys = s.key.includes("*")
      ? Object.keys(v).filter((k) => segmentMatches(s.key, k))
      : (Object.prototype.hasOwnProperty.call(v, s.key) ? [s.key] : []);
    for (const k of keys) {
      const next = (v as any)[k];
      if (next === undefined) continue;
      if (s.arr) {
        // `[]` demands a list: `session_*[]` also matches the NAME `session_1_date_time`, a string.
        if (!Array.isArray(next)) continue;
        next.forEach((x, n) => rec(i + 1, x, trail.concat(`${k}[${n}]`), depth + 1));
      } else {
        rec(i + 1, next, trail.concat(k), depth + 1);
      }
    }
  };
  // A record ROOT (`""`) is the payload itself — or, when the payload is a list, each element,
  // which is exactly how the walker treats a top-level array.
  if (!S.length) {
    if (Array.isArray(data)) data.forEach((d, n) => hits.push({ trail: [`$[${n}]`], value: d }));
    else hits.push({ trail: ["$"], value: data });
    return hits;
  }
  if (Array.isArray(data)) data.forEach((d, n) => rec(0, d, [`$[${n}]`], 0));
  else rec(0, data, ["$"], 0);
  return hits;
}

export const ordinalOf = (trail: string[]) => {
  const m = /\[(\d+)\]$/.exec(trail[trail.length - 1] || "");
  return m ? Number(m[1]) : null;
};

/** The scalar fields of a matched value — what the forms offer as identity / attributes. */
export function fieldsOf(v: any): string[] {
  if (v && typeof v === "object" && !Array.isArray(v)) return Object.keys(v);
  return ["$value"];
}

export const valueOf = (v: any, f: string) =>
  f === "$value" ? v : (v && typeof v === "object" ? (v as any)[f] : undefined);

/** Every path present in a payload, in the walker's own grammar. Used by the path pickers. */
export function collectPaths(data: any, maxDepth = 5): string[] {
  const out = new Set<string>([""]);
  const rec = (v: any, p: string, d: number) => {
    if (d > maxDepth || v == null) return;
    if (Array.isArray(v)) {
      const ap = p ? `${p}[]` : "";
      if (ap) out.add(ap);
      if (v.length && typeof v[0] === "object") rec(v[0], ap, d + 1);
      return;
    }
    if (typeof v !== "object") return;
    for (const k of Object.keys(v)) {
      const child = p ? `${p}.${k}` : k;
      const val = (v as any)[k];
      if (Array.isArray(val)) {
        out.add(`${child}[]`);
        if (val.length && typeof val[0] === "object") rec(val[0], `${child}[]`, d + 1);
      } else {
        out.add(child);
        if (val && typeof val === "object") rec(val, child, d + 1);
      }
    }
  };
  rec(Array.isArray(data) ? data[0] : data, "", 0);
  return Array.from(out);
}

/* ─────────────────────────────────────────────────────────── the materializer */

export interface GNode { id: string; type: string; key: string; props: Record<string, any> }
export interface GEdge { source: string; type: string; target: string; props: Record<string, any> }
export interface Materialized {
  nodes: GNode[];
  edges: GEdge[];
  unresolved: { type: string; on: string; value: string }[];
  warnings: string[];
}

/**
 * What a node row compiles to — the ONE definition, mirroring `translate._node_key` exactly.
 *
 * `becomes` is the user's choice and wins over `decision`, which the detector stamped when it first
 * sampled the payload; "ignore" compiles to "skip". The inline filter this replaces read `decision`
 * as well, so a row the detector marked "skip" and the user then set to Node was dropped here while
 * ingestion walked it — and the preview and the declared canvas each had their own, different idea
 * of which rules count. Measured on a real mapping: the canvas drew 13 types for a mapping that
 * compiles to 4, because 11 of its rules were set to Ignore.
 */
const BECOMES_DECISION: Record<string, string> = {
  node: "node", container: "node", dict: "dict", embed: "embed", skip: "skip", ignore: "skip",
};

export function nodeDecision(r: KeyRow): string {
  return (r.becomes && BECOMES_DECISION[r.becomes]) || r.decision || "node";
}

/**
 * Does this row get a node of ITS OWN? False for "skip" (nothing is produced) and for "embed" (its
 * fields fold into the parent — `schema.py`: "embedded objects fold into their parent"). This is what
 * the declared canvas draws a box for: a box for a skipped or embedded rule is a type ingestion never
 * makes, which is exactly how the picture came to disagree with the mapping.
 */
export function ownsNode(r: KeyRow): boolean {
  if (r.kind !== "node") return false;
  const d = nodeDecision(r);
  return d !== "skip" && d !== "embed";
}

/**
 * Node rows that actually produce nodes, shallowest first (ancestors before descendants).
 *
 * Embedded rows are deliberately KEPT here, unlike `ownsNode`: the compiler folds their fields into
 * the parent, and this preview has no folding step, so excluding them would silently drop those
 * fields from the picture rather than show them. Only `skip` is removed.
 */
function activeNodeRows(rows: KeyRow[], enabled?: (r: KeyRow) => boolean) {
  return rows
    .filter((r) => r.kind === "node")
    .filter((r) => nodeDecision(r) !== "skip")
    .filter((r) => (enabled ? enabled(r) : true))
    // Depth of the CANONICAL path: `payload_root` IS the root (depth 0), not a one-segment path that
    // can tie with a first-level rule and be processed after its own children.
    .sort((a, b) => segs(canonicalPath(a.path)).length - segs(canonicalPath(b.path)).length);
}

/** Attribute rows that belong to a node row — the ones that become its properties / identity. */
function attrsFor(rows: KeyRow[], nodePath: string) {
  const prefix = nodePath ? `${nodePath}.` : "";
  return rows.filter((r) => {
    if (r.kind !== "attribute") return false;
    if ((r.becomes ?? r.role) === "ignore" || r.role === "skip") return false;
    if (!r.path.startsWith(prefix)) return false;
    const rest = r.path.slice(prefix.length);
    return rest.length > 0 && !rest.includes(".") && !rest.includes("[]");
  });
}

export function materialize(
  rows: KeyRow[],
  edgeRules: EdgeRule[],
  data: any,
  opts: { enabled?: (r: KeyRow) => boolean } = {}
): Materialized {
  const nodes = new Map<string, GNode>();
  const edges = new Map<string, GEdge>();
  const byTrail = new Map<string, string>();          // trail        → node id  (context)
  const identity = new Map<string, string>();         // type|field|v → node id  (join)
  const position = new Map<string, string>();         // path|ordinal → node id  (position)
  const unresolved: Materialized["unresolved"] = [];
  const warnings: string[] = [];

  const nodeRows = activeNodeRows(rows, opts.enabled);

  /**
   * Which rule OWNS a matched element — ingest_json._PathIndex, exactly: an exact path beats every
   * pattern; among patterns the longest literal prefix wins, then the fewest wildcards; a tie goes to the
   * rule declared first. Without this an exact `session_1[]` rule and a `session_*[]` rule both produced a
   * node for the very same turn.
   */
  type Seg = ReturnType<typeof segs>[number];
  const segsMatch = (want: Seg[], got: Seg[], i = 0, j = 0): boolean => {
    if (i === want.length) return j === got.length;
    if (want[i].key === ANY_DEPTH) {
      for (let k = j; k <= got.length; k++) if (segsMatch(want, got, i + 1, k)) return true;
      return false;
    }
    if (j === got.length) return false;
    return want[i].arr === got[j].arr && segmentMatches(want[i].key, got[j].key) &&
      segsMatch(want, got, i + 1, j + 1);
  };
  const specificity = (cp: string): [number, number] =>
    cp.includes("*") ? [cp.split("*", 1)[0].length, -(cp.split("*").length - 1)] : [Number.MAX_SAFE_INTEGER, 0];
  const ranked = nodeRows.map((r) => {
    const cp = canonicalPath(r.path);
    return { r, want: segs(cp), rank: specificity(cp) };
  });
  const ownerCache = new Map<string, KeyRow | null>();
  const ownerOf = (trail: string[]): KeyRow | null => {
    const instance = trail
      .filter((t, k) => !(k === 0 && t.startsWith("$")))
      .map((t) => { const b = t.indexOf("["); return b > 0 && t.endsWith("]") ? `${t.slice(0, b)}[]` : t; })
      .join(".");
    const cached = ownerCache.get(instance);
    if (cached !== undefined) return cached;
    const got = segs(instance);
    let best: { r: KeyRow; rank: [number, number] } | null = null;
    for (const c of ranked) {
      if (!segsMatch(c.want, got)) continue;
      if (!best || c.rank[0] > best.rank[0] || (c.rank[0] === best.rank[0] && c.rank[1] > best.rank[1])) {
        best = { r: c.r, rank: c.rank };
      }
    }
    const owner = best ? best.r : null;
    ownerCache.set(instance, owner);
    return owner;
  };
  const anchorOf = new Map<string, KeyRow>();
  nodeRows.forEach((r) => { if (r.node_type) anchorOf.set(r.node_type, r); });

  for (const r of nodeRows) {
    const attrs = attrsFor(rows, r.path);
    // A wildcard FIELD (`session_*_date_time`) is paired with its sibling array by ingestion
    // (ingest_json._SiblingPairs), which this preview does not do. Say so, rather than drop it silently.
    for (const a of attrs) {
      const field = a.path.slice(r.path ? r.path.length + 1 : 0);
      if (field.includes("*")) {
        const note = `"${a.path}" is a wildcard field: ingestion pairs it with its sibling array, and this preview does not show it`;
        if (!warnings.includes(note)) warnings.push(note);
      }
    }
    const ids = attrs.filter((a) => (a.becomes === "id" || a.role === "id"));
    const type = r.node_type || (r.path ? r.path.replace(/\[\]$/, "").split(".").pop()! : "Record");
    for (const { trail, value } of walk(r.path, data)) {
      if (value == null) continue;
      // Two rules can match one element (`session_1[]` and `session_*[]`); only its owner makes the node.
      const owner = ownerOf(trail);
      if (owner && owner !== r) continue;
      const parentTrail = trail.slice(0, -1);
      let parentId: string | undefined;
      for (let i = trail.length - 1; i > 0 && !parentId; i--) parentId = byTrail.get(trail.slice(0, i).join("."));

      const keyParts = ids
        .map((a) => valueOf(value, a.path.slice(r.path ? r.path.length + 1 : 0)))
        .filter((x) => x !== undefined && x !== null && x !== "");
      const scoped = r.id_scope === "parent" && parentId ? `${parentId}|` : "";
      const key = keyParts.length === ids.length && ids.length ? `${scoped}${keyParts.join("|")}` : "";
      const id = key ? `${type}:${key}` : `${type}@${trail.join(".")}`;

      if (!nodes.has(id)) {
        const props: Record<string, any> = {};
        for (const a of attrs) {
          const f = a.path.slice(r.path ? r.path.length + 1 : 0);
          const v = valueOf(value, f);
          if (v !== undefined && v !== null && v !== "" && typeof v !== "object") props[a.attr_name || f] = v;
        }
        nodes.set(id, { id, type, key: key || trail.join("."), props });
      }
      byTrail.set(trail.join("."), id);
      const ord = ordinalOf(trail);
      if (ord !== null) position.set(`${r.path}|${ord}`, id);
      // index every scalar under this node: a join may match on any column, not only the key
      if (value && typeof value === "object") {
        for (const [k, v] of Object.entries(value)) {
          if (typeof v === "string" || typeof v === "number") identity.set(`${type}|${k}|${v}`, id);
        }
      }

      // containment edge — the CHILD row carries it, exactly as the compiled form does
      if (r.edge_type || parentId) {
        let src = parentId;
        if (r.attachToNode) {
          const wanted = r.attachToNode;
          let found: string | undefined;
          for (let i = trail.length; i > 0 && !found; i--) {
            const cand = byTrail.get(trail.slice(0, i).join("."));
            if (cand && nodes.get(cand)!.type === wanted) found = cand;
          }
          if (!found) { const vals = Array.from(byTrail.values());
            for (const nid of vals) if (nodes.get(nid)!.type === wanted) { found = nid; break; } }
          src = found || parentId;
        }
        const etype = r.edge_type || `HAS_${(r.path.replace(/\[\]$/, "").split(".").pop() || "ITEM").toUpperCase()}`;
        if (src && src !== id) {
          const [a, b] = (r.edge_dir === "in") ? [id, src] : [src, id];
          edges.set(`${a}|${etype}|${b}`, { source: a, type: etype, target: b, props: {} });
        }
      }
    }
  }

  const resolveSide = (
    side: { kind: RefKind; type: string; field?: string; on?: string; array?: string },
    trail: string[], value: any, anchorId: string
  ): string[] => {
    if (side.kind === "self") return anchorId ? [anchorId] : [];
    if (side.kind === "context") {
      for (let i = trail.length; i > 0; i--) {
        const n = byTrail.get(trail.slice(0, i).join("."));
        if (n && nodes.get(n)!.type === side.type) return [n];
      }
      return [];
    }
    if (side.kind === "join") {
      const raw = valueOf(value, side.field || "id");
      const list = Array.isArray(raw) ? raw : (raw === undefined || raw === null || raw === "" ? [] : [raw]);
      const out: string[] = [];
      for (const v of list) {
        const hit = identity.get(`${side.type}|${side.on || side.field}|${v}`);
        if (hit) out.push(hit);
        else unresolved.push({ type: side.type, on: side.on || side.field || "", value: String(v) });
      }
      return out;
    }
    if (side.kind === "position") {
      const n = position.get(`${side.array}|${ordinalOf(trail)}`);
      return n ? [n] : [];
    }
    return [];
  };

  for (const e of edgeRules || []) {
    if (!e.edgeLabel || !e.fromNode || !e.toNode) continue;
    const isJoin = !!(e.fromKey || e.toKey);
    if (!isJoin) continue;                       // containment edges were drawn above
    const anchorType = e.readFrom || (e.toKey && !e.fromKey ? e.fromNode : e.toNode);
    const anchor = anchorOf.get(anchorType) || anchorOf.get(e.fromNode);
    if (!anchor) { warnings.push(`${e.edgeLabel}: no rule produces "${anchorType}"`); continue; }
    const from = e.fromKey
      ? { kind: "join" as RefKind, type: e.fromNode, field: e.fromKey, on: e.fromOn || e.fromKey }
      : { kind: "self" as RefKind, type: e.fromNode };
    const to = e.toKey
      ? { kind: "join" as RefKind, type: e.toNode, field: e.toKey, on: e.toOn || e.toKey }
      : { kind: "self" as RefKind, type: e.toNode };
    for (const { trail, value } of walk(anchor.path, data)) {
      const anchorId = byTrail.get(trail.join(".")) || "";
      const props: Record<string, any> = {};
      for (const p of e.props || []) {
        const v = valueOf(value, p.from_field);
        if (v !== undefined && v !== null && v !== "") props[p.prop] = v;
      }
      for (const s of resolveSide(from, trail, value, anchorId))
        for (const t of resolveSide(to, trail, value, anchorId))
          if (s && t && s !== t) edges.set(`${s}|${e.edgeLabel}|${t}`,
            { source: s, type: e.edgeLabel.toUpperCase(), target: t, props });
    }
  }

  return { nodes: Array.from(nodes.values()), edges: Array.from(edges.values()), unresolved, warnings };
}

/* ───────────────────────────────────────────────────────────────── chunking */

export function chunkText(text: string, cfg: ChunkCfg): { heading: string | null; text: string; label?: string }[] {
  const t = (text || "").trim();
  if (!t) return [];
  const out: { heading: string | null; text: string; label?: string }[] = [];
  if (cfg.strategy === "layout") {
    const lines = t.split("\n");
    let currentHeading: string | null = null;
    let block: string[] = [];
    let inTable = false;

    const flush = (label: string = "Paragraph") => {
      if (block.length) {
        const textContent = block.join(" ").replace(/\s+/g, " ").trim();
        if (textContent) {
          out.push({ heading: currentHeading, text: textContent, label });
        }
        block = [];
      }
    };

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) {
        flush(inTable ? "Table" : "Paragraph");
        inTable = false;
        continue;
      }
      if (trimmed.startsWith("#")) {
        flush(inTable ? "Table" : "Paragraph");
        inTable = false;
        const m = trimmed.match(/^#{1,6}\s+(.+)/);
        currentHeading = m ? m[1].trim() : trimmed;
        out.push({ heading: currentHeading, text: trimmed, label: "SectionHeader" });
      } else if (trimmed.includes("|") && (trimmed.startsWith("|") || trimmed.endsWith("|") || inTable)) {
        inTable = true;
        block.push(trimmed);
      } else if (/^[-*]\s+|\d+\.\s+/.test(trimmed)) {
        if (inTable) {
          flush("Table");
          inTable = false;
        } else {
          flush("Paragraph");
        }
        out.push({ heading: currentHeading, text: trimmed, label: "ListItem" });
      } else {
        if (inTable) {
          flush("Table");
          inTable = false;
        }
        block.push(trimmed);
      }
    }
    flush(inTable ? "Table" : "Paragraph");
    return out;
  }
  const flat = t.replace(/\s+/g, " ");
  const step = Math.max(40, cfg.size - cfg.overlap);
  for (let i = 0; i < flat.length; i += step) {
    out.push({ heading: null, text: flat.slice(i, i + cfg.size), label: "Chunk" });
    if (i + cfg.size >= flat.length) break;
  }
  return out;
}

/* ────────────────────────────────────────────────── auto-propose (discovery) */

const titleCase = (s: string) =>
  s.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()).replace(/\s+/g, "").replace(/s$/, "");


/**
 * Propose key rows + edge rules from a payload nobody has described.
 *
 * This is the step every comparable product leaves to the author — PuppyGraph, TigerGraph, Neo4j and
 * RML all start from a blank mapping. The proposal is a SUGGESTION: node names are marked
 * `auto_type` so the backend keeps them out of the authoritative vocabulary until a human accepts.
 */
export function proposeRules(data: any): { keyRows: KeyRow[]; edgeRules: EdgeRule[] } {
  const keyRows: KeyRow[] = [];
  const edgeRules: EdgeRule[] = [];
  const root = Array.isArray(data) ? data[0] : data;
  if (!root || typeof root !== "object") return { keyRows, edgeRules };

  const addNode = (path: string, sample: any, type: string) => {
    keyRows.push({ path, kind: "node", type: path.endsWith("[]") ? "item" : "object",
                   node_type: type, decision: "node", becomes: "node", auto_type: true });
    // Every scalar is an attribute — an `id` field included: a node's id is generated, not read.
    for (const [k, v] of Object.entries(sample || {})) {
      if (v && typeof v === "object") continue;
      keyRows.push({ path: path ? `${path}.${k}` : k, kind: "attribute",
                     type: typeof v === "number" ? "number" : "string",
                     role: "metadata", becomes: "label", sample: String(v).slice(0, 40) });
    }
  };

  addNode("", root, titleCase("record"));
  for (const [k, v] of Object.entries(root)) {
    if (Array.isArray(v) && v.length && typeof v[0] === "object") addNode(`${k}[]`, v[0], titleCase(k));
    else if (v && typeof v === "object" && !Array.isArray(v)) addNode(k, v, titleCase(k));
  }

  // A `*_id` on one node whose name matches another node's identity column is a foreign key — the
  // one relationship a tree walk can never see, and the reason the join primitive exists.
  const nodes = keyRows.filter((r) => r.kind === "node" && r.node_type);
  for (const n of nodes) {
    const prefix = n.path ? `${n.path}.` : "";
    for (const a of keyRows) {
      if (a.kind !== "attribute" || !a.path.startsWith(prefix)) continue;
      const col = a.path.slice(prefix.length);
      if (col.includes(".") || !/_id$/i.test(col)) continue;
      const targetName = titleCase(col.replace(/_id$/i, ""));
      const target = nodes.find((m) => m.node_type === targetName && m !== n);
      if (!target) continue;
      // the value is matched against the same-named field on the target, else its plain `id`
      const tPrefix = target.path ? `${target.path}.` : "";
      const tKey = [col, "id"].map((f) => keyRows.find((x) => x.kind === "attribute" && x.path === `${tPrefix}${f}`))
        .find(Boolean);
      edgeRules.push({
        id: `auto-${edgeRules.length}`,
        fromNode: n.node_type!, toNode: target.node_type!,
        edgeLabel: `HAS_${targetName.toUpperCase()}`,
        toKey: col,
        toOn: tKey ? tKey.path.split(".").pop()! : col,
        readFrom: n.node_type!, direction: "out", props: [],
      });
    }
  }
  return { keyRows, edgeRules };
}

/** Minimal CSV → rows, for the structured tab's upload. */
export function parseCSV(text: string): Record<string, any>[] {
  const lines = text.trim().split(/\r?\n/).filter(Boolean);
  if (!lines.length) return [];
  const split = (l: string) =>
    (l.match(/("([^"]|"")*"|[^,]*)(,|$)/g) || []).slice(0, -1)
      .map((s) => s.replace(/,$/, "").replace(/^"|"$/g, "").replace(/""/g, '"').trim());
  const head = split(lines[0]);
  return lines.slice(1).map((l) => {
    const cells = split(l);
    const row: Record<string, any> = {};
    head.forEach((h, i) => {
      const raw = cells[i];
      row[h] = raw !== "" && raw !== undefined && !isNaN(Number(raw)) ? Number(raw) : raw;
    });
    return row;
  });
}

/**
 * A parsed document from the layout reader's reading of a file (PDF, Word, ...), in the same shape as
 * `docFromText`: one section per heading, holding the elements under it with their own kind
 * (text, list_item, table, ...). Running page headers and footers are kept apart, in
 * `page_furniture`, because they belong to the document rather than to any section.
 */
export function docFromLayout(name: string, elements: { id: string; label: string; text: string; level: number | null; page: number; parent_id: string | null }[]) {
  const BEFORE = "(before the first heading)";
  const sections: { heading: string; level: number; page: number; blocks: { ord: number; kind: string; text: string }[] }[] = [];
  const byId: Record<string, (typeof sections)[number]> = {};
  const furniture: { ord: number; kind: string; page: number; text: string }[] = [];
  elements.forEach((e, i) => {
    const ord = i + 1;
    if (e.label === "page_header" || e.label === "page_footer") {
      furniture.push({ ord, kind: e.label, page: e.page, text: e.text });
      return;
    }
    if (e.label === "title" || e.label === "section_header") {
      const s = { heading: e.text, level: e.level ?? 1, page: e.page, blocks: [] };
      sections.push(s);
      byId[e.id] = s;
      return;
    }
    let s = (e.parent_id && byId[e.parent_id]) || sections[sections.length - 1];
    if (!s) {
      s = { heading: BEFORE, level: 0, page: e.page, blocks: [] };
      sections.push(s);
    }
    s.blocks.push({ ord, kind: e.label, text: e.text });
  });
  const title = sections.find((s) => s.level === 0 && s.heading !== BEFORE);
  return {
    document: {
      doc_id: name.replace(/\.[^.]+$/, "").slice(0, 32),
      title: title?.heading || name,
      pages: Math.max(1, ...elements.map((e) => e.page || 1)),
      sections,
      page_furniture: furniture,
    },
    mentions: [] as any[],
  };
}

/** A parsed document from raw text, in the shape the unstructured preview walks. */
export function docFromText(name: string, text: string) {
  const chunks = chunkText(text, { strategy: "sliding", mode: "save", size: 1200, overlap: 0, node: "Chunk" });
  return {
    document: {
      doc_id: name.replace(/\.[^.]+$/, "").slice(0, 32),
      title: name,
      pages: 1,
      sections: chunks.map((c, i) => ({
        heading: c.heading || `Section ${i + 1}`,
        page: i + 1,
        blocks: [{ ord: i * 2 + 2, kind: "paragraph", text: c.text }],
      })),
    },
    mentions: [] as any[],
  };
}

/* ──────────────────────────────────────────────── structured: rules from a LIVE schema */

export interface SqlColumn { name: string; type?: string; primary_key?: boolean; foreign_key?: boolean; references_table?: string; references_column?: string }
export interface SqlTable { name: string; schema?: string; row_count?: number; columns: SqlColumn[] }

/**
 * Node and edge rules from an introspected SQL schema.
 *
 * Better than proposing from a CSV, and not by a little: the database already KNOWS which column is
 * the primary key and which is a foreign key, so nothing has to be guessed from a name. That is the
 * exact difference that avoids the `customer_id`-in-`customers` trap — a primary key whose name ends
 * in `_id`, read as a reference, dropped from the properties, and left as a factless duplicate.
 */
export function proposeFromTables(tables: SqlTable[]): { keyRows: KeyRow[]; edgeRules: EdgeRule[] } {
  const keyRows: KeyRow[] = [];
  const edgeRules: EdgeRule[] = [];
  const typeOf = (t: string) => titleCase(t);
  const pkOf = (t: SqlTable) => (t.columns || []).filter((c) => c.primary_key).map((c) => c.name);

  for (const t of tables) {
    const path = `${t.name}[]`;
    keyRows.push({ path, kind: "node", type: "item", node_type: typeOf(t.name),
                   decision: "node", becomes: "node", auto_type: true });
    for (const c of t.columns || []) {
      keyRows.push({
        path: `${path}.${c.name}`, kind: "attribute",
        type: /int|numeric|float|double|decimal/i.test(c.type || "") ? "number" : "string",
        // the schema's own answer, not an inference from the column's name
        role: c.primary_key ? "id" : c.foreign_key ? "metadata" : "metadata",
        becomes: c.primary_key ? "id" : "label",
        sample: c.type || "",
      });
    }
  }
  for (const t of tables) {
    for (const c of t.columns || []) {
      if (!c.foreign_key || !c.references_table) continue;
      const target = tables.find((x) => x.name === c.references_table);
      if (!target) continue;
      edgeRules.push({
        id: `fk-${t.name}-${c.name}`,
        fromNode: typeOf(t.name), toNode: typeOf(target.name),
        edgeLabel: `HAS_${typeOf(target.name).toUpperCase()}`,
        toKey: c.name,
        toOn: c.references_column || pkOf(target)[0] || c.name,
        readFrom: typeOf(t.name), direction: "out", props: [],
      });
    }
  }
  return { keyRows, edgeRules };
}
