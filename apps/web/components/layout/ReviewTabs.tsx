"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { FULL_UI } from "@/lib/simple";

/**
 * Review, as one place: what needs a decision, how good matching is (Quality), hand edits, and what
 * could not be read. In the simple product (lib/simple.ts) only Review is in the sidebar, so every tab
 * shows; with the full UI Hand edits and Not read are sidebar entries of their own, and Decisions and
 * Quality stay here (Quality has no sidebar entry in either).
 */
const TABS = [
  { href: "/dashboard/review", label: "Decisions", exact: true, always: true },
  { href: "/dashboard/review/quality", label: "Quality", exact: false, always: true },
  { href: "/dashboard/curate", label: "Hand edits", exact: false, always: false },
  { href: "/dashboard/quarantine", label: "Not read", exact: false, always: false },
];

export default function ReviewTabs() {
  const path = usePathname();
  // Full UI: Hand edits and Not read are pages of their own, with no Review tabs over them.
  if (FULL_UI && !path?.startsWith("/dashboard/review")) return null;
  const tabs = TABS.filter((t) => t.always || !FULL_UI);
  return (
    <nav aria-label="Review" className="flex flex-wrap gap-1 border-b border-stone-200 dark:border-stone-800 mb-4">
      {tabs.map((t) => {
        const active = t.exact ? path === t.href : !!path?.startsWith(t.href);
        return (
          <Link key={t.href} href={t.href as any} aria-current={active ? "page" : undefined}
                className={`px-3 py-2 text-sm font-medium border-b-2 -mb-px ${active
                  ? "border-primary text-primary" : "border-transparent text-stone-500 hover:text-stone-800"}`}>
            {t.label}
          </Link>
        );
      })}
    </nav>
  );
}
