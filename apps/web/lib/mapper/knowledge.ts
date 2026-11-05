/**
 * What a mapping says its data means (`payload.knowledge`), measured against the data itself.
 *
 * Shared by the Mapper page and the per-mapping editor: how a record source reads in words, what a
 * kind's files hold at its record path, each field's role, and the knowledge a save writes.
 */

import type { KnowledgeEntity, KnowledgeRelationship, ShapeSource } from "@/lib/api/contextV2";

export type Field = ShapeSource["fields"][number];

/** How a record source reads in a sentence. */
export function recordLabel(s: ShapeSource): string {
  if (!s.path) return s.kind === "structured" ? "each row" : "each record";
  const key = s.path.replace(/^\$\./, "").replace(/(\[\*\])+$/, "");
  if (s.path.endsWith("[*][*]")) return `each item of each list in "${key}"`;
  const [outer, inner] = key.split("[*].");    // a list inside each element: a project's risks
  if (inner) return `each item in "${inner}" of each item in "${outer}"`;
  return s.path.endsWith("[*]") ? `each item in "${key}"` : `the "${key}" object in each record`;
}

/** "rows", "$" and "" all mean each record of the file. */
export const normPath = (p?: string) => (["rows", "row", "$"].includes((p || "").trim().toLowerCase()) ? "" : (p || "").trim());

/** A link's relationship name until a person names it: the type it points to ("Customer" -> CUSTOMER).
 *  The same rule as the server's `detector._relation`. */
export const relationName = (target: string) =>
  target.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toUpperCase() || "REFERENCES";

/** What a type's files hold at its record path, judged across all of them: a field identifies only if
 *  it identifies in every file, and names only if it names in every file that has it. */
export interface Measured {
  loading: boolean;
  paths: ShapeSource[];          // the record sources on offer (one per path, from the first file that has it)
  fields: Field[];
  records: number;
  rows: Record<string, any>[];
  errors: string[];
  perAsset: Record<string, ShapeSource>;
}

export function measure(e: KnowledgeEntity, shapes: Record<string, ShapeSource[]>): Measured {
  const path = normPath(e.path);
  const ids = (e.assets || []).map((a) => a.id).filter(Boolean);
  const paths: ShapeSource[] = [];
  const perAsset: Record<string, ShapeSource> = {};
  for (const a of ids) {
    for (const s of shapes[a] || []) {
      if (!paths.some((p) => p.path === s.path)) paths.push(s);
      if (s.path === path) perAsset[a] = s;
    }
  }
  const srcs = Object.values(perAsset);
  const byName = new Map<string, Field>();
  for (const s of srcs) {
    for (const f of s.fields) {
      const cur = byName.get(f.name);
      byName.set(f.name, cur ? { ...cur, filled: cur.filled + f.filled, distinct: cur.distinct + f.distinct,
        repeats: cur.repeats + f.repeats, words: Math.max(cur.words, f.words),
        identifier: cur.identifier && f.identifier, nameable: cur.nameable && f.nameable } : { ...f });
    }
  }
  // a field missing from some file cannot identify those files' records
  const fields = Array.from(byName.values()).map((f) =>
    srcs.every((s) => s.fields.some((x) => x.name === f.name)) ? f : { ...f, identifier: false });
  return {
    loading: ids.some((a) => !(a in shapes)),
    paths, fields, perAsset,
    records: srcs.reduce((n, s) => n + (s.records || 0), 0),
    rows: srcs.flatMap((s) => s.rows).slice(0, 3),
    errors: srcs.filter((s) => s.error).map((s) => `${s.asset_name}: ${s.error}`),
  };
}

/** The roles that make a field knowledge: what a published record carries by default (the rest stays
 *  in the data store). The same rule as the server's `store.KNOWLEDGE_ROLES`. */
export const KNOWLEDGE_ROLES = ["key", "text", "prose", "time", "reference"];

/** A field's role: a person's choice (identifier, link) first, then what detection measured, then a guess. */
export function roleFor(f: Field, e: KnowledgeEntity, key: string, refs: Set<string>): string {
  if (f.name === key) return "key";
  if (refs.has(f.name)) return "reference";
  return (e.properties || []).find((p) => p.name === f.name)?.role || roleOf(f, key, refs);
}

function roleOf(f: Field, key: string, refs: Set<string>): string {
  if (f.name === key) return "key";
  if (refs.has(f.name)) return "reference";
  if (f.data_type === "date") return "time";
  if (f.data_type === "number") return "measure";
  if (f.words >= 8) return "prose";
  return "text";
}

/** A position in the file ("#") or a value read from the enclosing record ("^.id") never identifies a
 *  record; "^.ids[i]" (its own entry in the parent's list) can. */
export const lent = (f: Field) => /^#|^\^(?!.*\[i\]$)/.test(String(f.source || ""));

/** The fields offered as the identifier: unique in every file - or, with one record (where uniqueness
 *  cannot be measured), any filled field that is not lent. */
export function identifierChoices(m: Measured): { field: Field; checked: boolean }[] {
  const ids = m.fields.filter((f) => f.identifier).map((field) => ({ field, checked: true }));
  const unchecked = m.records < 2
    ? m.fields.filter((f) => !f.identifier && f.filled > 0 && !f.repeats && !lent(f)).map((field) => ({ field, checked: false }))
    : [];
  return [...ids, ...unchecked];
}

/** The fields a published record carries: a person's choice, else its knowledge columns and its name. */
export function carriedFields(e: KnowledgeEntity, m: Measured, refs: Set<string>): string[] {
  const key = e.keys[0] || "";
  return (e.attributes?.length ? e.attributes
    : m.fields.filter((f) => KNOWLEDGE_ROLES.includes(roleFor(f, e, key, refs))).map((f) => f.name)
      .concat(e.label ? [e.label] : [])).filter((x, k, a) => a.indexOf(x) === k);
}

/** An entity as a person's save writes it: fields and properties as the data shows them now, its kind
 *  from its identifier, and accepted unless ignored. Unmeasured (its files not read here), it keeps
 *  what it had. */
export function savedEntity(e: KnowledgeEntity, m: Measured | null, rels: KnowledgeRelationship[]): KnowledgeEntity {
  const key = e.keys[0] || "";
  const refs = new Set(rels.filter((r) => r.from_type === e.type).map((r) => r.from_field));
  const read = !!m && !m.loading && m.fields.length > 0;
  return {
    ...e,
    type: e.type.trim(),
    path: normPath(e.path),
    kind: key ? "entity" : "event",
    status: e.status === "rejected" ? "rejected" : "accepted",
    assets: (e.assets || []).map((a) => {
      const s = m?.perAsset[a.id];
      return s ? { ...a, fields: Object.fromEntries(s.fields.map((f) => [f.name, f.source])) } : a;
    }),
    properties: read ? m!.fields.map((f) => ({ name: f.name, data_type: f.data_type, role: roleFor(f, e, key, refs),
                                               distinct: f.distinct, words: f.words }))
                     : e.properties,
    rows: read ? m!.records : e.rows,
    confidence: 1,
  };
}
