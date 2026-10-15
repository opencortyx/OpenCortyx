"use client";

import { useEffect, useState } from "react";
import { Anchor, Plus, Trash2, Wand2, Check } from "lucide-react";
import { contextV2Api } from "@/lib/api/contextV2";
import type { AnchorRule } from "./page";

type NodeHit = { id: string; label: string; type: string };

function NodePicker({
  nodeTypes, value, label, onPick,
}: {
  nodeTypes: string[]; value?: string; label?: string;
  // The picked node's TYPE travels with it: "which node" and "what kind of node" are different
  // questions, and the summary below cannot honestly name the target without the second answer.
  onPick: (id: string, label: string, type: string) => void;
}) {
  const [type, setType] = useState("");
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<NodeHit[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!type) { setHits([]); return; }
    let live = true;
    // Debounced: this fires per keystroke and the search hits the DB.
    const t = setTimeout(async () => {
      setBusy(true);
      try {
        const r = await contextV2Api.searchNodes(q, type, 20);
        if (live) setHits((r?.results || []) as NodeHit[]);
      } catch { if (live) setHits([]); } finally { if (live) setBusy(false); }
    }, 250);
    return () => { live = false; clearTimeout(t); };
  }, [q, type]);

  if (value) {
    return (
      <span className="inline-flex items-center gap-1.5 text-[11px] bg-sky-50 text-sky-700 border border-sky-200 rounded px-2 py-1">
        <Check className="w-3 h-3" />
        {label || value.slice(0, 8)}
        <button type="button" onClick={() => onPick("", "", "")} className="ml-1 text-sky-400 hover:text-sky-700">×</button>
      </span>
    );
  }
  if (!nodeTypes.length) {
    return (
      <input
        defaultValue={value || ""}
        onBlur={(e) => onPick(e.target.value.trim(), "", "")}
        placeholder="node id (type list unavailable)"
        className="flex-1 min-w-[180px] font-mono text-[11px] border border-stone-200 rounded px-2 py-1"
      />
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 flex-wrap">
      <select value={type} onChange={(e) => { setType(e.target.value); setQ(""); }}
        className="text-[11px] border border-stone-200 rounded px-1.5 py-1">
        <option value="">Node type…</option>
        {nodeTypes.map((t) => <option key={t} value={t}>{t}</option>)}
      </select>
      {type && (
        <span className="relative">
          <input value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={busy ? "Searching…" : `Find a ${type}`}
            className="w-44 text-[11px] border border-stone-200 rounded px-2 py-1" />
          {!!hits.length && (
            <span className="absolute left-0 top-full mt-0.5 z-20 w-56 max-h-44 overflow-auto bg-white border border-stone-200 rounded shadow-sm block">
              {hits.map((h) => (
                <button key={h.id} type="button" onClick={() => onPick(h.id, h.label, h.type || type)}
                  className="block w-full text-left text-[11px] px-2 py-1 hover:bg-stone-50 truncate">
                  {h.label || <span className="text-stone-400">(unlabelled)</span>}
                </button>
              ))}
            </span>
          )}
          {type && !busy && !hits.length && (
            <span className="absolute left-0 top-full mt-0.5 text-[11px] text-stone-400">No {type} nodes match</span>
          )}
        </span>
      )}
    </span>
  );
}


const HAS_CAPTURE = /\{[A-Za-z_][A-Za-z0-9_]*\}/;

// The word the user chose inside `{...}`, echoed back so the rule reads in THEIR vocabulary
// ("the person named in the path", not "the capture group").
function captureName(match: string): string {
  return match.match(/\{([A-Za-z_][A-Za-z0-9_]*)\}/)?.[1] || "name";
}

// One labelled line of the sentence. A fixed-width label column is what turns three stacked
// controls into "files matching X · are a Y · and belong to Z" instead of an unordered form.
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-start gap-x-2 gap-y-1">
      <span className="text-[11px] text-stone-500 w-[92px] shrink-0 pt-1.5 text-right">{label}</span>
      <div className="flex-1 min-w-[200px]">{children}</div>
    </div>
  );
}

const Pill = ({ text, sub, tone }: { text: string; sub?: string; tone: "sky" | "stone" }) => (
  <span className={`inline-flex items-baseline gap-1 rounded px-1.5 py-0.5 border ${
    tone === "sky" ? "bg-sky-50 border-sky-200 text-sky-800" : "bg-white border-stone-200 text-stone-700"}`}>
    <b className="font-semibold">{text}</b>
    {sub && <span className="text-[10px] opacity-70 truncate max-w-[140px]">{sub}</span>}
  </span>
);

