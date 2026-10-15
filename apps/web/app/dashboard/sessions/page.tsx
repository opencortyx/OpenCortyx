"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { contextV2Api } from "@/lib/api/contextV2";
import {
  MessagesSquare, RefreshCw, Loader2, Bot, User, Lightbulb, CheckCircle2, Star,
  Link2, ArrowRight, ShieldCheck,
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";

type Session = {
  id: string; title: string; agent?: string; project?: string; status?: string;
  summary?: string; started_at?: string; ended_at?: string; updated_at?: string;
};
type Item = { id: string; type: string; label: string; text?: string };
type Turn = { id: string; role?: string; text?: string; ordinal?: number };
type Detail = {
  status: string; session: Session & { rollup_score?: number };
  turns: Turn[]; decisions: Item[]; facts: Item[]; preferences: Item[];
  artifacts: Item[]; references: Item[];
  related_sessions?: { id: string; title: string; dir: string }[];
};

export default function SessionsPage() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try { setSessions((await contextV2Api.listSessions(100))?.items || []); }
    catch { setSessions([]); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const open = async (id: string) => {
    setSel(id); setDetail(null); setLoadingDetail(true);
    try { setDetail(await contextV2Api.memorySession(id)); }
    catch { setDetail(null); }
    finally { setLoadingDetail(false); }
  };

  const dayKey = (s: Session) => {
    const d = s.ended_at || s.updated_at || s.started_at;
    return d ? new Date(d).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "Undated";
  };
  const groups: { day: string; items: Session[] }[] = [];
  sessions.forEach((s) => {
    const k = dayKey(s);
    const g = groups.find((x) => x.day === k);
    if (g) g.items.push(s); else groups.push({ day: k, items: [s] });
  });

  return (
    <div className="max-w-7xl mx-auto space-y-6">
      <PageHeader
        title="Conversations"
        icon={MessagesSquare}
        description="What your connected AI agents captured through MCP — transcripts, decisions, and durable facts, as a graph."
        actions={<Button variant="secondary" icon={RefreshCw} busy={loading} onClick={load}>Refresh</Button>}
      />

      <div className="text-xs text-stone-500 bg-stone-50 border border-stone-200 rounded-lg px-3 py-2 flex items-start gap-2">
        <ShieldCheck size={14} className="text-stone-400 mt-0.5 shrink-0" />
        <span>Capture is <b>opt-in</b>: connected agents&apos; OpenCortyx interactions are recorded here only while
          <b> Capture agent sessions</b> is enabled in <Link href="/dashboard/settings" className="text-primary font-medium">Settings → Agents</Link>.
          Only activity through OpenCortyx is stored — never an agent&apos;s private chat — scoped to this workspace.</span>
      </div>

      {loading ? (
        <div className="space-y-3">{[0, 1, 2].map((i) => <div key={i} className="h-16 rounded-xl border border-stone-200 bg-stone-50 animate-pulse" />)}</div>
      ) : sessions.length === 0 ? (
        <div className="bg-stone-50 border border-dashed border-stone-300 rounded-2xl p-12 text-center text-stone-500 flex flex-col items-center">
          <MessagesSquare size={40} className="mb-3 text-stone-300" />
          <h2 className="text-lg font-bold text-stone-700 mb-1">No agent sessions yet</h2>
          <p className="max-w-md text-sm">Connect an agent in <Link href="/dashboard/connectors" className="text-primary font-semibold">Connectors → AI Agents</Link>. When it calls <code>opencortyx_save_session</code> over MCP, its session (chat, decisions, facts) is saved here as a graph.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          {/* session list — grouped by date */}
          <div className="lg:col-span-1 space-y-4">
            {groups.map((g) => (
              <div key={g.day} className="space-y-2">
                <div className="text-[11px] font-bold uppercase tracking-wider text-stone-400 px-1">{g.day}</div>
                {g.items.map((s) => (
                  <button key={s.id} onClick={() => open(s.id)}
                    className={`w-full text-left bg-white border rounded-xl p-3 shadow-sm hover:border-primary/40 transition-colors ${sel === s.id ? "border-primary ring-1 ring-primary/20" : "border-stone-200"}`}>
                    <div className="flex items-center gap-2">
                      <Bot size={14} className="text-primary shrink-0" />
                      <span className="font-semibold text-stone-800 text-sm truncate flex-1">{s.title || "Session"}</span>
                      {s.status && <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-stone-100 text-stone-500 shrink-0">{s.status}</span>}
                    </div>
                    {s.summary && <p className="text-xs text-stone-500 mt-1 line-clamp-2">{s.summary}</p>}
                    <div className="mt-1.5 flex items-center gap-2 text-[11px] text-stone-400">
                      {s.agent && <span className="font-medium text-stone-500">{s.agent}</span>}
                      {s.project && <span>· {s.project}</span>}
                    </div>
                  </button>
                ))}
              </div>
            ))}
          </div>

          {/* session detail */}
          <div className="lg:col-span-2">
            {!sel ? (
              <div className="bg-white border border-stone-200 rounded-xl p-10 text-center text-sm text-stone-400 shadow-sm">Select a session to view its transcript and what the agent learned.</div>
            ) : loadingDetail || !detail ? (
              <div className="bg-white border border-stone-200 rounded-xl p-10 text-center text-sm text-stone-400 shadow-sm flex items-center justify-center gap-2"><Loader2 size={16} className="animate-spin" /> Loading session…</div>
            ) : detail.status !== "ok" ? (
              <div className="bg-white border border-stone-200 rounded-xl p-10 text-center text-sm text-stone-400 shadow-sm">Could not load this session.</div>
            ) : (
              <div className="space-y-4">
                {/* header */}
                <div className="bg-white border border-stone-200 rounded-xl p-5 shadow-sm">
                  <h2 className="text-lg font-bold text-stone-900 flex items-center gap-2"><Bot size={18} className="text-primary" /> {detail.session.title || "Session"}</h2>
                  <div className="mt-1 flex flex-wrap gap-2 text-[11px] text-stone-400">
                    {detail.session.agent && <span className="px-1.5 py-0.5 rounded bg-stone-100 text-stone-600 font-medium">{detail.session.agent}</span>}
                    {detail.session.project && <span className="px-1.5 py-0.5 rounded bg-stone-100 text-stone-600">{detail.session.project}</span>}
                    {detail.session.started_at && <span>started {new Date(detail.session.started_at).toLocaleString()}</span>}
                  </div>
                  {detail.session.summary && <p className="text-sm text-stone-600 mt-3">{detail.session.summary}</p>}
                </div>

                {detail.related_sessions && detail.related_sessions.length > 0 && (
                  <div className="bg-white border border-stone-200 rounded-xl p-4 shadow-sm">
                    <h3 className="font-semibold text-stone-800 text-sm flex items-center gap-2 mb-2"><Link2 size={15} className="text-primary" /> Connected sessions</h3>
                    <div className="flex flex-wrap gap-2">
                      {detail.related_sessions.map((rs) => (
                        <button key={rs.id + rs.dir} onClick={() => open(rs.id)}
                          className="text-xs border border-stone-200 rounded-full px-3 py-1 hover:border-primary/40 hover:text-primary flex items-center gap-1">
                          <span className="text-stone-400">{rs.dir === "continues" ? "↳ continues" : "↰ continued by"}</span> {rs.title}
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                <Section title="Decisions" icon={<Lightbulb size={15} className="text-amber-500" />} items={detail.decisions} />
                <Section title="Facts learned" icon={<CheckCircle2 size={15} className="text-emerald-500" />} items={detail.facts} />
                <Section title="Preferences" icon={<Star size={15} className="text-primary" />} items={detail.preferences} />
                <Section title="References" icon={<Link2 size={15} className="text-teal-500" />} items={detail.references} linked />

                {/* transcript */}
                {detail.turns.length > 0 && (
                  <div className="bg-white border border-stone-200 rounded-xl shadow-sm overflow-hidden">
                    <div className="px-5 py-3 border-b border-stone-100"><h3 className="font-semibold text-stone-800 text-sm">Transcript ({detail.turns.length})</h3></div>
                    <div className="p-4 space-y-3 max-h-[28rem] overflow-y-auto">
                      {detail.turns.map((t) => {
                        const agent = (t.role || "").toLowerCase() !== "user";
                        return (
                          <div key={t.id} className={`flex gap-2.5 ${agent ? "" : "flex-row-reverse"}`}>
                            <div className={`w-7 h-7 rounded-full flex items-center justify-center shrink-0 ${agent ? "bg-primary/10 text-primary" : "bg-stone-100 text-stone-500"}`}>
                              {agent ? <Bot size={15} /> : <User size={15} />}
                            </div>
                            <div className={`max-w-[80%] rounded-xl px-3 py-2 text-sm whitespace-pre-wrap ${agent ? "bg-stone-50 text-stone-700" : "bg-primary/5 text-stone-800"}`}>
                              <div className="text-[10px] uppercase tracking-wider text-stone-400 mb-0.5">{t.role || "agent"}</div>
                              {t.text}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function Section({ title, icon, items, linked }: { title: string; icon: React.ReactNode; items: Item[]; linked?: boolean }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="bg-white border border-stone-200 rounded-xl shadow-sm overflow-hidden">
      <div className="px-5 py-3 border-b border-stone-100 flex items-center gap-2">{icon}<h3 className="font-semibold text-stone-800 text-sm">{title}</h3><span className="text-xs text-stone-400">{items.length}</span></div>
      <ul className="divide-y divide-stone-100">
        {items.map((it) => (
          <li key={it.id} className="px-5 py-2.5 text-sm text-stone-700 flex items-center justify-between gap-3">
            <span className="min-w-0 truncate">{it.text || it.label}</span>
            {linked && (
              <Link href={`/dashboard/catalogue/${it.id}` as any} className="text-xs text-primary font-semibold inline-flex items-center gap-1 shrink-0">open <ArrowRight size={12} /></Link>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
