/**
 * The Extractors form: one editor for every extractor a person makes, and the one place that turns it
 * into what the engine runs (`toTemplate`) and back (`fromTemplate`).
 *
 * An extractor reads whatever it is given (any file), records of a type, or nodes already in the
 * graph; finds things with the model, a pattern or Python; creates kinds of thing with fields, fields
 * on the input itself, and connections. Every name - a kind, its fields, a relationship - is the
 * person's or the model's to choose, and every slot the model fills carries the person's instruction.
 *
 * The engine side is `ingest/extraction/templates.py` (`_model_records` reads the model method's
 * config; `_apply_emit_unit` writes it). `SAVED_REFS` is checked against the engine by
 * apps/api/tests/test_mechanism_vocabulary.py.
 */

export type Method = "model" | "pattern" | "python";
export type ReadsKind = "any" | "type" | "graph";
export type Vocabulary = "known" | "open" | "only";
export type How = "always" | "model";

export interface Reads {
  kind: ReadsKind;
  /** any file: the columns, JSON keys or paths, or section headings it reads ("" = everything) */
  only: string;
  /** records of a type: the type, and one field of it ("" = the whole record) */
  type: string;
  field: string;
  /** nodes in the graph: their type, and the fields read ("" = the node's text) */
  nodeType: string;
  fields: string;
}

export interface Field { key: string; ask: string }

export interface Kind {
  /** the model names each thing's type (one such kind at most) */
  byModel: boolean;
  type: string;
  /** what to look for: the model's instruction for this kind */
  ask: string;
  /** the model names the fields: any fact the text states about the thing */
  fieldsByModel: boolean;
  fieldsAsk: string;
  fields: Field[];
}

/**
 * A connection's end: "input" (what was read), "any" (anything it creates), "model" (a thing whose kind
 * the model names), "kind:<Type>", or "existing:<Type>:<field>" (a node already in the graph whose
 * <field> is the thing's name).
 */
export type End = string;

export interface Conn {
  from: End;
  to: End;
  /** the model names the relationship */
  byModel: boolean;
  rel: string;
  how: How;
  ask: string;
}

export interface PatternSpec { mode: "patterns" | "words" | "frequent"; lines: string; ignoreCase: boolean; topN: number }
export interface PythonSpec { code: string; modelCapable: boolean }

export interface ExtractorForm {
  id?: string;
  name: string;
  method: Method;
  reads: Reads;
  kinds: Kind[];
  /** fields written onto what was read */
  inputFields: Field[];
  conns: Conn[];
  vocabulary: Vocabulary;
  /** registry model id; "" = the workspace default */
  model: string;
  pattern: PatternSpec;
  python: PythonSpec;
}

export interface SavedTemplate {
  id?: string;
  name: string;
  source: Record<string, unknown> | null;
  mechanism: Record<string, any>;
  emit: Record<string, any>;
  status?: string;
  category?: string;
  executable?: boolean;
  locked?: boolean;
}

/** The mechanisms this form saves - one per method or pattern mode. */
export const SAVED_REFS = ["unstructured.ner", "unstructured.regex", "unstructured.dictionary",
                           "unstructured.keyword", "unstructured.python"] as const;

/** Field names every thing already uses for itself and its links (the engine's RESERVED_FIELDS). */
export const RESERVED_FIELDS = ["name", "type", "relation", "object", "object_type", "records", "links"];

const NAME = /^[A-Za-z][A-Za-z0-9_]*$/;
const EXTRACTOR_NAME = /^[A-Za-z0-9][A-Za-z0-9 _.-]*$/;

const DEFAULT_CODE = `def extract(inputs):
    text = inputs["text"]
    # return one dict per thing found, e.g. {"name": "...", "amount": "..."}
    return []`;

export const blankKind = (byModel = false): Kind =>
  ({ byModel, type: "", ask: "", fieldsByModel: byModel, fieldsAsk: "", fields: byModel ? [] : [{ key: "", ask: "" }] });

export const blankConn = (): Conn => ({ from: "input", to: "any", byModel: false, rel: "MENTIONS", how: "always", ask: "" });

export function emptyForm(): ExtractorForm {
  return {
    name: "", method: "model",
    reads: { kind: "any", only: "", type: "", field: "", nodeType: "", fields: "" },
    kinds: [blankKind()], inputFields: [], conns: [blankConn()], vocabulary: "known", model: "",
    pattern: { mode: "patterns", lines: "", ignoreCase: true, topN: 8 },
    python: { code: DEFAULT_CODE, modelCapable: false },
  };
}

