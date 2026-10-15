"use client";

import { useEffect, useState } from "react";
import { Boxes, Info } from "lucide-react";
import { contextV2Api } from "@/lib/api/contextV2";

type ShapeNode = { type: string; role?: string; attributes?: string[]; measured?: string[] };
type Shape = { label: string; configured_in?: string; nodes?: ShapeNode[]; edges?: any[] };

export default function ReadingShape({ paradigm, accent = "emerald" }: {
  paradigm: "document" | "semistructured" | "structured";
  accent?: "emerald" | "violet" | "teal";
}) {
  const [shape, setShape] = useState<Shape | null>(null);

  useEffect(() => {
    contextV2Api.getReadingShapes()
      .then((all: any) => setShape(all?.[paradigm] || null))
      .catch(() => setShape(null));
  }, [paradigm]);

  if (!shape) return null;
  const tone = accent === "violet" ? "violet" : accent === "teal" ? "teal" : "emerald";

  return (
    <details className="mt-3 rounded-lg border border-stone-200 bg-stone-50/60 overflow-hidden">
      <summary className="px-3 py-2 flex items-center gap-2 cursor-pointer list-none select-none">
        <Boxes size={14} className={`text-${tone}-600`} />
        <span className="text-[11px] font-bold uppercase tracking-wider text-stone-600">
          What this creates
        </span>
        <span className="text-[10px] text-stone-400">
          {(shape.nodes || []).map((n) => n.type).join(" · ")}
        </span>
      </summary>
      <div className="px-3 pb-3 pt-1 space-y-2">
        {(shape.nodes || []).map((n) => (
          <div key={n.type} className="rounded-md bg-white border border-stone-200 px-2.5 py-2">
            <div className="flex items-baseline gap-2">
              <span className={`text-[12px] font-bold text-${tone}-700`}>{n.type}</span>
              <span className="text-[10px] text-stone-400">{n.role}</span>
            </div>
            <div className="flex flex-wrap gap-1 mt-1.5">
              {(n.attributes || []).map((a) => {
                const measured = (n.measured || []).includes(a);
                return (
                  <span key={a}
                    title={measured ? "filled by ingestion — read from the file or the database"
                                    : "declare or rename it in the schema mapping"}
                    className={`px-1.5 py-0.5 rounded text-[10px] font-mono border ${
                      measured ? "bg-amber-50 text-amber-700 border-amber-100"
                               : "bg-stone-100 text-stone-500 border-stone-200"}`}>
                    {a}
                  </span>
                );
              })}
            </div>
          </div>
        ))}
        {(shape.edges || []).length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {(shape.edges || []).map((e: any, i: number) => (
              <span key={i} className="px-2 py-0.5 bg-indigo-50 text-indigo-700 rounded border border-indigo-100 text-[10px] font-mono">
                {e.from} <span className="text-indigo-400">—{e.type}→</span> {e.to}
              </span>
            ))}
          </div>
        )}
        <p className="text-[10px] text-stone-400 flex items-start gap-1.5 leading-relaxed">
          <Info size={11} className="mt-0.5 shrink-0" />
          <span>
            Amber attributes are filled by ingestion itself (read from the file or measured against
            the database). The rest are yours: {shape.configured_in}.
          </span>
        </p>
      </div>
    </details>
  );
}
