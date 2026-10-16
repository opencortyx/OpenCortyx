"use client";

// The three lists on the Custom Extractors page: your extractors, relationships, built-ins.
// Every card action from the old page is here (Run / Edit / Delete, approve / reject, companion
// Run + Edit prompt & model); the dense built-in card body now opens from a row instead of always
// being on screen.
import { useEffect, useState, type ReactNode } from "react";
import {
  Network, Settings2, Code, Trash2, Edit2, Workflow, Activity, AlertCircle, ChevronRight, Play,
} from "lucide-react";
import Button from "@/components/ui/Button";
import type { ExtractorDraft } from "./useExtractorDraft";
import {
  RELATE_MODES, epLabel, extractorBadge, readsLabel, howLabel, producesLabel, CATEGORY_LABELS, searchText,
} from "./extractorVocab";
import { chipCls } from "./ui";

const matches = (ext: any, q: string) => !q || searchText(ext).includes(q.toLowerCase());

function DeleteButton({ name, onDelete }: { name: string; onDelete: () => void }) {
  const [confirm, setConfirm] = useState(false);
  useEffect(() => {
    if (!confirm) return;
    const t = setTimeout(() => setConfirm(false), 3000);
    return () => clearTimeout(t);
  }, [confirm]);
  return confirm ? (
    <button type="button" onClick={() => { setConfirm(false); onDelete(); }}
      className="px-2.5 py-1.5 rounded-lg text-xs font-semibold text-white bg-red-600 hover:bg-red-500">Delete?</button>
  ) : (
    <button type="button" onClick={() => setConfirm(true)} aria-label={`Delete ${name}`} title="Delete"
      className="p-2 rounded-lg text-stone-400 hover:text-red-600 hover:bg-red-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500"><Trash2 size={16} /></button>
  );
}

function RunButton({ d, id }: { d: ExtractorDraft; id: string }) {
  const busy = d.runningId === id;
  return <Button size="sm" icon={Play} busy={busy} onClick={() => d.handleRun(id)}>{busy ? "Running…" : "Run"}</Button>;
}

function EditButton({ label = "Edit", name, onClick, title }: { label?: string; name: string; onClick: () => void; title?: string }) {
  return (
    <Button size="sm" variant="ghost" icon={Edit2} onClick={onClick} aria-label={`${label} ${name}`} title={title}>{label}</Button>
  );
}

/** The result of the last run, on the row that started it. */
function RunStatus({ d, ids }: { d: ExtractorDraft; ids: string[] }) {
  if (!d.runMsg || !d.runMsgId || !ids.includes(d.runMsgId)) return null;
  const failed = d.runMsg.startsWith("Failed");
  return (
    <div className={`mx-4 mb-3 sm:ml-[3.75rem] text-[12.5px] rounded-lg px-3 py-2 flex flex-wrap items-center gap-2 ${failed ? "bg-rose-50 text-rose-700" : "bg-emerald-50 text-emerald-800"}`}>
      {d.runMsg}
      {!failed && <a href="/dashboard/progress" className="ml-auto font-semibold underline">View progress</a>}
    </div>
  );
}

function Flow({ reads, how, produces, tech }: { reads: ReactNode; how: ReactNode; produces?: ReactNode; tech?: string }) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mt-1 text-[13px] text-stone-600">
      <span className="inline-flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-sm bg-teal-600" />{reads}</span>
      <span className="text-stone-300">→</span>
      <span className="inline-flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-sm bg-emerald-600" />{how}</span>
      {produces !== undefined && <>
        <span className="text-stone-300">→</span>
        <span className="inline-flex items-center gap-1.5"><span className="w-1.5 h-1.5 rounded-sm bg-rose-500" />{produces}</span>
      </>}
      {tech && <code className="text-[11px] text-stone-400">{tech}</code>}
    </div>
  );
}