function RuleResult({ rule, derived, stat }: { rule: AnchorRule; derived: boolean; stat?: any }) {
  const sample = stat?.samples?.[0];
  if (!rule.match.trim()) {
    return (
      <div className="rounded-lg px-2.5 py-2 text-[11px] bg-amber-50 text-amber-900">
        <b>No path pattern yet.</b> The grey text above is an example, not a value — type a pattern
        or this rule is discarded.
      </div>
    );
  }
  const asType = rule.as_type?.trim();
  const file = sample?.path?.split("/").pop() || "the file";
  // Fall back to the placeholder shapes when nothing has matched yet: an empty box would read as
  // "this rule is broken", when the truth is only that no sample has been listed.
  const owner = derived ? (sample?.name || `«${captureName(rule.match)}»`) : (rule.node_label || "");
  const ownerType = derived ? (rule.resolve_as || "Person") : (rule.node_type || "node");
  const targeted = derived || !!rule.node_id;
  const none = !stat ? null : stat.files === 0;

  return (
    <div className={`rounded-lg px-2.5 py-2 text-[11px] ${none ? "bg-amber-50" : "bg-stone-50"}`}>
      <div className="text-stone-500 mb-1">
        {none ? "Matches none of these files" : stat ? `Creates this for ${stat.files} file(s)` : "Creates"}
      </div>
      <div className="flex items-center flex-wrap gap-1.5 text-stone-700">
        {targeted && owner && (
          <>
            <Pill tone="stone" text={ownerType} sub={owner} />
            <span className="text-stone-400 font-mono">──{rule.edge_type}──▶</span>
          </>
        )}
        <Pill tone="sky" text={asType || "Document"} sub={file} />
        {!asType && (
          <span className="text-stone-400">(default type — set “are a” to make it something specific)</span>
        )}
      </div>
    </div>
  );
}

