// Vocabulary shared by the Custom Extractors screens. Moved out of page.tsx unchanged when the UI was
// split from its logic (useExtractorDraft.ts) — `test_mechanism_vocabulary.py` reads
// DEPENDENT_MECHANISMS from THIS file.
import {
  Network, Settings2, Code, Plus, Workflow, Hexagon, Database,
} from "lucide-react";

// Edge types entities commonly attach to a context node by (the "via" link). Free text also allowed.
export const VIA_EDGES = ["MENTIONS", "EXTRACTED_FROM", "MENTIONS_KEYWORD", "HAS_CLAIM", "RELATES_TO", "ABOUT"];
export const RELATE_MODES = [
  { ref: "relate.llm", label: "LLM (dynamic)", desc: "Model reads the context and decides which pairs relate." },
  { ref: "relate.fixed", label: "Fixed (all pairs)", desc: "Connect every From to every To — deterministic, no LLM." },
  { ref: "relate.regex", label: "Co-occurrence", desc: "Connect a pair when both labels appear in the context text." },
];

// An edge endpoint can be a role keyword ("source"/"emitted"), a type name, or a declarative object
// ({resolve:"entity",name:"$f"} | {match:"Type",on:{field:"$f"}}). Render it readably, using the SAME
// field words the prompt uses (source/target/entity) so the card and the prompt line up.
export const epLabel = (ep: any): string => {
  if (ep == null) return "?";
  if (typeof ep === "string") return ep === "source" ? "the chunk" : ep === "emitted" ? "this node" : ep;
  if (ep.resolve) {
    const f = String(ep.name || "").replace(/^\$/, "");   // "$target" → "target"
    return f === "entity" ? "the entity" : `the “${f}” entity`;   // → the "source" entity, the "target" entity
  }
  if (ep.find) return `the existing “${String(ep.name || "").replace(/^\$/, "")}” entity`;
  if (ep.match) return `${ep.match} (matched by ${Object.keys(ep.on || {})[0] || "field"})`;
  if (ep.node_id) return `the node(s) in “${String(ep.node_id).replace(/^\$/, "")}”`;
  return JSON.stringify(ep);
};

// The badge names the mechanism an extractor ACTUALLY runs. Built-ins and custom extractors share
// this, because after the migration a built-in is an ordinary extractor: Python / constant records /
// keyword / LLM — never a vague "deterministic, no prompt".
export const extractorBadge = (ext: any): { label: string; cls: string } => {
  const ref = String(ext?.mechanism?.ref || "");
  const capable = !!ext?.mechanism?.model_capable;
  if (ref === "unstructured.python")
    return { label: capable ? "Python · model-capable" : "Python · coded", cls: "bg-sky-100 text-sky-700" };
  if (ref === "unstructured.attributes")
    return { label: "Per-attribute", cls: "bg-violet-100 text-violet-700" };
  if (ref === "seed.literal") return { label: "Constant records", cls: "bg-stone-200 text-stone-700" };
  if (ref === "unstructured.keyword") return { label: capable ? "Keyword · model-capable" : "Keyword · top-N", cls: "bg-sky-100 text-sky-700" };
  if (ref === "unstructured.regex") return { label: "Regex", cls: "bg-sky-100 text-sky-700" };
  if (ref === "unstructured.dictionary") return { label: "Dictionary", cls: "bg-sky-100 text-sky-700" };
  if (ref === "unstructured.ner") return { label: "NER", cls: "bg-sky-100 text-sky-700" };
  if (ref.startsWith("relate.")) return { label: "Relationship", cls: "bg-indigo-100 text-indigo-700" };
  // Not extractors: they run when a question is answered or a session is saved, over text that was
  // never a node. Saying so is more useful than calling them a pipeline.
  if (ref === "pipeline.answer") return { label: "Runtime prompt · answer time", cls: "bg-amber-100 text-amber-700" };
  if (ref === "pipeline.memory") return { label: "Runtime prompt · session save", cls: "bg-amber-100 text-amber-700" };
  if (ref.startsWith("deterministic") || ref.startsWith("pipeline"))
    return ext?.mechanism?.prompt
      ? { label: "LLM · pipeline", cls: "bg-emerald-100 text-emerald-700" }
      : { label: "Loader · produced during ingest", cls: "bg-amber-100 text-amber-700" };
  return { label: "LLM", cls: "bg-emerald-100 text-emerald-700" };
};