/** Two worked examples: the same form, differing only in who names what. */
export const EXAMPLES: Record<"rdf" | "fields", { label: string; form: () => ExtractorForm }> = {
  rdf: {
    label: "The model names everything (RDF)",
    form: () => ({
      ...emptyForm(), name: "contract-knowledge",
      kinds: [{ byModel: true, type: "", ask: "Parties, people, courts and places a contract or dispute names",
                fieldsByModel: true, fieldsAsk: "Dates, durations, amounts and obligations; skip addresses and boilerplate", fields: [] }],
      conns: [
        { from: "model", to: "model", byModel: true, rel: "", how: "model",
          ask: "Disputes and obligations between them: who disputes, claims against, owes or signed for whom. Name it by the verb." },
        { from: "input", to: "any", byModel: false, rel: "MENTIONS", how: "always", ask: "" },
      ],
    }),
  },
  fields: {
    label: "A contract and its fields",
    form: () => ({
      ...emptyForm(), name: "contract-terms",
      kinds: [
        { byModel: false, type: "Contract", ask: "The agreement itself", fieldsByModel: false, fieldsAsk: "",
          fields: [{ key: "start_date", ask: "The date it starts" }, { key: "term", ask: "How long it runs" },
                   // no example value here: a small model copies it into every answer
                   { key: "payment_terms", ask: "When payment is due, only if the text says" }, { key: "governing_law", ask: "Whose law governs it" }] },
        { byModel: false, type: "Organization", ask: "The parties to it", fieldsByModel: false, fieldsAsk: "",
          fields: [{ key: "role", ask: "Supplier, customer, partner…" }] },
      ],
      inputFields: [{ key: "risk_level", ask: "Low, medium or high, from the clauses" }],
      conns: [
        { from: "kind:Contract", to: "kind:Organization", byModel: false, rel: "HAS_PARTY", how: "model",
          ask: "Organizations that sign or are bound by this contract, not ones only mentioned in it" },
        { from: "input", to: "any", byModel: false, rel: "MENTIONS", how: "always", ask: "" },
      ],
    }),
  },
};

// ── ends ─────────────────────────────────────────────────────────────────────────────────────────

export const isCreated = (e: End) => e === "any" || e === "model" || e.startsWith("kind:");
export const kindEnd = (type: string) => `kind:${type}`;
export const existingEnd = (type: string, field = "name") => `existing:${type}:${field}`;

export function endLabel(e: End): string {
  if (e === "input") return "the input";
  if (e === "any") return "anything it creates";
  if (e === "model") return "a thing the model names";
  if (e.startsWith("kind:")) return e.slice(5) || "(unnamed kind)";
  if (e.startsWith("existing:")) {
    const [, type, field] = e.split(":");
    return `an existing ${type}, matched on ${field}`;
  }
  return e;
}

/** The ends a connection can use: the input, what this extractor creates, and existing node types. */
export function endChoices(form: ExtractorForm, existingTypes: string[]): { value: End; label: string }[] {
  const created = form.method === "model" ? form.kinds : form.kinds.slice(0, 1);
  const fixed = created.filter((k) => !k.byModel && k.type.trim()).map((k) => kindEnd(k.type.trim()));
  return [
    { value: "input", label: endLabel("input") },
    { value: "any", label: endLabel("any") },
    ...Array.from(new Set(fixed)).map((v) => ({ value: v, label: endLabel(v) })),
    ...(form.method === "model" && form.kinds.some((k) => k.byModel) ? [{ value: "model", label: endLabel("model") }] : []),
    ...existingTypes.map((t) => ({ value: existingEnd(t), label: endLabel(existingEnd(t)) })),
  ];
}

/** Whether the model can decide or name this connection: both ends are things the extractor creates. */
export const modelCanDecide = (form: ExtractorForm, c: Conn) =>
  form.method === "model" && isCreated(c.from) && isCreated(c.to);

// ── form → template ─────────────────────────────────────────────────────────────────────────────

const list = (s: string) => s.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
const engineEnd = (e: End) => (e === "any" ? "*" : e === "model" ? "~" : e.slice(5));
const relName = (s: string) => s.trim().replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toUpperCase();
const fieldRows = (rows: Field[]) => rows.filter((f) => f.key.trim()).map((f) => ({ key: f.key.trim(), ask: f.ask.trim() }));

