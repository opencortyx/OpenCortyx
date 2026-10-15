"use client";
import { useEffect, useRef, useState } from "react";
import { type ChatThread } from "@/lib/api/contextV2";
import { Plus, Search, Pencil, Trash2, Check, X, MessageSquare, Loader2 } from "lucide-react";

// The Assistant's conversations, grouped the way people remember them: by when they last talked.
const GROUPS: { label: string; days: number }[] = [
  { label: "Today", days: 0 }, { label: "Yesterday", days: 1 },
  { label: "Previous 7 days", days: 7 }, { label: "Previous 30 days", days: 30 },
];

function groupOf(iso: string | null, now: Date): string {
  if (!iso) return "Older";
  const d = new Date(iso);
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86_400_000);
  for (const g of GROUPS) if (days <= g.days) return g.label;
  return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

export default function ChatHistory({
  threads, activeId, loading, query, onQuery, onOpen, onNew, onRename, onDelete, disabled,
}: {
  threads: ChatThread[]; activeId: string | null; loading: boolean; query: string;
  onQuery: (q: string) => void; onOpen: (id: string) => void; onNew: () => void;
  onRename: (id: string, title: string) => void; onDelete: (t: ChatThread) => void; disabled?: boolean;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { if (editing) inputRef.current?.select(); }, [editing]);

  const commit = () => {
    if (editing && draft.trim()) onRename(editing, draft.trim());
    setEditing(null);
  };

  const now = new Date();
  const groups: [string, ChatThread[]][] = [];
  for (const t of threads) {
    const g = groupOf(t.updated_at, now);
    const last = groups[groups.length - 1];
    if (last && last[0] === g) last[1].push(t); else groups.push([g, [t]]);
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="p-3 space-y-2 border-b border-stone-200">
        <button onClick={onNew} disabled={disabled}
          className="w-full flex items-center justify-center gap-2 px-3 py-2 text-sm font-semibold rounded-lg bg-primary text-white hover:bg-primary/90 disabled:opacity-50 shadow-sm">
          <Plus size={16} aria-hidden="true" /> New chat
        </button>
        <label className="relative block">
          <span className="sr-only">Search chats</span>
          <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-stone-400" aria-hidden="true" />
          <input value={query} onChange={(e) => onQuery(e.target.value)} placeholder="Search chats"
            className="w-full pl-8 pr-2 py-1.5 text-xs border border-stone-200 rounded-md bg-white focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary" />
        </label>
      </div>

      <nav aria-label="Chat history" className="flex-1 min-h-0 overflow-y-auto p-2 custom-scrollbar">
        {loading && !threads.length && (
          <div className="flex items-center gap-2 text-xs text-stone-400 p-3"><Loader2 size={14} className="animate-spin" /> Loading chats…</div>
        )}
        {!loading && !threads.length && (
          <div className="text-xs text-stone-400 p-3 leading-relaxed">
            {query ? "No chats match." : "Your conversations appear here, so you can come back to any of them."}
          </div>
        )}
        {groups.map(([label, items]) => (
          <div key={label} className="mb-3">
            <div className="px-2 pb-1 text-[10px] font-bold uppercase tracking-widest text-stone-400">{label}</div>
            <ul className="space-y-0.5">
              {items.map((t) => {
                const active = t.id === activeId;
                return (
                  <li key={t.id} className={`group relative rounded-md ${active ? "bg-primary/10" : "hover:bg-stone-100"}`}>
                    {editing === t.id ? (
                      <div className="flex items-center gap-1 p-1">
                        <input ref={inputRef} value={draft} onChange={(e) => setDraft(e.target.value)}
                          onKeyDown={(e) => { if (e.key === "Enter") commit(); if (e.key === "Escape") setEditing(null); }}
                          aria-label="Chat title"
                          className="flex-1 min-w-0 px-2 py-1 text-xs border border-primary/40 rounded bg-white focus:outline-none" />
                        <button onClick={commit} aria-label="Save title" className="p-1 text-emerald-600 hover:bg-white rounded"><Check size={14} /></button>
                        <button onClick={() => setEditing(null)} aria-label="Cancel" className="p-1 text-stone-400 hover:bg-white rounded"><X size={14} /></button>
                      </div>
                    ) : (
                      <>
                        <button onClick={() => onOpen(t.id)} disabled={disabled} title={t.title}
                          aria-current={active ? "page" : undefined}
                          className={`w-full text-left flex items-center gap-2 px-2 py-2 pr-14 text-xs rounded-md disabled:cursor-not-allowed ${active ? "text-primary font-semibold" : "text-stone-700"}`}>
                          <MessageSquare size={13} className="shrink-0 opacity-50" aria-hidden="true" />
                          <span className="truncate">{t.title}</span>
                        </button>
                        <div className={`absolute right-1 top-1/2 -translate-y-1/2 flex gap-0.5 ${active ? "flex" : "hidden group-hover:flex group-focus-within:flex"}`}>
                          <button onClick={() => { setEditing(t.id); setDraft(t.title); }} aria-label={`Rename ${t.title}`}
                            className="p-1 rounded text-stone-400 hover:text-stone-700 hover:bg-white"><Pencil size={12} /></button>
                          <button onClick={() => onDelete(t)} disabled={disabled} aria-label={`Delete ${t.title}`}
                            className="p-1 rounded text-stone-400 hover:text-red-600 hover:bg-white disabled:opacity-40"><Trash2 size={12} /></button>
                        </div>
                      </>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>
    </div>
  );
}
