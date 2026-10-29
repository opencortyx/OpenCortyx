"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { FULL_UI } from "@/lib/simple";

/**
 * Review, as one place: what needs a decision, the types in the data, hand edits, and what could not
 * be read. In the simple product (lib/simple.ts) only Review is in the sidebar; these tabs reach the
 * other three. With the full UI each is its own sidebar entry, and this renders nothing.
 */
const TABS = [
  { href: "/dashboard/review", label: "Decisions" },
  { href: "/dashboard/schema", label: "Types" },
  { href: "/dashboard/curate", label: "Hand edits" },
  { href: "/dashboard/quarantine", label: "Not read" },
];

export default function ReviewTabs() {
  const path = usePathname();
  if (FULL_UI) return null;
  return (
    <nav aria-label="Review" className="flex flex-wrap gap-1 border-b border-stone-200 dark:border-stone-800 mb-4">
      {TABS.map((t) => {
        const active = path?.startsWith(t.href);
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