function sourceOf(r: Reads): Record<string, unknown> {
  if (r.kind === "type") return { select: "*", data: { type: r.type.trim(), ...(r.field.trim() ? { column: r.field.trim() } : {}) } };
  if (r.kind === "graph") return { select: r.nodeType.trim(), ...(list(r.fields).length ? { fields: list(r.fields) } : {}) };
  return { select: "*", ...(list(r.only).length ? { only: list(r.only) } : {}) };
}

/** An edge between something created and the input or an existing node, one per record of its kind. */
function recordEdge(form: ExtractorForm, c: Conn) {
  const created = isCreated(c.from) ? c.from : c.to;
  const side = (e: End) => {
    if (isCreated(e)) return "emitted";
    if (e === "input") return "source";
    const [, type, field] = e.split(":");
    return { match: type, on: { [field || "name"]: "$name" } };
  };
  const only = form.method !== "model" || created === "any" ? undefined : created === "model" ? "~" : created.slice(5);
  return { type: relName(c.rel), from: side(c.from), to: side(c.to), per: "record", mode: "relate.fixed",
           ...(only ? { only } : {}) };
}

export const LINKS_EDGE = {
  type: "$relation", over: "links", mode: "relate.fixed",
  from: { resolve: "entity", name: "$name", type: "$type" },
  to: { resolve: "entity", name: "$object", type: "$object_type" },
};

function inputAttributes(fields: Field[]) {
  const rows = fieldRows(fields);
  return rows.length ? [{ on: "source", set: Object.fromEntries(rows.map((f) => [f.key, `$${f.key}`])) }] : [];
}

export function toTemplate(form: ExtractorForm): SavedTemplate {
  const name = form.name.trim();
  const source = sourceOf(form.reads);
  const edges: any[] = [];
  if (form.method === "model") {
    const links: any[] = [], pairs: any[] = [];
    for (const c of form.conns) {
      if (isCreated(c.from) && isCreated(c.to)) {
        if (c.byModel || c.how === "model") {
          links.push({ from: engineEnd(c.from), to: engineEnd(c.to), relation: c.byModel ? "" : relName(c.rel), ask: c.ask.trim() });
        } else {
          pairs.push({ from: engineEnd(c.from), to: engineEnd(c.to), relation: relName(c.rel) });
        }
      } else {
        edges.push(recordEdge(form, c));
      }
    }
    const mechanism = {
      ref: "unstructured.ner", category: "unstructured", model: form.model || null, vocabulary: form.vocabulary,
      kinds: form.kinds.map((k) => ({
        type: k.byModel ? "" : k.type.trim(), ask: k.ask.trim(),
        ...(k.fieldsByModel ? { fields_by_model: true, fields_ask: k.fieldsAsk.trim() } : { fields: fieldRows(k.fields) }) })),
      links, pairs, input_fields: fieldRows(form.inputFields),
    };
    const emit = {
      nodes: [{ type: "$type", dedup: true, name: "$name" }],
      edges: [...(links.length || pairs.length ? [LINKS_EDGE] : []), ...edges],
      attributes: inputAttributes(form.inputFields),
    };
    return { id: form.id, name, source, mechanism, emit };
  }

  for (const c of form.conns) edges.push(recordEdge(form, c));
  const kind = form.kinds[0] || blankKind();
  if (form.method === "pattern") {
    const p = form.pattern;
    // a pattern may hold a comma, so patterns are one a line; words may be listed either way
    const rows = p.lines.split("\n").map((x) => x.trim()).filter(Boolean);
    const mechanism = p.mode === "patterns"
      ? { ref: "unstructured.regex", category: "unstructured", lines: rows, ignore_case: p.ignoreCase,
          pattern: rows.length === 1 ? rows[0] : rows.map((x) => `(?:${x})`).join("|") }
      : p.mode === "words"
        ? { ref: "unstructured.dictionary", category: "unstructured", terms: list(p.lines) }
        : { ref: "unstructured.keyword", category: "unstructured", top_n: p.topN };
    const emit = { nodes: [{ type: kind.type.trim(), dedup: true, name: p.mode === "patterns" ? "$match" : "$term" }], edges, attributes: [] };
    return { id: form.id, name, source, mechanism, emit };
  }
  const mechanism = {
    ref: "unstructured.python", category: "unstructured", language: "python", code: form.python.code,
    model_capable: form.python.modelCapable, ...(form.python.modelCapable ? { model: form.model || null } : {}),
  };
  const fields = fieldRows(kind.fields).map((f) => f.key);
  const emit = {
    nodes: kind.type.trim() ? [{ type: kind.type.trim(), ...(fields.length ? { attributes: fields } : {}) }] : [],
    edges, attributes: inputAttributes(form.inputFields),
  };
  return { id: form.id, name, source, mechanism, emit };
}

