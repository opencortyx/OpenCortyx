/**
 * One model for every pipeline, of every kind.
 *
 * The three builders were three pages because the UI split by source kind, but the DATA never did:
 * `/api/v2/pipelines` takes one `stages` array whatever the kind, and every pipeline in the system
 * has the same two-part shape —
 *
 *    Stage 1   the ANCHOR, shown as the "reading rule": a bound mapping plus two options, accept
 *              newly detected shapes and extract only. Exactly one, always first, and the same
 *              settings for every kind; only its wire format below differs.
 *
 *    Stage 2..N the STEPS: extractors and transforms over whatever the anchor produced.
 *              Identical across kinds — same shape, same config, same card.
 *
 * So the anchor is pluggable and everything after it is shared. The one thing this file must get
 * exactly right is the anchor's wire format, because the backend reads it: `document` and
 * `semistructured` carry `type`, `structured` carries `action` instead, and the semi-structured
 * anchor's id is the literal string "structures" rather than "stage_0". Those are not tidy, but they
 * are what is already stored in every saved pipeline — normalising them here would silently
 * re-key live data.
 */

export type PipelineKind = "document" | "structured" | "semistructured";

export interface PipelineStage {
  id: string;
  /** document + semistructured */
  type?: string;
  /** structured uses this instead of `type` */
  action?: string;
  source?: string;
  name?: string;
  config: Record<string, any>;
}

export const KIND_META: Record<PipelineKind, {
  label: string; sub: string; anchorTitle: string; anchorHint: string; unit: string;
}> = {
  document: {
    label: "Unstructured", sub: "PDFs, docs, prose",
    anchorTitle: "Reading rule",
    anchorHint: "Bind a mapping, or let the default walker split each document into chunks.",
    unit: "chunk",
  },
  structured: {
    label: "Structured", sub: "SQL tables, CSV, warehouses",
    anchorTitle: "Reading rule",
    anchorHint: "Bind a mapping, or let the default walker read every table.",
    unit: "row",
  },
  semistructured: {
    label: "Semi-structured", sub: "JSON, APIs, events",
    anchorTitle: "Reading rule",
    anchorHint: "Bind a mapping, or let the default walker read the whole structure.",
    unit: "node",
  },
};

/**
 * A saved anchor config → the only settings the reading rule has: `mapping_ids`, `auto_accept` and
 * `extract_only`. Everything else is dropped, so saving a pipeline clears settings the form no longer
 * shows.
 *
 * Pipelines saved before the options were unified spell them differently, and those spellings are
 * read here so the form shows what the pipeline does: a single `mapping_id`, and "extract only" as
 * `structure_only` (JSON), `save_chunks: false` (documents) or catalog `mode` (tables).
 */
export function readingRule(config: Record<string, any> | undefined): Record<string, any> {
  const c = config || {};
  const ids: string[] = Array.isArray(c.mapping_ids)
    ? c.mapping_ids.filter(Boolean)
    : c.mapping_id ? [String(c.mapping_id)] : [];
  const extractOnly = c.extract_only === true || c.structure_only === true
    || c.save_chunks === false || c.mode === "catalog";
  return {
    ...(ids.length ? { mapping_ids: ids } : {}),
    ...(c.auto_accept === false ? { auto_accept: false } : {}),
    ...(extractOnly ? { extract_only: true } : {}),
  };
}

/** The anchor's id and discriminator, per kind — the shape already stored in saved pipelines. */
const ANCHOR_SPEC: Record<PipelineKind, { id: string; key: "type" | "action"; value: string; source?: string }> = {
  document:       { id: "stage_0",    key: "type",   value: "chunking",        source: "document" },
  structured:     { id: "stage_0",    key: "action", value: "base_schema" },
  semistructured: { id: "structures", key: "type",   value: "json_structures", source: "document" },
};

const ANCHOR_VALUES: Record<string, PipelineKind> = {
  chunking: "document", base_schema: "structured", json_structures: "semistructured",
};

