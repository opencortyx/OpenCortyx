import { expect, test } from "@jest/globals";
import type { KnowledgeEntity, ShapeSource } from "@/lib/api/contextV2";
import { carriedFields, identifierChoices, measure, relationName, savedEntity } from "./knowledge";

const field = (name: string, extra: Partial<ShapeSource["fields"][number]> = {}) => ({
  name, source: name, data_type: "text", filled: 3, distinct: 3, repeats: 0, words: 2, identifier: false, nameable: false, ...extra,
});
const entity = (patch: Partial<KnowledgeEntity> = {}): KnowledgeEntity => ({
  type: "Customer", keys: ["id"], kind: "entity", path: "", assets: [{ id: "a1", name: "customers.csv", fields: {} }],
  properties: [], materialize: true, status: "accepted", confidence: 0.8, rows: 0, ...patch,
});
const shapes = (fields: ShapeSource["fields"], records = 3): Record<string, ShapeSource[]> => ({
  a1: [{ asset_id: "a1", asset_name: "customers.csv", kind: "structured", path: "", records, fields, rows: [] }],
});

test("a relationship is named after the kind it points to, as the server names it", () => {
  expect(relationName("Customer")).toBe("CUSTOMER");
  expect(relationName("Sessions (haystack_session_ids)")).toBe("SESSIONS_HAYSTACK_SESSION_IDS");
  expect(relationName("  ")).toBe("REFERENCES");
});

test("only a field the data shows unique is offered as the identifier; with one record, any filled field but a borrowed one", () => {
  const many = measure(entity(), shapes([field("id", { identifier: true }), field("city")]));
  expect(identifierChoices(many).map((c) => c.field.name)).toEqual(["id"]);

  const one = measure(entity(), shapes([field("id"), field("#", { source: "#" }), field("project", { source: "^.name" }),
                                        field("own", { source: "^.ids[i]" })], 1));
  expect(identifierChoices(one).map((c) => [c.field.name, c.checked])).toEqual([["id", false], ["own", false]]);
});

test("a node carries the identifier, the name and the knowledge fields unless a person chose", () => {
  const m = measure(entity({ label: "full_name" }), shapes([field("id", { identifier: true }), field("full_name"),
    field("amount", { data_type: "number" }), field("notes", { words: 12 })]));
  expect(carriedFields(entity({ label: "full_name" }), m, new Set())).toEqual(["id", "full_name", "notes"]);
  expect(carriedFields(entity({ attributes: ["amount"] }), m, new Set())).toEqual(["amount"]);
});

test("a saved kind takes its fields from the data; without its files measured it keeps what it had", () => {
  const e = entity({ status: "suggested", properties: [{ name: "old", data_type: "text", role: "text" }], rows: 9 });
  const m = measure(e, shapes([field("id", { identifier: true }), field("city")]));
  const saved = savedEntity(e, m, []);
  expect(saved).toMatchObject({ status: "accepted", kind: "entity", rows: 3, confidence: 1 });
  expect(saved.properties.map((p) => [p.name, p.role])).toEqual([["id", "key"], ["city", "text"]]);
  expect(saved.assets[0].fields).toEqual({ id: "id", city: "city" });
  expect(savedEntity({ ...e, keys: [] }, null, []).properties).toEqual(e.properties);
  expect(savedEntity({ ...e, keys: [] }, null, []).kind).toBe("event");
});