// ── template → form ─────────────────────────────────────────────────────────────────────────────

function readsOf(source: any): Reads | null {
  const r: Reads = { kind: "any", only: "", type: "", field: "", nodeType: "", fields: "" };
  if (!source || typeof source !== "object") return null;
  if (source.data?.type) return { ...r, kind: "type", type: String(source.data.type), field: String(source.data.column || "") };
  if (source.data) return null;                                   // one fixed file: not something this form makes
  const sel = source.select;
  if (typeof sel === "string" && sel !== "*") {
    return { ...r, kind: "graph", nodeType: sel, fields: (source.fields || []).join(", ") };
  }
  if (sel === undefined || sel === "*") return { ...r, only: (source.only || []).join(", ") };
  return null;
}

const fromEngineEnd = (e: any): End => (e === "*" || e == null || e === "" ? "any" : e === "~" ? "model" : kindEnd(String(e)));

/** A saved record edge back into a connection, or null when it is not one this form makes. */
function connOf(e: any, method: Method): Conn | null {
  const created = (only: any): End => (method !== "model" || !only ? "any" : only === "~" ? "model" : kindEnd(String(only)));
  const end = (x: any): End | null => {
    if (x === "emitted") return created(e.only);
    if (x === "source" || x == null) return "input";
    if (x && typeof x === "object" && x.match && x.on) {
      const [field, value] = Object.entries(x.on)[0] || [];
      return value === "$name" ? existingEnd(String(x.match), String(field)) : null;
    }
    return null;
  };
  const from = end(e.from), to = end(e.to);
  if (!from || !to || typeof e.type !== "string" || e.type.startsWith("$")) return null;
  return { from, to, byModel: false, rel: e.type, how: "always", ask: "" };
}

const isLinksEdge = (e: any) => e?.over === "links" || (e?.from === "emitted" && e?.to?.name === "$object");

/** The form for a saved extractor, or null when it was made some other way (an older editor, a built-in). */
export function fromTemplate(t: SavedTemplate): ExtractorForm | null {
  if (!t || String(t.name).startsWith("builtin:")) return null;
  const reads = readsOf(t.source);
  const mech = t.mechanism || {};
  const emit = t.emit || {};
  if (!reads) return null;
  const base: ExtractorForm = { ...emptyForm(), id: t.id, name: t.name, reads, model: mech.model || "", conns: [] };
  const ref = String(mech.ref || "");
  const rest: Conn[] = [];
  for (const e of emit.edges || []) {
    if (ref === "unstructured.ner" && isLinksEdge(e)) continue;
    const c = connOf(e, ref === "unstructured.ner" ? "model" : ref === "unstructured.python" ? "python" : "pattern");
    if (!c) return null;
    rest.push(c);
  }
  const inputFields = (emit.attributes || []).flatMap((a: any) =>
    a?.on === "source" && a.set && typeof a.set === "object" ? Object.keys(a.set).map((key) => ({ key, ask: "" })) : []);

  if (ref === "unstructured.ner") {
    const said = new Map<string, string>((mech.input_fields || []).map((f: any) => [f.key, f.ask || ""]));
    const kinds: Kind[] = mech.kinds?.length
      ? mech.kinds.map((k: any) => ({
          byModel: !k.type, type: k.type || "", ask: k.ask || "", fieldsByModel: !!k.fields_by_model,
          fieldsAsk: k.fields_ask || "", fields: (k.fields || []).map((f: any) => ({ key: f.key, ask: f.ask || "" })) }))
      : [{ ...blankKind(true), fieldsByModel: false }];          // saved before the form: names only
    const links = mech.kinds?.length ? (mech.links || []) : [{ from: "*", to: "*", relation: "" }];
    const conns: Conn[] = [
      ...links.map((l: any) => ({ from: fromEngineEnd(l.from), to: fromEngineEnd(l.to), byModel: !l.relation,
                                  rel: l.relation || "", how: "model" as How, ask: l.ask || "" })),
      ...(mech.pairs || []).map((p: any) => ({ from: fromEngineEnd(p.from), to: fromEngineEnd(p.to), byModel: false,
                                               rel: p.relation || "", how: "always" as How, ask: "" })),
      ...rest,
    ];
    return { ...base, method: "model", kinds, conns, vocabulary: mech.vocabulary || "known",
             inputFields: inputFields.map((f: Field) => ({ ...f, ask: said.get(f.key) || "" })) };
  }
  const node = (emit.nodes || [])[0] || {};
  const kind: Kind = { ...blankKind(), type: node.type && !String(node.type).startsWith("$") ? node.type : "",
                       fields: (node.attributes || []).map((key: string) => ({ key, ask: "" })) };
  if (ref === "unstructured.python") {
    return { ...base, method: "python", kinds: [kind], conns: rest, inputFields,
             python: { code: mech.code || "", modelCapable: !!mech.model_capable } };
  }
  const pattern = ref === "unstructured.regex"
    ? { mode: "patterns" as const, lines: (mech.lines?.length ? mech.lines : [mech.pattern || ""]).join("\n"), ignoreCase: !!mech.ignore_case, topN: 8 }
    : ref === "unstructured.dictionary"
      ? { mode: "words" as const, lines: (mech.terms || []).join("\n"), ignoreCase: true, topN: 8 }
      : ref === "unstructured.keyword"
        ? { mode: "frequent" as const, lines: "", ignoreCase: true, topN: Number(mech.top_n) || 8 }
        : null;
  if (!pattern) return null;
  return { ...base, method: "pattern", kinds: [{ ...kind, fields: [] }], conns: rest, inputFields: [], pattern };
}

