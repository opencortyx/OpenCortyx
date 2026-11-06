import { expect, test } from "@jest/globals";
import {
  EXAMPLES, LINKS_EDGE, blankConn, check, describe as describeIt, emptyForm, endChoices, fromTemplate, methodOf,
  namesByModel, toTemplate, type ExtractorForm,
} from "./form";

// connections come back grouped (the model's, then always, then to the input or existing nodes)
const order = (c: ExtractorForm["conns"][number]) => JSON.stringify(c);
const strip = (f: ExtractorForm | null) => (f ? { ...f, id: undefined, conns: [...f.conns].sort((a, b) => order(a).localeCompare(order(b))) } : f);

test("the RDF example saves one kind and one link the model names, each with its instruction", () => {
  const t = toTemplate(EXAMPLES.rdf.form());
  expect(t.source).toEqual({ select: "*" });
  expect(t.mechanism.ref).toBe("unstructured.ner");
  expect(t.mechanism.kinds).toEqual([{ type: "", ask: expect.stringContaining("Parties"), fields_by_model: true,
                                       fields_ask: expect.stringContaining("Dates") }]);
  expect(t.mechanism.links).toEqual([{ from: "~", to: "~", relation: "", ask: expect.stringContaining("Disputes") }]);
  expect(t.emit.nodes).toEqual([{ type: "$type", dedup: true, name: "$name" }]);
  expect(t.emit.edges[0]).toEqual(LINKS_EDGE);
  expect(t.emit.edges[1]).toEqual({ type: "MENTIONS", from: "source", to: "emitted", per: "record", mode: "relate.fixed" });
});

test("the contract example: named kinds and fields, a link the model decides, a field on the input", () => {
  const t = toTemplate(EXAMPLES.fields.form());
  expect(t.mechanism.kinds.map((k: any) => k.type)).toEqual(["Contract", "Organization"]);
  expect(t.mechanism.kinds[0].fields.map((f: any) => f.key)).toEqual(["start_date", "term", "payment_terms", "governing_law"]);
  expect(t.mechanism.links).toEqual([{ from: "Contract", to: "Organization", relation: "HAS_PARTY", ask: expect.any(String) }]);
  expect(t.mechanism.input_fields).toEqual([{ key: "risk_level", ask: expect.any(String) }]);
  expect(t.emit.attributes).toEqual([{ on: "source", set: { risk_level: "$risk_level" } }]);
});

test("every example and method comes back from what it saves exactly as it was", () => {
  const pattern: ExtractorForm = { ...emptyForm(), name: "terms", method: "pattern", kinds: [{ ...emptyForm().kinds[0], type: "Term", fields: [] }],
                                   inputFields: [], pattern: { mode: "patterns", lines: "net \\d+\nauto-renew", ignoreCase: true, topN: 8 } };
  const words: ExtractorForm = { ...pattern, pattern: { mode: "words", lines: "SAR\nKYC", ignoreCase: true, topN: 8 } };
  const python: ExtractorForm = { ...emptyForm(), name: "py", method: "python",
                                  kinds: [{ ...emptyForm().kinds[0], type: "Amount", fields: [{ key: "value", ask: "" }] }],
                                  inputFields: [{ key: "currency", ask: "" }], python: { code: "def extract(inputs):\n    return []", modelCapable: false } };
  const graph: ExtractorForm = { ...EXAMPLES.fields.form(), reads: { ...emptyForm().reads, kind: "graph", nodeType: "Vendor", fields: "notes, summary" } };
  const typed: ExtractorForm = { ...EXAMPLES.rdf.form(), reads: { ...emptyForm().reads, kind: "type", type: "Turn", field: "content" } };
  const only: ExtractorForm = { ...EXAMPLES.rdf.form(), reads: { ...emptyForm().reads, only: "notes, $.party.name" } };
  const existing: ExtractorForm = { ...EXAMPLES.fields.form(),
    conns: [{ from: "kind:Organization", to: "existing:Vendor:name", byModel: false, rel: "SAME_AS", how: "always", ask: "" },
            { from: "kind:Contract", to: "kind:Organization", byModel: false, rel: "PARTY", how: "always", ask: "" }] };
  for (const f of [EXAMPLES.rdf.form(), EXAMPLES.fields.form(), pattern, words, python, graph, typed, existing]) {
    expect(strip(fromTemplate({ ...toTemplate(f), id: "x" }))).toEqual(strip(f));
  }
  expect(fromTemplate(toTemplate(only))?.reads.only).toBe("notes, $.party.name");
});

test("an extractor saved by the old named-things form opens as one kind the model names", () => {
  const old = {
    name: "document_entities", source: { select: "*" }, mechanism: { ref: "unstructured.ner" },
    emit: { nodes: [{ type: "$type", dedup: true, name: "$name" }],
            edges: [{ type: "MENTIONS", from: "source", to: "emitted", mode: "relate.fixed" },
                    { type: "$relation", from: "emitted", when: "$relation", mode: "relate.fixed",
                      to: { resolve: "entity", name: "$object", type: "$object_type" } }] },
  };
  const f = fromTemplate(old)!;
  expect(f.method).toBe("model");
  expect(f.kinds).toEqual([{ byModel: true, type: "", ask: "", fieldsByModel: false, fieldsAsk: "", fields: [] }]);
  expect(f.conns).toEqual([
    { from: "any", to: "any", byModel: true, rel: "", how: "model", ask: "" },
    { from: "input", to: "any", byModel: false, rel: "MENTIONS", how: "always", ask: "" },
  ]);
  const again = toTemplate(f);
  expect(again.mechanism.links).toEqual([{ from: "*", to: "*", relation: "", ask: "" }]);
  expect(again.emit.edges).toContainEqual(LINKS_EDGE);
});