function Row({ icon, iconCls, title, badges, flow, actions, children }: {
  icon: ReactNode; iconCls: string; title: string; badges?: ReactNode; flow: ReactNode; actions: ReactNode; children?: ReactNode;
}) {
  return (
    <div className="border-t border-stone-100 first:border-t-0">
      <div className="grid grid-cols-[32px_minmax(0,1fr)] sm:grid-cols-[32px_minmax(0,1fr)_auto] gap-x-3 gap-y-2 items-center px-4 py-3.5">
        <span className={`w-8 h-8 rounded-lg grid place-items-center ${iconCls}`}>{icon}</span>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-mono text-[14.5px] font-semibold text-stone-800 [overflow-wrap:anywhere]">{title}</h3>
            {badges}
          </div>
          {flow}
        </div>
        <div className="col-start-2 sm:col-start-auto flex items-center gap-1">{actions}</div>
      </div>
      {children}
    </div>
  );
}

const ListShell = ({ children }: { children: ReactNode }) => (
  <div className="bg-white border border-stone-200 rounded-xl overflow-hidden">{children}</div>
);
const Empty = ({ children }: { children: ReactNode }) => (
  <div className="text-sm text-stone-500 bg-white border border-dashed border-stone-300 rounded-xl p-8 text-center">{children}</div>
);

export function CustomList({ d, q }: { d: ExtractorDraft; q: string }) {
  const all = d.savedExtractions.filter((e: any) => !e.name.startsWith("builtin:") && !String(e.mechanism?.ref || "").startsWith("relate."));
  const items = all.filter((e: any) => matches(e, q));
  if (!all.length) return <Empty>No extractors yet. Click <b>New extractor</b> to turn existing nodes into new nodes, relationships or fields.</Empty>;
  if (!items.length) return <Empty>Nothing matches “{q}”.</Empty>;
  return (
    <ListShell>
      {items.map((ext: any, idx: number) => (
        <Row key={ext.id || idx} icon={<Workflow size={16} />} iconCls="bg-emerald-50 text-emerald-700" title={ext.name}
          flow={<Flow reads={readsLabel(ext)} how={howLabel(ext)} produces={producesLabel(ext)} tech={ext.mechanism?.ref} />}
          actions={<>
            {ext.executable && <RunButton d={d} id={ext.id} />}
            <EditButton name={ext.name} onClick={() => d.handleEditExtraction(ext)} />
            <DeleteButton name={ext.name} onDelete={() => d.handleDelete(ext.id)} />
          </>}>
          <RunStatus d={d} ids={[ext.id]} />
        </Row>
      ))}
    </ListShell>
  );
}

export function RelationshipList({ d, q }: { d: ExtractorDraft; q: string }) {
  const all = d.savedExtractions.filter((e: any) => String(e.mechanism?.ref || "").startsWith("relate."));
  const rels = all.filter((e: any) => matches(e, q));
  const modeLabel = (ref: string) => RELATE_MODES.find((m) => m.ref === ref)?.label || ref;
  if (!all.length) return <Empty>No relationship extractors yet. Click <b>New relationship</b> to connect entities your extractors already found.</Empty>;
  if (!rels.length) return <Empty>Nothing matches “{q}”.</Empty>;
  return (
    <ListShell>
      {rels.map((ext: any) => {
        const m = ext.mechanism || {};
        const isBuiltin = String(ext.name || "").startsWith("builtin:");
        const shown = ext.name.replace("builtin:", "");
        return (
          <Row key={ext.id} icon={<Network size={16} />} iconCls="bg-indigo-50 text-indigo-600" title={shown}
            badges={isBuiltin && <span className={`${chipCls} bg-stone-100 text-stone-500`}>Built-in · auto-runs</span>}
            flow={<Flow
              reads={<>inside each {Array.isArray(ext.source?.select) ? ext.source.select.join(", ") : ext.source?.select}, via <code className="text-[12px]">{m.via || "MENTIONS"}</code></>}
              how={modeLabel(m.ref)}
              produces={<code className="text-[12px] text-indigo-700">{m.from_type} —{m.relation}→ {m.to_type}{m.direction === "both" ? " ↔" : ""}</code>} />}
            actions={<>
              {ext.executable && <RunButton d={d} id={ext.id} />}
              <EditButton name={shown} label={isBuiltin ? "Edit prompt" : "Edit"} title={isBuiltin ? "Edit prompt (built-in shape is fixed)" : "Edit"} onClick={() => d.handleEditRelationship(ext)} />
              {!isBuiltin && <DeleteButton name={ext.name} onDelete={() => d.handleDelete(ext.id)} />}
            </>}>
            <RunStatus d={d} ids={[ext.id]} />
          </Row>
        );
      })}
    </ListShell>
  );
}

