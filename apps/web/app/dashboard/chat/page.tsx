"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { contextV2Api, type ChatThread, type Citation, type CitationSource } from "@/lib/api/contextV2";
import ChatHistory from "@/components/chat/ChatHistory";
import { Send, Loader2, ShieldCheck, AlertTriangle, Sliders, MessageSquare, ChevronRight, ChevronDown, ArrowRight, Save, Check, FileText, History } from "lucide-react";

type Msg = {
  role: "user" | "assistant";
  text: string;
  meta?: {
    abstained?: boolean; grounded?: boolean; confidence?: number;
    retrievers?: string[]; citations?: Citation[]; caveats?: string[]; unsupported?: string[];
    trust_score?: number; crag?: { verdict?: string; score?: number; threshold?: number; reason?: string };
    trace?: any[]; retrieved_context?: string;
    stage_trace?: { stage: string; detail?: string; degraded?: boolean; [k: string]: any }[];
    generation?: { model?: string; template?: string; cot?: boolean; instructions?: string };
  };
};

// Two different corpora live in the same graph: what you ingested, and what we said to each other.
// Mixing them is what made the assistant cite its own transcript as evidence about your data.
const SCOPES = [
  { id: "knowledge" as const, label: "Your knowledge", desc: "Only ingested data — documents, records, entities. Our chat history is excluded." },
  { id: "conversations" as const, label: "Our conversations", desc: "Only saved sessions and turns. Ask what we did, decided, or discussed." },
  { id: "all" as const, label: "Both", desc: "Everything. Useful when a decision we made refers to the data." },
];

