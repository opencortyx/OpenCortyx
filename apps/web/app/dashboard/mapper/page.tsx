"use client";

/**
 * Mapper — the one place a file's meaning is checked and corrected.
 *
 * Pick a file (a table or a JSON file); see the kinds of thing detection found in it, each a graph
 * node type with its identifier, its name field and the attributes its nodes carry; see how they
 * connect; fix anything; save. Nothing has to be set up before an upload: detection reads the file
 * first, and what a person corrects here is kept for every file of that shape, then and later.
 *
 * Underneath, each kind is an entity of a detected (or hand-made) mapping's `knowledge`, and a
 * relationship lives in the mapping of the kind that holds the reference. A rename is made once
 * here and reaches every mapping that names the kind; a relationship named after its target follows
 * the new name. A document has no fields to map: it shows how its layout was read (and where long
 * paragraphs are split), and what extractors found in it. The kinds extractors found have nothing to
 * map: only whether each is used ("Found by extractors"). The rule editor stays reachable with the
 * full UI (lib/simple.ts).
 */

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, FileText, Loader2, RefreshCw, Search, Workflow } from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import GraphPreview, { type PreviewLink, type PreviewNode } from "@/components/mapper/page/GraphPreview";
import KindCard from "@/components/mapper/page/KindCard";
import Links, { type LinkRow } from "@/components/mapper/page/Links";
import DocumentTypes from "@/components/mapper/page/DocumentTypes";
import DocumentView from "@/components/mapper/page/DocumentView";
import FileExtractors from "@/components/mapper/page/FileExtractors";
import {
  contextV2Api, type KnowledgeEntity, type KnowledgeRelationship, type MappingAsset, type MappingKnowledge,
  type MappingWithKnowledge, type ShapeSource,
} from "@/lib/api/contextV2";
import { measure, normPath, recordLabel, relationName, savedEntity } from "@/lib/mapper/knowledge";
import { FULL_UI } from "@/lib/simple";

type Mapping = MappingWithKnowledge & { knowledge: MappingKnowledge };
/** One kind as the page edits it: entity `i` of mapping `m`, saved under the name `orig`. */
interface Kind { m: Mapping; i: number; e: KnowledgeEntity; orig: string }

const DOCUMENTS = "documents";
/** The most files measured at once (the server's limit). */
const SHAPES_AT_ONCE = 20;
const FILES_BEFORE_SEARCH = 8;

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x));
const errorText = (e: any): string => e?.response?.data?.detail || e?.message || "Something went wrong";

