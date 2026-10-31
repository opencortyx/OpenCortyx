"use client";

/**
 * One pipeline builder, for every kind.
 *
 * There were three pages — document, structured, semistructured — carrying three copies of the same
 * stage list, the same execution rules and the same save path, drifting apart as each was edited.
 * They were never three things. Every pipeline in this system has the same two-part shape:
 *
 *    Stage 1     the READING RULE — a bound mapping and two options (accept newly detected shapes,
 *                extract only). The same form for every kind.
 *    Stage 2..N  the STEPS — extractors over whatever the anchor produced. Identical everywhere.
 *
 * `/pipelines/new?kind=…` creates; `/pipelines/<id>` edits. The three old routes now redirect here,
 * and the wire format is unchanged, so every pipeline already saved keeps loading and running.
 */

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import { contextV2Api } from "@/lib/api/contextV2";
import {
  GitBranch, ArrowLeft, Save, Plus, Loader2, AlertTriangle, CheckCircle2, Braces, Table as TableIcon, FileText,
} from "lucide-react";
import PipelineStageCard, { stageName, type StageOutput } from "@/components/pipelines/PipelineStageCard";
import {
  KIND_META, anchorKindOf, buildStages, emittedVocab, newStage, splitStages,
  type PipelineKind, type PipelineStage, type VocabNode,
} from "@/components/pipelines/model";
import StageInputBinding from "@/components/pipelines/StageInputBinding";
import ReadingRuleConfig from "@/components/pipelines/anchors/ReadingRuleConfig";

const KIND_ICON = { document: FileText, structured: TableIcon, semistructured: Braces };
/** The paradigm name the mapping store uses, which differs from the pipeline's `kind` for documents. */
const MAPPING_PARADIGM: Record<PipelineKind, string> = {
  document: "unstructured", structured: "structured", semistructured: "semistructured",
};

