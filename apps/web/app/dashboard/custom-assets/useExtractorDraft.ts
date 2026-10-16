"use client";

// Every piece of state and every builder the Custom Extractors page runs on — moved here VERBATIM from
// page.tsx when the UI was redesigned, so the screens can change without the save payload changing.
// buildMechanism / buildEmit / sourceSpec / buildSpec / generateYaml / buildRelMechanism are the
// contract with the engine: edit them here, never re-derive them in a view.
//
// The only addition is `runMsgId` — which extractor the last run message belongs to — so the result
// can be shown on that extractor's row instead of at the bottom of the list.
import { useState, useEffect } from "react";
import { contextV2Api } from "@/lib/api/contextV2";
import {
  attrRowsFromEmit, emitAttrsFromRows, idFieldText, idFieldList, normalizeEmitNodes,
} from "@/lib/extractorEmit";
import { DEPENDENT_MECHANISMS } from "./extractorVocab";

export type TabId = "extractions" | "relationship" | "built-in";

export function useExtractorDraft() {
  const [activeTab, setActiveTab] = useState<TabId>("extractions");
  const [isAdding, setIsAdding] = useState(false);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [runMsg, setRunMsg] = useState("");
  const [runMsgId, setRunMsgId] = useState<string | null>(null);
  // What just happened (saved / deleted / failed), shown as an in-page notice instead of alert().
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  // EXTRACTION BUILDER STATE
  const [extName, setExtName] = useState("");
  // Bound = runs OVER existing nodes; Independent = self-seeded, runs once. This is the extractor's
  // input SHAPE (its signature) and stays with the definition. WHICH concrete nodes/attributes a
  // bound extractor reads is bound per-stage in the pipeline (Add-Step editor), not here.
  const [sourceMode, setSourceMode] = useState<"dependent"|"independent">("dependent");
  const [mechRef, setMechRef] = useState(DEPENDENT_MECHANISMS[0]);
  const [mechPrompt, setMechPrompt] = useState("");
  const [mechSeedItems, setMechSeedItems] = useState("[\n  { \"name\": \"financial_crime\", \"parent\": null }\n]");
  const [mechGazetteer, setMechGazetteer] = useState("sanction_terms");
  const [mechTerms, setMechTerms] = useState("SAR, KYC, PII");      // dictionary/gazetteer terms
  const [mechPattern, setMechPattern] = useState("");               // regex pattern
  const [mechCode, setMechCode] = useState("");                     // unstructured.python — extract(inputs) body
  const [mechModelCapable, setMechModelCapable] = useState(false);  // python: inject llm() so extract() can call the model
  const [mechTopN, setMechTopN] = useState(8);                      // keyword top-N
  const [mechModel, setMechModel] = useState("");                   // chosen generation model (registry id; "" = org default)
  const [genModels, setGenModels] = useState<any[]>([]);            // registered generation models (Model Registry)
  // per-attribute prompt+model overrides: { attrKey: { prompt, model } } — each attribute can be
  // minted by its OWN model/prompt (e.g. cheap model for synonyms, strong one for description).
  const [attrOverrides, setAttrOverrides] = useState<Record<string, { mechanism?: string; prompt?: string; model?: string; pattern?: string; input?: string; value?: string }>>({});

  // Emits State
  const [emitNodes, setEmitNodes] = useState<any[]>([]);
  const [emitEdges, setEmitEdges] = useState<any[]>([]);
  const [emitAttrs, setEmitAttrs] = useState<any[]>([]);        // editable rows {key, value}
  const [emitAttrsRest, setEmitAttrsRest] = useState<any[]>([]); // attribute specs kept verbatim (see attrRowsFromEmit)
  const [emitMode, setEmitMode] = useState<"source" | "node">("source");   // enrich source vs create new node
  const [editingBuiltin, setEditingBuiltin] = useState(false);  // the open extractor is a builtin: (name + ref locked)
  // The review queue below still reads proposals; the contradiction engine's own settings now live
  // in Settings -> Capabilities (one card), not on an extractor card.
  const [proposals, setProposals] = useState<any[]>([]);
  // Guardrail phrase library (only on builtin:answer.generate) — abstention non-answer/refusal phrases
  const [guardNonAnswer, setGuardNonAnswer] = useState("");
  const [guardRefusal, setGuardRefusal] = useState("");

  // LIVE PREVIEW (dry-run over a sample) — the studio centerpiece. Shows the exact nodes, edges and
  // per-attribute values this extractor would emit, without writing anything to the graph.
  const [sampleText, setSampleText] = useState(
    "Priya Nair joined Anthropic last spring after five years at Google. She now leads the safety tooling team and is thrilled about the work.");
  const [preview, setPreview] = useState<any>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewErr, setPreviewErr] = useState("");
  const [openAttr, setOpenAttr] = useState("");   // "<nodeType>::<key>" of the attribute expanded in the output
  const [enrichType, setEnrichType] = useState("");   // which EXISTING node type to enrich (source mode); "" = any

  // RELATIONSHIP BUILDER STATE (relate.* templates) — connect two entities already attached to a
  // shared anchor node (e.g. a Chunk) by type. The anchor is context; nothing new is minted.
  const [isAddingRel, setIsAddingRel] = useState(false);
  const [relEditId, setRelEditId] = useState<string | null>(null);
  const [relName, setRelName] = useState("");
  const [relAnchor, setRelAnchor] = useState("Chunk");          // context / source node type
  const [relVia, setRelVia] = useState("MENTIONS");             // edge entities attach to the anchor by
  const [relFrom, setRelFrom] = useState("entity");             // From type ("entity" = any extracted entity)
  const [relTo, setRelTo] = useState("entity");                 // To type (may equal From)
  const [relMode, setRelMode] = useState("relate.llm");         // relate.llm | relate.fixed | relate.regex
  const [relRelation, setRelRelation] = useState("RELATES_TO"); // edge type to create
  const [relPrompt, setRelPrompt] = useState("");
  const [relModel, setRelModel] = useState("");
  const [relDirection, setRelDirection] = useState("from_to");  // from_to | both

  // List of saved extractions (from PostgreSQL)
  const [savedExtractions, setSavedExtractions] = useState<any[]>([]);
  const [availableNodes, setAvailableNodes] = useState<string[]>([]);   // node types — loaded live from Curation
  const [nodeAttrsByType, setNodeAttrsByType] = useState<Record<string, string[]>>({});   // existing attributes per node type (for enrich)

  useEffect(() => {
    loadExtractions();
    loadOntology();
    loadProposals();
    contextV2Api.listModels("generation").then((d) => setGenModels(d?.items || [])).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadProposals = async () => {
    try { setProposals(await contextV2Api.getResolutionProposals()); } catch { /* ignore */ }
  };
  const decideProposal = async (id: string, approve: boolean) => {
    try {
      if (approve) await contextV2Api.approveResolutionProposal(id);
      else await contextV2Api.rejectResolutionProposal(id);
      await loadProposals();
      setNotice({ kind: "ok", text: approve ? "Approved — the older claim is retired." : "Rejected — the older claim is kept." });
    } catch (err: any) { setNotice({ kind: "error", text: `Couldn't record that decision: ${err?.response?.data?.detail || err?.message || err}` }); }
  };

  const loadOntology = async () => {
    try {
      const data = await contextV2Api.ontology();
      // the API returns { types: [{ name, kind, ... }] } — pull node/entity types for the source list
      if (data && data.types) {
        const nodeTypes = data.types.filter((t: any) => t.kind === "node" || t.kind === "entity");
        // live ontology from Curation — types switched OFF are excluded from the source picker
        const names = Array.from(new Set(nodeTypes.filter((t: any) => t.status !== "disabled").map((t: any) => String(t.name))));
        setAvailableNodes(names as string[]);
        // existing attributes per node type — shown (read-only) when enriching that node.
        const attrMap: Record<string, string[]> = {};
        nodeTypes.forEach((t: any) => {
          attrMap[t.name] = (t.attributes || []).map((a: any) => (typeof a === "string" ? a : a?.name)).filter(Boolean);
        });
        setNodeAttrsByType(attrMap);
      }
    } catch (err) {
      console.error("Failed to load ontology", err);
    }
  };

  const loadExtractions = async () => {
    try {
      const exts = await contextV2Api.getExtractionTemplates();
      setSavedExtractions(exts);
    } catch (err) {
      console.error("Failed to load extractions", err);
    }
  };

  const generateYaml = () => {
    let yaml = `extraction:\n  name: ${extName || "untitled_extraction"}\n`;
    // Bound extractor stays generic — `select: "*"` = "any node; the pipeline stage decides which".
    // Independent = self-seeded (source: null).
    yaml += sourceMode === "independent" ? `  source: null\n` : `  source: { select: "*" }\n`;
    if (mechRef === "seed.literal") {
      yaml += `  mechanism:\n    ref: seed.literal\n    items: ${mechSeedItems.includes('\\n') ? '\n      ' + mechSeedItems.replace(/\\n/g, '\n      ') : mechSeedItems}\n`;
    } else if (mechRef === "unstructured.dictionary") {
      yaml += `  mechanism: { ref: unstructured.dictionary, gazetteer: ${mechGazetteer} }\n`;
    } else if (mechRef === "unstructured.python") {
      yaml += `  mechanism:\n    ref: unstructured.python\n    language: python\n    code: |\n${mechCode.split("\n").map(l => "      " + l).join("\n")}\n`;
    } else {
      yaml += `  mechanism: { ref: "${mechRef}"${mechPrompt ? `, prompt: "${mechPrompt}"` : ""} }\n`;
    }

    yaml += `  emit:\n`;
    if (emitNodes.length > 0) {
      yaml += `    nodes:\n`;
      emitNodes.forEach(n => yaml += `      - { type: ${n.type}, id: [${idFieldList(n.id).join(", ")}]${n.dedup ? ", dedup: true" : ""} }\n`);
    }
    if (emitEdges.length > 0) {
      yaml += `    edges:\n`;
      emitEdges.forEach(e => {
        const rel = e.type || e.rel || "RELATION";
        yaml += `      - { rel: ${rel}`;

        // FROM endpoint
        if (e.from === "match_on") {
          yaml += `,\n          from: { match: ${e.fromMatchNode || 'Node'}, on: { ${e.fromMatchField || 'id'}: "${e.fromMatchValue || '$val'}" } }`;
        } else {
          const frm = typeof e.from === "string" ? e.from : (e.from ? JSON.stringify(e.from) : "source");
          yaml += `, from: ${frm}`;
        }

        // TO endpoint
        if (e.to === "match_on") {
          yaml += `,\n          to: { match: ${e.toMatchNode || 'Node'}, on: { ${e.toMatchField || 'id'}: "${e.toMatchValue || '$val'}" } }`;
        } else {
          const to = typeof e.to === "string" ? e.to : (e.to ? JSON.stringify(e.to) : "emitted");
          yaml += `, to: ${to}`;
        }

        if (e.when) {
          yaml += `,\n          when: "${e.when}"`;
        }
        if (e.mode && e.mode !== "relate.fixed") {
          yaml += `,\n          mode: "${e.mode}"`;
          if (e.prompt) yaml += `,\n          prompt: "${e.prompt.replace(/"/g, '\\"').replace(/\n/g, '\\n')}"`;
          if (e.model) yaml += `,\n          model: "${e.model}"`;
        }
        yaml += ` }\n`;
      });
    }
    // Exactly what gets saved — `emit.attributes` is one `set` map per `on` target, not a row list.
    (buildEmit().attributes || []).forEach((a: any, i: number) => {
      if (i === 0) yaml += `    attributes:\n`;
      const pairs = Object.entries(a.set || {}).map(([k, v]) => `${k}: "${v}"`).join(", ");
      yaml += `      - { on: ${typeof a.on === "string" ? a.on : JSON.stringify(a.on)}, set: { ${pairs} } }\n`;
    });
    return yaml;
  };

  // With the extractor-level Mechanism step gone, the main LLM prompt is derived from the attributes
  // being authored — it produces the records/fields that per-attribute tools then refine. Fields with
  // a non-LLM override (regex/copy/constant) are filled by that tool, so they're left out of the prompt.
  const autoPrompt = () => {
    const keys = emitMode === "node"
      ? Array.from(new Set(emitNodes.flatMap((n: any) => n.attributes || [])))
      : emitAttrs.map((a: any) => a.key).filter(Boolean);
    const llmKeys = keys.filter((k) => { const o = attrOverrides[k]; return !o || (o.mechanism || "llm") === "llm"; });
    if (emitMode === "node") {
      return llmKeys.length
        ? `From the TEXT, extract one or more records. For each record return these fields as JSON: ${llmKeys.join(", ")}. Return ONLY {"records":[{...}]}.\n\nTEXT:\n{text}`
        : `Return ONLY {"records":[{}]}.`;
    }
    return llmKeys.length
      ? `From the TEXT, extract these fields as JSON: ${llmKeys.join(", ")}.\n\nTEXT:\n{text}`
      : `Return ONLY {}.`;
  };

  const buildMechanism = () => {
    const m: any = { ref: mechRef, category: mechRef.startsWith("structured") ? "structured" : "unstructured" };
    // The main prompt is auto-derived from the authored attributes (the Mechanism step was removed).
    // NB `unstructured.attributes` is deliberately excluded: it has NO shared prompt — every attribute
    // is minted by its own — and inventing one here would quietly turn it into a different extractor.
    if (mechRef !== "unstructured.attributes" && (mechRef.includes("llm") || mechPrompt)) m.prompt = mechPrompt || autoPrompt();
    // The model travels for every mechanism that can call one, not only the prompt-driven ones — the
    // Studio offers the picker to keyword/NER/attribute extractors too, and a picker whose value is
    // never sent is worse than no picker. null = org default.
    if (mechRef !== "seed.literal" && mechRef !== "unstructured.regex") m.model = mechModel || null;
    // per-attribute overrides — keep only entries whose chosen mechanism is fully specified.
    const ov: Record<string, any> = {};
    Object.entries(attrOverrides).forEach(([k, v]) => {
      if (!v) return;
      const mech = v.mechanism || "llm";
      const active = (mech === "llm" && (v.prompt || "").trim())
        || (mech === "regex" && (v.pattern || "").trim())
        || (mech === "copy" && (v.input || "").trim())
        || (mech === "constant" && (v.value || "") !== "");
      if (active) ov[k] = { mechanism: mech, prompt: v.prompt || "", model: v.model || null,
                            pattern: v.pattern || "", input: v.input || "", value: v.value || "" };
    });
    m.attr_overrides = ov;   // always send (empty {} clears any previous overrides)
    if (extName.includes("answer.generate")) {             // guardrail phrase library (abstention)
      m.non_answer_phrases = guardNonAnswer.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
      m.refusal_phrases = guardRefusal.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
    }
    if (mechRef === "seed.literal") { try { m.items = JSON.parse(mechSeedItems); } catch { m.items = []; } }
    if (mechRef === "unstructured.dictionary") {
      m.terms = mechTerms.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
      m.gazetteer = mechGazetteer;
    }
    if (mechRef === "unstructured.regex") m.pattern = mechPattern;
    if (mechRef === "unstructured.keyword") m.top_n = mechTopN;
    if (mechRef === "unstructured.python") { m.language = "python"; m.code = mechCode; m.model_capable = mechModelCapable; delete m.prompt; if (!mechModelCapable) delete m.model; }
    if (emitEdges.length > 0) {
      if (relMode) m.rel_mode = relMode;
      if (relMode === "relate.llm") {
        if (relPrompt) m.rel_prompt = relPrompt;
        if (relModel) m.rel_model = relModel;
      }
    }
    return m;
  };

  // Bound extractors normally bind their node per pipeline stage ({select:"*"}); when enriching a
  // specific existing node type, bind straight to it so the extractor targets that node.
  const sourceSpec = () => sourceMode === "independent" ? null
    : (emitMode === "source" && enrichType) ? { select: enrichType }
    : { select: "*" };

  // The emit exactly as the ENGINE reads it — one place, so the live preview, the YAML and the save
  // can never describe three different extractors.
  const buildEmit = () => {
    const attributes = emitAttrsFromRows(emitAttrs, emitAttrsRest);
    const edges = emitEdges.map(e => {
      const mode = e.mode || "relate.fixed";
      return {
        ...e,
        mode,
        ...(mode === "relate.llm" && e.prompt ? { prompt: e.prompt } : {}),
        ...(mode === "relate.llm" && e.model ? { model: e.model } : {}),
      };
    });
    return {
      ...(emitNodes.length ? { nodes: normalizeEmitNodes(emitNodes) } : {}),
      ...(edges.length ? { edges } : {}),
      ...(attributes.length ? { attributes } : {}),
    };
  };

  // Assemble the current (unsaved) extractor exactly as it would be saved, for the live preview.
  const buildSpec = () => ({
    name: extName || "preview",
    source: sourceSpec(),
    mechanism: buildMechanism(),
    emit: buildEmit(),
  });

  const runPreview = async () => {
    setPreviewing(true);
    setPreviewErr("");
    try {
      const r = await contextV2Api.previewExtractionTemplate({ ...buildSpec(), sample_text: sampleText });
      setPreview(r);
    } catch (e: any) {
      setPreviewErr(e?.response?.data?.detail || e?.message || String(e));
    } finally {
      setPreviewing(false);
    }
  };

  // Debounced auto-preview: whenever the recipe or the sample changes while authoring, re-run the
  // read-only dry-run so the output panel stays live. Deterministic mechanisms are instant; LLM ones
  // cost one call per edit-pause (900ms), which is the price of the live loop.
  useEffect(() => {
    if (!isAdding || !sampleText.trim()) return;
    const t = setTimeout(() => { runPreview(); }, 900);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdding, sampleText, sourceMode, mechRef, mechPrompt, mechModel, mechPattern, mechCode, mechModelCapable, mechTerms, mechTopN,
      mechSeedItems, emitMode, enrichType, relMode, relPrompt, relModel,
      JSON.stringify(emitNodes), JSON.stringify(emitEdges), JSON.stringify(emitAttrs), JSON.stringify(emitAttrsRest),
      JSON.stringify(attrOverrides)]);

  // ── In-place attribute authoring (the prototype's editable output) ─────────────────────────────
  // Each attribute in the live output can be expanded to pick its tool + prompt + model + weight.
  // These map onto the exact save schema: mechanism.attr_overrides[key].
  // "From the node" is NOT the same as "Copy field", though it reads like it. Copy resolves against
  // `{...ctx, ...record}` — the model's own output SHADOWS the node when both carry the field name,
  // which is exactly the case worth protecting (measured: node "Priya", model "Nate", copy returned
  // "Nate"). `from_source` is applied after the record, against the node alone, so the node wins.
  const ATTR_TOOLS = [
    { m: "llm", label: "AI · LLM" }, { m: "regex", label: "Regex" },
    { m: "copy", label: "Copy field" }, { m: "constant", label: "Constant" },
    { m: "source", label: "From the node" },
  ];

  // `from_source` lives on the emitted NODE spec, not in attr_overrides — it is a property of what
  // the node is made of, not of a per-attribute mechanism, so it is toggled on emitNodes.
  const isFromSource = (nodeType: string, key: string) =>
    ((emitNodes.find((n: any) => n.type === nodeType)?.from_source) || []).includes(key);

  const setFromSource = (nodeType: string, key: string, on: boolean) =>
    setEmitNodes(prev => prev.map((n: any) => {
      if (n.type !== nodeType) return n;
      const cur = new Set<string>(n.from_source || []);
      on ? cur.add(key) : cur.delete(key);
      const next = { ...n, from_source: Array.from(cur) };
      if (!next.from_source.length) delete next.from_source;
      return next;
    }));
  const setAttrOv = (key: string, patch: any) =>
    setAttrOverrides(prev => ({ ...prev, [key]: { mechanism: "llm", ...(prev[key] || {}), ...patch } }));

  // Add a fresh attribute to a node (create mode → the emit node spec of that type; enrich → emit.attributes),
  // `$out.` or `$llm.` — the SAME thing to the engine (both mean "a field of whatever this
  // mechanism produced"), but only one of them is true on the card. A python/regex/dictionary/
  // keyword extractor has no model and no prompt anywhere in it, so writing `$llm.normalized_date`
  // onto a Python function names a component that is not involved, and every author who read it
  // asked the same question. The engine also accepts a bare `$normalized_date` now; the explicit
  // form is kept here because it distinguishes the output from `$source.` at a glance.
  const outToken = (key: string) =>
    (mechRef.includes("llm") ? `$llm.${key}` : `$out.${key}`);

  // default it to an LLM attribute, and open its editor so the tool + prompt are decided right there, live.
  const addAttrToNode = (nodeType: string) => {
    const taken = new Set<string>();
    emitNodes.forEach(n => (n.attributes || []).forEach((k: string) => taken.add(k)));
    emitAttrs.forEach((a: any) => taken.add(a.key));
    let key = "new_attribute", i = 1;
    while (taken.has(key)) key = `new_attribute_${i++}`;
    // The TARGET is the card the button lives on, not the mode: an extractor may both emit a node and
    // enrich the node it ran over, and "+ Add an attribute to the node" must mean that node either way.
    if (nodeType !== "__source") {
      setEmitNodes(prev => {
        const arr = prev.map(n => ({ ...n, attributes: [...(n.attributes || [])] }));
        let spec = arr.find(n => n.type === nodeType);
        if (!spec) { spec = { type: nodeType || "Node", id: "name", attributes: [] }; arr.push(spec); }
        spec.attributes.push(key);
        return arr;
      });
    } else {
      setEmitAttrs(prev => [...prev, { key, value: outToken(key) }]);
    }
    // A per-attribute override is a SECOND, separate model call. Defaulting one on a
    // deterministic extractor turns a free step into a paid one the moment you add an
    // attribute to it — silently, since the card still reads "python".
    setAttrOv(key, mechRef.includes("llm") ? { mechanism: "llm", prompt: "" }
                                          : { mechanism: "none", prompt: "" });
    setOpenAttr(nodeType + "::" + key);
  };

  const renameAttr = (nodeType: string, oldKey: string, newKey: string) => {
    if (!newKey || newKey === oldKey) return;
    setEmitNodes(prev => prev.map(n => ({ ...n, attributes: (n.attributes || []).map((k: string) => k === oldKey ? newKey : k) })));
    setEmitAttrs(prev => prev.map((a: any) => a.key === oldKey ? { key: newKey, value: outToken(newKey) } : a));
    setAttrOverrides(prev => { const n = { ...prev }; if (n[oldKey] !== undefined) { n[newKey] = n[oldKey]; delete n[oldKey]; } return n; });
    setOpenAttr(nodeType + "::" + newKey);
  };

  const removeAttr = (key: string) => {
    setEmitNodes(prev => prev.map(n => ({ ...n, attributes: (n.attributes || []).filter((k: string) => k !== key) })));
    setEmitAttrs(prev => prev.filter((a: any) => a.key !== key));
    setAttrOverrides(prev => { const n = { ...prev }; delete n[key]; return n; });
    setOpenAttr("");
  };

  // Node specs edited inline in the output (create mode) — one card per node type.
  const addNode = () => setEmitNodes(prev => [...prev, { type: "Node", id: "name", attributes: [] }]);
  const setNode = (i: number, patch: any) => setEmitNodes(prev => prev.map((n, j) => j === i ? { ...n, ...patch } : n));
  const removeNode = (i: number) => setEmitNodes(prev => prev.filter((_, j) => j !== i));

  // Colour a mechanism badge in the preview so LLM / NER / regex / dictionary / copy / constant are
  // distinguishable at a glance (matches the authoring vocabulary).
  // Every string `_mech_short` / the preview can report. A mechanism missing here rendered as an
  // unexplained grey chip — which is how `source` once looked on a claim's `speaker`: a word the card
  // gave no colour and no control to, beside an editor still offering an LLM prompt for it.
  const mechBadge = (m: string) => ({
    llm: "bg-violet-100 text-violet-700", ner: "bg-blue-100 text-blue-700",
    regex: "bg-amber-100 text-amber-700", dictionary: "bg-green-100 text-green-700",
    keyword: "bg-teal-100 text-teal-700", copy: "bg-teal-100 text-teal-700",
    python: "bg-sky-100 text-sky-700", source: "bg-indigo-100 text-indigo-700",
    "per-attribute": "bg-fuchsia-100 text-fuchsia-700",
    constant: "bg-stone-100 text-stone-600",
  }[m] || "bg-stone-100 text-stone-600");

  // WHICH node, WHICH field. Both "Copy field" and "From the node" read a property off the node the
  // extractor runs over, and the box for it used to be free text with a placeholder guess
  // ("e.g. text, physical_column") — leaving the only two questions that matter unanswered. These
  // are the attributes that node type actually carries in the graph today (GET /node-types/attributes,
  // the same source the pipeline builder's input picker uses).
  const readsNodeType = enrichType || "";   // "" = {select:"*"}, i.e. the pipeline stage decides
  const nodeFields: string[] = nodeAttrsByType[readsNodeType] || [];
  // "Any node (bind in pipeline)" is a real and common choice — the stage decides. Say so rather
  // than showing an empty list as if the node had no fields.
  const boundInPipeline = !readsNodeType;

  const modelLabel = (id?: string) => {
    if (!id) return "Org default";
    const m = genModels.find((x) => x.id === id);
    return m ? `${m.name} (${m.provider}/${m.model_id})` : "custom";
  };

  const handleSaveExtraction = async () => {
    if (!extName) return;
    try {
      await contextV2Api.createExtractionTemplate({
        name: extName,
        source: sourceSpec(),   // {select:"*"} bound per-stage, or the chosen existing node type when enriching
        mechanism: buildMechanism(),
        // send every populated emit part so editing edges/nodes/attributes of ANY extractor (incl.
        // built-ins with edges but no nodes) is preserved, not dropped.
        emit: buildEmit(),
      });
      await loadExtractions();
      setIsAdding(false);
      setExtName("");
      setEditingBuiltin(false);
      setEmitNodes([]);
      setEmitEdges([]);
      setEmitAttrs([]);
      setEmitAttrsRest([]);
      setEmitMode("source");
      setMechModel("");
      setMechCode("");
      setMechModelCapable(false);
      setMechRef(DEPENDENT_MECHANISMS[0]);
      setRelMode("relate.llm");
      setRelPrompt("");
      setRelModel("");
      setNotice({ kind: "ok", text: `Saved ${extName.replace(/^builtin:/, "")}.` });
    } catch (err: any) {
      setNotice({ kind: "error", text: `Couldn't save ${extName}: ${err?.response?.data?.detail || err?.message || err}` });
    }
  };

  /** Which of the three authoring shapes a mechanism corresponds to, or null when it is none of
   *  them (seed.literal, relate.llm, pipeline.*, structured.llm — those have their own knobs and
   *  are not one of the bound-extractor shapes). Kept identical to the tile logic used for custom
   *  extractors so a built-in and a custom card never disagree about what "Prompt" means. */
  const BUILTIN_SHAPE = (ref: string, prompt: string): string | null => {
    if (ref === "unstructured.python") return "Coded · Python";
    if (ref === "unstructured.attributes") return "Attributes";
    if (ref === "unstructured.llm") return prompt.trim() ? "Prompt" : "Attributes";
    return null;
  };

  /** Open an editable COPY of a built-in. The copy is an ordinary custom extractor: its mechanism
   *  is free to change, and the catalog will not overwrite it. The original is untouched — which is
   *  the point of locking it, not a limitation to work around. */
  const duplicateAsCustom = () => {
    const base = extName.replace(/^builtin:/, "").replace(/[^a-zA-Z0-9._-]/g, "-");
    setExtName(`${base}-copy`);
    setEditingBuiltin(false);
    setPreview(null);
    setPreviewErr("");
  };

  const handleEditExtraction = (ext: any) => {
    setExtName(ext.name);
    setEditingBuiltin(String(ext.name || "").startsWith("builtin:"));
    setSourceMode(ext.source ? "dependent" : "independent");
    { const sel = ext.source?.select; setEnrichType(typeof sel === "string" && sel !== "*" ? sel : ""); }
    setMechRef(ext.mechanism.ref || "unstructured.llm");
    setMechPrompt(ext.mechanism.prompt || "");
    setMechModel(ext.mechanism.model || "");
    if (ext.mechanism.items) setMechSeedItems(JSON.stringify(ext.mechanism.items, null, 2));
    if (ext.mechanism.terms) setMechTerms((ext.mechanism.terms || []).join(", "));
    if (ext.mechanism.pattern) setMechPattern(ext.mechanism.pattern);
    setMechCode(ext.mechanism.code || "");
    setMechModelCapable(!!ext.mechanism.model_capable);
    if (ext.mechanism.top_n) setMechTopN(ext.mechanism.top_n);
    // `id` is stored as the list of record fields that identify the node; the card edits it as text.
    setEmitNodes((ext.emit?.nodes || []).map((n: any) => ({ ...n, id: idFieldText(n?.id) })));
    setEmitEdges(ext.emit?.edges || []);
    { const { rows, rest } = attrRowsFromEmit(ext.emit?.attributes || []); setEmitAttrs(rows); setEmitAttrsRest(rest); }
    const edge0 = (ext.emit?.edges || [])[0] || {};
    setRelMode(ext.mechanism?.rel_mode || edge0.mode || "relate.llm");
    setRelPrompt(ext.mechanism?.rel_prompt || edge0.prompt || "");
    setRelModel(ext.mechanism?.rel_model || edge0.model || "");
    setGuardNonAnswer((ext.mechanism?.non_answer_phrases || []).join(", "));
    setGuardRefusal((ext.mechanism?.refusal_phrases || []).join(", "));
    setEmitMode((ext.emit?.nodes || []).length ? "node" : "source");
    setAttrOverrides(ext.mechanism?.attr_overrides || {});
    setPreview(null);
    setPreviewErr("");
    setIsAdding(true);
  };

  /** Start a blank extractor — used by "New Extraction" so nothing from a previous edit leaks in. */
  const startNewExtraction = () => {
    setExtName("");
    setEditingBuiltin(false);
    setSourceMode("dependent");
    setMechRef(DEPENDENT_MECHANISMS[0]);
    setMechPrompt(""); setMechCode(""); setMechModel(""); setMechPattern("");
    setMechModelCapable(false);
    setEmitNodes([]); setEmitEdges([]); setEmitAttrs([]); setEmitAttrsRest([]);
    setRelMode("relate.llm"); setRelPrompt(""); setRelModel("");
    setAttrOverrides({});
    setGuardNonAnswer(""); setGuardRefusal("");
    setEmitMode("source"); setEnrichType(""); setPreview(null); setPreviewErr("");
    setIsAdding(true);
  };

  const handleDelete = async (id: string) => {
    try {
      await contextV2Api.deleteExtractionTemplate(id);
      await loadExtractions();
      setNotice({ kind: "ok", text: "Extractor deleted." });
    } catch (err: any) {
      setNotice({ kind: "error", text: `Couldn't delete it: ${err?.response?.data?.detail || err?.message || err}` });
    }
  };

  // Run an executable enrichment template over the existing graph (attach attrs/nodes/edges).
  const handleRun = async (id: string) => {
    setRunningId(id);
    setRunMsg("");
    setRunMsgId(id);
    try {
      const r = await contextV2Api.runExtractionTemplate(id);
      setRunMsg(`Dispatched (${r.status}). Enrichment is running in the background.`);
    } catch (err: any) {
      setRunMsg(`Failed to run: ${err?.response?.data?.detail || err?.message || err}`);
    } finally {
      setRunningId(null);
    }
  };

  // ── Relationship extractors (relate.* templates) ──────────────────────────
  const resetRel = () => {
    setRelEditId(null); setRelName(""); setRelAnchor(availableNodes.includes("Chunk") ? "Chunk" : (availableNodes[0] || "Chunk")); setRelVia("MENTIONS");
    setRelFrom("entity"); setRelTo("entity"); setRelMode("relate.llm");
    setRelRelation("RELATES_TO"); setRelPrompt(""); setRelModel(""); setRelDirection("from_to");
  };

  const buildRelMechanism = () => {
    const m: any = {
      ref: relMode, category: "unstructured",
      from_type: relFrom, to_type: relTo, via: relVia || "MENTIONS",
      relation: (relRelation || "RELATES_TO").trim(), direction: relDirection,
    };
    if (relMode === "relate.llm") { m.prompt = relPrompt; m.model = relModel || null; }
    return m;
  };

  const handleSaveRelationship = async () => {
    if (!relName.trim() || !relFrom || !relTo) return;
    try {
      await contextV2Api.createExtractionTemplate({
        name: relName.trim(),
        source: { select: relAnchor },
        mechanism: buildRelMechanism(),
        // emit.edges is descriptive (powers the card) — the relate engine reads `mechanism`.
        emit: { edges: [{ type: (relRelation || "RELATES_TO").trim(), from: relFrom, to: relTo }] },
      });
      await loadExtractions();
      setIsAddingRel(false);
      resetRel();
      setNotice({ kind: "ok", text: `Saved ${relName.trim().replace(/^builtin:/, "")}.` });
    } catch (err: any) {
      setNotice({ kind: "error", text: `Couldn't save the relationship: ${err?.response?.data?.detail || err?.message || err}` });
    }
  };

  const handleEditRelationship = (ext: any) => {
    const m = ext.mechanism || {};
    setRelEditId(ext.id);
    setRelName(ext.name);
    setRelAnchor(Array.isArray(ext.source?.select) ? ext.source.select[0] : (ext.source?.select || "Chunk"));
    setRelVia(m.via || "MENTIONS");
    setRelFrom(m.from_type || "Person");
    setRelTo(m.to_type || "Person");
    setRelMode(m.ref || "relate.llm");
    setRelRelation(m.relation || "RELATES_TO");
    setRelPrompt(m.prompt || "");
    setRelModel(m.model || "");
    setRelDirection(m.direction || "from_to");
    setIsAddingRel(true);
  };

  return {
    // tabs + list
    activeTab, setActiveTab, notice, setNotice, isAdding, setIsAdding, runningId, runMsg, runMsgId,
    savedExtractions, availableNodes, nodeAttrsByType, genModels, proposals, decideProposal,
    handleEditExtraction, startNewExtraction, handleDelete, handleRun, modelLabel,
    // extractor draft
    extName, setExtName, sourceMode, setSourceMode, mechRef, setMechRef, mechPrompt, setMechPrompt,
    mechSeedItems, setMechSeedItems, mechGazetteer, setMechGazetteer, mechTerms, setMechTerms,
    mechPattern, setMechPattern, mechCode, setMechCode, mechModelCapable, setMechModelCapable,
    mechTopN, setMechTopN, mechModel, setMechModel,
    attrOverrides, emitNodes, emitEdges, setEmitEdges, emitAttrs, setEmitAttrs,
    emitAttrsRest, setEmitAttrsRest, emitMode, setEmitMode, editingBuiltin,
    guardNonAnswer, setGuardNonAnswer, guardRefusal, setGuardRefusal,
    sampleText, setSampleText, preview, previewing, previewErr, runPreview,
    openAttr, setOpenAttr, enrichType, setEnrichType,
    generateYaml, autoPrompt, buildMechanism, sourceSpec, buildEmit, buildSpec,
    ATTR_TOOLS, isFromSource, setFromSource, setAttrOv, outToken,
    addAttrToNode, renameAttr, removeAttr, addNode, setNode, removeNode, mechBadge,
    readsNodeType, nodeFields, boundInPipeline,
    handleSaveExtraction, BUILTIN_SHAPE, duplicateAsCustom,
    // relationship draft
    isAddingRel, setIsAddingRel, relEditId, relName, setRelName, relAnchor, setRelAnchor,
    relVia, setRelVia, relFrom, setRelFrom, relTo, setRelTo, relMode, setRelMode,
    relRelation, setRelRelation, relPrompt, setRelPrompt, relModel, setRelModel,
    relDirection, setRelDirection, resetRel, buildRelMechanism, handleSaveRelationship, handleEditRelationship,
  };
}

export type ExtractorDraft = ReturnType<typeof useExtractorDraft>;
