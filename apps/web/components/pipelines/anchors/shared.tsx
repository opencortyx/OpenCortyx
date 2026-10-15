"use client";

/** Small shared bits for the three Stage-1 anchors, so they look like one form with three fillings. */

export const field =
  "w-full px-2.5 py-2 rounded-lg border border-stone-200 bg-white text-[13px] text-stone-800 " +
  "focus:outline-none focus:ring-2 focus:ring-primary/25 focus:border-primary/40";

export interface MappingOption { id: string; name: string; summary?: any }

export function AnchorSection({ title, hint, children }: {
  title: string; hint?: string; children: React.ReactNode;
}) {
  return (
    <section className="border border-stone-200 rounded-xl bg-white overflow-hidden">
      <header className="px-4 py-2.5 border-b border-stone-100 bg-stone-50/70">
        <h4 className="text-[12px] font-semibold uppercase tracking-wider text-stone-600">{title}</h4>
        {hint && <p className="text-[11.5px] text-stone-500 mt-0.5">{hint}</p>}
      </header>
      <div className="p-4 space-y-3">{children}</div>
    </section>
  );
}

export const Row = ({ children }: { children: React.ReactNode }) => (
  <div className="grid md:grid-cols-2 gap-3">{children}</div>
);

export function Field({ label, hint, children }: {
  label: string; hint?: string; children: React.ReactNode;
}) {
  return (
    <div>
      <label className="block text-[10px] font-semibold uppercase tracking-wider text-stone-500 mb-1">
        {label}{hint && <span className="ml-1 normal-case font-normal text-stone-400">{hint}</span>}
      </label>
      {children}
    </div>
  );
}
