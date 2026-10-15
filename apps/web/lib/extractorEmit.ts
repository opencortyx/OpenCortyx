
/** A single attribute as the Studio edits it. */
export interface AttrRow { key: string; value?: any; }

const SOURCE_ON = (on: any) => on === undefined || on === null || on === "source";

/**
 * Saved `emit.attributes` → editable rows.
 *
 * Specs the Studio cannot express as rows — a `set` given as a whole-object token ("$llm"), or a
 * merge onto a RESOLVED endpoint rather than the source — come back in `rest` untouched, so saving preserves them exactly as authored.
 */
export function attrRowsFromEmit(specs: any[]): { rows: AttrRow[]; rest: any[] } {
  const rows: AttrRow[] = [];
  const rest: any[] = [];
  (specs || []).forEach((a: any) => {
    if (!a || typeof a !== "object") return;
    if (a.key !== undefined && a.set === undefined) { rows.push({ key: a.key, value: a.value }); return; }  // already a row
    if (!SOURCE_ON(a.on) || typeof a.set !== "object" || a.set === null || Array.isArray(a.set)) { rest.push(a); return; }
    Object.entries(a.set).forEach(([key, value]) => rows.push({ key, value }));
    if (a.when) rest.push({ ...a, set: {} });          // keep a conditional merge's condition on save
  });
  return { rows, rest };
}

/** Editable rows (+ anything preserved) → the engine's `emit.attributes`. */
export function emitAttrsFromRows(rows: AttrRow[], rest: any[] = []): any[] {
  const set: Record<string, any> = {};
  (rows || []).forEach((r: any) => {
    const k = String(r?.key || "").trim();
    if (!k) return;
    set[k] = r.value === undefined || r.value === "" ? `$llm.${k}` : r.value;
  });
  const keep = (rest || []).filter((a: any) => a && (typeof a.set !== "object" || Object.keys(a.set || {}).length));
  return [...(Object.keys(set).length ? [{ on: "source", set }] : []), ...keep];
}

export const idFieldText = (id: any): string => (Array.isArray(id) ? id.join(", ") : String(id || ""));
export const idFieldList = (text: any): string[] =>
  String(text || "").split(",").map((s) => s.trim()).filter(Boolean);
export const normalizeEmitNodes = (nodes: any[]): any[] =>
  (nodes || []).map((n: any) => ({ ...n, id: idFieldList(n?.id) }));