/**
 * Is this stage an anchor?
 *
 * Matched on EITHER discriminator and against EVERY known anchor type, not just the one implied by
 * the pipeline's `kind`. Both halves are forced by the live data:
 *
 *   • the seeded defaults set `type` AND `action` to the same value, while pipelines authored in the
 *     old builders set only one — so keying on a single field misses whole families;
 *   • `kind` and the anchor do not always agree. Six live pipelines are labelled `document` or
 *     `structured` while carrying a `json_structures` anchor. Trusting the label there means the
 *     anchor is read as an ordinary step and a second, empty anchor is prepended on save — the
 *     pipeline silently gains a stage and changes what it does.
 */
export function isAnchor(stage: PipelineStage, _kind?: PipelineKind): boolean {
  const t = stage.type || "", a = stage.action || "";
  return t in ANCHOR_VALUES || a in ANCHOR_VALUES;
}

/** Which anchor editor this stage actually needs — from the stage itself, not from the label. */
export function anchorKindOf(stage: PipelineStage | null, fallback: PipelineKind): PipelineKind {
  if (!stage) return fallback;
  return ANCHOR_VALUES[stage.type || ""] || ANCHOR_VALUES[stage.action || ""] || fallback;
}

/**
 * Split a saved pipeline into its reading rule and its ordered steps.
 *
 * The whole anchor STAGE comes back, not just its config, because its id and discriminator have to
 * be written back exactly as they were found — a live pipeline whose anchor is `stage_0` must not
 * come out of an edit as `structures`.
 */
export function splitStages(stages: PipelineStage[], kind: PipelineKind): {
  anchor: Record<string, any>;
  anchorStage: PipelineStage | null;
  steps: PipelineStage[];
} {
  const list = stages || [];
  const at = list.findIndex((s) => isAnchor(s, kind));
  if (at < 0) return { anchor: {}, anchorStage: null, steps: list };
  return {
    anchor: readingRule(list[at].config),
    anchorStage: list[at],
    steps: list.filter((_, i) => i !== at),
  };
}

/**
 * Rebuild the wire format: the reading rule first, then the steps unchanged.
 *
 * `original` is the anchor stage this pipeline was loaded with. When there is one, every key but
 * the config is carried over verbatim — id, `type`, `action`, `source` — so editing a pipeline
 * cannot re-key it. The canonical shape below is only for a pipeline that has never been saved.
 */
export function buildStages(
  kind: PipelineKind,
  anchorConfig: Record<string, any>,
  steps: PipelineStage[],
  original?: PipelineStage | null,
): PipelineStage[] {
  const spec = ANCHOR_SPEC[kind];
  const config = readingRule(anchorConfig);
  const anchor: PipelineStage = original
    ? { ...original, config }
    : {
        id: spec.id,
        ...(spec.key === "type" ? { type: spec.value } : { action: spec.value }),
        ...(spec.source ? { source: spec.source } : {}),
        config,
      };
  return [anchor, ...(steps || [])];
}

/** A step's kind-agnostic display name. */
export const stageTitle = (s: PipelineStage) =>
  s.name || String(s.type || s.action || "step").replace(/_/g, " ");

/** Shared defaults for a newly added step, whatever the pipeline's kind. */
export function newStage(type: string, index: number): PipelineStage {
  return { id: `stage_${Date.now()}_${index}`, type, config: {} };
}

/* ══════════════════════════════════════════════════════════════════════════════════════════════
 * WHAT A STEP FEEDS ITS EXTRACTOR
 *
 * A step does not only read "the previous stage" — it is bound to specific NODE TYPES and specific
 * ATTRIBUTES on them, because one extractor is meant to be reusable across structures. That binding
 * is what these helpers describe, and it is the part of the old builders most worth keeping: three
 * things on an extractor read a field off the node it runs over, and each fails SILENTLY when the
 * binding does not supply it — no error, no empty result, just an attribute that is never written
 * and a graph that looks thinner than expected for no visible reason.
 * ══════════════════════════════════════════════════════════════════════════════════════════════ */

