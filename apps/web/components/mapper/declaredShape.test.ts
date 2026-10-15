/**
 * The declared graph must show the shape ingestion builds — pinned against a REAL mapping.
 *
 * `LOCOMO_*` below is the `locomo_test` mapping exactly as it is saved. Its compiled walk spec (read
 * from the running API) is four node types — Dialogue, Conversation, Turn, Img_url — joined by
 * HAS_CONVERSATION, HAS_TURN and CONTAINS_IMG_URL, with the other 11 branches skipped. The canvas drew
 * 13 types and 2 arrows for it. Every assertion here is a statement the compiler makes about that same
 * mapping, so if the picture and ingestion ever disagree again, this fails.
 */
import { describe, expect, it } from "@jest/globals";
import type { EdgeRule, KeyRow } from "./model";
import { nodeDecision, ownsNode } from "./model";
import {
  autoLayout, buildDeclaredShape, canonicalPath, isAncestorPath, ROOT_PATH_ALIASES,
  type DeclaredShape,
} from "./declaredShape";

const node = (
  becomes: string, node_type: string, path: string, edge_type: string, attachToNode?: string
): KeyRow => ({
  kind: "node", becomes, decision: "node", node_type, path, edge_type,
  ...(attachToNode ? { attachToNode } : {}),
});

const LOCOMO_ROWS: KeyRow[] = [
  node("node", "Dialogue", "payload_root", "ROOT"),
  node("ignore", "Qa", "payload_root[].qa[]", "CONTAINS_QA", "Record"),
  node("ignore", "Evidence", "payload_root[].qa[].evidence[]", "CONTAINS_EVIDENCE", "Qa"),
  node("node", "Conversation", "payload_root[].conversation", "HAS_CONVERSATION", "Record"),
  node("node", "Turn", "payload_root[].conversation.session_*[]", "HAS_TURN", "Conversation"),
  node("node", "Img_url", "payload_root[].conversation.session_*[].img_url[]", "CONTAINS_IMG_URL", "Session_9"),
  node("ignore", "Event_summary", "payload_root[].event_summary", "HAS_EVENT_SUMMARY", "Record"),
  node("ignore", "Events_session", "payload_root[].event_summary.events_session_*", "HAS_EVENTS_SESSION", "Event_summary"),
  node("ignore", "Joanna", "payload_root[].event_summary.events_session_*.Joanna[]", "CONTAINS_JOANNA", "Events_session_1"),
  node("ignore", "Nate", "payload_root[].event_summary.events_session_*.Nate[]", "CONTAINS_NATE", "Events_session_1"),
  node("ignore", "Observation", "payload_root[].observation", "HAS_OBSERVATION", "Record"),
  node("ignore", "Session_observation", "payload_root[].observation.session_*_observation", "HAS_SESSION_OBSERVATION", "Observation"),
  node("ignore", "Nate", "payload_root[].observation.session_*_observation.Nate[]", "CONTAINS_NATE", "Session_1_observation"),
  node("ignore", "Joanna", "payload_root[].observation.session_*_observation.Joanna[]", "CONTAINS_JOANNA", "Session_1_observation"),
  node("ignore", "Session_summary", "payload_root[].session_summary", "HAS_SESSION_SUMMARY", "Record"),
];

const LOCOMO_EDGES: EdgeRule[] = [
  { id: "er-6", fromNode: "Record", toNode: "Conversation", direction: "out", edgeLabel: "HAS_CONVERSATION", cardinality: "1:N" },
  { id: "er-10", fromNode: "Conversation", toNode: "Turn", direction: "out", edgeLabel: "HAS_TURN", cardinality: "1:N" },
  { id: "er-14", fromNode: "Turn", toNode: "Img_url", direction: "out", edgeLabel: "CONTAINS_IMG_URL", cardinality: "1:N" },
];

const arrows = (s: DeclaredShape) => s.edges.map((e) => `${e.from} -${e.label}-> ${e.to}`).sort();

