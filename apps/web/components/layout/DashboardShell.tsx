"use client";

/**
 * The dashboard frame: sidebar, header, content.
 *
 * Below `lg` (1024px) the sidebar is a DRAWER. It used to be a permanent 256px column at every width
 * with no way to collapse it, so every page got `viewport - 256px`: 512px on a tablet, about 120px on
 * a phone, and a page with a side column of its own (the assistant's 320px settings panel) had almost
 * nothing left. One line of layout, inherited by every screen — which is why it looked like a
 * platform-wide problem: it was one.
 *
 * `min-w-0` is what lets the content column shrink below its children's intrinsic width; without it a
 * flex child refuses to be narrower than its widest descendant and pushes the page off-screen.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { usePathname } from "next/navigation";
import { DashboardNav } from "./DashboardNav";
import { DashboardHeader } from "./DashboardHeader";

export function DashboardShell({ children }: { children: ReactNode }) {
  const [navOpen, setNavOpen] = useState(false);
  const menuRef = useRef<HTMLButtonElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const path = usePathname();

  // Dismissed (Escape, the backdrop, the close button): focus returns to the button that opened it,
  // so a keyboard user is not dropped at the top of the document.
  const dismiss = useCallback(() => {
    setNavOpen(false);
    // Lift `inert` now, not after the re-render: an inert element cannot take focus, so the focus
    // below would otherwise fail silently and leave the keyboard user nowhere.
    if (contentRef.current) contentRef.current.inert = false;
    menuRef.current?.focus();
  }, []);
  // Navigated (a link — including the page you are already on — or back/forward): the drawer closes
  // and the page that is now showing keeps focus.
  const closeForNavigation = useCallback(() => setNavOpen(false), []);
  useEffect(() => { setNavOpen(false); }, [path]);

  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") dismiss(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen, dismiss]);

  useEffect(() => {
    if (contentRef.current) contentRef.current.inert = navOpen;
  }, [navOpen]);

  // The drawer exists only below lg (Tailwind's 1024px). Widening the window past it with the drawer
  // open would leave `navOpen` set — and the page inert — with no drawer on screen to close.
  useEffect(() => {
    const lg = window.matchMedia("(min-width: 1024px)");
    const onChange = (e: MediaQueryListEvent) => { if (e.matches) setNavOpen(false); };
    lg.addEventListener("change", onChange);
    return () => lg.removeEventListener("change", onChange);
  }, []);

  return (
    <div className="flex h-dvh bg-stone-50 font-sans text-stone-900">
      <DashboardNav open={navOpen} onClose={dismiss} onNavigate={closeForNavigation} />
      <div ref={contentRef} className="flex flex-col flex-1 min-w-0 overflow-hidden relative">
        <DashboardHeader onMenu={() => setNavOpen(true)} navOpen={navOpen} menuRef={menuRef} />
        <main className="flex-1 min-w-0 overflow-y-auto p-3 sm:p-4 lg:p-6">{children}</main>
      </div>
    </div>
  );
}
