/**
 * The declared graph's SHAPE — computed the way ingestion computes it, not the way the canvas guessed.
 *
 * WHY THIS FILE EXISTS
 * The canvas used to decide on its own which rules to draw and where the arrows go, and it decided
 * differently from the compiler that actually runs the mapping. Measured on a real mapping
 * (`locomo_test`): the canvas drew 13 types and 2 arrows for a mapping that compiles to 4 types and a
 * clean 3-edge chain. Two causes:
 *   1. it drew a box for EVERY node rule, including the 11 set to Ignore — types ingestion never makes;
 *   2. it drew only the explicit edge rules and never the containment the node rows carry, so the root
 *      `Dialogue` had no arrow at all and was laid out in a column with the ignored rules.
 *
 * Every rule below mirrors a named line of the Python compiler, so the picture is what ingestion builds
 * rather than a second opinion that can quietly disagree with it:
 *
 *   which rules produce a node   translate._node_key       `becomes` beats `decision`; ignore → skip;
 *                                                          embed folds into the parent (no own node)
 *   editor path → walker path    schema.canonical_path     the ROOT_PATH_ALIASES are folded away
 *   natural parent               schema.compile_walk_spec  the deepest active ancestor path
 *   a declared parent            translate._apply_edge_rules
 *                                a containment edge rule overrides the row's Attach → X, and it only
 *                                takes effect when it names a type some rule produces AND differs from
 *                                the natural parent
 *
 * Pure functions: no React, no DOM. Tested directly in declaredShape.test.ts.
 */
import type { EdgeRule, KeyRow } from "./model";
import { canonicalPath, nodeDecision, ownsNode, ROOT_PATH_ALIASES } from "./model";

// ONE definition of the root sentinel, in model.ts, shared by the preview's walker and this shape. Two
// copies are how a picture and a preview come to disagree about what `payload_root` means.
export { canonicalPath, ROOT_PATH_ALIASES };

export interface CanvasPos { x: number; y: number }

export const NODE_W = 132;
export const NODE_H = 44;
export const HANDLE_R = 6;
export const COL_GAP = 208;   // > NODE_W, so a column clears the previous one and its arrow label
export const ROW_GAP = 76;    // > NODE_H
export const PAD = 56;

/**
 * Is `anc` an ancestor of `path`? Segment-aware, deliberately.
 *
 * A plain string prefix says `conversation` is an ancestor of `conversation_meta`, which is two sibling
 * keys that merely share letters. The Python compiler made exactly that mistake, and the harmful
 * consequence is silent: an author's declared parent is judged to "match the natural one" and dropped,
 * so the node attaches somewhere the arrow never said. The one legitimate non-dotted case is a
 * container and its elements — `sessions` holds `sessions[]` — and that is kept.
 */
export function isAncestorPath(anc: string, path: string): boolean {
  if (anc === path) return false;
  if (anc === "") return path !== "";
  return path.startsWith(`${anc}.`) || path === `${anc}[]` || path.startsWith(`${anc}[].`);
}

export interface ShapeNode {
  type: string;
  path: string;        // canonical path of the first rule declaring this type
  rowIndex: number;    // the rule that opens when the box is clicked
  rules: number;       // how many active rules declare this type
}

export interface ShapeEdge {
  from: string;
  to: string;
  label: string;
  kind: "containment" | "join";
  /** Set when an explicit edge rule backs this arrow — then it can be selected and deleted. */
  ruleIndex?: number;
  /** For containment that comes from nesting alone: the child rule to open on click. */
  rowIndex?: number;
}

/** A reference that points at nothing drawable, with what ingestion does about it — never dropped silently. */
export interface DanglingRef {
  from: string;
  to: string;
  label: string;
  reason: string;
  ruleIndex?: number;
  rowIndex?: number;
}

export interface DeclaredShape {
  nodes: ShapeNode[];
  edges: ShapeEdge[];
  dangling: DanglingRef[];
  activeRules: number;    // node rules that produce a node of their own
  ignoredRules: number;   // set to Ignore / skip: not ingested at all
  embeddedRules: number;  // embedded: ingested, but folded into the parent
}

const fallbackLabel = (type: string) => `HAS_${type.replace(/[^A-Za-z0-9]/g, "").toUpperCase()}`;