// ── checks ──────────────────────────────────────────────────────────────────────────────────────

export interface Check { errors: string[]; warnings: string[] }

function checkFields(rows: Field[], where: string, errors: string[]) {
  const seen = new Set<string>();
  for (const f of rows) {
    const key = f.key.trim();
    if (!key) continue;
    if (!NAME.test(key)) errors.push(`${where}: "${key}" — a field name is letters, digits and _, starting with a letter.`);
    else if (RESERVED_FIELDS.includes(key.toLowerCase())) errors.push(`${where}: "${key}" is used by every thing; call the field something else.`);
    else if (seen.has(key.toLowerCase())) errors.push(`${where}: the field "${key}" is listed twice.`);
    seen.add(key.toLowerCase());
  }
}

/** What stops a save (errors) and what is worth a second look (warnings). `taken`: other extractors' names. */
export function check(form: ExtractorForm, taken: string[] = []): Check {
  const errors: string[] = [], warnings: string[] = [];
  const name = form.name.trim();
  if (!name) errors.push("Give the extractor a name.");
  else if (name.startsWith("builtin:") || !EXTRACTOR_NAME.test(name)) errors.push("A name is letters, digits, spaces, - _ and .");
  else if (taken.some((t) => t.toLowerCase() === name.toLowerCase())) errors.push(`An extractor called ${name} already exists.`);

  const r = form.reads;
  if (r.kind === "type" && !r.type.trim()) errors.push("Say which type's records it reads.");
  if (r.kind === "graph" && !r.nodeType.trim()) errors.push("Say which nodes it reads.");

  const kinds = form.method === "model" ? form.kinds : form.kinds.slice(0, 1);
  const fixedTypes = new Set<string>();
  kinds.forEach((k, i) => {
    const t = k.type.trim();
    if (k.byModel && form.method === "model") {
      if (!k.ask.trim()) warnings.push("Say what things the model should look for, or it returns every named thing it finds.");
    } else if (!t) {
      if (form.method !== "python") errors.push(`Name kind ${i + 1}, or let the model name it.`);
    } else if (!NAME.test(t)) {
      errors.push(`"${t}" — a kind's name is letters, digits and _, starting with a letter (Contract, Work_Order).`);
    } else if (fixedTypes.has(t.toLowerCase())) {
      errors.push(`The kind ${t} is listed twice.`);
    } else fixedTypes.add(t.toLowerCase());
    if (form.method === "model" && k.fieldsByModel && !k.fieldsAsk.trim()) {
      warnings.push(`Say what facts to look for about ${k.byModel ? "the things the model names" : t || "this kind"}, or every fact becomes a field.`);
    }
    if (!k.fieldsByModel && form.method !== "pattern") checkFields(k.fields, t || "the kind the model names", errors);
  });
  if (form.method === "model") {
    if (!kinds.length) errors.push("Say what it creates: add a kind of thing.");
    if (kinds.filter((k) => k.byModel).length > 1) errors.push("One kind the model names is enough: it names each thing's type itself.");
  }
  if (form.method !== "pattern") checkFields(form.inputFields, "On the input", errors);
  if (form.method === "python") {
    if (!form.python.code.trim()) errors.push("Write the Python extract(inputs) function.");
    if (!kinds[0]?.type.trim() && !fieldRows(form.inputFields).length) errors.push("Say what it creates: a kind of thing or fields on the input.");
  }
  if (form.method === "pattern") {
    if (form.pattern.mode !== "frequent" && !form.pattern.lines.trim()) {
      errors.push(form.pattern.mode === "patterns" ? "Write at least one pattern." : "List at least one word.");
    }
    if (form.pattern.mode === "frequent" && !(form.pattern.topN >= 1 && form.pattern.topN <= 50)) errors.push("Most frequent terms: between 1 and 50.");
  }

  const ends = new Set(endChoices(form, []).map((c) => c.value));
  form.conns.forEach((c, i) => {
    const n = `Connection ${i + 1}`;
    const bad = [c.from, c.to].find((e) => !e.startsWith("existing:") && !ends.has(e));
    if (bad) errors.push(`${n}: ${endLabel(bad)} is not something this extractor creates.`);
    if (!isCreated(c.from) && !isCreated(c.to)) errors.push(`${n}: one end must be something it creates.`);
    if (!(c.byModel && modelCanDecide(form, c))) {
      if (!c.rel.trim()) errors.push(`${n}: name the relationship.`);
      else if (!NAME.test(relName(c.rel) || "_")) errors.push(`${n}: a relationship name is letters, digits and _.`);
    }
    if ((c.byModel || c.how === "model") && !modelCanDecide(form, c)) {
      errors.push(`${n}: the model can only decide connections between things it creates.`);
    } else if ((c.byModel || c.how === "model") && !c.ask.trim()) {
      warnings.push(`${n}: say what relationship to look for, or the model returns every one it can find.`);
    }
  });
  return { errors, warnings };
}

