"use client";

/**
 * Step 1 of every pipeline: the mapping that reads the source, and two options.
 *
 * The same form for documents, tables and JSON. A bound mapping declares what a source becomes;
 * without one the default walker reads it. The options behave the same for every kind:
 *
 *   • Accept a newly detected shape automatically — a source the bound mapping does not match is
 *     read by the default walker; switched off, it is skipped. With no mapping bound, the default
 *     walker reads every source either way.
 *   • Extract only — keep the schema, not the data.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { ExternalLink, Loader2 } from "lucide-react";
import SchemaMappingPicker from "@/components/pipelines/SchemaMappingPicker";
import { contextV2Api } from "@/lib/api/contextV2";
import type { PipelineKind } from "@/components/pipelines/model";
import { AnchorSection, MappingOption } from "./shared";

const EXTRACT_ONLY_HINT: Record<PipelineKind, string> = {
  document: "Chunks are used to extract entities and claims, then discarded. Only the extracted nodes are kept.",
  structured: "Only the schema: tables and columns, no rows.",
  semistructured: "Only the structure, read from one sample record, without record values.",
};

export default function ReadingRuleConfig({ kind, config, onChange, mappings }: {
  kind: PipelineKind;
  config: Record<string, any>;
  onChange: (patch: Record<string, any>) => void;
  mappings: MappingOption[];
}) {
  const bound: string[] = config.mapping_ids || [];

  return (
    <div className="space-y-4">
      <AnchorSection title="Mapping">
        <SchemaMappingPicker
          options={mappings} value={bound}
          onChange={(ids) => onChange({ mapping_ids: ids })}
          engineHref="/dashboard/mapper"
          accent="teal"
          emptyHint="none: read automatically" />
        {kind === "semistructured" && <DeclaredPaths mappingId={bound[0]} />}
      </AnchorSection>

      <AnchorSection title="Options">
        <Option
          checked={config.auto_accept !== false}
          onChange={(value) => onChange({ auto_accept: value })}
          label={bound.length ? "Read files the mapping does not fit automatically" : "Read new kinds of file automatically"}
          hint={bound.length ? "Off: they are skipped." : ""} />
        <Option
          checked={config.extract_only === true}
          onChange={(value) => onChange({ extract_only: value })}
          label="Keep only what is extracted"
          hint={EXTRACT_ONLY_HINT[kind]} />
      </AnchorSection>
    </div>
  );
}

function Option({ checked, onChange, label, hint }: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  hint: string;
}) {
  return (
    <label className="flex items-start gap-2 text-[12.5px] text-stone-700">
      <input type="checkbox" className="mt-0.5 accent-[hsl(var(--primary))]" checked={checked}
        onChange={(e) => onChange(e.target.checked)} />
      <span>
        <b className="font-semibold">{label}</b>
        {hint && <span className="block text-[11.5px] text-stone-500 mt-0.5">{hint}</span>}
      </span>
    </label>
  );
}

/** The node paths a bound semi-structured mapping declares. Read-only: they are edited in the Mapper. */
function DeclaredPaths({ mappingId }: { mappingId?: string }) {
  const [paths, setPaths] = useState<{ path: string; node_type?: string }[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!mappingId) { setPaths([]); return; }
    setLoading(true);
    contextV2Api.getSchemaMapping(mappingId)
      .then((m: any) => setPaths(((m?.payload?.structures || []) as any[])
        .flatMap((s: any) => s.keys || [])
        .filter((k: any) => k?.kind === "node")
        .map((k: any) => ({ path: k.path || "«record root»", node_type: k.node_type }))))
      .catch(() => setPaths([]))
      .finally(() => setLoading(false));
  }, [mappingId]);

  if (loading) {
    return (
      <p className="flex items-center gap-2 text-[12px] text-stone-500">
        <Loader2 size={13} className="animate-spin" /> reading the mapping&apos;s declared paths…
      </p>
    );
  }
  if (!paths.length) return null;

  return (
    <div className="rounded-lg border border-stone-200 bg-stone-50/70 p-3">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-stone-500">
          Declared paths — {paths.length} node{paths.length === 1 ? "" : "s"}
        </span>
        <Link href={`/dashboard/mapper/${mappingId}`}
          className="ml-auto inline-flex items-center gap-1 text-[11.5px] text-primary hover:underline underline-offset-2">
          edit in the Mapper <ExternalLink size={11} />
        </Link>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {paths.slice(0, 24).map((p) => (
          <span key={p.path} className="inline-flex items-center gap-1.5 font-mono text-[11px] px-2 py-1 rounded border border-stone-200 bg-white">
            <b className="font-semibold text-stone-800">{p.node_type || "—"}</b>
            <span className="text-stone-500">{p.path}</span>
          </span>
        ))}
        {paths.length > 24 && (
          <span className="text-[11px] text-stone-500 self-center">+{paths.length - 24} more</span>
        )}
      </div>
    </div>
  );
}
