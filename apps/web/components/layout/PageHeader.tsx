"use client";
import { Info, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

/**
 * The single, unified header for every top-level dashboard page.
 *
 * Professional + minimal (Atlan / DataHub house style): a subtle icon tile, a
 * clean title, and an optional right-side actions slot. The description is NOT
 * shown inline — it hides behind a small ⓘ next to the title and appears on
 * hover/focus, so the page stays uncluttered but the context is one glance away.
 */
export default function PageHeader({
  title,
  description,
  icon: Icon,
  actions,
}: {
  title: string;
  description?: string;
  icon?: LucideIcon;
  actions?: ReactNode;
}) {
  return (
    <div className="relative flex flex-wrap items-start justify-between gap-x-4 gap-y-3 border-b border-stone-200 pb-4 mb-4 sm:mb-6">
      <div className="flex items-center gap-3 min-w-0 max-w-full">
        {Icon && (
          <div className="p-2 rounded-xl bg-primary/10 text-primary ring-1 ring-primary/15 shrink-0">
            <Icon size={22} />
          </div>
        )}
        <h1 className="text-2xl font-semibold tracking-tight text-stone-900 truncate">{title}</h1>

        {description && (
          <span className="group inline-flex shrink-0" tabIndex={0}>
            <Info
              size={15}
              className="text-stone-400 hover:text-stone-600 focus:text-stone-600 cursor-help mt-0.5 transition-colors"
              aria-label={description}
            />
            {/* hover/focus tooltip — hidden until the ⓘ is hovered or focused */}
            <span
              role="tooltip"
              className="pointer-events-none absolute left-0 top-full z-30 mt-2 w-72 max-w-full
                         rounded-lg bg-stone-900 px-3 py-2 text-xs leading-relaxed text-stone-100 shadow-xl
                         opacity-0 translate-y-1 transition-all duration-150
                         group-hover:opacity-100 group-hover:translate-y-0
                         group-focus-within:opacity-100 group-focus-within:translate-y-0"
            >
              {description}
            </span>
          </span>
        )}
      </div>

      {actions && <div className="flex flex-wrap items-center gap-2 max-w-full">{actions}</div>}
    </div>
  );
}