/** Whether any name in the form is the model's to choose (the vocabulary setting applies). */
export const namesByModel = (form: ExtractorForm) =>
  form.method === "model" && (form.kinds.some((k) => k.byModel) || form.conns.some((c) => c.byModel && modelCanDecide(form, c)));

// ── the list ─────────────────────────────────────────────────────────────────────────────────────

/** How an extractor finds things, in the form's words. */
export function methodOf(t: SavedTemplate): string {
  const ref = String(t.mechanism?.ref || "");
  if (ref === "unstructured.ner") return "Model";
  if (["unstructured.regex", "unstructured.dictionary", "unstructured.keyword"].includes(ref)) return "Pattern";
  if (ref === "unstructured.python") return "Python";
  if (ref.startsWith("relate.")) return "Connections";
  if (ref === "seed.literal") return "Fixed records";
  if (ref.startsWith("pipeline.")) return "Answer time";
  return "Model prompt";
}

/** One line on what a saved extractor reads and creates. */
export function describe(t: SavedTemplate): string {
  const f = fromTemplate(t);
  if (!f) {
    const types = (t.emit?.nodes || []).map((n: any) => n.type).filter((x: string) => x && !x.startsWith("$"));
    return types.length ? `Creates ${types.join(", ")}` : "";
  }
  const reads = f.reads.kind === "graph" ? `Reads ${f.reads.nodeType} nodes`
    : f.reads.kind === "type" ? `Reads ${f.reads.type} records` : "Reads any file";
  const named = (f.method === "model" ? f.kinds : f.kinds.slice(0, 1)).filter((k) => !k.byModel && k.type).map((k) => k.type);
  const parts = [...named, ...(f.method === "model" && f.kinds.some((k) => k.byModel) ? ["things the model names"] : [])];
  const creates = parts.length ? `creates ${parts.join(", ")}` : f.inputFields.length ? "adds fields to the input" : "";
  const conns = f.conns.length ? `${f.conns.length} connection${f.conns.length > 1 ? "s" : ""}` : "";
  return [reads, creates, conns].filter(Boolean).join(" · ");
}