export default function ChatPage() {
  const [k, setK] = useState(12);
  const [mode, setMode] = useState<"answer" | "context">("answer");
  // What the assistant is allowed to answer FROM. Defaults to "knowledge" so a question about your
  // data can never be answered out of our own chat history (which is itself stored in the graph).
  const [scope, setScope] = useState<"knowledge" | "conversations" | "all">("knowledge");
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);   // set on first save → idempotent re-save
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  // ── chat history (sidebar) ──
  // Every question is saved server-side as it is asked (save_chat), so the sidebar is just a view of
  // what the API already holds. `chatId` is the open chat; null means the next question starts one.
  const [chatId, setChatId] = useState<string | null>(null);
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [threadsLoading, setThreadsLoading] = useState(true);
  const [historyQ, setHistoryQ] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const [opening, setOpening] = useState(false);

  const refreshThreads = useCallback(async (q = historyQ) => {
    try { setThreads((await contextV2Api.listChats(q)).items); }
    catch { /* keep the list shown */ }
    finally { setThreadsLoading(false); }
  }, [historyQ]);

  useEffect(() => {
    const t = setTimeout(() => refreshThreads(historyQ), historyQ ? 250 : 0);
    return () => clearTimeout(t);
  }, [historyQ, refreshThreads]);

  // The open chat lives in the URL (?chat=<id>) so a reload or a shared link reopens it.
  const selectChat = (id: string | null) => {
    setChatId(id);
    setSessionId(null);
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("chat", id); else url.searchParams.delete("chat");
    window.history.replaceState(null, "", url.toString());
  };

  const openChat = async (id: string) => {
    if (busy) return;
    setOpening(true); setShowHistory(false);
    try {
      const t = await contextV2Api.getChat(id);
      setMsgs(t.messages.map((m) => ({
        role: m.role, text: m.text,
        // a failed turn was stored with {error:true} — render it bare, as it looked live
        meta: m.meta && !m.meta.error ? m.meta : undefined,
      })));
      selectChat(id);
    } catch {
      selectChat(null); setMsgs([]);
    } finally { setOpening(false); }
  };

  const newChat = () => {
    if (busy) return;
    selectChat(null); setMsgs([]); setInput(""); setShowHistory(false);
  };

  const renameChat = async (id: string, title: string) => {
    setThreads((ts) => ts.map((t) => (t.id === id ? { ...t, title } : t)));
    try { await contextV2Api.renameChat(id, title); } catch { refreshThreads(); }
  };

  const deleteChat = async (t: ChatThread) => {
    if (!confirm(`Delete “${t.title}”? This removes the whole conversation.`)) return;
    try {
      await contextV2Api.deleteChat(t.id);
      setThreads((ts) => ts.filter((x) => x.id !== t.id));
      if (t.id === chatId) newChat();
    } catch { refreshThreads(); }
  };

  useEffect(() => {
    const id = new URL(window.location.href).searchParams.get("chat");
    if (id) openChat(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // One-click "Save this conversation" → stores the chat as durable, searchable, citable context.
  // Re-saving the same chat checkpoints it (upsert) rather than duplicating. Links the sources it used.
  const saveConversation = async () => {
    if (saving || busy || msgs.length === 0) return;
    setSaving(true); setSaveMsg(null);
    try {
      const turns = msgs.map((m) => ({ role: m.role, text: m.text }));
      const refs = Array.from(new Set(
        msgs.flatMap((m) => (m.meta?.citations || []).map((c: any) => c?.node_id).filter(Boolean))
      )) as string[];
      const firstQ = msgs.find((m) => m.role === "user")?.text || "Conversation";
      const res = await contextV2Api.saveSession({
        // The chat's id IS its session's id — the session live capture already records it into —
        // so saving adds a summary to that one session instead of writing a second copy of the chat.
        session_id: chatId ?? sessionId, agent: "OpenCortyx Assistant",
        title: firstQ.slice(0, 80), turns, refs,
      });
      if (res?.session_id) setSessionId(res.session_id);
      setSaveMsg(sessionId ? "Updated" : "Saved to memory");
    } catch {
      setSaveMsg("Save failed");
    } finally {
      setSaving(false);
      setTimeout(() => setSaveMsg(null), 3000);
    }
  };

  useEffect(() => {
    contextV2Api.retrieversCatalog()
      .then((c) => { if (typeof c.defaults?.k === "number") setK(c.defaults.k); })
      .catch(() => { /* the slider keeps its own default */ });
  }, []);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs, busy]);

  const send = async () => {
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    setMsgs((m) => [...m, { role: "user", text: q }]);
    setBusy(true);
    const opts = {
      k,      // the L4 diversify cut — how many blocks come back
      scope,  // knowledge (ingested data) | conversations (our sessions) | all
      chat_id: chatId, save_chat: true,   // saved to the history sidebar; follow-ups resolve against it
    };
    const joined = (id?: string) => {
      if (id && id !== chatId) selectChat(id);
      refreshThreads();
    };
    try {
      if (mode === "answer") {
        // stream tokens into a single assistant bubble; finalize with verification meta on `done`.
        let started = false;
        const ensure = () => {
          if (!started) { started = true; setMsgs((m) => [...m, { role: "assistant", text: "" }]); }
        };
        const appendText = (t: string) => {
          ensure();
          setMsgs((m) => {
            const c = [...m]; const last = c[c.length - 1];
            if (last?.role === "assistant") c[c.length - 1] = { ...last, text: (last.text || "") + t };
            return c;
          });
        };
        const finalize = (d: any) => {
          ensure();
          setMsgs((m) => {
            const c = [...m]; const last = c[c.length - 1];
            // A failed generation (no model connected, server down) carries the reason in
            // `answer`; only a real abstention on the evidence reads as one.
            const text = d.error
              ? `⚠ ${d.answer || d.error}`
              : d.abstained
              ? "No grounded evidence found — abstained instead of guessing."
              : (d.answer ?? last?.text ?? "");
            c[c.length - 1] = {
              role: "assistant", text,
              meta: {
                abstained: d.abstained, grounded: d.grounded, confidence: d.confidence,
                retrievers: d.retrievers, citations: d.citations, caveats: d.caveats,
                unsupported: d.unsupported_sentences, trust_score: d.trust_score, crag: d.crag,
                trace: d.trace, retrieved_context: d.retrieved_context, stage_trace: d.stage_trace,
                generation: d.generation
              },
            };
            return c;
          });
        };
        await contextV2Api.answerStream(
          q, { onToken: appendText, onMeta: ensure, onDone: finalize, onChat: joined }, undefined, opts);
        refreshThreads();   // the answer landed — re-sort the sidebar
      } else {
        const r = await contextV2Api.getContext(q, undefined, opts);
        joined(r.chat_id);
        setMsgs((m) => [...m, {
          role: "assistant",
          text: r.abstained ? "No grounded context above threshold — abstained." : r.context,
          meta: {
            abstained: r.abstained, confidence: r.confidence,
            retrievers: (r as any).retrievers, citations: r.citations, caveats: r.caveats,
            trace: r.citations, stage_trace: (r as any).stage_trace
          },
        }]);
      }
    } catch (e: any) {
      // surface the API's real reason (e.g. "No generation model is connected") instead of a generic message
      const detail = e?.response?.data?.detail || e?.response?.data?.message || e?.message || String(e);
      setMsgs((m) => [...m, { role: "assistant", text: `⚠ ${detail}` }]);
    } finally {
      setBusy(false);
    }
  };

  // Below xl the settings panel is closed until asked for; from xl up it is always shown.
  // Likewise the history sidebar below lg.
  const [showSettings, setShowSettings] = useState(false);

  return (
    <div className="flex flex-col lg:flex-row h-full min-h-[28rem] bg-white border border-stone-200 rounded-lg shadow-sm overflow-hidden">
      {/* history sidebar */}
      <aside id="chat-history" aria-label="Chats"
        className={`${showHistory ? "flex" : "hidden"} lg:flex flex-col w-full lg:w-64 max-h-[45%] lg:max-h-none border-b lg:border-b-0 lg:border-r border-stone-200 bg-stone-50/50 shrink-0 min-h-0`}>
        <ChatHistory threads={threads} activeId={chatId} loading={threadsLoading}
          query={historyQ} onQuery={setHistoryQ} onOpen={openChat} onNew={newChat}
          onRename={renameChat} onDelete={deleteChat} disabled={busy || opening} />
      </aside>

      {/* config sidebar */}
      <aside id="chat-settings" aria-label="Retrieval settings"
        className={`${showSettings ? "block" : "hidden"} xl:block order-first lg:order-last w-full lg:w-72 max-h-[45%] lg:max-h-none border-b lg:border-b-0 lg:border-l border-stone-200 bg-stone-50/50 overflow-y-auto p-4 xl:p-5 space-y-6 shrink-0 custom-scrollbar`}>
        <div className="flex items-center gap-2 text-stone-900 font-bold text-sm">
          <Sliders size={18} className="text-primary" /> Retrieval Settings
        </div>

        <div>
          <div className="text-[10px] font-bold uppercase tracking-widest text-stone-500 mb-3">Answer from</div>
          <div className="flex flex-col gap-2">
            {SCOPES.map((s) => (
              <label key={s.id}
                className={`flex items-start gap-2.5 text-xs cursor-pointer rounded-lg p-3 border shadow-sm transition-colors ${
                  scope === s.id ? "bg-primary/5 border-primary/40" : "bg-white border-stone-200 hover:border-stone-300"}`}>
                <input type="radio" name="scope" checked={scope === s.id} onChange={() => setScope(s.id)}
                  className="mt-0.5 w-3.5 h-3.5 text-primary focus:ring-primary/20 border-stone-300" />
                <span className="flex-1">
                  <span className="font-semibold text-stone-900 block">{s.label}</span>
                  <span className="block text-stone-500 mt-0.5 leading-relaxed">{s.desc}</span>
                </span>
              </label>
            ))}
          </div>
        </div>

        <div className="space-y-5 pt-2 border-t border-stone-200">
          <label className="block text-xs">
            <span className="text-stone-600 font-medium mb-2 block flex justify-between">
              Evidence blocks <b className="text-stone-900">{k}</b>
            </span>
            <input type="range" min={4} max={40} value={k} onChange={(e) => setK(+e.target.value)} className="w-full accent-primary" />
            <span className="block mt-1.5 text-[10px] text-stone-500 leading-tight">How many pieces of evidence the answer is built from.</span>
          </label>
        </div>

        <div className="pt-2 border-t border-stone-200">
          <div className="text-[10px] font-bold uppercase tracking-widest text-stone-500 mb-3">Mode</div>
          <div className="flex rounded-md border border-stone-200 overflow-hidden text-xs font-medium shadow-sm">
            {(["answer", "context"] as const).map((mm) => (
              <button key={mm} onClick={() => setMode(mm)}
                className={`flex-1 py-2 transition-colors ${mode === mm ? "bg-primary text-white" : "bg-white text-stone-600 hover:bg-stone-50"}`}>
                {mm === "answer" ? "Answer (Verified)" : "Raw Context"}
              </button>
            ))}
          </div>
        </div>
      </aside>

      {/* chat thread */}
      <div className="flex flex-col flex-1 min-w-0 min-h-0 bg-white">
        <div className="px-4 sm:px-6 py-3 sm:py-4 border-b border-stone-200 bg-white shadow-sm z-10 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <div className="min-w-0 flex-1 basis-64">
            <h1 className="text-xl font-bold tracking-tight text-stone-900 flex items-center gap-2">
              <MessageSquare size={20} className="text-primary" /> AI Assistant
            </h1>
            {chatId && (
              <p className="text-xs font-medium text-stone-700 mt-1 truncate" title={threads.find((t) => t.id === chatId)?.title}>
                {threads.find((t) => t.id === chatId)?.title}
              </p>
            )}
            <p className="text-xs text-stone-500 mt-1">Grounded answers powered by your knowledge graph. Cites sources or abstains — never guesses.</p>
          </div>
          <div className="flex flex-wrap items-center gap-2 shrink-0 max-w-full">
            <button type="button" onClick={() => setShowHistory((v) => !v)}
              aria-expanded={showHistory} aria-controls="chat-history"
              className={`lg:hidden px-3 py-2 text-xs font-semibold rounded-lg border flex items-center gap-1.5 shadow-sm ${showHistory ? "border-primary/40 bg-primary/5 text-primary" : "border-stone-200 text-stone-700 hover:bg-stone-50"}`}>
              <History size={14} aria-hidden="true" /> Chats
            </button>
            <button type="button" onClick={() => setShowSettings((v) => !v)}
              aria-expanded={showSettings} aria-controls="chat-settings"
              className={`xl:hidden px-3 py-2 text-xs font-semibold rounded-lg border flex items-center gap-1.5 shadow-sm ${showSettings ? "border-primary/40 bg-primary/5 text-primary" : "border-stone-200 text-stone-700 hover:bg-stone-50"}`}>
              <Sliders size={14} aria-hidden="true" /> Settings
            </button>
            {saveMsg && (
              <span className={`text-xs font-medium ${saveMsg.includes("fail") ? "text-red-600" : "text-emerald-600"}`}>{saveMsg}</span>
            )}
            <button
              onClick={saveConversation}
              disabled={saving || busy || msgs.length === 0}
              title="Save this conversation as searchable, citable context your AI can recall later"
              className="px-3 py-2 text-xs font-semibold rounded-lg border border-stone-200 text-stone-700 hover:bg-stone-50 disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5 shadow-sm">
              {saving ? <Loader2 size={14} className="animate-spin" /> : sessionId ? <Check size={14} className="text-emerald-500" /> : <Save size={14} />}
              {sessionId ? "Update saved" : "Save conversation"}
            </button>
          </div>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto p-3 sm:p-6 space-y-4 sm:space-y-6" style={{ background: "#f8fafc" }}>
          {opening && (
            <div className="flex items-center justify-center h-full text-stone-400 text-sm gap-2">
              <Loader2 className="animate-spin text-primary" size={16} /> Opening chat…
            </div>
          )}
          {!opening && msgs.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-center max-w-sm mx-auto">
              <div className="w-16 h-16 bg-teal-50 text-teal-500 rounded-full flex items-center justify-center mb-4">
                <MessageSquare size={32} />
              </div>
              <h2 className="text-lg font-bold text-stone-900 mb-2">How can I help?</h2>
              <p className="text-sm text-stone-500 leading-relaxed">
                Ask anything about your data. Choose what answers are drawn from in <b>Retrieval Settings</b>.
              </p>
            </div>
          )}
          {msgs.map((m, i) => (
            <div key={i} className={"flex " + (m.role === "user" ? "justify-end" : "justify-start")}>
              <div className={`max-w-3xl min-w-0 rounded-2xl px-4 sm:px-5 py-3 sm:py-4 text-sm shadow-sm ${m.role === "user"
                  ? "bg-primary text-white rounded-tr-sm"
                  : "bg-white border border-stone-200 text-stone-800 rounded-tl-sm"
                }`}>
                <div className="whitespace-pre-wrap [overflow-wrap:anywhere] leading-relaxed">{m.text}</div>
                {m.meta && !m.meta.abstained && !!m.meta.citations?.length && (
                  <SourceList citations={m.meta.citations} question={msgs[i - 1]?.role === "user" ? msgs[i - 1].text : ""} />
                )}
                {m.meta && <AssistantMeta meta={m.meta} />}
              </div>
            </div>
          ))}
          {busy && (
            <div className="flex items-center gap-2 text-stone-400 text-sm bg-white border border-stone-200 rounded-full px-4 py-2 w-fit shadow-sm">
              <Loader2 className="animate-spin text-primary" size={16} />
              <span className="font-medium">Retrieving and synthesizing...</span>
            </div>
          )}
          <div ref={endRef} />
        </div>

        <div className="bg-white p-3 sm:p-4 border-t border-stone-200">
          <div className="max-w-4xl mx-auto flex gap-3 relative">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
              placeholder="Ask a question..."
              className="flex-1 border border-stone-200 rounded-full pl-5 pr-24 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary shadow-sm transition-all"
            />
            <button onClick={send} disabled={busy || !input.trim()}
              className="absolute right-1.5 top-1.5 bottom-1.5 px-4 rounded-full bg-primary hover:bg-primary/90 text-white text-sm font-semibold disabled:opacity-50 flex items-center gap-2 transition-colors">
              <Send size={16} /> Ask
            </button>
          </div>
          <div className="text-center mt-2 text-[10px] text-stone-400">
            AI answers can be wrong. Check the citations to verify.
          </div>
        </div>
      </div>
    </div>
  );
}