export function buildDeclaredShape(rows: KeyRow[], edgeRules: EdgeRule[]): DeclaredShape {
  const active: { r: KeyRow; i: number; cp: string }[] = [];
  let ignoredRules = 0;
  let embeddedRules = 0;
  rows.forEach((r, i) => {
    if (r.kind !== "node" || !r.node_type) return;
    if (ownsNode(r)) active.push({ r, i, cp: canonicalPath(r.path) });
    else if (nodeDecision(r) === "embed") embeddedRules += 1;
    else ignoredRules += 1;
  });

  const byType = new Map<string, ShapeNode>();
  for (const { r, i, cp } of active) {
    const t = r.node_type!;
    const cur = byType.get(t);
    if (cur) cur.rules += 1;
    else byType.set(t, { type: t, path: cp, rowIndex: i, rules: 1 });
  }
  const nodes = Array.from(byType.values());
  const activeTypes = new Set(nodes.map((n) => n.type));

  const edges: ShapeEdge[] = [];
  const dangling: DanglingRef[] = [];
  // translate._apply_edge_rules: a containment rule is addressed to the CHILD type it re-parents.
  const containmentByChild = new Map<string, { index: number; from: string; label: string }>();

  edgeRules.forEach((e, index) => {
    const label = String(e.edgeLabel || "").trim();
    if (e.fromKey || e.toKey) {
      // A JOIN matches values; it has nothing to do with nesting and is drawn exactly as declared.
      if (activeTypes.has(e.fromNode) && activeTypes.has(e.toNode)) {
        edges.push({ from: e.fromNode, to: e.toNode, label, kind: "join", ruleIndex: index });
      } else {
        const missing = [e.fromNode, e.toNode].filter((t) => !activeTypes.has(t));
        dangling.push({
          from: e.fromNode, to: e.toNode, label, ruleIndex: index,
          reason: `joins on ${missing.map((m) => `"${m}"`).join(" and ")}, which no active rule produces, so this edge is never created`,
        });
      }
      return;
    }
    const [from, to] = (e.direction || "out") === "in" ? [e.toNode, e.fromNode] : [e.fromNode, e.toNode];
    if (!activeTypes.has(to)) {
      dangling.push({
        from, to, label, ruleIndex: index,
        reason: `"${to}" is not produced by any active rule, so this edge has nothing to attach to and is ignored`,
      });
      return;
    }
    containmentByChild.set(to, { index, from, label });
  });

  const seen = new Set<string>();
  for (const { r, i, cp } of active) {
    if (cp === "") continue;                                   // the root has no parent
    const childType = r.node_type!;

    let natural: { r: KeyRow; cp: string } | null = null;
    for (const cand of active) {
      if (isAncestorPath(cand.cp, cp) && (!natural || cand.cp.length > natural.cp.length)) natural = cand;
    }
    const naturalType = natural?.r.node_type ?? null;

    // `by_type.setdefault` in the compiler: an edge rule binds to the FIRST rule declaring that type.
    const override = byType.get(childType)!.rowIndex === i ? containmentByChild.get(childType) : undefined;
    const declaredParent = override?.from ?? r.attachToNode ?? null;

    let parentType = naturalType;
    if (declaredParent && declaredParent !== naturalType) {
      if (activeTypes.has(declaredParent)) {
        parentType = declaredParent;                           // an explicit re-parent that resolves
      } else {
        dangling.push({
          from: declaredParent, to: childType, label: override?.label || r.edge_type || fallbackLabel(childType),
          ruleIndex: override?.index, rowIndex: override ? undefined : i,
          reason: `"${declaredParent}" is not a type any active rule produces, so ingestion attaches ${childType} ` +
                  (naturalType ? `to its natural parent (${naturalType}) instead` : "to nothing — it has no parent in this mapping"),
        });
      }
    }
    if (!parentType) continue;

    const label = (override?.label || r.edge_type || fallbackLabel(childType)).trim();
    const [from, to] = r.edge_dir === "in" ? [childType, parentType] : [parentType, childType];
    const key = `${from}|${label}|${to}`;
    if (seen.has(key)) continue;
    seen.add(key);
    edges.push({ from, to, label, kind: "containment", ruleIndex: override?.index, rowIndex: i });
  }

  return { nodes, edges, dangling, activeRules: active.length, ignoredRules, embeddedRules };
}

/**
 * Place the declared types as a left-to-right DAG: one depth per column, siblings stacked.
 *
 * Depth comes from the shape's edges, so a type nothing points at is a root. What changed is the
 * overflow: a depth holding more types than fit is split into several columns instead of one long
 * stack. The old layout only wrapped when there were NO edges at all, so a handful of edges plus many
 * unconnected types produced exactly the picture that prompted this — one 11-box column and an empty
 * half-canvas. Connected types are placed first so the chain reads from the top.
 */
export function autoLayout(shape: Pick<DeclaredShape, "nodes" | "edges">): Record<string, CanvasPos> {
  const out: Record<string, CanvasPos> = {};
  const types = shape.nodes.map((n) => n.type);
  if (!types.length) return out;
  const known = new Set(types);
  const rel = shape.edges.filter((e) => known.has(e.from) && known.has(e.to) && e.from !== e.to);

  // Longest-path layering, capped at the node count so a cycle settles instead of spinning.
  const depth = new Map<string, number>(types.map((t) => [t, 0]));
  for (let pass = 0; pass < types.length; pass++) {
    let moved = false;
    for (const e of rel) {
      const d = (depth.get(e.from) ?? 0) + 1;
      if (d > (depth.get(e.to) ?? 0) && d < types.length) { depth.set(e.to, d); moved = true; }
    }
    if (!moved) break;
  }

  const connected = new Set(rel.flatMap((e) => [e.from, e.to]));
  const byDepth = new Map<number, string[]>();
  for (const t of types) {
    const d = depth.get(t) ?? 0;
    (byDepth.get(d) || byDepth.set(d, []).get(d)!).push(t);
  }

  const maxRows = Math.max(4, Math.ceil(Math.sqrt(types.length)));
  const columns: string[][] = [];
  Array.from(byDepth.keys()).sort((a, b) => a - b).forEach((d) => {
    const list = byDepth.get(d)!;
    const ordered = [...list.filter((t) => connected.has(t)), ...list.filter((t) => !connected.has(t))];
    for (let k = 0; k < ordered.length; k += maxRows) columns.push(ordered.slice(k, k + maxRows));
  });

  const tallest = Math.max(...columns.map((c) => c.length));
  columns.forEach((list, ci) => {
    // Centre each column against the tallest one, so the picture reads as a tree not a staircase.
    const top = PAD + ((tallest - list.length) * ROW_GAP) / 2;
    list.forEach((t, ri) => {
      out[t] = { x: Math.round(PAD + NODE_W / 2 + ci * COL_GAP), y: Math.round(top + ri * ROW_GAP) };
    });
  });
  return out;
}