function PipelineBuilder() {
  const { id } = useParams<{ id: string }>();
  const params = useSearchParams();
  const router = useRouter();
  const isNew = String(id) === "new";

  const [kind, setKind] = useState<PipelineKind>((params.get("kind") as PipelineKind) || "document");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [anchor, setAnchor] = useState<Record<string, any>>({});
  // the anchor stage exactly as loaded — written back key-for-key so an edit cannot re-key it
  const [anchorStage, setAnchorStage] = useState<PipelineStage | null>(null);
  const [steps, setSteps] = useState<PipelineStage[]>([]);
  const [templates, setTemplates] = useState<any[]>([]);
  const [mappings, setMappings] = useState<{ id: string; name: string }[]>([]);
  const [loading, setLoading] = useState(!isNew);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");
  const [dirty, setDirty] = useState(false);
  const [observedAttrs, setObservedAttrs] = useState<Record<string, { name: string }[]>>({});
  const [observedSubtypes, setObservedSubtypes] = useState<string[]>([]);

  /* ─────────────────────────────────────────────────────────────────── load */

  useEffect(() => {
    if (isNew) return;
    (async () => {
      setLoading(true); setErr("");
      try {
        const p: any = await contextV2Api.getPipeline(String(id));
        const k = (p.kind as PipelineKind) || "document";
        setKind(k); setName(p.name || ""); setDescription(p.description || "");
        const { anchor: a, anchorStage: orig, steps: s } = splitStages(p.stages || [], k);
        setAnchor(a); setAnchorStage(orig); setSteps(s);
      } catch (e: any) {
        setErr(e?.response?.data?.detail || e?.message || "Could not load this pipeline");
      } finally { setLoading(false); }
    })();
  }, [id, isNew]);

  useEffect(() => {
    contextV2Api.observedNodeAttributes()
      .then((res: any) => {
        const by: Record<string, { name: string }[]> = {};
        const subs = new Set<string>();
        (res?.items || res || []).forEach((it: any) => {
          if (it?.node_type) by[it.node_type] = it.attributes || [];
          (it?.subtypes || []).forEach((sx: any) => sx?.name && subs.add(sx.name));
        });
        setObservedAttrs(by); setObservedSubtypes(Array.from(subs).sort());
      })
      .catch(() => { /* a picker hint must never break the builder */ });
  }, []);

  useEffect(() => {
    contextV2Api.getExtractionTemplates()
      .then((r: any) => setTemplates(Array.isArray(r) ? r : (r?.templates || r?.items || [])))
      .catch(() => setTemplates([]));
  }, []);

  useEffect(() => {
    contextV2Api.listSchemaMappings(MAPPING_PARADIGM[kind] as any, true)
      .then((r: any) => setMappings((r?.items || []).map((m: any) => ({ id: m.id, name: m.name }))))
      .catch(() => setMappings([]));
  }, [kind]);

  /* ────────────────────────────────────────────────────────────── mutations */

  const patchAnchor = (patch: Record<string, any>) => { setDirty(true); setAnchor((a) => ({ ...a, ...patch })); };
  const addStep = (type: string) => {
    setDirty(true);
    setSteps((s) => [...s, { ...newStage(type, s.length), source: s.length ? "previous" : "anchor" }]);
  };
  const removeStep = (i: number) => { setDirty(true); setSteps((s) => s.filter((_, j) => j !== i)); };
  const moveStep = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= steps.length) return;
    setDirty(true);
    setSteps((s) => { const n = s.slice(); [n[i], n[j]] = [n[j], n[i]]; return n; });
  };
  const changeStep = (i: number, key: string, value: any) => {
    setDirty(true);
    setSteps((s) => s.map((x, j) => (j === i ? { ...x, [key]: value } : x)));
  };
  const configStep = (i: number, key: string, value: any) => {
    setDirty(true);
    setSteps((s) => s.map((x, j) => (j === i ? { ...x, config: { ...x.config, [key]: value } } : x)));
  };

  /* ────────────────────────────────────────────────────────── derived state */

  const meta = KIND_META[kind];
  const Icon = KIND_ICON[kind];

  /** Node types a step can bind to: emitted by an earlier step, or already present in the graph. */
  const inputVocab = useCallback((index: number): VocabNode[] => {
    const emitted = emittedVocab(steps, index, templates);
    const observed = Object.entries(observedAttrs).map(([node_type, attrs]) => ({
      node_type, attrs: (attrs || []).map((a: any) => a.name).filter(Boolean),
    }));
    return contextV2Api.stageInputVocab({ emitted, fallback: observed }) as VocabNode[];
  }, [steps, templates, observedAttrs]);

  const nodeTypeVocab = useMemo(() => Array.from(new Set([
    ...inputVocab(steps.length).map((v) => v.node_type),
    ...Object.keys(observedAttrs || {}),
    ...observedSubtypes,
  ])).filter(Boolean).sort(), [inputVocab, steps.length, observedAttrs, observedSubtypes]);

  const edgeVocab = useMemo(() => Array.from(new Set(
    templates.flatMap((t: any) => ((t?.emit?.edges) || []).map((e: any) => e.type || e.rel)),
  )).filter(Boolean).sort() as string[], [templates]);

  /** What a step can read: the anchor's output, or anything an earlier step produced. */
  const sourceOptions = useCallback((index: number) => {
    const opts = [{ value: "anchor", label: `the ${meta.unit}s from step 1` }];
    steps.slice(0, index).forEach((s, i) =>
      opts.push({ value: `stage:${s.id}`, label: `what step ${i + 2} produced — ${stageName(s.type)}` }));
    if (index > 0) opts.push({ value: "previous", label: "the previous step's output" });
    return opts;
  }, [steps, meta.unit]);

  /** A step's declared emit, read from the extractor template rather than from its prompt wording. */
  const producesFor = useCallback((type?: string): StageOutput => {
    const t = templates.find((x: any) => x.name === type || x.id === type);
    const nodes = ((t?.emit?.nodes || []) as any[]).filter((n) => n?.type).map((n) => ({
      type: String(n.type).startsWith("$") ? "(per record)" : String(n.type),
      attrs: (n.attributes || []).map(String),
    }));
    const edges = Array.from(new Set(((t?.emit?.edges || []) as any[])
      .map((e) => String(e?.type || "")).filter(Boolean)));
    return { nodes, edges };
  }, [templates]);

  /** builtin:doc.* / builtin:extract.* / builtin:struct.* … grouped so a long list stays navigable.
   *  Only extractors the engine can run: answer and memory prompts are edited, not scheduled. */
  const offered = useMemo(() => templates.filter((t: any) =>
    t.executable !== false
    // table-bound extractors read Tables and columns — only a structured pipeline has them
    && (kind === "structured" || t.category !== "structured")), [templates, kind]);
  const groupedExtractors = useMemo(() => {
    const out: Record<string, string[]> = {};
    offered.map((t: any) => t.name).filter(Boolean)
      .forEach((n: string) => {
        const m = /^builtin:([a-z]+)\./.exec(n);
        const group = m ? `Built-in · ${m[1]}` : "Custom";
        (out[group] = out[group] || []).push(n);
      });
    return Object.fromEntries(Object.entries(out).sort(([a], [b]) => a.localeCompare(b)));
  }, [offered]);

  const stageChoices = useMemo(() =>
    Array.from(new Set(offered.map((t: any) => t.name).filter(Boolean))), [offered]);

  /* ───────────────────────────────────────────────────────────────── save */

  const save = async (thenRun = false) => {
    if (!name.trim()) { setErr("Please name the pipeline."); return; }
    setBusy(true); setErr(""); setOk("");
    try {
      const stages = buildStages(kind, anchor, steps, anchorStage);
      let pid = String(id);
      if (isNew) {
        const created: any = await contextV2Api.createPipeline({ name, description, kind, stages });
        pid = created?.id;
        router.replace(`/dashboard/pipelines/${pid}` as any);
      } else {
        await contextV2Api.updatePipeline(pid, { name, description, kind, stages });
      }
      setDirty(false);
      setOk(`Saved — 1 reading rule and ${steps.length} step${steps.length === 1 ? "" : "s"}.`);
      if (thenRun) router.push("/dashboard/ingest" as any);
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || "Save failed");
    } finally { setBusy(false); }
  };

  /* ────────────────────────────────────────────────────────────────── view */

  if (loading) {
    return <div className="p-6 flex items-center gap-2 text-sm text-stone-500">
      <Loader2 size={16} className="animate-spin" /> loading pipeline…
    </div>;
  }

  const anchorKind = anchorKindOf(anchorStage, kind);

  return (
    <div className="p-6 max-w-[1100px] mx-auto">
      <PageHeader
        title={name || (isNew ? "New pipeline" : "Pipeline")}
        icon={GitBranch}
        description={`${meta.label} — ${meta.sub}.`}
        actions={
          <div className="flex items-center gap-2">
            <Link href="/dashboard/pipelines">
              <Button variant="ghost" size="sm" icon={ArrowLeft}>All pipelines</Button>
            </Link>
            <Button variant="secondary" size="sm" busy={busy} onClick={() => save(true)}>Save &amp; ingest</Button>
            <Button variant="primary" size="sm" icon={Save} busy={busy} onClick={() => save(false)}
              disabled={!dirty && !isNew}>
              {dirty || isNew ? "Save pipeline" : "Saved"}
            </Button>
          </div>
        }
      />

      {err && (
        <div className="mb-4 flex items-start gap-2 px-3.5 py-3 rounded-xl border border-red-200 bg-red-50 text-sm text-red-800">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" /><div>{err}</div>
        </div>
      )}
      {ok && (
        <div className="mb-4 flex items-start gap-2 px-3.5 py-3 rounded-xl border border-lime-300 bg-lime-50 text-sm text-lime-800">
          <CheckCircle2 size={16} className="mt-0.5 shrink-0" /><div>{ok}</div>
        </div>
      )}

      <div className="grid md:grid-cols-[1fr_auto] gap-3 mb-5 items-start">
        <div className="grid gap-2">
          <input value={name} onChange={(e) => { setName(e.target.value); setDirty(true); }}
            placeholder="Pipeline name" aria-label="Pipeline name"
            className="px-3 py-2 rounded-lg border border-stone-200 bg-white text-sm
                       focus:outline-none focus:ring-2 focus:ring-primary/25 focus:border-primary/40" />
          <input value={description} onChange={(e) => { setDescription(e.target.value); setDirty(true); }}
            placeholder="What this pipeline is for (optional)" aria-label="Description"
            className="px-3 py-2 rounded-lg border border-stone-200 bg-white text-[13px] text-stone-600
                       focus:outline-none focus:ring-2 focus:ring-primary/25 focus:border-primary/40" />
        </div>
        {isNew ? (
          <div className="flex gap-1">
            {(Object.keys(KIND_META) as PipelineKind[]).map((k) => {
              const K = KIND_ICON[k];
              return (
                <button key={k} type="button" onClick={() => { setKind(k); setAnchor({}); setDirty(true); }}
                  className={`flex items-center gap-1.5 text-[12.5px] px-3 py-2 rounded-lg border transition-colors
                              ${kind === k ? "border-primary/50 bg-primary/10 text-primary font-semibold"
                                           : "bg-white border-stone-200 text-stone-600 hover:border-stone-300"}`}>
                  <K size={14} />{KIND_META[k].label}
                </button>
              );
            })}
          </div>
        ) : (
          <span className="inline-flex items-center gap-1.5 text-[12.5px] text-stone-600 px-3 py-2
                           rounded-lg border border-stone-200 bg-white">
            <Icon size={14} className="text-stone-400" />{meta.label}
          </span>
        )}
      </div>

      {/* ── STAGE 1 — the reading rule: a mapping and two options, the same for every kind ── */}
      <div className="flex gap-3 mb-4">
        <div className="flex flex-col items-center pt-5">
          <div className="w-7 h-7 rounded-full bg-primary text-primary-foreground flex items-center
                          justify-center text-[11px] font-bold z-10 shadow-sm">1</div>
          {!!steps.length && <div className="w-0.5 bg-stone-200 flex-1 my-1" />}
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="text-[13.5px] font-semibold text-stone-800 mb-0.5">{KIND_META[anchorKind].anchorTitle}</h2>
          <p className="text-[12px] text-stone-500 mb-3">{KIND_META[anchorKind].anchorHint}</p>
          {anchorKind !== kind && (
            <div className="mb-3 px-3 py-2.5 rounded-lg border border-amber-300 bg-amber-50 text-[12px] text-amber-900">
              <b className="font-semibold">This pipeline is labelled {meta.label} but its reading rule is
              {" "}{KIND_META[anchorKind].label}.</b>{" "}
              The rule below is the one it actually runs; the label is what it was saved under.
            </div>
          )}
          <ReadingRuleConfig kind={anchorKind} config={anchor} onChange={patchAnchor} mappings={mappings} />
        </div>
      </div>

      {/* ── STAGE 2..N — identical for every kind ───────────────────────────────────────────── */}
      <div className="space-y-3">
        {steps.map((s, i) => (
          <PipelineStageCard
            key={s.id} stage={s} index={i} total={steps.length} number={i + 2}
            produces={producesFor(s.type)} sourceOptions={sourceOptions(i)}
            onRemove={() => removeStep(i)} onMove={(d) => moveStep(i, d)}
            onChange={(k, v) => changeStep(i, k, v)} onConfig={(k, v) => configStep(i, k, v)}>
            <StageInputBinding
              stage={s} vocab={inputVocab(i)} templates={templates}
              nodeTypeVocab={nodeTypeVocab} edgeVocab={edgeVocab}
              onConfig={(k, v) => configStep(i, k, v)} />
          </PipelineStageCard>
        ))}
      </div>

      <div className="mt-4 border border-dashed border-stone-300 rounded-xl bg-white p-4">
        <span className="block text-[10px] font-semibold uppercase tracking-wider text-stone-500 mb-2">
          Add a step
        </span>
        {/* A dropdown, not a grid of buttons. There are ~60 registered extractors; listing them all
            as buttons buries the pipeline itself under its own menu. */}
        <div className="flex items-center gap-2">
          <select value="" aria-label="Add a step"
            onChange={(e) => { if (e.target.value) addStep(e.target.value); e.currentTarget.value = ""; }}
            className="flex-1 max-w-md px-3 py-2 rounded-lg border border-stone-200 bg-white text-sm
                       focus:outline-none focus:ring-2 focus:ring-primary/25 focus:border-primary/40">
            <option value="">Choose an extractor…</option>
            {Object.entries(groupedExtractors).map(([group, names]) => (
              <optgroup key={group} label={group}>
                {names.map((t) => <option key={t} value={t}>{stageName(t)}</option>)}
              </optgroup>
            ))}
          </select>
        </div>
      </div>
    </div>
  );
}

/** `useSearchParams` (the `?kind=` on a new pipeline) needs a Suspense boundary to render. */
export default function PipelineBuilderPage() {
  return (
    <Suspense fallback={
      <div className="p-6 flex items-center gap-2 text-sm text-stone-500">loading pipeline…</div>
    }>
      <PipelineBuilder />
    </Suspense>
  );
}