// ── sources ────────────────────────────────────────────────────────────────────────────────────
// Every citation says where it came from: its asset (ranked for the question when a shared entity
// lives in many), the chunk that says it, or — for nodes that belong to no asset — their origin.
// Numbered like the answer's [n] markers, which index this same list. The records the answer
// rests on (`supports`) are listed first, grouped by the file they came from; whatever else
// retrieval considered is folded under "Other evidence", one click away.

const ORIGIN_LABEL: Record<string, string> = {
  curation: "Added by a curator", template: "Built-in template", memory: "Agent memory",
  system: "System node", unknown: "Source unknown",
};

function AssetChip({ s }: { s: CitationSource }) {
  return (
    <a href={`/dashboard/catalogue/${s.asset_id}`} title={s.snippet || s.label}
      className="inline-flex items-center gap-1 max-w-[220px] px-1.5 py-0.5 rounded border border-stone-200 bg-white text-stone-700 hover:border-primary/40 hover:text-primary">
      <FileText size={11} className="shrink-0" />
      <span className="truncate">{s.label}</span>
    </a>
  );
}

/** One cited record. `inGroup`: listed under its file's heading, so only the OTHER files it is in
 *  are shown here. */
function SourceRow({ c, n, question, inGroup = false }: { c: Citation; n: number; question: string; inGroup?: boolean }) {
  const [more, setMore] = useState<CitationSource[] | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const sources = more ?? c.sources ?? [];
  const count = total ?? c.source_count ?? sources.length;
  const loadMore = async () => {
    setLoading(true);
    try {
      const r = await contextV2Api.nodeSources(c.node_id, question, sources.length, 25);
      setMore([...sources, ...r.items.filter((x) => !sources.some((s) => s.asset_id === x.asset_id))]);
      setTotal(r.total);
    } catch { /* keep what is shown */ } finally { setLoading(false); }
  };
  const lead = sources[0];
  return (
    <li className="flex gap-2 items-start">
      <span className="shrink-0 w-5 text-right font-mono text-stone-400">[{n}]</span>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-mono text-[10px] text-stone-400">{c.type}</span>
          {inGroup
            ? (more ? sources.slice(1) : []).map((s) => <AssetChip key={s.asset_id} s={s} />)
            : sources.length ? sources.slice(0, more ? sources.length : 3).map((s) => <AssetChip key={s.asset_id} s={s} />)
            : <span className="px-1.5 py-0.5 rounded bg-stone-100 text-stone-500">{ORIGIN_LABEL[c.origin || "unknown"] || c.origin}</span>}
          {count > 1 && (inGroup
            ? <span className="text-stone-400">also in {(count - 1).toLocaleString()}{c.source_count_capped && !total ? "+" : ""} more asset{count - 1 === 1 ? "" : "s"}</span>
            : <span className="text-stone-400">in {count.toLocaleString()}{c.source_count_capped && !total ? "+" : ""} assets</span>)}
          {(count > sources.length || (c.source_count_capped && !total)) && (
            <button onClick={loadMore} disabled={loading} className="text-primary font-semibold hover:underline disabled:opacity-50">
              {loading ? "loading…" : "show more"}
            </button>
          )}
        </div>
        {lead?.snippet && <div className="text-stone-500 line-clamp-2 [overflow-wrap:anywhere]">“{lead.snippet}”</div>}
      </div>
    </li>
  );
}

