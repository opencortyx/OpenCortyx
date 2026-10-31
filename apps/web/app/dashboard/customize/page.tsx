"use client";

import Link from "next/link";
import { CalendarClock, GitBranch, SlidersHorizontal, Wand2, Workflow } from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";

/**
 * Customize — the one door to shaping how data is read and connected, for people who want more than
 * the defaults. Nothing here is a step anyone has to take: every file is read, detected and connected
 * without it. Each card opens the page that does the work.
 */
const CARDS = [
  { href: "/dashboard/mapper", icon: Workflow, title: "Mappings",
    text: "What your data means, found after each upload. Correct an identifier, a link or a name, or shape the graph yourself.",
    action: "Open mappings" },
  { href: "/dashboard/pipelines", icon: GitBranch, title: "Saved setups",
    text: "Mappings and extractors saved together from Add data, to reuse in scheduled syncs.",
    action: "Open saved setups" },
  { href: "/dashboard/custom-assets", icon: SlidersHorizontal, title: "Extractors",
    text: "Your own extractors: what to pull out of documents and records, with a model, a pattern or a list.",
    action: "Open extractors" },
  { href: "/dashboard/ingest", icon: CalendarClock, title: "Scheduled syncs",
    text: "Read a connection again on demand, on a schedule or as it changes, with the pipeline you choose.",
    action: "Open scheduled syncs" },
];

export default function CustomizePage() {
  return (
    <div className="max-w-4xl mx-auto">
      <PageHeader title="Customize" icon={Wand2}
        description="Shape how your data is read and connected. Optional: everything works without it." />
      <div className="grid gap-3 sm:grid-cols-2">
        {CARDS.map((c) => (
          <Link key={c.href} href={c.href as any}
                className="group rounded-xl border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900 p-4
                           hover:border-stone-300 dark:hover:border-stone-700 transition-colors">
            <div className="flex items-center gap-2">
              <c.icon size={18} className="text-primary" aria-hidden="true" />
              <h2 className="font-semibold text-stone-900 dark:text-stone-100">{c.title}</h2>
            </div>
            <p className="mt-1.5 text-sm text-stone-500 dark:text-stone-400">{c.text}</p>
            <p className="mt-3 text-sm font-medium text-primary group-hover:underline">{c.action} →</p>
          </Link>
        ))}
      </div>
    </div>
  );
}