export default function AnchorRulesEditor({
  rules, onChange, paths = [],
}: { rules: AnchorRule[]; onChange: (r: AnchorRule[]) => void; paths?: string[] }) {
  const [open, setOpen] = useState(rules.length > 0);
  const [nodeTypes, setNodeTypes] = useState<string[]>([]);
  const [edgeTypes, setEdgeTypes] = useState<string[]>([]);

  useEffect(() => {
    if (!open || nodeTypes.length) return;
    (async () => {
      const [st, on] = await Promise.allSettled([contextV2Api.stats(), contextV2Api.ontology()]);
      const stats: any = st.status === "fulfilled" ? st.value : {};
      const declared = (on.status === "fulfilled" ? (on.value as any)?.types : []) || [];
      const present = Object.keys(stats?.nodes_by_type || {});
      const declaredNodes = declared.filter((t: any) => t.kind !== "edge").map((t: any) => t.name);
      const declaredEdges = declared.filter((t: any) => t.kind === "edge").map((t: any) => t.name);
      // Array.from rather than [...new Set()] — the build targets a pre-ES2015 lib where
      // spreading a Set does not downlevel.
      const uniq = (xs: string[]) => Array.from(new Set(xs)).filter(Boolean).sort();
      setNodeTypes(uniq([...present, ...declaredNodes]));
      setEdgeTypes(uniq([...Object.keys(stats?.edges_by_type || {}), ...declaredEdges]));
    })();
  }, [open, nodeTypes.length]);

  const [pv, setPv] = useState<any>(null);
  const [pvErr, setPvErr] = useState("");

  useEffect(() => {
    const usable = rules.filter((r) => r.match.trim());
    if (!usable.length || !paths.length) { setPv(null); setPvErr(""); return; }
    let live = true;
    const t = setTimeout(async () => {
      try {
        const r = await contextV2Api.previewAnchors(usable, paths);
        if (live) { setPv(r); setPvErr(""); }
      } catch (e: any) {
        // A 400 carries the SPECIFIC rule problem — showing it beats a silent empty preview,
        // because "no matches" and "rule 2 is malformed" need to look different while typing.
        if (live) { setPv(null); setPvErr(e?.response?.data?.detail || "Could not preview"); }
      }
    }, 400);
    return () => { live = false; clearTimeout(t); };
  }, [JSON.stringify(rules), paths.length]);

  const update = (i: number, patch: Partial<AnchorRule>) =>
    onChange(rules.map((r, n) => (n === i ? { ...r, ...patch } : r)));

  const add = () =>
    onChange([...rules, { match: "", edge_type: "RELATED_TO", resolve_as: "Person" }]);

  if (!open) {
    return (
      <button type="button" onClick={() => { setOpen(true); if (!rules.length) add(); }}
        className="w-full flex items-center gap-2 text-xs text-stone-600 hover:text-stone-900 border border-dashed border-stone-300 rounded-lg px-3 py-2">
        <Anchor className="w-3.5 h-3.5" />
        Say what these files are, and attach them to existing nodes (optional)
      </button>
    );
  }

  return (
    <div className="border border-stone-200 rounded-lg p-3">
      <div className="flex items-baseline justify-between mb-1">
        <span className="text-xs font-bold text-stone-700">Anchor rules</span>
        <span className="text-[11px] text-stone-400">Optional</span>
      </div>
      <p className="text-[11px] text-stone-500 mb-3 leading-relaxed">
        Say what the files in a folder <b>are</b>, and who they <b>belong to</b> — matched on their
        path. Either half on its own is a complete rule. Files matching no rule ingest normally with
        entity resolution.
      </p>

      {rules.map((r, i) => {
        const derived = HAS_CAPTURE.test(r.match);
        const targeted = derived || !!r.node_id;
        const stat = pv?.rules?.[i];
        return (
          <div key={i} className="border border-stone-200 rounded-lg mb-2 overflow-hidden">
            <div className="flex items-center justify-between px-2.5 py-1.5 bg-stone-50 border-b border-stone-100">
              <span className="text-[11px] font-bold text-stone-600">Rule {i + 1}</span>
              <button type="button" aria-label="Remove rule"
                onClick={() => onChange(rules.filter((_, n) => n !== i))}
                className="text-stone-400 hover:text-red-600 p-0.5">
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>

            {/* The card is laid out as the SENTENCE the rule actually is — "files matching X are a
                Y and belong to Z" — because as a bare grid of fields it reads as three unrelated
                settings and nothing says how they combine. */}
            <div className="p-2.5 space-y-2">
              <Field label="Files matching">
                <input
                  value={r.match}
                  onChange={(e) => {
                    const match = e.target.value;
                    const patch: Partial<AnchorRule> = { match };
                    if (HAS_CAPTURE.test(match) && r.node_id) {
                      patch.node_id = undefined;
                      patch.node_label = undefined;
                      patch.node_type = undefined;
                    }
                    update(i, patch);
                  }}
                  placeholder="ids/{person}/aadhaar/**"
                  className="w-full font-mono text-xs border border-stone-200 rounded px-2 py-1.5"
                />
                <p className="text-[11px] text-stone-400 mt-1">
                  <code className="text-stone-500">**</code> any folders ·{" "}
                  <code className="text-stone-500">*</code> part of one name
                </p>
              </Field>

              {/* IS A — what the files themselves are. Free text with a datalist of existing types,
                  not a select: the whole point is naming a kind the graph does not have yet
                  ("Aadhaar"), and a select over what exists could never offer it. The list still
                  makes the common case one click and keeps spellings consistent. */}
              <Field label="…are a">
                <input
                  list={`anchor-types-${i}`}
                  value={r.as_type || ""}
                  onChange={(e) => update(i, { as_type: e.target.value })}
                  placeholder="Aadhaar"
                  className="w-44 text-[11px] border border-stone-200 rounded px-2 py-1"
                />
                <datalist id={`anchor-types-${i}`}>
                  {nodeTypes.map((t) => <option key={t} value={t} />)}
                </datalist>
                <span className="text-[11px] text-stone-400 ml-2">
                  optional — blank keeps the usual document/table type
                </span>
              </Field>

              <Field label="…and belong to">
                <div className="flex flex-wrap items-center gap-2">
                  {derived ? (
                    <span className="inline-flex items-center gap-1 text-[11px] bg-violet-50 text-violet-700 border border-violet-200 rounded px-2 py-1">
                      <Wand2 className="w-3 h-3" /> the {captureName(r.match)} named in the path
                    </span>
                  ) : (
                    <NodePicker
                      nodeTypes={nodeTypes}
                      value={r.node_id}
                      label={r.node_label}
                      onPick={(id, label, type) => update(i, {
                        node_id: id || undefined, node_label: label || undefined,
                        node_type: type || undefined,
                      })}
                    />
                  )}
                  {derived && (nodeTypes.length ? (
                    <select value={r.resolve_as || ""} onChange={(e) => update(i, { resolve_as: e.target.value })}
                      className="text-[11px] border border-stone-200 rounded px-1.5 py-1">
                      <option value="">Any type</option>
                      {nodeTypes.map((t) => <option key={t} value={t}>{t}</option>)}
                    </select>
                  ) : (
                    <input
                      value={r.resolve_as || ""}
                      onChange={(e) => update(i, { resolve_as: e.target.value })}
                      placeholder="Person"
                      className="w-28 text-[11px] border border-stone-200 rounded px-2 py-1"
                    />
                  ))}
                  {/* The edge only exists once there is something to point at, so it is not offered
                      until then — an edge selector on a rule that creates no edge is a lie. */}
                  {targeted && (
                    <>
                      <span className="text-[11px] text-stone-400">linked by</span>
                      {edgeTypes.length ? (
                        <select value={r.edge_type} onChange={(e) => update(i, { edge_type: e.target.value })}
                          className="font-mono text-[11px] border border-stone-200 rounded px-1.5 py-1">
                          {/* the value may predate the ontology — keep it selectable, don't drop it */}
                          {!edgeTypes.includes(r.edge_type) && <option value={r.edge_type}>{r.edge_type}</option>}
                          {edgeTypes.map((t) => <option key={t} value={t}>{t}</option>)}
                        </select>
                      ) : (
                        <input
                          value={r.edge_type}
                          onChange={(e) => update(i, { edge_type: e.target.value.toUpperCase() })}
                          className="w-36 font-mono text-[11px] border border-stone-200 rounded px-2 py-1"
                        />
                      )}
                    </>
                  )}
                </div>
                {derived && (
                  <div className="flex flex-wrap items-center gap-3 mt-1.5">
                    <label className="flex items-center gap-1.5 text-[11px] text-stone-600">
                      <input type="checkbox" checked={!!r.create_missing}
                        onChange={(e) => update(i, { create_missing: e.target.checked })} />
                      Create the node if it doesn&apos;t exist
                    </label>
                    <span className="text-[11px] text-stone-400">
                      Names that match nothing are reported, not invented.
                    </span>
                  </div>
                )}
                {/* A rule with a type and no target is COMPLETE, not half-finished — say so, or the
                    empty node picker reads as an error the user has to clear. */}
                {/* The {capture} trick is explained HERE, where it is the thing you would reach
                    for, rather than in the pattern hint where it repeats on every rule including
                    the ones that never use it. */}
                {!targeted && (
                  <p className="text-[11px] text-stone-400 mt-1">
                    {r.as_type?.trim()
                      ? "Nobody — this rule only says what the files are. That's a complete rule."
                      : "Pick a node above, or give the files a type — a rule needs at least one."}
                    {" "}Put <code className="text-stone-500">{"{person}"}</code> in the pattern to
                    take the owner from that folder&apos;s name instead.
                  </p>
                )}
              </Field>

              <RuleResult rule={r} derived={derived} stat={stat} />
            </div>
          </div>
        );
      })}

      {(pv || pvErr) && (
        <div className="bg-stone-50 rounded-lg p-2.5 mb-2">
          {pvErr ? (
            <p className="text-[11px] text-red-700">{pvErr}</p>
          ) : (
            <>
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-[11px] text-stone-500">Match preview</span>
                <span className="text-[11px] text-stone-700 font-bold">{pv.matched} of {pv.total} files</span>
              </div>
              <div className="flex h-1.5 rounded overflow-hidden mb-1.5">
                <div className="bg-sky-500" style={{ width: `${pv.total ? (pv.matched / pv.total) * 100 : 0}%` }} />
                <div className="bg-stone-300 flex-1" />
              </div>
              <p className="text-[11px] text-stone-500 leading-relaxed">
                {pv.unmatched
                  ? `${pv.unmatched} match no rule — they ingest normally and are reported as unmatched.`
                  : "Every file matches at least one rule."}
              </p>
              {/* What the run will TYPE. The retype has no other visible symptom until after an
                  ingestion has already rewritten every matching asset's type, so the count belongs
                  next to the match count rather than in a log. */}
              {pv.rules?.some((r: any) => r.as_type && r.files > 0) && (
                <p className="text-[11px] text-stone-600 mt-1">
                  {pv.rules.filter((r: any) => r.as_type && r.files > 0)
                    .map((r: any) => `${r.files} → ${r.as_type}`).join(" · ")}
                </p>
              )}
              {pv.rules?.some((r: any) => r.distinct_names > 0) && (
                <p className="text-[11px] text-stone-400 mt-1">
                  {pv.rules.filter((r: any) => r.distinct_names > 0)
                    .map((r: any) => `${r.distinct_names} distinct from ${r.match}`).join(" · ")}
                </p>
              )}
            </>
          )}
        </div>
      )}
      <button type="button" onClick={add}
        className="w-full flex items-center justify-center gap-1.5 text-xs text-stone-600 hover:text-stone-900 border border-dashed border-stone-300 rounded-lg px-3 py-1.5">
        <Plus className="w-3.5 h-3.5" /> Add rule
      </button>
    </div>
  );
}