function SourceList({ citations, question }: { citations: Citation[]; question: string }) {
  const [showOther, setShowOther] = useState(false);
  const rows = citations.map((c, i) => ({ c, n: i + 1 }));
  // No flag (a reply saved before support was attributed) counts as used, so old chats read as before.
  const used = rows.filter(({ c }) => c.supports !== false);
  const other = rows.filter(({ c }) => c.supports === false);
  const groups: { key: string; head: CitationSource | null; origin: string; rows: typeof rows }[] = [];
  for (const r of used) {
    const key = r.c.asset_id || `origin:${r.c.origin || "unknown"}`;
    let g = groups.find((x) => x.key === key);
    if (!g) {
      g = { key, head: r.c.asset_id ? (r.c.sources?.[0] ?? null) : null, origin: r.c.origin || "unknown", rows: [] };
      groups.push(g);
    }
    g.rows.push(r);
  }
  return (
    <div className="mt-3 pt-3 border-t border-stone-100 text-[11px] space-y-3">
      <div className="font-bold text-stone-500 uppercase tracking-wider">Sources</div>
      {groups.map((g) => (
        <div key={g.key} className="space-y-1.5">
          <div className="flex items-center gap-2">
            {g.head ? <AssetChip s={g.head} />
              : <span className="px-1.5 py-0.5 rounded bg-stone-100 text-stone-500">{ORIGIN_LABEL[g.origin] || g.origin}</span>}
            <span className="text-stone-400">{g.rows.length} record{g.rows.length === 1 ? "" : "s"}</span>
          </div>
          <ol className="space-y-2 pl-1">
            {g.rows.map(({ c, n }) => <SourceRow key={`${c.node_id}-${n}`} c={c} n={n} question={question} inGroup />)}
          </ol>
        </div>
      ))}
      {!!other.length && (
        <div>
          <button onClick={() => setShowOther((v) => !v)} aria-expanded={showOther}
            className="inline-flex items-center gap-1 text-stone-500 font-semibold hover:text-stone-800">
            {showOther ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            Other evidence ({other.length})
          </button>
          {showOther && (
            <ol className="space-y-2 mt-2 opacity-80">
              {other.map(({ c, n }) => <SourceRow key={`${c.node_id}-${n}`} c={c} n={n} question={question} />)}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}

function AssistantMeta({ meta }: { meta: NonNullable<Msg["meta"]> }) {
  const [open, setOpen] = useState(false);
  const trace = meta.trace || [];
  const retrievers = meta.retrievers || [];
  const crag = meta.crag;

  const Step = ({ label, ok }: { label: string; ok?: boolean }) => (
    <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${ok === undefined ? "bg-stone-100 text-stone-500" : ok ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>{label}</span>
  );

  return (
    <div className="mt-3 pt-3 border-t border-stone-100 text-[11px] space-y-1.5 text-stone-500">
      <div className="flex flex-wrap items-center gap-2">
        {meta.abstained
          ? <span className="inline-flex items-center gap-1 font-medium text-amber-600 bg-amber-50 px-1.5 py-0.5 rounded"><AlertTriangle size={12} /> Abstained</span>
          : <span className="inline-flex items-center gap-1 font-medium text-emerald-600 bg-emerald-50 px-1.5 py-0.5 rounded"><ShieldCheck size={12} /> Grounded</span>}
        {typeof meta.confidence === "number" && <span className="font-medium bg-stone-100 px-1.5 py-0.5 rounded text-stone-600">conf {meta.confidence.toFixed(2)}</span>}
        {typeof meta.trust_score === "number" && <span className="font-medium bg-stone-100 px-1.5 py-0.5 rounded text-stone-600">CRAG {meta.trust_score.toFixed(2)}</span>}
        {!!meta.citations?.length && <span className="font-medium bg-teal-50 text-teal-600 px-1.5 py-0.5 rounded">{meta.citations.length} citations</span>}
        <button onClick={() => setOpen((o) => !o)} className="ml-auto text-primary font-semibold inline-flex items-center gap-1 hover:underline">
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />} Retrieval flow
        </button>
      </div>

      {retrievers.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1">
          {retrievers.map((r) => <span key={r} className="px-1.5 py-0.5 rounded text-[10px] uppercase font-bold tracking-wide bg-stone-100 text-stone-400">{r}</span>)}
        </div>
      )}
      {!!meta.caveats?.length && meta.caveats.map((c, j) => <div key={j} className="text-amber-600 font-medium mt-1">⚠ {c}</div>)}
      {!!meta.unsupported?.length && <div className="text-rose-500 font-medium mt-1">⚠ {meta.unsupported.length} sentence(s) not fully grounded.</div>}

      {open && (
        <div className="mt-2 space-y-3 bg-stone-50 rounded-lg p-3 border border-stone-100">
          {/* pipeline flow */}
          <div className="flex items-center gap-1.5 flex-wrap">
            <Step label={`${retrievers.length} retriever${retrievers.length === 1 ? "" : "s"}`} ok={retrievers.length > 0} />
            <ArrowRight size={11} className="text-stone-300" />
            <Step label={`${trace.length} candidate${trace.length === 1 ? "" : "s"}`} ok={trace.length > 0} />
            <ArrowRight size={11} className="text-stone-300" />
            {crag ? <Step label={`CRAG ${crag.verdict}`} ok={crag.verdict === "accepted"} /> : <Step label="CRAG skipped" />}
            <ArrowRight size={11} className="text-stone-300" />
            <Step label={meta.abstained ? "Abstained" : "Answered"} ok={!meta.abstained} />
          </div>

          {/* live stage-by-stage pipeline trace (query → recall → rerank → collapse → order → assemble) */}
          {!!meta.stage_trace?.length && (
            <div className="space-y-1">
              <div className="font-bold text-stone-600 text-[11px]">Pipeline trace</div>
              {meta.stage_trace!.map((s, i) => (
                <div key={i} className={`flex gap-2 items-baseline ${s.degraded ? "text-rose-600 font-medium" : "text-stone-600"}`}>
                  <span className="font-mono text-[10px] uppercase tracking-wide bg-stone-100 px-1.5 py-0.5 rounded shrink-0">{s.stage}</span>
                  <span className="min-w-0 text-[11px] leading-relaxed [overflow-wrap:anywhere]">{s.detail}</span>
                </div>
              ))}
            </div>
          )}

          {/* generation: model + whether the CoT <think> scaffold is active + the actual prompt */}
          {meta.generation && (
            <div className="space-y-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-bold text-stone-600 text-[11px]">Generation</span>
                {meta.generation.model && <span className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-stone-100 text-stone-500">{meta.generation.model}</span>}
                <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${meta.generation.cot ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>
                  CoT {meta.generation.cot ? "on" : "off"}
                </span>
                {meta.generation.template && <span className="text-[10px] text-stone-400">{meta.generation.template}</span>}
              </div>
              {meta.generation.instructions && (
                <pre className="text-[10px] text-stone-500 bg-white border border-stone-100 rounded p-2 whitespace-pre-wrap max-h-40 overflow-auto">{meta.generation.instructions}</pre>
              )}
            </div>
          )}

          {/* CRAG detail */}
          {crag && (
            <div className="text-[11px] text-stone-600">
              <span className="font-bold">CRAG relevance:</span> {crag.verdict}
              {typeof crag.score === "number" && typeof crag.threshold === "number" &&
                <> · score {crag.score.toFixed(2)} vs threshold {crag.threshold.toFixed(2)}</>}
              {crag.reason && <div className="text-stone-500 mt-0.5">{crag.reason}</div>}
            </div>
          )}

          {/* retrieved candidates (what was the response/context) */}
          {trace.length > 0 ? (
            <div>
              <div className="font-bold text-stone-500 uppercase tracking-wider mb-1">Retrieved context{meta.abstained ? " (rejected)" : ""}</div>
              <ul className="space-y-1 max-h-60 overflow-y-auto">
                {trace.slice(0, 20).map((c: any, i: number) => (
                  <li key={i} className="bg-white border border-stone-200 rounded p-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[10px] text-stone-500 truncate">
                        <span className="font-mono">{c.type || c.node_type || "node"}</span>
                        {c.label ? <span className="text-stone-800 font-medium"> · {c.label}</span> : null}
                      </span>
                      <span className="flex items-center gap-2 shrink-0">
                        {typeof c.score === "number" && <span className="text-[10px] text-stone-400">score {c.score.toFixed(3)}</span>}
                        {(c.asset_id || c.node_id) && <a href={`/dashboard/catalogue/${c.asset_id || c.node_id}`} className="text-[10px] text-primary font-semibold hover:underline">open</a>}
                      </span>
                    </div>
                    {(c.text || c.snippet) && <div className="text-stone-600 mt-0.5 line-clamp-3 [overflow-wrap:anywhere]">{c.text || c.snippet}</div>}
                  </li>
                ))}
                {trace.length > 20 && <li className="text-[10px] text-stone-400">+{trace.length - 20} more…</li>}
              </ul>
            </div>
          ) : (
            <div className="text-stone-400">No context was retrieved for this query.</div>
          )}

          {meta.retrieved_context && (
            <div>
              <div className="font-bold text-stone-500 uppercase tracking-wider mb-1">Context the evaluator saw</div>
              <pre className="bg-white border border-stone-200 rounded p-2 text-[10px] text-stone-600 whitespace-pre-wrap max-h-40 overflow-y-auto">{meta.retrieved_context}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
