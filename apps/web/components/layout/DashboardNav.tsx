"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Logo } from "@/components/brand/Logo";
import { contextV2Api } from "@/lib/api/contextV2";
import {
  MessageSquare, 
  UploadCloud, 
  Library, 
  Share2,
  SlidersHorizontal,
  Settings,
  HelpCircle,
  AlertCircle,
  Activity,
  GitBranch,
  Cable,
  Cpu,
  HeartPulse,
  PenSquare,
  Inbox,
  FlaskConical,
  MessagesSquare,
  UserCog,
  Network,
  LayoutGrid,
  Workflow,
  Fingerprint, Sparkles, X } from "lucide-react";

const SECTIONS = [
  {
    title: "Discover",
    items: [
      { href: "/dashboard/catalogue", label: "Assets", icon: Library },
      { href: "/dashboard/graph", label: "Lineage", icon: Share2 },
    ],
  },
  {
    title: "Trust",
    items: [
      { href: "/dashboard/curate", label: "Curation", icon: PenSquare },
      { href: "/dashboard/schema", label: "Nodes & Relationships", icon: Network },
      { href: "/dashboard/review", label: "Review Queue", icon: Inbox },
      { href: "/dashboard/eval", label: "Evaluations", icon: FlaskConical },
      { href: "/dashboard/quarantine", label: "Quarantine", icon: AlertCircle },
    ],
  },
  {
    title: "Integration",
    items: [
      // First in the section, and before the builders, because it is the path that needs no
      // configuration: it reads a file, shows what it would do, and shows what came out. Mapper
      // and Pipelines stay exactly where they were — they are the refinement, reachable from a
      // result rather than the step everyone assumes they must complete first.
      { href: "/dashboard/add", label: "Add data", icon: Sparkles },
      { href: "/dashboard/connectors", label: "Connections", icon: Cable },
      { href: "/dashboard/mapper", label: "Mapper", icon: Workflow },
      { href: "/dashboard/custom-assets", label: "Custom Extractors", icon: SlidersHorizontal },
      { href: "/dashboard/pipelines", label: "Pipelines", icon: GitBranch },
      { href: "/dashboard/ingest", label: "Ingestion", icon: UploadCloud },
      { href: "/dashboard/progress", label: "Runs", icon: Activity },
    ],
  },
  {
    title: "Intelligence",
    items: [
      { href: "/dashboard/chat", label: "AI Assistant", icon: MessageSquare },
      { href: "/dashboard/sessions", label: "Conversations", icon: MessagesSquare },
    ],
  },
  {
    title: "Administration",
    items: [
      { href: "/dashboard/users", label: "Users & Access", icon: UserCog, adminOnly: true },
      { href: "/dashboard/models", label: "Models", icon: Cpu, adminOnly: true },
      { href: "/dashboard/health", label: "System Health", icon: HeartPulse },
    ],
  },
];

export function DashboardNav({ open = false, onClose, onNavigate }: { open?: boolean; onClose?: () => void; onNavigate?: () => void }) {
  const path = usePathname();
  const [quarantineCount, setQuarantineCount] = useState(0);
  const [role, setRole] = useState<string>("");

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const d = await contextV2Api.getQuarantine();
        if (alive) setQuarantineCount((d?.items || []).length);
      } catch { /* ignore */ }
    };
    tick();
    contextV2Api.authMe().then((r) => { if (alive) setRole(r?.role || ""); }).catch(() => {});
    const t = setInterval(tick, 30000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const isAdmin = role === "admin";

  // Opening the drawer moves focus into it, so a keyboard user is inside it rather than behind it.
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (open) closeRef.current?.focus(); }, [open]);

  const renderLink = ({ href, label, icon: Icon }: any) => {
    const isActive = path?.startsWith(href) || false;
    return (
      <li key={href}>
        <Link
          href={href}
          onClick={onNavigate}
          className={`flex items-center gap-3 px-3 py-2.5 rounded-md text-sm font-medium transition-all ${
            isActive
              ? "bg-primary/10 text-primary shadow-[inset_2px_0_0_0_hsl(var(--primary))]"
              : "text-muted-foreground hover:bg-secondary hover:text-foreground"
          }`}
        >
          <Icon size={18} className={isActive ? "text-primary" : "text-muted-foreground/70"} />
          {label}
          {href === "/dashboard/quarantine" && quarantineCount > 0 && (
            <span className="ml-auto text-[10px] font-bold bg-red-500 text-white rounded-full px-1.5 py-0.5 min-w-[18px] text-center">
              {quarantineCount}
            </span>
          )}
        </Link>
      </li>
    );
  };

  return (
    <>
      {/* Below lg this is a drawer over the page. `invisible` while closed is not decoration: a nav
          that is only translated off-screen still takes keyboard focus, so Tab would walk through
          thirty links nobody can see. Closing, visibility transitions with the transform, so the drawer
          stays visible while it slides out and leaves the tab order once it has gone. Opening, it is
          visible AT ONCE: a hidden-to-visible transition still reads `hidden` on its first frame, which is
          when the focus below runs — measured, focus stayed on the menu button behind the drawer. From lg up it is the
          permanent column it always was (`relative` is what makes its z-10 apply). */}
      {open && <div className="fixed inset-0 z-30 bg-stone-900/40 lg:hidden" onClick={onClose} aria-hidden="true" />}
      <nav id="dashboard-nav" aria-label="Main navigation"
        className={`fixed inset-y-0 left-0 z-40 w-64 max-w-[85vw] border-r bg-card flex flex-col h-dvh shadow-lg duration-200 ease-out motion-reduce:transition-none ${open ? "translate-x-0 visible transition-transform" : "-translate-x-full invisible transition-[transform,visibility]"} lg:relative lg:z-10 lg:max-w-none lg:translate-x-0 lg:visible lg:shadow-sm`}>
      <div className="h-16 flex items-center justify-between px-5 border-b">
        <Link href="/dashboard/catalogue" onClick={onNavigate} aria-label="OpenCortyx home">
          <Logo />
        </Link>
        <button ref={closeRef} type="button" onClick={onClose} aria-label="Close navigation"
          className="lg:hidden p-1.5 -mr-1.5 rounded-md text-stone-400 hover:text-stone-700 hover:bg-stone-100">
          <X size={18} aria-hidden="true" />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto py-4">
        {SECTIONS.map((section) => {
          const items = section.items.filter((it: any) => !it.adminOnly || isAdmin);
          if (items.length === 0) return null;
          return (
            <div key={section.title} className="mb-6">
              <div className="px-4 mb-2 text-[11px] font-semibold text-muted-foreground/70 uppercase tracking-wider">
                {section.title}
              </div>
              <ul className="px-3 space-y-1">{items.map(renderLink)}</ul>
            </div>
          );
        })}
      </div>
      
      <div className="p-4 border-t bg-secondary/40">
        <ul className="space-y-1">
          {isAdmin && (
            <li>
              <Link href="/dashboard/settings" onClick={onNavigate} className="flex items-center gap-3 px-3 py-2 rounded-md text-sm font-medium text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors">
                <Settings size={18} className="text-muted-foreground/70" /> Settings
              </Link>
            </li>
          )}
          <li>
            <Link href="#" onClick={onNavigate} className="flex items-center gap-3 px-3 py-2 rounded-md text-sm font-medium text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors">
              <HelpCircle size={18} className="text-muted-foreground/70" /> Support
            </Link>
          </li>
        </ul>
      </div>
    </nav>
    </>
  );
}