describe("the real mapping: the picture matches what ingestion builds", () => {
  const shape = buildDeclaredShape(LOCOMO_ROWS, LOCOMO_EDGES);

  it("draws the 4 types the mapping compiles to — not 13", () => {
    expect(shape.nodes.map((n) => n.type)).toEqual(["Dialogue", "Conversation", "Turn", "Img_url"]);
  });

  it("draws the whole chain, root included", () => {
    expect(arrows(shape)).toEqual([
      "Conversation -HAS_TURN-> Turn",
      "Dialogue -HAS_CONVERSATION-> Conversation",
      "Turn -CONTAINS_IMG_URL-> Img_url",
    ]);
  });

  it("never draws a box or an arrow for a rule set to Ignore", () => {
    const ignored = new Set(LOCOMO_ROWS.filter((r) => r.becomes === "ignore").map((r) => r.node_type));
    expect(shape.nodes.some((n) => ignored.has(n.type))).toBe(false);
    expect(shape.edges.some((e) => ignored.has(e.from) || ignored.has(e.to))).toBe(false);
  });

  it("counts every rule honestly, including the ones it hides", () => {
    expect(shape.activeRules).toBe(4);
    expect(shape.ignoredRules).toBe(11);
    expect(shape.embeddedRules).toBe(0);
  });

  it("draws a relationship declared both by nesting and by an edge rule ONCE", () => {
    expect(shape.edges).toHaveLength(3);
  });

  it("reports the stale `Record` parent instead of dropping it — and says where it really attaches", () => {
    expect(shape.dangling).toHaveLength(1);
    const [d] = shape.dangling;
    expect([d.from, d.to]).toEqual(["Record", "Conversation"]);
    expect(d.reason).toContain("Dialogue");
  });

  it("lays the chain out left to right, one type per column", () => {
    const pos = autoLayout(shape);
    const xs = ["Dialogue", "Conversation", "Turn", "Img_url"].map((t) => pos[t].x);
    expect(xs).toEqual([...xs].sort((a, b) => a - b));
    expect(new Set(xs).size).toBe(4);
    expect(new Set(Object.values(pos).map((p) => p.y)).size).toBe(1);
  });
});

describe("which rules produce a node — mirrors translate._node_key", () => {
  const r = (extra: Partial<KeyRow>): KeyRow => ({ kind: "node", path: "x", node_type: "X", ...extra });

  it("lets the user's `becomes` beat the detector's `decision`", () => {
    expect(ownsNode(r({ becomes: "node", decision: "skip" }))).toBe(true);
    expect(ownsNode(r({ becomes: "ignore", decision: "node" }))).toBe(false);
  });

  it("gives Ignore, skip and embed no node of their own", () => {
    expect(ownsNode(r({ becomes: "ignore" }))).toBe(false);
    expect(ownsNode(r({ decision: "skip" }))).toBe(false);
    expect(ownsNode(r({ becomes: "embed" }))).toBe(false);
    expect(nodeDecision(r({ becomes: "ignore" }))).toBe("skip");
  });

  it("keeps containers and dict rules as nodes", () => {
    expect(ownsNode(r({ becomes: "container" }))).toBe(true);
    expect(ownsNode(r({ becomes: "dict" }))).toBe(true);
  });

  it("separates embedded rules from ignored ones in the counts", () => {
    const shape = buildDeclaredShape(
      [r({ node_type: "A", path: "", becomes: "node" }), r({ node_type: "B", path: "b", becomes: "embed" }),
       r({ node_type: "C", path: "c", becomes: "ignore" })], []);
    expect([shape.activeRules, shape.embeddedRules, shape.ignoredRules]).toEqual([1, 1, 1]);
  });
});