/**
 * Placeholders the engine fills from the node itself, whatever is ticked.
 * Flagging these would put a warning on every stage, which trains people to ignore warnings.
 */
const AUTOMATIC_PLACEHOLDERS = new Set([
  "text", "chunk_text", "label", "type", "node_type", "id", "attributes", "words", "per_node",
  "top_n", "columns", "table_name", "excerpt", "description", "members", "context", "query",
  "statements", "stats", "title", "transcript",
]);

/** Fields an extractor reads off its node: emit.from_source, copy overrides, prompt placeholders. */
export function requiredFields(tmpl: any): string[] {
  if (!tmpl) return [];
  const out = new Set<string>();
  ((tmpl.emit?.nodes) || []).forEach((n: any) =>
    (n?.from_source || []).forEach((f: any) => {
      const name = typeof f === "string" ? f : (f?.field || f?.name || f?.input || "");
      if (name) out.add(String(name));
    }));
  Object.values((tmpl.mechanism?.attr_overrides) || {}).forEach((ov: any) => {
    if (ov && String(ov.mechanism || "").toLowerCase() === "copy" && ov.input) out.add(String(ov.input));
  });
  const prompt = String(tmpl.mechanism?.prompt || "");
  (prompt.match(/\{([a-z_][a-z0-9_]*)\}/g) || []).forEach((m: string) => {
    const name = m.slice(1, -1);
    if (!AUTOMATIC_PLACEHOLDERS.has(name)) out.add(name);
  });
  return Array.from(out).filter((f) => typeof f === "string" && f.length > 0);
}

/** Fields this step's extractor needs that its current binding does not tick. */
export function unmetFields(stage: PipelineStage, templates: any[]): string[] {
  const tmpl = templates.find((t: any) => t.name === stage.type);
  const need = requiredFields(tmpl);
  if (!need.length) return [];
  const ticked = new Set<string>((stage.config?.input_nodes || []).flatMap((n: any) => n.attrs || []));
  return need.filter((f) => !ticked.has(f));
}

/**
 * A `relate.*` extractor does not enrich the node it runs over — it uses that node as an ANCHOR,
 * looks at what hangs off it by one edge type, and connects those to each other. Three things decide
 * its behaviour: the anchor, the edge it traverses, and the two endpoint types.
 */
export function relateSpec(stageType: string | undefined, templates: any[]) {
  const t = templates.find((x: any) => x.name === stageType);
  const ref = String(t?.mechanism?.ref || "");
  if (!ref.startsWith("relate.")) return null;
  const m = t.mechanism || {};
  return {
    via: String(m.via || m.traverse || "MENTIONS"),
    from: String(m.from_type || "entity"),
    to: String(m.to_type || "entity"),
    relation: String(m.relation || "RELATES_TO"),
    direction: String(m.direction || "from_to"),
  };
}

/** Which steps accept node-targeted input — extractors, not the reading-rule step. */
export const stageTakesNodes = (t?: string) => String(t || "") !== "chunking";

export interface VocabNode {
  node_type: string; attrs: string[]; origin?: string;
  from_stage?: string; from_extractor?: string;
}

/** Node types an EARLIER step in this pipeline emits — tagged with who produces them. */
export function emittedVocab(steps: PipelineStage[], upto: number, templates: any[]): VocabNode[] {
  return steps.slice(0, upto).flatMap((st) => {
    const nm = st.type || "";
    const tmpl = templates.find((t: any) => t.name === nm);
    return ((tmpl?.emit?.nodes || []) as any[])
      .filter((n) => n?.type && !String(n.type).startsWith("$"))
      .map((n) => ({
        node_type: String(n.type),
        attrs: (n.attributes || ["text"]).map(String),
        from_stage: st.id,
        from_extractor: nm,
      }));
  });
}
