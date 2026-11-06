"use client";
import { useEffect, useRef, useState, type Ref } from "react";
import { useRouter } from "next/navigation";
import { useSession, signOut } from "next-auth/react";
import { FileText, MessageSquare, Search, User, Menu } from "lucide-react";
import { contextV2Api } from "@/lib/api/contextV2";

/** What the header search lists: files (assets), which open on their own page. */
const ASSET_TYPES = "Source,Table,StructuredAsset,SemiStructuredAsset,UnstructuredAsset";

export function DashboardHeader({ onMenu, navOpen = false, menuRef }: { onMenu?: () => void; navOpen?: boolean; menuRef?: Ref<HTMLButtonElement> }) {
  const { data: session } = useSession();
  const router = useRouter();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<{ id: string; label: string | null; type: string }[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const text = q.trim();
    if (text.length < 2) { setHits([]); return; }
    const t = setTimeout(() => {
      contextV2Api.searchNodes(text, ASSET_TYPES, 8).then((r: any) => setHits(r?.results || [])).catch(() => setHits([]));
    }, 200);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  // the files found, then "ask the Assistant" - the last row, always there for a question
  const go = (i: number) => {
    const text = q.trim();
    if (!text) return;
    setOpen(false);
    if (i >= 0 && i < hits.length) router.push(`/dashboard/catalogue/${hits[i].id}` as any);
    else router.push(`/dashboard/chat?q=${encodeURIComponent(text)}` as any);
    setQ("");
  };

  return (
    <header className="h-16 border-b bg-card flex items-center gap-2 sm:gap-4 px-3 sm:px-6 shadow-sm z-10">
      {/* Below lg the sidebar is a drawer, and this is the only way to open it. */}
      <button ref={menuRef} type="button" onClick={onMenu} aria-label="Open navigation"
        aria-expanded={navOpen} aria-controls="dashboard-nav"
        className="lg:hidden p-2 -ml-1 rounded-md text-stone-500 hover:text-stone-800 hover:bg-stone-100 shrink-0">
        <Menu size={20} aria-hidden="true" />
      </button>
      <div className="flex-1 min-w-0 max-w-2xl" ref={box}>
        <div className="relative group">
          <Search size={16} aria-hidden="true" className="absolute left-3 top-1/2 -translate-y-1/2 text-stone-400 group-focus-within:text-primary transition-colors" />
          <input
            type="search"
            role="combobox"
            aria-label="Search files, or ask a question"
            aria-expanded={open && q.trim().length >= 2}
            aria-controls="header-search-results"
            value={q}
            onChange={(e) => { setQ(e.target.value); setOpen(true); setActive(-1); }}
            onFocus={() => setOpen(true)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, hits.length)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, -1)); }
              else if (e.key === "Enter") { e.preventDefault(); go(active >= 0 ? active : hits.length ? 0 : -1); }
              else if (e.key === "Escape") setOpen(false);
            }}
            placeholder="Search files, or ask a question…"
            className="w-full min-w-0 bg-stone-50 border border-stone-200 rounded-md py-2 pl-10 pr-4 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary transition-all"
          />
          {open && q.trim().length >= 2 && (
            <ul id="header-search-results" role="listbox"
                className="absolute left-0 right-0 top-full mt-1 bg-white border border-stone-200 rounded-md shadow-lg overflow-hidden z-50">
              {hits.map((h, i) => (
                <li key={h.id} role="option" aria-selected={active === i}>
                  <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => go(i)}
                    className={`w-full text-left px-3 py-2 text-sm flex items-center gap-2 ${active === i ? "bg-stone-100" : "hover:bg-stone-50"}`}>
                    <FileText size={14} className="text-stone-400 shrink-0" /><span className="truncate">{h.label || "(untitled)"}</span>
                  </button>
                </li>
              ))}
              <li role="option" aria-selected={active === hits.length}>
                <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => go(-1)}
                  className={`w-full text-left px-3 py-2 text-sm flex items-center gap-2 border-t border-stone-100 text-primary ${active === hits.length ? "bg-stone-100" : "hover:bg-stone-50"}`}>
                  <MessageSquare size={14} className="shrink-0" /><span className="truncate">Ask the Assistant: “{q.trim()}”</span>
                </button>
              </li>
            </ul>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 sm:gap-3 ml-auto shrink-0">
        <div className="flex flex-col items-end">
          {/* The name gives way on a phone; Sign out never does. */}
          <span className="hidden sm:block text-sm font-semibold text-stone-800 leading-tight">
            {session?.user?.email ? session.user.email.split('@')[0] : 'Administrator'}
          </span>
          <button onClick={() => signOut()} className="text-xs text-stone-500 hover:text-primary transition-colors">
            Sign out
          </button>
        </div>
        <div aria-hidden="true" className="h-9 w-9 rounded-full bg-primary/10 flex items-center justify-center text-primary border border-primary/20">
          <User size={18} />
        </div>
      </div>
    </header>
  );
}