// MUST match `DEPENDENT_MECHANISM_REFS` in enrich_templates.py — the engine declares the vocabulary
// and `test_mechanism_vocabulary.py` fails when these drift. This list previously offered
// `unstructured.topic` and `structured.sql`, which the engine has no branch for: an extractor built
// on either saved, ran, reported success and wrote nothing. `structured.llm` and
// `unstructured.attributes` were the reverse — implemented, used by seven built-ins, unpickable.
export const DEPENDENT_MECHANISMS = [
  "unstructured.llm",
  "structured.llm",
  "unstructured.attributes",
  "unstructured.python",
  "unstructured.regex",
  "unstructured.dictionary",
  "unstructured.keyword",
  "unstructured.ner",
];

export const INDEPENDENT_MECHANISMS = [
  "loader.documents",
  "structured.sql",
  "seed.literal",
  "unstructured.generative_llm"
];

// Friendly, human-readable presentation for each mechanism ref — so the picker reads like a studio,
// not a raw enum. The values are unchanged (still the exact refs the engine expects).
export const MECH_META: Record<string, { label: string; hint: string; icon: any }> = {
  "unstructured.llm": { label: "AI · LLM", hint: "A prompt reads the text", icon: Settings2 },
  "unstructured.ner": { label: "Named entities", hint: "Tag people, orgs, places", icon: Network },
  "unstructured.dictionary": { label: "Dictionary", hint: "Match a term list", icon: Database },
  "unstructured.keyword": { label: "Keywords", hint: "Top terms by frequency", icon: Hexagon },
  "unstructured.topic": { label: "Topics", hint: "Cluster into themes", icon: Workflow },
  "unstructured.regex": { label: "Pattern · regex", hint: "Match a pattern", icon: Code },
  "unstructured.python": { label: "Code · Python", hint: "Run an extract() function", icon: Code },
  "structured.sql": { label: "SQL", hint: "Query structured data", icon: Database },
  "loader.documents": { label: "Load documents", hint: "Pull from a connector", icon: Database },
  "seed.literal": { label: "Seed list", hint: "A fixed JSON list", icon: Plus },
  "unstructured.generative_llm": { label: "Generate · LLM", hint: "Model invents records", icon: Settings2 },
};

// ── Presentation-only helpers for the list rows (new UI). They describe a saved extractor in plain
// words; nothing here feeds the save payload.

/** "What it reads" for a saved extractor. */
export const readsLabel = (ext: any): string => {
  const sel = ext?.source?.select;
  if (!ext?.source) return "Nothing (creates its own records)";
  // read where the data lives: a type's records, or a file (and the field)
  const data = ext.source.data;
  if (data?.type) return data.column ? `${data.type} - ${data.column}` : `Each ${data.type}`;
  if (data?.asset) return [data.asset, data.column || data.path].filter(Boolean).join(" - ");
  if (!sel || sel === "*") return "Any node (set in pipeline)";
  return Array.isArray(sel) ? sel.join(", ") : String(sel);
};

/** "How it works" for a saved extractor, in words a person recognises. */
export const howLabel = (ext: any): string => {
  const ref = String(ext?.mechanism?.ref || "");
  if (ref === "unstructured.llm" || ref === "structured.llm") return ext?.mechanism?.prompt ? "AI prompt" : "AI, field by field";
  if (ref === "unstructured.attributes") return "Each field its own tool";
  if (MECH_META[ref]) return MECH_META[ref].label;
  return extractorBadge(ext).label;
};

/** "What it produces" — nodes AND edges AND source attributes (never just nodes). */
export const producesLabel = (ext: any): string => {
  const n = ext?.emit?.nodes?.length || 0;
  const e = ext?.emit?.edges?.length || 0;
  const a = ext?.emit?.attributes?.length || 0;
  return [
    n && `${n} node${n > 1 ? "s" : ""}`,
    e && `${e} relationship${e > 1 ? "s" : ""}`,
    a && "fields on the node it reads",
  ].filter(Boolean).join(" + ") || "nothing";
};

export const CATEGORY_LABELS: Record<string, { title: string; hint: string }> = {
  structured: { title: "Structured tables", hint: "Tables, columns and their meaning" },
  semistructured: { title: "Semi-structured (JSON)", hint: "Walks JSON keys into nodes" },
  unstructured: { title: "Documents & text", hint: "Chunks, entities, keywords" },
  memory: { title: "Memory", hint: "Sessions and what was said in them" },
  answer: { title: "Answers", hint: "Prompts used when a question is answered" },
};

/** Text a list filter matches against. */
export const searchText = (ext: any): string => [
  ext?.name, ext?.mechanism?.ref, JSON.stringify(ext?.source?.select ?? ""),
  ...(ext?.emit?.nodes || []).map((n: any) => n?.type),
  ...(ext?.emit?.edges || []).map((e: any) => e?.type || e?.rel),
  ext?.mechanism?.from_type, ext?.mechanism?.to_type, ext?.mechanism?.relation,
].filter(Boolean).join(" ").toLowerCase();
