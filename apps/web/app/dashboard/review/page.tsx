"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { contextV2Api, type ResolutionDecision } from "@/lib/api/contextV2";
import {
  Inbox, Check, X, RefreshCw, ArrowUpRight, Network, Lightbulb, History, ArrowRight, GitCompare, Sparkles,
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";

type Candidate = { name: string; kind: string; description: string };
// A SimHash version match (capabilities/modules/versioning.py): `id` is the OLD document, which stays
// searchable until approved; `winner` is the newer upload that would replace it.
type VersionMatch = {
  id: string; type: string; kind: string; about?: string | null; distance?: number | null;
  loser_text: string; winner?: { id: string; text: string } | null;
};
type Tab = "auto" | "conflicts" | "versions" | "ontology";

/** A resolution decision in words: what was decided and the value involved. */
function why(d: ResolutionDecision): string {
  const a = d.subject_label || "a record", b = d.target_label || "an existing record";
  const identity = Object.entries(d.evidence?.identity || {}).map(([k, v]) => `${k} = ${v}`).join(", ");
  if (d.relation === "SAME") return `${a} is the same ${d.node_type} as ${b}${identity ? ` (matched on ${identity})` : ""}.`;
  if (d.relation === "SUPPORTS") return `${d.attribute} agrees with ${b}: ${String(d.evidence?.value)}.`;
  if (d.relation === "EXTENDS") return `${d.attribute} was added to ${b}: ${String(d.evidence?.value)}.`;
  return `${d.attribute} differs: ${String(d.evidence?.value)} vs ${String(d.evidence?.other)}.`;
}
const RELATION_STYLE: Record<string, string> = {
  SAME: "bg-sky-50 text-sky-700", SUPPORTS: "bg-emerald-50 text-emerald-700",
  EXTENDS: "bg-violet-50 text-violet-700", CONTRADICTS: "bg-rose-50 text-rose-700",
};

export default function ReviewInboxPage() {
  const [tab, setTab] = useState<Tab>("auto");
  const [applied, setApplied] = useState<ResolutionDecision[]>([]);              // decided automatically
  const [attrConflicts, setAttrConflicts] = useState<ResolutionDecision[]>([]);  // awaiting a decision
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [versions, setVersions] = useState<VersionMatch[]>([]);
  // Contradiction proposals: the same record as a version match (a proposed supersession), raised
  // by the contradiction engine instead of the fingerprint. They were fetched and dropped here.
  const [conflicts, setConflicts] = useState<VersionMatch[]>([]);
  const [loading, setLoading] = useState(true);
  const [acting, setActing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const seq = useRef(0);
  const decided = useRef<Set<string>>(new Set());

  const load = useCallback(async () => {
    const mine = ++seq.current;
    setLoading(true);
    try {
      const [a, open, c, r] = await Promise.all([
        contextV2Api.resolutionDecisions({ needs_review: false, limit: 300 }).catch(() => [] as ResolutionDecision[]),
        contextV2Api.resolutionDecisions({ needs_review: true, limit: 300 }).catch(() => [] as ResolutionDecision[]),
        contextV2Api.candidates().catch(() => ({ candidates: [] })),
        contextV2Api.getResolutionProposals().catch(() => [] as any[]),
      ]);
      if (mine !== seq.current) return;               // a newer load has already answered
      setApplied(a || []);
      setAttrConflicts((open || []).filter((x) => !decided.current.has(x.id)));
      setVersions((r || []).filter((x: VersionMatch) => x.kind === "version" && !decided.current.has(x.id)));
      setConflicts((r || []).filter((x: VersionMatch) => x.kind !== "version" && !decided.current.has(x.id)));
      setCandidates((c?.candidates || []).filter((x: Candidate) => !decided.current.has(`${x.kind}:${x.name}`)));
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const decideConflict = async (d: ResolutionDecision, choice: "keep" | "use_new") => {
    setActing(d.id);
    setError(null);
    try {
      await contextV2Api.decideConflict(d.id, choice);
      decided.current.add(d.id);
      setAttrConflicts((xs) => xs.filter((x) => x.id !== d.id));
    } catch (e: any) {
      if (e?.response?.status === 404) {
        decided.current.add(d.id);
        setAttrConflicts((xs) => xs.filter((x) => x.id !== d.id));
        setError("That conflict was already decided elsewhere, so it has been removed from the queue.");
      } else {
        setError(`Could not save the decision: ${e?.response?.data?.detail || e?.message || "unknown error"}`);
      }
    }
    finally { setActing(null); }
  };

  const decideResolution = async (v: VersionMatch, approve: boolean) => {
    const drop = (xs: VersionMatch[]) => xs.filter((x) => x.id !== v.id);
    setActing(v.id);
    setError(null);
    try {
      if (approve) await contextV2Api.approveResolutionProposal(v.id);
      else await contextV2Api.rejectResolutionProposal(v.id);
      decided.current.add(v.id);
      setVersions(drop);
      setConflicts(drop);
    } catch (e: any) {
      if (e?.response?.status === 404) {
        decided.current.add(v.id);
        setVersions(drop);
        setConflicts(drop);
        setError("That item was already handled elsewhere, so it has been removed from the queue.");
      } else {
        setError(`${approve ? "Approve" : "Reject"} failed: ${e?.response?.data?.detail || e?.message || "unknown error"}`);
      }
    }
    finally { setActing(null); }
  };

  const decideCandidate = async (c: Candidate, promote: boolean) => {
    const key = `${c.kind}:${c.name}`;
    setActing(key);
    setError(null);
    try {
      if (promote) await contextV2Api.promoteType(c.name, c.kind);
      else await contextV2Api.deleteType(c.name, c.kind);
      decided.current.add(key);
      setCandidates((xs) => xs.filter((x) => `${x.kind}:${x.name}` !== key));
    } catch (e: any) {
      setError(`${promote ? "Promote" : "Reject"} failed: ${e?.response?.data?.detail || e?.message || "unknown error"}`);
    }
    finally { setActing(null); }
  };

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      <PageHeader
        title="Review Queue"
        icon={Inbox}
        description="What resolution applied automatically and why, and what needs your decision: conflicting values and statements, possible duplicates, new versions of documents, and newly discovered types."
        actions={<Button variant="secondary" icon={RefreshCw} busy={loading} onClick={load}>Refresh</Button>}
      />

      <div className="inline-flex bg-stone-100 rounded-lg p-1 text-sm">
        <button onClick={() => setTab("auto")}
          className={`px-4 py-1.5 rounded-md font-medium flex items-center gap-1.5 ${tab === "auto" ? "bg-white shadow-sm text-stone-900" : "text-stone-500 hover:text-stone-700"}`}>
          <Sparkles size={14} /> Auto-resolution {applied.length > 0 && <span className="text-[10px] bg-stone-300 text-stone-700 rounded-full px-1.5">{applied.length}</span>}
        </button>
        <button onClick={() => setTab("conflicts")}
          className={`px-4 py-1.5 rounded-md font-medium flex items-center gap-1.5 ${tab === "conflicts" ? "bg-white shadow-sm text-stone-900" : "text-stone-500 hover:text-stone-700"}`}>
          <GitCompare size={14} /> Conflicts {conflicts.length + attrConflicts.length > 0 && <span className="text-[10px] bg-red-500 text-white rounded-full px-1.5">{conflicts.length + attrConflicts.length}</span>}
        </button>
        <button onClick={() => setTab("versions")}
          className={`px-4 py-1.5 rounded-md font-medium flex items-center gap-1.5 ${tab === "versions" ? "bg-white shadow-sm text-stone-900" : "text-stone-500 hover:text-stone-700"}`}>
          <History size={14} /> Versions {versions.length > 0 && <span className="text-[10px] bg-red-500 text-white rounded-full px-1.5">{versions.length}</span>}
        </button>
        <button onClick={() => setTab("ontology")}
          className={`px-4 py-1.5 rounded-md font-medium flex items-center gap-1.5 ${tab === "ontology" ? "bg-white shadow-sm text-stone-900" : "text-stone-500 hover:text-stone-700"}`}>
          <Network size={14} /> Ontology {candidates.length > 0 && <span className="text-[10px] bg-red-500 text-white rounded-full px-1.5">{candidates.length}</span>}
        </button>
      </div>

      {error && (
        <div role="alert" className="mb-4 p-3 rounded-lg text-sm font-medium bg-red-50 text-red-700 border border-red-100 flex items-center justify-between gap-3">
          <span>{error}</span>
          <button onClick={() => setError(null)} className="text-red-400 hover:text-red-700" aria-label="Dismiss"><X size={14} /></button>
        </div>
      )}
      {loading ? (
        <div className="space-y-3">{[0, 1, 2].map((i) => <div key={i} className="h-20 rounded-xl border border-stone-200 bg-stone-50 animate-pulse" />)}</div>
      ) : tab === "auto" ? (
        applied.length === 0 ? (
          <Empty icon={<Sparkles size={40} className="text-stone-300" />} title="Nothing resolved automatically yet"
            body={<>After each asset is ingested, its nodes are matched with the rest of your data using the identity each node type declares (set in the Extractor). What was linked, confirmed or added shows here, with the reason and the score.</>} />
        ) : (
          <ul className="space-y-2">
            {applied.map((d) => (
              <li key={d.id} className="bg-white border border-stone-200 rounded-xl p-3.5 flex flex-col sm:flex-row sm:items-center gap-3">
                <span className={`self-start sm:self-center shrink-0 px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${RELATION_STYLE[d.relation] || "bg-stone-100 text-stone-600"}`}>{d.relation}</span>
                <p className="flex-1 min-w-0 text-sm text-stone-800 break-words">{why(d)}</p>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-stone-500 shrink-0">
                  <span title="How sure the engine was" className="font-mono">score {Math.round((d.confidence ?? 0) * 100)}%</span>
                  <span title="How it was decided">{d.method === "similar" ? "similar identity" : "identity match"}</span>
                  {d.source_label && <span className="truncate max-w-[16rem]" title="The asset whose ingestion made this decision">from {d.source_label}</span>}
                  {d.created_at && <span>{new Date(d.created_at).toLocaleString()}</span>}
                </div>
              </li>
            ))}
          </ul>
        )
      ) : tab === "conflicts" ? (
        conflicts.length === 0 && attrConflicts.length === 0 ? (
          <Empty icon={<GitCompare size={40} className="text-stone-300" />} title="No conflicts to review"
            body={<>When two records of the same thing carry different values, two records might be the same thing, or two statements disagree and the evidence is not strong enough to settle it automatically, it waits here. Nothing is changed until you decide.</>} />
        ) : (
          <ul className="space-y-3">
            {attrConflicts.map((d) => (
              <li key={d.id} className="bg-white border border-stone-200 rounded-xl shadow-sm p-4 flex flex-col sm:flex-row sm:items-start gap-4">
                <div className="p-2 rounded-lg bg-rose-50 text-rose-600 mt-0.5 self-start"><GitCompare size={18} /></div>
                {d.relation === "SAME" ? (
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-stone-800">Possibly the same {d.node_type}: <b>{d.subject_label}</b> and <b>{d.target_label}</b></p>
                    <p className="mt-1 text-[11px] text-stone-500"><span className="font-mono">score {Math.round((d.confidence ?? 0) * 100)}%</span>{d.source_label ? ` · from ${d.source_label}` : ""}</p>
                  </div>
                ) : (
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-stone-800"><b>{d.target_label || d.node_type}</b> · <span className="font-mono">{d.attribute}</span></p>
                    <div className="mt-1.5 grid sm:grid-cols-2 gap-2 text-sm">
                      <div className="rounded-lg bg-stone-50 px-3 py-2"><span className="block text-[10px] font-bold uppercase tracking-wider text-stone-400">Existing</span><span className="break-all">{String(d.evidence?.other)}</span></div>
                      <div className="rounded-lg bg-stone-50 px-3 py-2"><span className="block text-[10px] font-bold uppercase tracking-wider text-stone-400">New{d.source_label ? ` · from ${d.source_label}` : ""}</span><span className="break-all">{String(d.evidence?.value)}</span></div>
                    </div>
                  </div>
                )}
                <div className="flex items-center gap-2 shrink-0">
                  <Button variant="secondary" size="sm" icon={X} busy={acting === d.id} onClick={() => decideConflict(d, "keep")}>{d.relation === "SAME" ? "Different" : "Keep existing"}</Button>
                  <Button variant="primary" size="sm" icon={Check} disabled={acting === d.id} onClick={() => decideConflict(d, "use_new")}>{d.relation === "SAME" ? "Same thing" : "Use new"}</Button>
                </div>
              </li>
            ))}
            {conflicts.map((v) => (
              <li key={v.id} className="bg-white border border-stone-200 rounded-xl shadow-sm p-4 flex flex-col sm:flex-row sm:items-start gap-4">
                <div className="p-2 rounded-lg bg-rose-50 text-rose-600 mt-0.5 self-start"><GitCompare size={18} /></div>
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-semibold text-stone-800 break-all">{v.winner?.text || "(statement removed)"}</span>
                    <span className="text-stone-400 flex items-center gap-1 text-xs"><ArrowRight size={12} /> would supersede</span>
                    <span className="text-stone-500 line-through decoration-stone-300 break-all">{v.loser_text}</span>
                  </div>
                  <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-stone-400">
                    <span className="px-1.5 py-0.5 rounded bg-stone-100 text-stone-500 font-bold uppercase tracking-wider">{v.type}</span>
                    {v.about && <span>about {v.about}</span>}
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <Button variant="primary" size="sm" icon={Check} busy={acting === v.id} onClick={() => decideResolution(v, true)}>Approve</Button>
                  <Button variant="secondary" size="sm" icon={X} disabled={acting === v.id} onClick={() => decideResolution(v, false)}>Reject</Button>
                </div>
              </li>
            ))}
          </ul>
        )
      ) : tab === "versions" ? (
        versions.length === 0 ? (
          <Empty icon={<History size={40} className="text-stone-300" />} title="No version matches to review"
            body={<>When an uploaded document looks like a new version of one you already have, the match waits here. The older document stays searchable until you <b>approve</b>; <b>reject</b> keeps both as separate documents.</>} />
        ) : (
          <ul className="space-y-3">
            {versions.map((v) => (
              <li key={v.id} className="bg-white border border-stone-200 rounded-xl shadow-sm p-4 flex flex-col sm:flex-row sm:items-start gap-4">
                <div className="p-2 rounded-lg bg-amber-50 text-amber-600 mt-0.5 self-start"><History size={18} /></div>
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <span className="font-semibold text-stone-800 break-all">{v.winner?.text || "(new document removed)"}</span>
                    <span className="text-stone-400 flex items-center gap-1 text-xs"><ArrowRight size={12} /> replaces</span>
                    <span className="text-stone-500 line-through decoration-stone-300 break-all">{v.loser_text}</span>
                  </div>
                  <p className="text-xs text-stone-500 mt-1">
                    Approving hides <b>{v.loser_text}</b> and everything extracted from it from search. The link is kept, so it can be traced later.
                  </p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-stone-400">
                    <span className="px-1.5 py-0.5 rounded bg-stone-100 text-stone-500 font-bold uppercase tracking-wider">{v.type}</span>
                    {typeof v.distance === "number" && (
                      <span title="How many of the 64 fingerprint bits differ. Lower means more alike.">
                        {v.distance}/64 bits differ
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <Button variant="primary" size="sm" icon={Check} busy={acting === v.id} onClick={() => decideResolution(v, true)}>Approve</Button>
                  <Button variant="secondary" size="sm" icon={X} disabled={acting === v.id} onClick={() => decideResolution(v, false)}>Reject</Button>
                </div>
              </li>
            ))}
          </ul>
        )
      ) : (
        candidates.length === 0 ? (
          <Empty icon={<Network size={40} className="text-stone-300" />} title="No candidate types"
            body={<>When extraction discovers a brand-new node/edge/entity type, it appears here as a candidate to <b>promote</b> into the active ontology or <b>reject</b>. Active types live on the <Link href="/dashboard/curate" className="text-primary font-semibold">Curation</Link> page.</>} />
        ) : (
          <ul className="space-y-3">
            {candidates.map((c) => {
              const key = `${c.kind}:${c.name}`;
              return (
                <li key={key} className="bg-white border border-stone-200 rounded-xl shadow-sm p-4 flex items-start gap-4">
                  <div className="p-2 rounded-lg bg-amber-50 text-amber-600 mt-0.5"><Lightbulb size={18} /></div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-stone-800 text-sm">{c.name}</span>
                      <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-stone-100 text-stone-500">{c.kind}</span>
                    </div>
                    {c.description && <p className="text-xs text-stone-500 mt-1">{c.description}</p>}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <Button variant="primary" size="sm" icon={ArrowUpRight} busy={acting === key} onClick={() => decideCandidate(c, true)}>Promote</Button>
                    <Button variant="secondary" size="sm" icon={X} disabled={acting === key} onClick={() => decideCandidate(c, false)}>Reject</Button>
                  </div>
                </li>
              );
            })}
          </ul>
        )
      )}
    </div>
  );
}

function Empty({ icon, title, body }: { icon: React.ReactNode; title: string; body: React.ReactNode }) {
  return (
    <div className="bg-stone-50 border border-dashed border-stone-300 rounded-2xl p-12 text-center text-stone-500 flex flex-col items-center">
      <div className="mb-3">{icon}</div>
      <h2 className="text-lg font-bold text-stone-700 mb-1">{title}</h2>
      <p className="max-w-md text-sm">{body}</p>
    </div>
  );
}