export function BuiltinList({ d, q }: { d: ExtractorDraft; q: string }) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (id: string) => setOpen(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const { proposals } = d;
  return (
    <div className="space-y-6">
      {proposals.length > 0 && (
        <div className="bg-amber-50 border border-amber-200 rounded-xl p-4">
          <div className="flex flex-wrap items-center gap-2 mb-1">
            <AlertCircle size={17} className="text-amber-600" />
            <h3 className="font-semibold text-amber-900">{proposals.length} update{proposals.length > 1 ? "s" : ""} waiting for your approval</h3>
          </div>
          <p className="text-[12.5px] text-amber-800/80 mb-3">A newer claim would replace an older one. Approve to retire the old claim; reject to keep it.</p>
          <div className="space-y-2">
            {proposals.map((p: any) => (
              <div key={p.id} className="flex flex-wrap items-center gap-3 bg-white border border-amber-100 rounded-lg px-3 py-2.5 text-[13px]">
                <div className="flex-1 min-w-[220px]">
                  <div className="text-xs text-stone-400">about <b className="text-stone-600">{p.about || "—"}</b></div>
                  <div className="text-rose-600 line-through truncate">{p.loser_text}</div>
                  <div className="text-emerald-700 truncate">↳ replaced by: {p.winner?.text || "(newer claim)"}</div>
                </div>
                <Button variant="primary" size="sm" onClick={() => d.decideProposal(p.id, true)}>Approve</Button>
                <Button size="sm" onClick={() => d.decideProposal(p.id, false)}>Reject</Button>
              </div>
            ))}
          </div>
        </div>
      )}

      {["structured", "semistructured", "unstructured", "memory", "answer"].map((cat) => {
        const items = d.savedExtractions.filter((e: any) => e.name.startsWith("builtin:") && (e.category || "unstructured") === cat);
        if (items.length === 0) return null;
        const isStruct = cat === "structured";
        const isSemi = cat === "semistructured";
        // Fold semantic enrichers (LLM, source-bound, no emitted nodes) INTO the node descriptor
        // card they enrich → one row per node type shows structural + semantic fields.
        const descByType: Record<string, any> = {};
        items.forEach((x: any) => { if (x.deterministic) (x.emit?.nodes || []).forEach((n: any) => { if (n.type) descByType[n.type] = x; }); });
        const selOf = (e: any) => { const s = e.source?.select; return s ? (Array.isArray(s) ? s : [s]) : []; };
        // Fold ONLY attribute enrichers (no emitted nodes AND no emitted edges) into the node card.
        const isFolded = (e: any) => !e.deterministic && !!e.mechanism?.prompt && !((e.emit?.nodes || []).length) && !((e.emit?.edges || []).length) && selOf(e).some((t: string) => descByType[t]);
        const companionsFor = (nodeType: string) => items.filter((e: any) => isFolded(e) && selOf(e).includes(nodeType));
        const visible = items.filter((e: any) => !isFolded(e)).filter((e: any) => {
          if (!q) return true;
          const nts = (e.emit?.nodes || []).map((n: any) => n.type).filter(Boolean);
          return matches(e, q) || nts.flatMap((nt: string) => companionsFor(nt)).some((c: any) => matches(c, q));
        });
        if (!visible.length) return null;
        const label = CATEGORY_LABELS[cat] || { title: cat, hint: "" };
        const iconCls = isSemi ? "bg-violet-50 text-violet-600" : isStruct ? "bg-teal-50 text-teal-600" : "bg-emerald-50 text-emerald-600";
        return (
          <div key={cat}>
            <div className="flex flex-wrap items-baseline gap-x-3 mb-2">
              <h3 className="font-display text-lg font-semibold text-stone-800">{label.title}</h3>
              <span className="text-xs text-stone-400">{items.length} extractor{items.length > 1 ? "s" : ""} · {label.hint}</span>
            </div>
            <ListShell>
              {visible.map((ext: any) => {
                const nodeTypes = (ext.emit?.nodes || []).map((n: any) => n.type).filter(Boolean);
                const attrs = (ext.emit?.nodes || []).flatMap((n: any) => n.attributes || []);
                const companions = nodeTypes.flatMap((nt: string) => companionsFor(nt));
                const badge = extractorBadge(ext);
                const isOpen = open.has(ext.id);
                return (
                  <Row key={ext.id} icon={<Settings2 size={16} />} iconCls={iconCls} title={ext.name.replace("builtin:", "")}
                    badges={<>
                      <span className={`${chipCls} ${badge.cls}`}>{badge.label}</span>
                      {companions.length > 0 && <span className={`${chipCls} bg-emerald-50 text-emerald-700`}>+ AI fields</span>}
                      {ext.executable && <span className={`${chipCls} bg-indigo-50 text-indigo-700`}>Runnable</span>}
                    </>}
                    flow={!ext.executable ? (
                      <div className="flex flex-wrap items-center gap-2 mt-1 text-[13px]">
                        <span className="inline-flex items-center gap-1.5 text-amber-700 font-medium"><Activity size={13} />
                          {String(ext.mechanism?.ref) === "pipeline.answer" ? "Runs when a question is answered" : "Runs when a session is saved"}</span>
                        <span className="text-xs text-stone-400">not an extractor — it has no nodes to run over; edit its prompt and model here</span>
                      </div>
                    ) : (
                      <Flow reads={ext.source?.select ? readsLabel(ext) : "Independent (no source)"} how={howLabel(ext)} produces={producesLabel(ext)} tech={ext.mechanism?.ref || "—"} />
                    )}
                    actions={<>
                      {ext.executable && <RunButton d={d} id={ext.id} />}
                      {/* Every extractor opens in the editor — a row with a folded companion is still an extractor of its own. */}
                      <EditButton name={ext.name.replace("builtin:", "")} onClick={() => d.handleEditExtraction(ext)}
                        title="Open in the editor — mechanism, code/prompt, model, per-field tools, produced nodes & relationships, test run" />
                      <button type="button" onClick={() => toggle(ext.id)} aria-expanded={isOpen} aria-label={isOpen ? "Hide details" : "Show details"}
                        className="p-2 rounded-lg text-stone-400 hover:text-stone-700 hover:bg-stone-100">
                        <ChevronRight size={16} className={`transition-transform ${isOpen ? "rotate-90" : ""}`} />
                      </button>
                    </>}>
                    <RunStatus d={d} ids={[ext.id, ...companions.map((c: any) => c.id)]} />
                    {isOpen && (
                      <div className="px-4 pb-4 sm:pl-[3.75rem] space-y-3">
                        {ext.mechanism?.code && (
                          <details className="rounded-lg overflow-hidden border border-stone-200">
                            <summary className="bg-stone-800 text-stone-300 px-3 py-1.5 font-mono text-[11px] flex items-center gap-2 cursor-pointer list-none"><Code size={12} /> Code — extract(inputs) · editable</summary>
                            <div className="p-3 overflow-x-auto bg-stone-900 max-h-64"><pre className="text-[11px] leading-relaxed text-emerald-400 font-mono">{ext.mechanism.code}</pre></div>
                          </details>
                        )}
                        {ext.mechanism?.items && (
                          <details className="rounded-lg overflow-hidden border border-stone-200">
                            <summary className="bg-stone-800 text-stone-300 px-3 py-1.5 font-mono text-[11px] flex items-center gap-2 cursor-pointer list-none"><Code size={12} /> Items — {(ext.mechanism.items || []).length} constant record(s) · editable</summary>
                            <div className="p-3 overflow-x-auto bg-stone-900 max-h-64"><pre className="text-[11px] leading-relaxed text-emerald-400 font-mono">{JSON.stringify(ext.mechanism.items, null, 2)}</pre></div>
                          </details>
                        )}
                        <div className="grid gap-3 md:grid-cols-2">
                          <div className="bg-stone-50 rounded-lg px-3 py-2.5">
                            {ext.mechanism?.prompt ? (
                              <>
                                <p className="text-[10.5px] font-semibold uppercase tracking-wider text-stone-400">
                                  Prompt{ext.deterministic && <span className="text-amber-600 normal-case font-medium tracking-normal"> · runs inside ingestion (editable)</span>}
                                </p>
                                <p className="text-[12.5px] text-stone-600 mt-1 line-clamp-4 whitespace-pre-wrap font-mono">{ext.mechanism?.prompt}</p>
                                <p className="text-xs text-emerald-700 mt-1.5 font-medium">Model: {d.modelLabel(ext.mechanism?.model)}</p>
                              </>
                            ) : companions.length > 0 ? (
                              <p className="text-[12.5px] text-stone-600">Structural fields are introspected deterministically. The semantic fields are generated by the AI enricher below — edit its prompt, pick its model, or set a per‑field model.</p>
                            ) : (
                              <>
                                <p className="text-[12.5px] text-stone-600">
                                  {ext.mechanism?.description
                                    || (String(ext.mechanism?.ref || "") === "unstructured.attributes"
                                        ? "No single prompt — every field is written by its own mechanism, prompt and model. "
                                        : String(ext.mechanism?.ref || "") === "unstructured.python"
                                        ? "Coded extractor — the rule is the Python above, editable like any custom one. "
                                        : String(ext.mechanism?.ref || "") === "seed.literal"
                                        ? "Constant records — edit the items above; no model involved. "
                                        : String(ext.mechanism?.ref || "").includes("introspect")
                                        ? "Structural fields are introspected (no LLM). "
                                        : "Deterministic by default. ")}
                                  {" Assign a "}<span className="text-emerald-700 font-medium">model</span>{" (and optional prompt) to the extractor or to any individual field to make it model‑generated."}
                                </p>
                                <p className="text-xs text-emerald-700 mt-1.5 font-medium">Model: {d.modelLabel(ext.mechanism?.model)}</p>
                              </>
                            )}
                          </div>
                          {(nodeTypes.length > 0 || (ext.emit?.edges || []).length > 0 || attrs.length > 0) && (
                            <div className="bg-stone-50 rounded-lg px-3 py-2.5 space-y-2 text-xs">
                              {nodeTypes.length > 0 && (
                                <div><span className="font-semibold text-stone-600">Nodes:</span> <span className="text-stone-500">{nodeTypes.join(", ")}</span></div>
                              )}
                              {(ext.emit?.edges || []).length > 0 && (
                                <div>
                                  <span className="font-semibold text-stone-600">Relationships:</span>
                                  <div className="mt-1 flex flex-wrap gap-1.5">
                                    {(ext.emit?.edges || []).map((e: any, i: number) => {
                                      const sel = ext.source?.select;
                                      const from = e.from === "source" ? ((Array.isArray(sel) ? sel[0] : sel) || "Chunk") : epLabel(e.from);
                                      const to = e.to === "emitted" ? (nodeTypes[0] || "node") : epLabel(e.to);
                                      return (
                                        <span key={i} className="px-2 py-0.5 bg-white text-indigo-700 rounded border border-indigo-100 text-[11px] font-mono">
                                          {from} <span className="text-indigo-400">—{e.type || e.rel}→</span> {to}
                                        </span>
                                      );
                                    })}
                                  </div>
                                </div>
                              )}
                              {attrs.length > 0 && (
                                <div className="flex flex-wrap gap-1">
                                  {attrs.slice(0, 24).map((a: string, i: number) => (
                                    <span key={i} className="px-1.5 py-0.5 bg-white border border-stone-200 text-stone-500 rounded text-[11px] font-mono">{a}</span>
                                  ))}
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                        {companions.map((comp: any) => (
                          <div key={comp.id} className="rounded-lg border border-emerald-200 bg-emerald-50/40 px-3 py-2.5">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <p className="text-[10.5px] font-semibold uppercase tracking-wider text-emerald-800">AI fields for these nodes · {comp.name.replace("builtin:", "")}</p>
                              <div className="flex items-center gap-1.5">
                                <RunButton d={d} id={comp.id} />
                                <Button size="sm" variant="ghost" icon={Edit2} onClick={() => d.handleEditExtraction(comp)}>Edit prompt &amp; model</Button>
                              </div>
                            </div>
                            <p className="text-xs text-stone-600 mt-1.5 line-clamp-2 whitespace-pre-wrap font-mono">{comp.mechanism?.prompt}</p>
                            <p className="text-xs text-emerald-700 mt-1 font-medium">Model: {d.modelLabel(comp.mechanism?.model)}</p>
                          </div>
                        ))}
                      </div>
                    )}
                  </Row>
                );
              })}
            </ListShell>
          </div>
        );
      })}
    </div>
  );
}
