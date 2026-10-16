"use client";

/**
 * Mapper — the hub.
 *
 * One list for all three paradigms. The three separate builders each had their own hub, their own
 * "switch to…" links and their own idea of what a mapping row looks like; a mapping's paradigm is a
 * property of the mapping, not a reason to send someone to a different page.
 *
 * Every saved mapping appears here, including the ones the platform generated for itself — those
 * arrive with a derived editor model (see ingest/mapping_migrate.py) so they open and edit like any
 * hand-authored card instead of looking empty.
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import { contextV2Api } from "@/lib/api/contextV2";
import {
  Workflow, Search, Plus, Trash2, AlertTriangle, Loader2, Wand2, RefreshCw, ChevronRight,
} from "lucide-react";
import { PARADIGM_META } from "@/components/mapper/panels";
import type { Paradigm } from "@/components/mapper/model";

type Row = {
  id: string; name: string; paradigm: Paradigm; status?: string;
  summary?: any; payload?: any; updated_at?: string | null;
  /** light listing only: whether a human authored the rules, and how many structure cards exist */
  authored?: boolean; structures?: number | null;
};

const PARADIGMS: Paradigm[] = ["semistructured", "structured", "unstructured"];

export default function MapperHubPage() {
  const router = useRouter();
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<Paradigm | "all">("all");
  const [busy, setBusy] = useState<string>("");
  const [confirmDel, setConfirmDel] = useState<{ row: Row; pipelines: string[] } | null>(null);

  const load = async () => {
    setLoading(true); setErr("");
    try {
      const res = await contextV2Api.listSchemaMappings(undefined, true);
      setRows((res.items || []) as Row[]);
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || "Could not load mappings");
    } finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return rows
      .filter((r) => filter === "all" || r.paradigm === filter)
      .filter((r) => !needle || r.name.toLowerCase().includes(needle));
  }, [rows, q, filter]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: rows.length };
    for (const p of PARADIGMS) c[p] = rows.filter((r) => r.paradigm === p).length;
    return c;
  }, [rows]);

  const create = async (paradigm: Paradigm) => {
    setBusy("create");
    try {
      const m = await contextV2Api.createSchemaMapping({
        name: `New ${PARADIGM_META[paradigm].label.toLowerCase()} mapping`,
        paradigm,
        payload: { editor: { keyRows: [], nodeRules: [], edgeRules: [], attributeRules: [], reading: {}, __touched: true } },
      });
      router.push(`/dashboard/mapper/${m.id}`);
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || "Could not create the mapping");
    } finally { setBusy(""); }
  };

  const askDelete = async (row: Row) => {
    setBusy(row.id);
    try {
      const u = await contextV2Api.schemaMappingUsage(row.id);
      setConfirmDel({ row, pipelines: (u.pipelines || []).map((p) => p.name) });
    } catch { setConfirmDel({ row, pipelines: [] }); }
    finally { setBusy(""); }
  };

  const doDelete = async () => {
    if (!confirmDel) return;
    setBusy(confirmDel.row.id);
    try {
      await contextV2Api.deleteSchemaMapping(confirmDel.row.id);
      setRows((rs) => rs.filter((r) => r.id !== confirmDel.row.id));
      setConfirmDel(null);
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e?.message || "Delete failed");
    } finally { setBusy(""); }
  };

  const shapeOf = (r: Row) => {
    const s = r.summary || {};
    const nodes = s.nodes ?? s.node_types?.length ?? 0;
    const edges = s.edges ?? s.edge_types?.length ?? 0;
    return { nodes, edges };
  };

  return (
    <div className="p-6 max-w-[1400px] mx-auto">
      <PageHeader
        title="Mapper"
        icon={Workflow}
        description="Declare the nodes, edges and attributes every source becomes. One builder for JSON, tables and documents."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" size="sm" icon={RefreshCw} onClick={load}>Refresh</Button>
            {PARADIGMS.map((p) => (
              <Button key={p} variant={p === "semistructured" ? "primary" : "secondary"} size="sm"
                icon={Plus} busy={busy === "create"} onClick={() => create(p)}>
                {PARADIGM_META[p].label}
              </Button>
            ))}
          </div>
        }
      />

      {err && (
        <div className="mb-4 flex items-start gap-2 px-3.5 py-3 rounded-xl border border-red-200 bg-red-50 text-sm text-red-800">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          <div><b className="font-semibold">{err}</b></div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 mb-4">
        <div className="relative">
          <Search size={15} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-stone-400" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search mappings…"
            className="pl-8 pr-3 py-2 w-64 rounded-lg border border-stone-200 bg-white text-sm
                       focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary/40" />
        </div>
        <div className="flex flex-wrap gap-1">
          {(["all", ...PARADIGMS] as const).map((p) => (
            <button key={p} type="button" onClick={() => setFilter(p as any)}
              className={`text-[12.5px] px-3 py-1.5 rounded-lg border transition-colors
                          ${filter === p ? "border-primary/50 bg-primary/10 text-primary font-semibold"
                                         : "bg-white border-stone-200 text-stone-600 hover:border-stone-300"}`}>
              {p === "all" ? "All" : PARADIGM_META[p as Paradigm].label}
              <span className="ml-1.5 tabular-nums opacity-70">{counts[p] ?? 0}</span>
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-stone-500 py-16 justify-center">
          <Loader2 size={16} className="animate-spin" /> loading mappings…
        </div>
      ) : !shown.length ? (
        <div className="text-center py-16 border border-dashed border-stone-300 rounded-xl bg-white">
          <p className="text-sm text-stone-600">No mappings match.</p>
        </div>
      ) : (
        <div className="bg-white border border-stone-200 rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-stone-50/70 border-b border-stone-200">
                {["Mapping", "Source", "Graph shape", "Status", ""].map((h, i) => (
                  <th key={h} className={`text-left text-[10px] font-semibold uppercase tracking-wider
                                          text-stone-500 px-4 py-2.5 ${i === 4 ? "w-10" : ""}`}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => {
                const meta = PARADIGM_META[r.paradigm] || PARADIGM_META.semistructured;
                const Icon = meta.icon;
                const { nodes, edges } = shapeOf(r);
                // `authored: false` = the platform generated it; the editor is derived on open.
                const cards = r.structures || 0;
                return (
                  <tr key={r.id} className="border-b border-stone-100 last:border-b-0 hover:bg-stone-50/70">
                    <td className="px-4 py-3">
                      <Link href={`/dashboard/mapper/${r.id}`} className="group inline-flex items-center gap-2 min-w-0">
                        <span className="font-medium text-stone-800 group-hover:text-primary truncate">{r.name}</span>
                        <ChevronRight size={14} className="text-stone-300 group-hover:text-primary shrink-0" />
                      </Link>
                      {r.authored === false && (
                        <span className="ml-2 align-middle text-[10px] font-semibold uppercase tracking-wide
                                         px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200"
                              title={cards > 1
                                ? `Generated by the platform. Its ${cards} structure cards are shown as one editable card; saving merges them.`
                                : "Generated by the platform — rules derived from the compiled mapping."}>
                          {cards > 1 ? `derived · ${cards} cards` : "derived"}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className="inline-flex items-center gap-1.5 text-[12.5px] text-stone-600">
                        <Icon size={14} className="text-stone-400" />{meta.label}
                      </span>
                    </td>
                    <td className="px-4 py-3 font-mono text-[12px] text-stone-600 tabular-nums">
                      ⬢ {nodes} nodes &nbsp;──► {edges} edges
                    </td>
                    <td className="px-4 py-3">
                      <span className="text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded
                                       bg-lime-50 text-lime-700 border border-lime-200">
                        {r.status || "active"}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right">
                      <button type="button" onClick={() => askDelete(r)} disabled={busy === r.id}
                        title="Delete mapping"
                        className="text-stone-400 hover:text-red-600 transition-colors disabled:opacity-40">
                        {busy === r.id ? <Loader2 size={15} className="animate-spin" /> : <Trash2 size={15} />}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {confirmDel && (
        <div className="fixed inset-0 z-50 bg-stone-900/30 flex items-center justify-center p-4"
             onClick={() => setConfirmDel(null)}>
          <div className="bg-white rounded-xl border border-stone-200 shadow-lg max-w-md w-full p-5"
               onClick={(e) => e.stopPropagation()}>
            <h3 className="text-base font-semibold text-stone-900 mb-2">Delete “{confirmDel.row.name}”?</h3>
            {confirmDel.pipelines.length ? (
              <div className="mb-3 px-3 py-2.5 rounded-lg border border-amber-300 bg-amber-50 text-[12.5px] text-amber-800">
                <b className="font-semibold">{confirmDel.pipelines.length} pipeline
                  {confirmDel.pipelines.length === 1 ? "" : "s"} bind this mapping.</b>{" "}
                They will keep running and silently stop applying its rules: {confirmDel.pipelines.join(", ")}.
              </div>
            ) : (
              <p className="text-[12.5px] text-stone-600 mb-3">No pipeline binds this mapping.</p>
            )}
            <div className="flex gap-2 justify-end">
              <Button variant="secondary" size="sm" onClick={() => setConfirmDel(null)}>Cancel</Button>
              <Button variant="danger" size="sm" icon={Trash2} busy={busy === confirmDel.row.id} onClick={doDelete}>
                Delete
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