test("what this form does not make opens nowhere, and says what it is in the list", () => {
  const prompt = { name: "x", source: { select: "*" }, mechanism: { ref: "unstructured.llm", prompt: "p" }, emit: { nodes: [{ type: "Risk" }] } };
  expect(fromTemplate(prompt)).toBeNull();
  expect(fromTemplate({ ...prompt, name: "builtin:doc.trust" })).toBeNull();
  expect(fromTemplate({ ...prompt, source: null })).toBeNull();
  expect(methodOf(prompt)).toBe("Model prompt");
  expect(describeIt(prompt)).toBe("Creates Risk");
  expect(methodOf({ ...prompt, mechanism: { ref: "relate.llm" } })).toBe("Connections");
  expect(describeIt(toTemplate(EXAMPLES.fields.form()))).toBe("Reads any file · creates Contract, Organization · 2 connections");
});

test("a save needs a free name and names the engine can use", () => {
  const ok = EXAMPLES.fields.form();
  expect(check(ok).errors).toEqual([]);
  expect(check({ ...ok, name: "" }).errors).toContain("Give the extractor a name.");
  expect(check(ok, ["Contract-Terms"]).errors).toContain("An extractor called contract-terms already exists.");
  expect(check({ ...ok, name: "builtin:x" }).errors.length).toBe(1);
  const badKind = { ...ok, kinds: [{ ...ok.kinds[0], type: "Two words" }, ok.kinds[1]] };
  expect(check(badKind).errors.join()).toMatch(/kind's name/);
  const twice = { ...ok, kinds: [ok.kinds[0], { ...ok.kinds[1], type: "contract" }] };
  expect(check(twice).errors.join()).toMatch(/listed twice/);
  const reserved = { ...ok, inputFields: [{ key: "name", ask: "" }] };
  expect(check(reserved).errors.join()).toMatch(/used by every thing/);
  const twoModel = { ...ok, kinds: [{ ...ok.kinds[0], byModel: true }, { ...ok.kinds[1], byModel: true }] };
  expect(check(twoModel).errors.join()).toMatch(/One kind the model names/);
});

test("connections: one end must be created, the model decides only between created things, and a name is needed", () => {
  const ok = EXAMPLES.fields.form();
  const conn = (c: Partial<ReturnType<typeof blankConn>>) => check({ ...ok, conns: [{ ...blankConn(), ...c }] }).errors.join(" ");
  expect(conn({ from: "input", to: "existing:Vendor:name" })).toMatch(/one end must be something it creates/);
  expect(conn({ from: "input", to: "any", how: "model" })).toMatch(/only decide connections between things it creates/);
  expect(conn({ rel: "" })).toMatch(/name the relationship/);
  expect(conn({ from: "kind:Gone", to: "any" })).toMatch(/not something this extractor creates/);
  expect(conn({ from: "model", to: "any" })).toMatch(/not something this extractor creates/);
  expect(check(EXAMPLES.rdf.form()).errors).toEqual([]);
});

test("an empty instruction where the model decides is a warning, not an error", () => {
  const f = EXAMPLES.rdf.form();
  f.kinds[0].ask = ""; f.kinds[0].fieldsAsk = ""; f.conns[0].ask = "";
  const c = check(f);
  expect(c.errors).toEqual([]);
  expect(c.warnings).toHaveLength(3);
  expect(namesByModel(f)).toBe(true);
  expect(namesByModel(EXAMPLES.fields.form())).toBe(false);
});

test("pattern and Python extractors: what they need", () => {
  const p: ExtractorForm = { ...emptyForm(), name: "p", method: "pattern", kinds: [{ ...emptyForm().kinds[0], type: "Term" }] };
  expect(check(p).errors).toContain("Write at least one pattern.");
  expect(check({ ...p, pattern: { ...p.pattern, mode: "words" } }).errors).toContain("List at least one word.");
  expect(check({ ...p, pattern: { ...p.pattern, mode: "frequent", topN: 0 } }).errors.join()).toMatch(/between 1 and 50/);
  const py: ExtractorForm = { ...emptyForm(), name: "py", method: "python", kinds: [{ ...emptyForm().kinds[0], type: "" }], conns: [] };
  expect(check(py).errors).toContain("Say what it creates: a kind of thing or fields on the input.");
  expect(check({ ...py, python: { code: " ", modelCapable: false } }).errors).toContain("Write the Python extract(inputs) function.");
  expect(toTemplate({ ...p, pattern: { mode: "patterns", lines: "a,b\nc", ignoreCase: false, topN: 8 } }).mechanism.pattern).toBe("(?:a,b)|(?:c)");
});

test("the ends offered: the input, what it creates, and nodes already in the graph", () => {
  const f = EXAMPLES.rdf.form();
  expect(endChoices(f, ["Vendor"]).map((c) => c.value)).toEqual(["input", "any", "model", "existing:Vendor:name"]);
  expect(endChoices(EXAMPLES.fields.form(), []).map((c) => c.value)).toEqual(["input", "any", "kind:Contract", "kind:Organization"]);
});