describe("paths — mirrors schema.canonical_path", () => {
  it("folds every root alias, whole and as a prefix", () => {
    for (const alias of ROOT_PATH_ALIASES) {
      expect(canonicalPath(alias)).toBe("");
      expect(canonicalPath(`${alias}.conversation`)).toBe("conversation");
    }
  });

  it("leaves strings that are not aliases alone, exactly as the compiler does", () => {
    expect(canonicalPath("$")).toBe("$");
    expect(canonicalPath("root")).toBe("root");
  });

  it("treats a shared NAME prefix as a sibling, not an ancestor", () => {
    expect(isAncestorPath("conversation", "conversation_meta")).toBe(false);
    expect(isAncestorPath("conversation", "conversation.turns[]")).toBe(true);
  });

  it("keeps a container as the parent of its own elements", () => {
    expect(isAncestorPath("sessions", "sessions[]")).toBe(true);
    expect(isAncestorPath("sessions", "sessions[].turns[]")).toBe(true);
  });

  it("makes the root an ancestor of everything but itself", () => {
    expect(isAncestorPath("", "anything")).toBe(true);
    expect(isAncestorPath("", "")).toBe(false);
    expect(isAncestorPath("a.b", "a.b")).toBe(false);
  });

  it("attaches a sibling whose name extends another's to the ROOT, not to that sibling", () => {
    const shape = buildDeclaredShape([
      node("node", "Root", "payload_root", "ROOT"),
      node("node", "Conversation", "conversation", "HAS_CONVERSATION"),
      node("node", "Meta", "conversation_meta", "HAS_META"),
    ], []);
    expect(arrows(shape)).toContain("Root -HAS_META-> Meta");
    expect(arrows(shape)).not.toContain("Conversation -HAS_META-> Meta");
  });
});

describe("declared parents and joins", () => {
  it("draws an explicit re-parent to a type beside the child — Order -HAVE_ITEM-> Item", () => {
    const shape = buildDeclaredShape([
      node("node", "Payload", "payload_root", "ROOT"),
      node("node", "Order", "payload_root[].order", "HAS_ORDER"),
      node("node", "Item", "payload_root[].items[]", "HAS_ITEMS"),
    ], [{ id: "e1", fromNode: "Order", toNode: "Item", edgeLabel: "HAVE_ITEM", direction: "out" }]);
    expect(arrows(shape)).toEqual(["Order -HAVE_ITEM-> Item", "Payload -HAS_ORDER-> Order"]);
    expect(shape.dangling).toHaveLength(0);
  });

  it("reports an Attach → pointing at a type nobody produces, with no edge rule behind it", () => {
    const shape = buildDeclaredShape([
      node("node", "Root", "payload_root", "ROOT"),
      node("node", "Child", "payload_root[].child", "HAS_CHILD", "Ghost"),
    ], []);
    expect(arrows(shape)).toEqual(["Root -HAS_CHILD-> Child"]);
    expect(shape.dangling.map((d) => d.from)).toEqual(["Ghost"]);
    expect(shape.dangling[0].rowIndex).toBe(1);
  });

  it("reports a join whose endpoint is not produced instead of silently not drawing it", () => {
    const shape = buildDeclaredShape([node("node", "A", "payload_root", "ROOT")],
      [{ id: "j", fromNode: "A", toNode: "Z", edgeLabel: "REFS", fromKey: "id", toKey: "a_id" }]);
    expect(shape.edges).toHaveLength(0);
    expect(shape.dangling).toHaveLength(1);
    expect(shape.dangling[0].reason).toContain('"Z"');
  });

  it("draws a join that resolves as a join, not as containment", () => {
    const shape = buildDeclaredShape([
      node("node", "Root", "payload_root", "ROOT"),
      node("node", "A", "payload_root[].a[]", "HAS_A"),
      node("node", "B", "payload_root[].b[]", "HAS_B"),
    ], [{ id: "j", fromNode: "A", toNode: "B", edgeLabel: "REFS", fromKey: "b_id", toKey: "id" }]);
    const join = shape.edges.find((e) => e.label === "REFS");
    expect(join?.kind).toBe("join");
    expect(join?.ruleIndex).toBe(0);
  });
});

describe("layout", () => {
  it("never stacks unconnected types into one long column", () => {
    const rows = Array.from({ length: 13 }, (_, k) => node("node", `T${k}`, `t${k}`, `HAS_T${k}`));
    const pos = autoLayout(buildDeclaredShape(rows, []));
    const perColumn = new Map<number, number>();
    Object.values(pos).forEach((p) => perColumn.set(p.x, (perColumn.get(p.x) || 0) + 1));
    expect(Math.max(...Array.from(perColumn.values()))).toBeLessThanOrEqual(4);
  });

  it("gives every type its own slot", () => {
    const shape = buildDeclaredShape(LOCOMO_ROWS.map((r) => ({ ...r, becomes: "node" })), LOCOMO_EDGES);
    const slots = Object.values(autoLayout(shape)).map((p) => `${p.x},${p.y}`);
    expect(new Set(slots).size).toBe(slots.length);
  });
});