export default function MapperPage() {
  const [assets, setAssets] = useState<MappingAsset[] | null>(null);
  const [saved, setSaved] = useState<Mapping[] | null>(null);
  const [drafts, setDrafts] = useState<Record<string, MappingKnowledge>>({});      // mapping id -> edited copy
  const [renames, setRenames] = useState<Record<string, string>>({});              // saved name -> name typed
  const [selected, setSelected] = useState("");
  const [shapes, setShapes] = useState<Record<string, ShapeSource[]>>({});
  const asked = useRef(new Set<string>());
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [issues, setIssues] = useState<string[]>([]);
  const [err, setErr] = useState("");

  const load = useCallback(async (quiet = false) => {
    try {
      const [a, ms] = await Promise.all([contextV2Api.mappingAssets(true), contextV2Api.listMappingKnowledge()]);
      setAssets(a);
      setSaved(ms.filter((m) => m.knowledge?.entities?.length) as Mapping[]);
      setErr("");
      // a file measured while it was still being read came back empty: measure it again
      setShapes((cur) => {
        const empty = Object.keys(cur).filter((id) => !cur[id].length);
        empty.forEach((id) => asked.current.delete(id));
        return empty.length ? Object.fromEntries(Object.entries(cur).filter(([, v]) => v.length)) : cur;
      });
    } catch (e) {
      if (!quiet) setErr(errorText(e));
      setAssets((x) => x ?? []); setSaved((x) => x ?? []);
    }
  }, []);

  const knowledgeOf = useCallback((m: Mapping) => drafts[m.id] ?? m.knowledge, [drafts]);
  const nameOf = useCallback((t: string) => (t in renames ? renames[t] : t), [renames]);
  const rn = useCallback((t: string) => renames[t]?.trim() || t, [renames]);

  const dirty = useMemo(() => Object.entries(drafts).some(([id, k]) => !same(k, saved?.find((m) => m.id === id)?.knowledge))
    || Object.entries(renames).some(([a, b]) => b.trim() && b.trim() !== a), [drafts, renames, saved]);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  useEffect(() => {
    load();
    // detection maps a new file a little after its upload: show it without a Refresh, unless mid-edit
    const every = setInterval(() => {
      if (document.visibilityState === "visible" && !dirtyRef.current) load(true);
    }, 20_000);
    return () => clearInterval(every);
  }, [load]);

  // ── every kind, and the ones the chosen file holds ─────────────────────────────────────────────
  const all: Kind[] = useMemo(() => (saved || []).flatMap((m) =>
    knowledgeOf(m).entities.map((e, i) => ({ m, i, e, orig: m.knowledge.entities[i]?.type ?? e.type }))), [saved, knowledgeOf]);
  const kindsIn = useCallback((assetId: string) => all.filter((k) => (k.e.assets || []).some((a) => a.id === assetId)), [all]);

  useEffect(() => {
    if (selected || !assets || !saved) return;
    const wanted = new URLSearchParams(window.location.search).get("asset") || "";
    const first = assets.find((a) => kindsIn(a.id).length) || assets[0];
    setSelected(wanted === DOCUMENTS || assets.some((a) => a.id === wanted) ? wanted : first?.id || DOCUMENTS);
  }, [assets, saved, selected, kindsIn]);

  const choose = (id: string) => {
    setSelected(id); setMsg(null); setIssues([]);
    window.history.replaceState(null, "", `?asset=${encodeURIComponent(id)}`);
  };

  const here = useMemo(() => (selected && selected !== DOCUMENTS ? kindsIn(selected) : []), [selected, kindsIn]);
  const origsHere = useMemo(() => new Set(here.map((k) => k.orig)), [here]);

  // Measure this file first, then the other files its kinds read (as many as one request takes).
  const measured = useMemo(() => new Set([selected, ...here.flatMap((k) => (k.e.assets || []).map((a) => a.id))]
    .filter((a) => a && a !== DOCUMENTS).slice(0, SHAPES_AT_ONCE)), [selected, here]);
  useEffect(() => {
    const missing = Array.from(measured).filter((a) => !(a in shapes) && !asked.current.has(a));
    if (!missing.length) return;
    missing.forEach((a) => asked.current.add(a));
    contextV2Api.mappingShape(missing)
      .then((srcs) => setShapes((cur) => ({ ...cur, ...Object.fromEntries(missing.map((a) => [a, srcs.filter((s) => s.asset_id === a)])) })))
      .catch(() => setShapes((cur) => ({ ...cur, ...Object.fromEntries(missing.map((a) => [a, []])) })));
  }, [measured, shapes]);
  const measureHere = (e: KnowledgeEntity) => measure({ ...e, assets: (e.assets || []).filter((a) => measured.has(a.id)) }, shapes);

  // ── editing ─────────────────────────────────────────────────────────────────────────────────────
  const editKnowledge = (m: Mapping, fn: (k: MappingKnowledge) => void) => {
    setDrafts((d) => { const k = clone(d[m.id] ?? m.knowledge); fn(k); return { ...d, [m.id]: k }; });
    setMsg(null);
  };
  const editKind = (k: Kind, patch: Partial<KnowledgeEntity>) => editKnowledge(k.m, (kn) => {
    const e = kn.entities[k.i];
    // a person working on a kind detection was unsure of is using it
    kn.entities[k.i] = { ...e, ...(e.status === "suggested" && !("status" in patch) ? { status: "accepted" } : {}), ...patch };
  });
  const rename = (k: Kind, name: string) => {
    setRenames((r) => ({ ...r, [k.orig]: name }));
    if (k.e.status === "suggested") editKind(k, {});
    setMsg(null);
  };

  // relationships of the kinds here, wherever they are stored
  const rels = useMemo(() => (saved || []).flatMap((m) => knowledgeOf(m).relationships.map((r, i) => ({ m, i, r })))
    .filter(({ r }) => origsHere.has(r.from_type) || origsHere.has(r.to_type)), [saved, knowledgeOf, origsHere]);
  const shownName = (r: KnowledgeRelationship) => (r.name === relationName(r.to_type) ? relationName(rn(r.to_type)) : r.name);
  const linkRows: LinkRow[] = rels.map(({ m, i, r }) => ({
    id: `${m.id}:${i}`, from: rn(r.from_type), to: rn(r.to_type), name: shownName(r),
    auto: r.name === relationName(r.to_type), on: r.status !== "rejected", added: i >= m.knowledge.relationships.length,
    why: `Through ${r.from_field}` + (r.origin === "inferred" && r.coverage
      ? `: ${Math.round(r.coverage * 100)}% of its values are ${rn(r.to_type)} identifiers` : "")
      + (i >= m.knowledge.relationships.length ? " (added by you)" : ""),
  }));
  const relAt = (id: string) => rels.find(({ m, i }) => `${m.id}:${i}` === id);
  const targets = useMemo(() => Array.from(new Map(all.filter((k) => k.e.keys[0] && k.e.status !== "rejected")
    .map((k) => [k.orig, { value: k.orig, name: rn(k.orig) }])).values()), [all, rn]);

  // ── the preview ────────────────────────────────────────────────────────────────────────────────
  const preview = useMemo(() => {
    const nodes: PreviewNode[] = here.map((k) => ({
      name: rn(k.orig), count: measureHere(k.e).records || k.e.rows || 0, event: !k.e.keys[0],
      inGraph: k.e.materialize && k.e.status !== "rejected", here: true }));
    const links: PreviewLink[] = [];
    for (const { r } of rels) {
      if (r.status === "rejected") continue;
      for (const t of [r.from_type, r.to_type]) {
        if (!nodes.some((n) => n.name === rn(t))) nodes.push({ name: rn(t), count: 0, event: false, inGraph: true, here: false });
      }
      links.push({ from: rn(r.from_type), to: rn(r.to_type), name: shownName(r) });
    }
    return { nodes, links };
  }, [here, rels, rn, shapes]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── saving ─────────────────────────────────────────────────────────────────────────────────────
  const changes = useMemo(() => {
    let n = Object.entries(renames).filter(([a, b]) => b.trim() && b.trim() !== a).length;
    for (const [id, k] of Object.entries(drafts)) {
      const before = saved?.find((m) => m.id === id)?.knowledge;
      if (!before) continue;
      n += k.entities.filter((e, i) => !same(e, before.entities[i])).length;
      n += k.relationships.filter((r, i) => !same(r, before.relationships[i])).length;
    }
    return n;
  }, [drafts, renames, saved]);

  const save = async () => {
    if (!saved) return;
    setBusy("save"); setMsg(null); setIssues([]);
    // a link into a kind joins on that kind's identifier as it is now
    const keyOf = new Map(all.filter((k) => k.e.keys[0]).map((k) => [k.orig, k.e.keys[0]] as [string, string]));
    const done: Mapping[] = [];
    try {
      for (const m of saved) {
        const before = m.knowledge;
        const k = knowledgeOf(m);
        const entities = k.entities.map((e, i) => {
          const edited = !same(e, before.entities[i]);
          const full = (e.assets || []).every((a) => a.id in shapes) ? measure(e, shapes) : null;
          return { ...(edited ? savedEntity(e, full, k.relationships) : e), type: rn(e.type) };
        });
        const relationships = k.relationships.map((r) => ({
          ...r, name: shownName(r), from_type: rn(r.from_type), to_type: rn(r.to_type), to_field: keyOf.get(r.to_type) || r.to_field }));
        const next = { ...before, entities, relationships };
        if (same(next, before)) continue;
        await contextV2Api.saveMappingKnowledge(m.id, next, m.status === "draft" ? "active" : undefined);
        done.push(m);
      }
      // a renamed kind keeps its Resolution (when two records are the same thing), set on its old name
      for (const [was, now] of Object.entries(renames)) {
        if (!now.trim() || now.trim() === was) continue;
        const r = await contextV2Api.getTypeResolution(was).catch(() => null);
        if (r?.resolution?.identify_by?.length) await contextV2Api.setTypeResolution(now.trim(), r.resolution).catch(() => null);
      }
      const checks = await Promise.all(done.map((m) => contextV2Api.validateMapping(m.id).catch(() => null)));
      const found = checks.flatMap((c) => (c && !c.valid ? c.issues.map((x) => `${x.where}: ${x.text}${x.examples?.length ? ` (e.g. ${x.examples.join(", ")})` : ""}`) : []));
      setDrafts({}); setRenames({});
      await load(true);
      setIssues(found);
      setMsg(found.length ? { ok: false, text: "Saved, but the data disagrees with something:" }
        : { ok: true, text: `Saved ${changes} change${changes === 1 ? "" : "s"}. The graph is updating; files of this shape read later use them too.` });
    } catch (e) {
      setMsg({ ok: false, text: errorText(e) + (done.length ? ` (${done.length} saved before this)` : "") });
      if (done.length) { setDrafts({}); setRenames({}); await load(true); }
    } finally { setBusy(""); }
  };

  const undo = () => { setDrafts({}); setRenames({}); setMsg(null); setIssues([]); };

  const redetect = async (m: Mapping) => {
    if (!confirm(`Read "${m.name}" again and take what detection finds now? Edits made to it are replaced; earlier versions are kept.`)) return;
    setBusy(`redetect:${m.id}`); setMsg(null);
    try {
      await contextV2Api.redetectMapping(m.id);
      setDrafts((d) => { const { [m.id]: _gone, ...rest } = d; return rest; });
      setShapes({}); asked.current.clear();
      await load(true);
      setMsg({ ok: true, text: "Detected again from the data." });
    } catch (e) { setMsg({ ok: false, text: errorText(e) }); } finally { setBusy(""); }
  };

  // ── the page ───────────────────────────────────────────────────────────────────────────────────
  const fileTypes = useMemo(() => new Set(all.flatMap((k) => [k.orig, rn(k.orig)])), [all, rn]);
  const needle = q.trim().toLowerCase();
  const files = (assets || []).filter((a) => !needle || a.name.toLowerCase().includes(needle)
    || kindsIn(a.id).some((k) => rn(k.orig).toLowerCase().includes(needle)));
  const current = assets?.find((a) => a.id === selected);
  const isDoc = (a?: MappingAsset) => a?.kind === "unstructured";
  const names = here.map((k) => rn(k.orig).toLowerCase());

  if (assets === null || saved === null) {
    return <div className="flex justify-center py-24"><Loader2 className="animate-spin text-stone-400" /></div>;
  }

  return (
    <div className="mx-auto max-w-[1280px] p-4 sm:p-6">
      <PageHeader title="Mapper" icon={Workflow}
        description="Pick a file. Check what was found in it: the kinds of things, their names and attributes, and how they connect, or for a document how it was read. Fix anything, then save."
        actions={<Button variant="secondary" size="sm" icon={RefreshCw} onClick={() => load()} disabled={dirty}
                         title={dirty ? "Save or undo your changes first" : "Look for newly read files"}>Refresh</Button>} />
      {err && <p className="mb-4 rounded-xl border border-red-200 bg-red-50 px-3.5 py-3 text-sm text-red-800">{err}</p>}

      <div className="grid gap-5 md:grid-cols-[250px_minmax(0,1fr)]">
        <aside aria-label="Files" className="flex flex-col gap-2">
          <h2 className="text-[11.5px] font-semibold uppercase tracking-wider text-stone-500">Files</h2>
          {(assets.length > FILES_BEFORE_SEARCH) && (
            <div className="relative">
              <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-stone-400" />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a file or a kind" aria-label="Find a file or a kind"
                className="w-full rounded-lg border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 py-1.5 pl-8 pr-2 text-sm" />
            </div>
          )}
          <div className="flex max-h-[70vh] flex-col gap-1.5 overflow-y-auto pr-0.5">
            {files.map((a) => {
              const ks = kindsIn(a.id);
              const unsure = ks.some((k) => k.m.status === "draft");
              return (
                <button key={a.id} type="button" onClick={() => choose(a.id)} aria-pressed={selected === a.id}
                  className={`grid grid-cols-[auto_1fr] items-center gap-x-2.5 rounded-xl border px-3 py-2 text-left bg-white dark:bg-stone-900 ${
                    selected === a.id ? "border-primary ring-1 ring-primary" : "border-stone-200 dark:border-stone-800 hover:border-stone-400"}`}>
                  <span className="row-span-2 rounded-md bg-teal-50 dark:bg-teal-950 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-teal-700 dark:text-teal-300">
                    {a.kind === "structured" ? "TABLE" : a.kind === "unstructured" ? "DOC" : "JSON"}
                  </span>
                  <span className="truncate text-sm font-semibold text-stone-900 dark:text-stone-100">{a.name}</span>
                  <span className="truncate text-xs text-stone-500">
                    {isDoc(a) ? "Read by its layout" : ks.length ? ks.map((k) => rn(k.orig)).join(", ") : "Being read…"}
                    {unsure && <span className="ml-1 font-semibold text-amber-700">· check</span>}
                  </span>
                </button>
              );
            })}
            {!assets.length && (
              <p className="text-sm text-stone-500">No tables or JSON files yet. <Link href="/dashboard/add" className="text-primary hover:underline">Add data</Link>.</p>
            )}
            <button type="button" onClick={() => choose(DOCUMENTS)} aria-pressed={selected === DOCUMENTS}
              className={`mt-1 flex items-center gap-2.5 rounded-xl border px-3 py-2 text-left bg-white dark:bg-stone-900 ${
                selected === DOCUMENTS ? "border-primary ring-1 ring-primary" : "border-stone-200 dark:border-stone-800 hover:border-stone-400"}`}>
              <FileText size={15} className="text-stone-500" />
              <span className="text-sm font-semibold text-stone-900 dark:text-stone-100">Found by extractors</span>
            </button>
          </div>
          <p className="text-xs text-stone-500">New file? <Link href="/dashboard/add" className="text-primary hover:underline">Add it</Link>. It appears here once read.</p>
          {FULL_UI && <Link href="/dashboard/mapper/rules" className="text-xs text-primary hover:underline">Rule mappings</Link>}
        </aside>

        <main className="flex min-w-0 flex-col gap-4">
          {selected === DOCUMENTS ? (
            <section className="rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900 p-4">
              <h2 className="font-semibold text-stone-900 dark:text-stone-100">Found by extractors</h2>
              <p className="mb-3 text-sm text-stone-500">What extractors found in documents and in the text of records. There is nothing to map: turn off a kind you do not want.</p>
              <DocumentTypes fileTypes={fileTypes} />
            </section>
          ) : !current ? null : isDoc(current) ? (
            <DocumentView key={current.id} assetId={current.id} />
          ) : !here.length ? (
            <section className="rounded-xl border border-dashed border-stone-300 dark:border-stone-700 p-8 text-center text-sm text-stone-500">
              <Loader2 className="mx-auto mb-2 animate-spin" size={16} />
              {current.name} is being read. What it holds appears here in a few minutes.
            </section>
          ) : (<>
            <section aria-labelledby="preview-h" className="rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900">
              <div className="flex flex-wrap items-baseline gap-x-3 px-4 pt-3">
                <h2 id="preview-h" className="font-semibold text-stone-900 dark:text-stone-100">How it looks in the graph</h2>
                <p className="text-sm text-stone-500">Updates as you edit.</p>
              </div>
              <div className="overflow-x-auto px-2 pb-2"><GraphPreview nodes={preview.nodes} links={preview.links} /></div>
            </section>

            <section aria-labelledby="kinds-h" className="rounded-xl border border-stone-200 dark:border-stone-800 bg-stone-50/60 dark:bg-stone-950 p-4">
              <div className="mb-3 flex flex-wrap items-baseline gap-x-3">
                <h2 id="kinds-h" className="font-semibold text-stone-900 dark:text-stone-100">What&apos;s in {current.name}</h2>
                <p className="text-sm text-stone-500">Each kind becomes a node type. Click a name to rename it everywhere.</p>
              </div>
              <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]">
                {here.map((k) => {
                  const refs = new Set(rels.filter(({ r }) => r.from_type === k.orig).map(({ r }) => r.from_field));
                  return (
                    <KindCard key={`${k.m.id}:${k.i}`} entity={k.e} name={nameOf(k.orig)} savedName={k.orig}
                      measured={measureHere(k.e)} refs={refs} draft={k.m.status === "draft"}
                      duplicate={names.filter((n) => n === rn(k.orig).toLowerCase()).length > 1}
                      otherFiles={Math.max(0, (k.e.assets || []).length - 1)}
                      onRename={(name) => rename(k, name)} onChange={(patch) => editKind(k, patch)}
                      onFlash={(text, ok = true) => setMsg({ ok, text })} />
                  );
                })}
              </div>
              <details className="mt-3">
                <summary className="cursor-pointer text-sm text-stone-500">Advanced: where each kind is read from in the file</summary>
                <div className="mt-2 flex flex-col gap-2">
                  {here.map((k) => {
                    const m = measureHere(k.e);
                    const path = normPath(k.e.path);
                    return (
                      <div key={`${k.m.id}:${k.i}`} className="grid items-center gap-2 text-sm sm:grid-cols-[150px_1fr_auto]">
                        <span className="truncate font-medium">{rn(k.orig)}</span>
                        <select value={path} aria-label={`Where ${rn(k.orig)} is read from`}
                          onChange={(ev) => editKind(k, { path: ev.target.value, keys: [], label: "", attributes: [] })}
                          className="min-w-0 rounded-md border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-900 px-2 py-1 font-mono text-[12.5px]">
                          {!m.paths.some((s) => s.path === path) && <option value={path}>{path || "each record"}</option>}
                          {m.paths.map((s) => <option key={s.path} value={s.path}>{s.path || "(the record)"} : {recordLabel(s)}</option>)}
                        </select>
                        <span className="flex items-center gap-3">
                          <button type="button" className="text-primary hover:underline disabled:opacity-40" disabled={!!busy || dirty}
                            title={dirty ? "Save or undo your changes first" : "Read the data again and take what detection finds now"}
                            onClick={() => redetect(k.m)}>{busy === `redetect:${k.m.id}` ? "Reading…" : "Detect again"}</button>
                          {FULL_UI && <Link href={`/dashboard/mapper/${k.m.id}`} className="text-primary hover:underline">Rules</Link>}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </details>
            </section>

            <section aria-labelledby="links-h" className="rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900 p-4">
              <div className="mb-3 flex flex-wrap items-baseline gap-x-3">
                <h2 id="links-h" className="font-semibold text-stone-900 dark:text-stone-100">How they connect</h2>
                <p className="text-sm text-stone-500">Each line becomes a relationship. Names marked auto follow a rename.</p>
              </div>
              <Links rows={linkRows}
                sources={here.filter((k) => k.e.status !== "rejected").map((k) => ({ value: k.orig, name: rn(k.orig) }))}
                targets={targets}
                fieldsOf={(orig) => { const k = here.find((x) => x.orig === orig); return k ? measureHere(k.e).fields.map((f) => f.name).filter((f) => f !== k.e.keys[0]) : []; }}
                onName={(id, name) => { const x = relAt(id); if (!x) return;
                  editKnowledge(x.m, (kn) => { kn.relationships[x.i].name = name === relationName(rn(x.r.to_type)) ? relationName(x.r.to_type) : name; }); }}
                onToggle={(id, on) => { const x = relAt(id); if (x) editKnowledge(x.m, (kn) => { kn.relationships[x.i].status = on ? "accepted" : "rejected"; }); }}
                onRemove={(id) => { const x = relAt(id); if (x) editKnowledge(x.m, (kn) => { kn.relationships.splice(x.i, 1); }); }}
                onAdd={(from, field, to) => {
                  const k = here.find((x) => x.orig === from);
                  const target = all.find((x) => x.orig === to && x.e.keys[0]);
                  if (!k || !target) return;
                  editKnowledge(k.m, (kn) => { kn.relationships.push({ name: relationName(to), from_type: from, from_field: field, to_type: to,
                    to_field: target.e.keys[0], origin: "declared", coverage: 0, status: "accepted" }); });
                }} />
            </section>

            <section aria-labelledby="found-h" className="rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900 p-4">
              <div className="mb-3 flex flex-wrap items-baseline gap-x-3">
                <h2 id="found-h" className="font-semibold text-stone-900 dark:text-stone-100">What extractors found in it</h2>
                <p className="text-sm text-stone-500">Things named in the text of its records, such as people or products.</p>
              </div>
              <FileExtractors key={current.id} assetId={current.id} fileName={current.name} kind={current.kind} />
            </section>
          </>)}

          {(dirty || msg) && (
            <div role="status" className="sticky bottom-3 z-10 flex flex-wrap items-center gap-2.5 rounded-xl bg-stone-900 px-4 py-2.5 text-stone-50 shadow-lg dark:bg-stone-100 dark:text-stone-900">
              <span className="flex flex-1 basis-60 items-center gap-1.5 text-sm">
                {msg ? (<>{msg.ok ? <CheckCircle2 size={15} className="text-emerald-400 dark:text-emerald-600" /> : <AlertTriangle size={15} className="text-amber-400 dark:text-amber-600" />}{msg.text}</>)
                  : `${changes || "Unsaved"} change${changes === 1 ? "" : "s"}. Saving updates the graph, and files of this shape read later use them too.`}
              </span>
              {dirty && <>
                <button type="button" onClick={undo} disabled={!!busy}
                  className="rounded-lg border border-stone-500 px-3 py-1.5 text-sm font-semibold disabled:opacity-40">Undo changes</button>
                <button type="button" onClick={save} disabled={!!busy}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-1.5 text-sm font-semibold text-white disabled:opacity-40">
                  {busy === "save" && <Loader2 size={14} className="animate-spin" />} Save
                </button>
              </>}
              {issues.length > 0 && (
                <ul className="basis-full list-disc space-y-0.5 pl-5 text-sm text-amber-200 dark:text-amber-800">
                  {issues.slice(0, 8).map((x, k) => <li key={k}>{x}</li>)}
                </ul>
              )}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
