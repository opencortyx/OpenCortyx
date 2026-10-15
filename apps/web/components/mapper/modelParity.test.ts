/**
 * The builder preview must build the same graph ingestion builds from the same fixture
 * (see apps/api/tests/integration/test_mapper_api.py).
 */
import { describe, expect, it } from "@jest/globals";
import type { EdgeRule, KeyRow } from "./model";
import { chunkText, materialize, proposeFromTables, proposeRules, walk } from "./model";

const data = {
  lines: [{ sku: "MUG-01", qty: 2 }, { sku: "PAN-77", qty: 1 }, { sku: "GHOST", qty: 9 }],
  products: [{ sku: "MUG-01", name: "Enamel mug" }, { sku: "PAN-77", name: "Steel pan" }],
};

const rows = [
  { path: "", kind: "node", node_type: "OrderRoot", decision: "node", becomes: "node" },
  { path: "lines[]", kind: "node", node_type: "Line", decision: "node", becomes: "node",
    edge_type: "HAS_LINE", attachToNode: "OrderRoot" },
  { path: "lines[].sku", kind: "attribute", role: "metadata", becomes: "label" },
  { path: "lines[].qty", kind: "attribute", role: "measure", becomes: "measure" },
  { path: "products[]", kind: "node", node_type: "Product", decision: "node", becomes: "node" },
  { path: "products[].sku", kind: "attribute", role: "id", becomes: "id" },
  { path: "products[].name", kind: "attribute", role: "metadata", becomes: "label" },
] as unknown as KeyRow[];

const edges = [{ id: "e1", fromNode: "Line", toNode: "Product", edgeLabel: "OF_PRODUCT",
                 toKey: "sku", readFrom: "Line", props: [{ prop: "qty", from_field: "qty" }] }] as unknown as EdgeRule[];

describe("materialize: value-matched edges", () => {
  const g = materialize(rows, edges, data, {});
  const of = g.edges.filter((e) => e.type === "OF_PRODUCT");

  it("matches the backend's edge count", () => {
    expect(of.length).toBe(2);
  });

  it("carries edge properties", () => {
    expect(of.map((e) => e.props.qty).sort()).toEqual([1, 2]);
  });

  it("reports an unmatched value instead of dropping it", () => {
    expect(g.unresolved.map((u: any) => `${u.type}.${u.on}=${u.value}`)).toEqual(["Product.sku=GHOST"]);
  });

  it("merges nodes on a declared id", () => {
    expect(g.nodes.filter((n) => n.type === "Product").map((n) => n.id).sort())
      .toEqual(["Product:MUG-01", "Product:PAN-77"]);
  });

  it("still draws containment edges", () => {
    expect(g.edges.filter((e) => e.type === "HAS_LINE").length).toBe(3);
  });
});

describe("materialize: identity", () => {
  it("keeps parent-scoped ids of different parents apart", () => {
    const scoped = materialize([
      { path: "convs[]", kind: "node", node_type: "Conv", decision: "node" },
      { path: "convs[].id", kind: "attribute", role: "id" },
      { path: "convs[].turns[]", kind: "node", node_type: "Turn", decision: "node", id_scope: "parent", edge_type: "HAS_TURN" },
      { path: "convs[].turns[].id", kind: "attribute", role: "id" },
    ] as unknown as KeyRow[], [], { convs: [{ id: "c1", turns: [{ id: "1" }, { id: "2" }] }, { id: "c2", turns: [{ id: "1" }] }] }, {});
    expect(scoped.nodes.filter((n) => n.type === "Turn").length).toBe(3);
  });
});

describe("proposeRules", () => {
  const p = proposeRules({ orders: [{ order_id: "O1", customer_id: "C1" }],
                           customers: [{ customer_id: "C1", name: "Ann" }] });

  it("finds a foreign key a tree walk cannot see", () => {
    expect(p.edgeRules.map((e) => `${e.fromNode}-${e.edgeLabel}->${e.toNode} on ${e.toKey}`))
      .toEqual(["Order-HAS_CUSTOMER->Customer on customer_id"]);
  });

  it("keeps id fields as plain attributes and joins on the same-named field", () => {
    expect(p.keyRows.filter((r) => r.kind === "attribute").every((r) => r.role === "metadata")).toBe(true);
    expect(p.edgeRules[0].toOn).toBe("customer_id");
  });

  it("marks proposed type names as suggestions", () => {
    expect(p.keyRows.filter((r) => r.kind === "node").every((r: any) => r.auto_type === true)).toBe(true);
  });
});

describe("proposeFromTables", () => {
  const sp = proposeFromTables([
    { name: "customers", row_count: 2, columns: [
        { name: "customer_id", type: "VARCHAR", primary_key: true, foreign_key: false },
        { name: "name", type: "VARCHAR", primary_key: false, foreign_key: false } ] },
    { name: "orders", row_count: 2, columns: [
        { name: "order_id", type: "VARCHAR", primary_key: true, foreign_key: false },
        { name: "customer_id", type: "VARCHAR", primary_key: false, foreign_key: true,
          references_table: "customers", references_column: "customer_id" } ] },
  ] as any);

  it("reads a primary key named *_id as identity", () => {
    expect(sp.keyRows.find((r) => r.path === "customers[].customer_id")?.role).toBe("id");
  });

  it("does not treat the same column name in another table as identity", () => {
    expect(sp.keyRows.find((r) => r.path === "orders[].customer_id")?.role).toBe("metadata");
  });

  it("turns a foreign key into an edge matched on the target's key", () => {
    expect(sp.edgeRules.map((e: any) => `${e.fromNode}-${e.edgeLabel}->${e.toNode} ${e.toKey}→${e.toOn}`))
      .toEqual(["Order-HAS_CUSTOMER->Customer customer_id→customer_id"]);
  });

  it("creates a node rule per table", () => {
    expect(sp.keyRows.filter((r) => r.kind === "node").map((r) => r.node_type)).toEqual(["Customer", "Order"]);
  });
});
describe("chunking and paths", () => {
  it("extracts layout-aware chunks with headings", () => {
    expect(chunkText("## A\nfirst.\n\n## B\nsecond.",
                     { strategy: "layout", mode: "save", size: 1200, overlap: 0, node: "Chunk" })
      .map((c) => c.heading)).toEqual(["A", "A", "B", "B"]);
  });

  it("extracts sliding window chunks", () => {
    const chunks = chunkText("first chunk text that is quite short.",
                             { strategy: "sliding", mode: "save", size: 100, overlap: 0, node: "Chunk" });
    expect(chunks.length).toBe(1);
    expect(chunks[0].label).toBe("Chunk");
  });

  it("uses the walker's path grammar", () => {
    expect(walk("lines[].sku", data).map((h) => h.value)).toEqual(["MUG-01", "PAN-77", "GHOST"]);
  });

  it("treats the empty path as the record root", () => {
    expect(walk("", { a: 1 }).length).toBe(1);
  });
});
