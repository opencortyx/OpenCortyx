"use client";

import { useEffect, useState, useMemo } from "react";
import { contextV2Api, StatsResult, AssetItem } from "@/lib/api/contextV2";
import {
  RefreshCw,
  Database,
  Table2,
  LayoutGrid,
  Search,
  ArrowRight,
  Tag,
  Share2,
  Library,
  Fingerprint,
  CheckCircle2,
  Braces,
  FileText,
  Trash2,
} from "lucide-react";
import PageHeader from "@/components/layout/PageHeader";
import Button from "@/components/ui/Button";
import DeleteAssetDialog from "@/components/assets/DeleteAssetDialog";
import DeleteCollectionDialog from "@/components/assets/DeleteCollectionDialog";
import Link from "next/link";

const KIND_LABEL: Record<string, string> = {
  document: "Document",
  semistructured: "Semi-structured",
  structured: "Structured",
};

function relativeTime(iso?: string | null): string {
  if (!iso) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  for (const [size, unit] of [[86400, "day"], [3600, "hour"], [60, "minute"]] as const) {
    if (seconds >= size) {
      const n = Math.floor(seconds / size);
      return `${n} ${unit}${n === 1 ? "" : "s"} ago`;
    }
  }
  return "just now";
}

function sizeOf(item: AssetItem): string {
  if (item.rows != null) {
    return `${item.rows.toLocaleString()} rows${item.columns != null ? ` · ${item.columns} columns` : ""}`;
  }
  if (item.words != null) return `${item.words.toLocaleString()} words`;
  return "—";
}

export default function CataloguePage() {
  const [stats, setStats] = useState<StatsResult | null>(null);
  const [assets, setAssets] = useState<AssetItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState("");
  const [kindFilter, setKindFilter] = useState<string>("All");
  // asset queued for permanent deletion (the dialog does the dry-run + confirmation)
  const [toDelete, setToDelete] = useState<AssetItem | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  // Collections are the SCOPE a corpus is ingested into, so wiping one is how you start a
  // rebuild from scratch. The API could always do it; nothing in the UI could.
  const [collections, setCollections] = useState<{ id: string; name: string }[]>([]);
  const [collToDelete, setCollToDelete] = useState<{ id: string; name: string } | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [s, m, c] = await Promise.all([
        contextV2Api.stats(),
        contextV2Api.listStructured(),
        contextV2Api.collections().catch(() => ({ collections: [] })),
      ]);
      setStats(s);
      setAssets(m.items || []);
      setCollections((c as any)?.collections || []);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const filteredAssets = useMemo(() => {
    return assets.filter((m) => {
      const query = searchQuery.toLowerCase();
      const matchesQuery = [m.label, m.table, m.description, m.domain, m.mime, m.format, m.fingerprint]
        .some((value) => (value || "").toLowerCase().includes(query));
      const matchesKind = kindFilter === "All" || m.kind === kindFilter;

      return matchesQuery && matchesKind;
    });
  }, [assets, searchQuery, kindFilter]);

  return (
    <div className="max-w-7xl mx-auto space-y-6 pb-12">
      {/* Header */}
      <PageHeader
        title="Assets Catalog"
        icon={Library}
        description="Discover, understand, and manage your connected enterprise data assets and schema signatures."
        actions={
          <div className="flex items-center gap-2">
            <Button variant="secondary" icon={RefreshCw} busy={loading} onClick={load}>
              Refresh
            </Button>
          </div>
        }
      />

      {/* KPIs & Stats Metric Cards Strip */}
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3">
        <div className="bg-white dark:bg-stone-900 p-3.5 rounded-xl border border-stone-200 dark:border-stone-800 shadow-xs space-y-1">
          <div className="p-2 rounded-lg bg-blue-50 dark:bg-blue-950 text-blue-600 w-max">
            <LayoutGrid size={18} />
          </div>
          <div className="text-[11px] text-stone-400 font-medium">Total Nodes</div>
          <div className="text-lg font-bold text-stone-900 dark:text-stone-100">
            {stats?.total_nodes?.toLocaleString() ?? 0}
          </div>
        </div>

        <div className="bg-white dark:bg-stone-900 p-3.5 rounded-xl border border-stone-200 dark:border-stone-800 shadow-xs space-y-1">
          <div className="p-2 rounded-lg bg-indigo-50 dark:bg-indigo-950 text-indigo-600 w-max">
            <Share2 size={18} />
          </div>
          <div className="text-[11px] text-stone-400 font-medium">Relationships</div>
          <div className="text-lg font-bold text-stone-900 dark:text-stone-100">
            {stats?.total_edges?.toLocaleString() ?? 0}
          </div>
        </div>

        <div className="bg-white dark:bg-stone-900 p-3.5 rounded-xl border border-stone-200 dark:border-stone-800 shadow-xs space-y-1">
          <div className="p-2 rounded-lg bg-emerald-50 dark:bg-emerald-950 text-emerald-600 w-max">
            <Database size={18} />
          </div>
          <div className="text-[11px] text-stone-400 font-medium">Embedded Assets</div>
          <div className="text-lg font-bold text-stone-900 dark:text-stone-100">
            {stats?.embedded_nodes?.toLocaleString() ?? 0}
          </div>
        </div>

        <div className="bg-white dark:bg-stone-900 p-3.5 rounded-xl border border-stone-200 dark:border-stone-800 shadow-xs space-y-1">
          <div className="p-2 rounded-lg bg-amber-50 dark:bg-amber-950 text-amber-600 w-max">
            <Tag size={18} />
          </div>
          <div className="text-[11px] text-stone-400 font-medium">Node Types</div>
          <div className="text-lg font-bold text-stone-900 dark:text-stone-100">
            {Object.keys(stats?.nodes_by_type || {}).length}
          </div>
        </div>

        <div className="bg-white dark:bg-stone-900 p-3.5 rounded-xl border border-stone-200 dark:border-stone-800 shadow-xs space-y-1">
          <div className="p-2 rounded-lg bg-purple-50 dark:bg-purple-950 text-purple-600 w-max">
            <Table2 size={18} />
          </div>
          <div className="text-[11px] text-stone-400 font-medium">Catalogued Assets</div>
          <div className="text-lg font-bold text-stone-900 dark:text-stone-100">{assets.length}</div>
        </div>

        <div className="bg-white dark:bg-stone-900 p-3.5 rounded-xl border border-stone-200 dark:border-stone-800 shadow-xs space-y-1">
          <div className="p-2 rounded-lg bg-blue-50 dark:bg-blue-950 text-blue-600 w-max">
            <Fingerprint size={18} />
          </div>
          <div className="text-[11px] text-stone-400 font-medium">Unique Fingerprints</div>
          <div className="text-lg font-bold text-stone-900 dark:text-stone-100">
            {new Set(assets.map((m) => m.fingerprint).filter(Boolean)).size}
          </div>
        </div>

        <div className="bg-white dark:bg-stone-900 p-3.5 rounded-xl border border-stone-200 dark:border-stone-800 shadow-xs space-y-1">
          <div className="p-2 rounded-lg bg-violet-50 dark:bg-violet-950 text-violet-600 w-max">
            <Library size={18} />
          </div>
          <div className="text-[11px] text-stone-400 font-medium">Collections</div>
          <div className="text-lg font-bold text-stone-900 dark:text-stone-100">{collections.length}</div>
        </div>
      </div>

      {/* Collections — the ingestion SCOPE, and the only place a whole corpus can be removed */}
      {collections.length > 0 && (
        <div className="bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-800 rounded-xl overflow-hidden shadow-sm">
          <div className="p-4 border-b border-stone-200 dark:border-stone-800 flex items-center justify-between bg-stone-50/50 dark:bg-stone-800/40">
            <h2 className="font-semibold text-stone-800 dark:text-stone-200 text-sm flex items-center gap-2">
              <Library size={16} className="text-violet-600" />
              Collections
              <span className="text-[10px] text-stone-400 font-normal">({collections.length})</span>
            </h2>
            <span className="text-[11px] text-stone-400">
              Deleting a collection removes every node scoped to it
            </span>
          </div>
          <div className="divide-y divide-stone-100 dark:divide-stone-800">
            {collections.map((c) => (
              <div key={c.id} className="px-4 py-2.5 flex items-center justify-between group">
                <span className="text-sm font-mono text-stone-700 dark:text-stone-300">{c.name}</span>
                <button
                  onClick={() => setCollToDelete(c)}
                  title="Delete this collection and everything in it"
                  aria-label={`Delete collection ${c.name}`}
                  className="p-1.5 rounded-md text-stone-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950/40 opacity-60 group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 transition-opacity"
                >
                  <Trash2 size={15} aria-hidden="true" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Asset Type Filter Tabs */}
      <div className="flex border-b border-stone-200 dark:border-stone-800 text-xs font-medium gap-6">
        {[
          { key: "All", label: "All Assets", count: assets.length },
          { key: "document", label: "Documents", count: assets.filter((m) => m.kind === "document").length },
          { key: "semistructured", label: "Semi-structured (JSON, XML, ...)", count: assets.filter((m) => m.kind === "semistructured").length },
          { key: "structured", label: "Structured (Tables)", count: assets.filter((m) => m.kind === "structured").length },
        ].map((t) => {
          const isActive = kindFilter === t.key;
          return (
            <button
              key={t.key}
              onClick={() => setKindFilter(t.key)}
              className={`pb-3 border-b-2 transition-colors flex items-center gap-1.5 ${isActive
                  ? "border-blue-600 text-blue-600 font-bold"
                  : "border-transparent text-stone-500 hover:text-stone-800 dark:hover:text-stone-300"
                }`}
            >
              {t.label}
              <span className="text-[10px] text-stone-400 font-normal">({t.count})</span>
            </button>
          );
        })}
      </div>

      {/* Full-Width Assets Catalog Table */}
      <div className="bg-white dark:bg-stone-900 border border-stone-200 dark:border-stone-800 rounded-xl overflow-hidden shadow-sm flex flex-col w-full">
        <div className="p-4 border-b border-stone-200 dark:border-stone-800 flex items-center justify-between bg-stone-50/50 dark:bg-stone-800/40">
          <h2 className="font-semibold text-stone-800 dark:text-stone-200 text-sm flex items-center gap-2">
            <Table2 size={16} className="text-blue-600" />
            Catalogued Entities
            <span className="bg-stone-200 dark:bg-stone-700 text-stone-700 dark:text-stone-300 py-0.5 px-2 rounded-full text-xs font-bold">
              {filteredAssets.length}
            </span>
          </h2>
          <div className="relative">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-stone-400" />
            <input
              type="text"
              placeholder="Filter by name, domain, format, fingerprint..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-9 pr-3 py-1.5 border border-stone-200 dark:border-stone-700 bg-white dark:bg-stone-800 rounded-lg text-xs focus:outline-none focus:ring-2 focus:ring-blue-500/20 w-64"
            />
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="bg-stone-50 dark:bg-stone-800/60 text-stone-500 font-semibold uppercase text-[10px] tracking-wider border-b border-stone-200 dark:border-stone-800">
                <th className="px-4 py-3.5">Asset Name</th>
                <th className="px-4 py-3.5">Fingerprint</th>
                <th className="px-4 py-3.5">Kind</th>
                <th className="px-4 py-3.5">Format</th>
                <th className="px-4 py-3.5">Schema</th>
                <th className="px-4 py-3.5">Domain</th>
                <th className="px-4 py-3.5">Source</th>
                <th className="px-4 py-3.5">Updated</th>
                <th className="px-4 py-3.5">Size</th>
                <th className="px-4 py-3.5">Classification</th>
                <th className="px-4 py-3.5 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-stone-100 dark:divide-stone-800">
              {loading ? (
                <tr>
                  <td colSpan={11} className="px-6 py-8 text-center text-sm text-stone-500">
                    <div className="flex flex-col items-center justify-center gap-2">
                      <RefreshCw className="animate-spin text-stone-400" size={24} />
                      Loading catalogued entities from database...
                    </div>
                  </td>
                </tr>
              ) : filteredAssets.length === 0 ? (
                <tr>
                  <td colSpan={11} className="px-6 py-8 text-center text-sm text-stone-500">
                    No catalogued entities found matching your filter criteria.
                  </td>
                </tr>
              ) : (
                filteredAssets.map((m) => {
                  const kind = m.kind || "document";
                  const schemaDef =
                    kind === "structured"
                      ? { label: "Schema · auto · editable", href: "/dashboard/mapper", tone: "blue" }
                      : kind === "semistructured"
                        ? { label: "Keys (auto)", href: "/dashboard/mapper", tone: "purple" }
                        : { label: "Document mapping", href: "/dashboard/mapper", tone: "stone" };
                  const KindIcon = kind === "structured" ? Table2 : kind === "semistructured" ? Braces : FileText;
                  const kindTone =
                    kind === "structured"
                      ? "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300"
                      : kind === "semistructured"
                        ? "bg-purple-100 text-purple-700 dark:bg-purple-950 dark:text-purple-300"
                        : "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300";
                  const sensitive = !!m.sensitivity && m.sensitivity !== "normal";

                  return (
                    <tr
                      key={m.id}
                      className="hover:bg-stone-50/80 dark:hover:bg-stone-800/40 transition-colors"
                    >
                      {/* 1. Asset Name */}
                      <td className="px-4 py-3.5 font-semibold text-stone-900 dark:text-stone-100">
                        <div className="flex items-center gap-2.5">
                          <div className={`p-1.5 rounded ${kindTone}`}>
                            <KindIcon size={15} />
                          </div>
                          <div>
                            <div className="font-semibold text-stone-900 dark:text-stone-100 text-sm">{m.label || m.table}</div>
                            <div className="text-[10px] text-stone-400 font-mono">{m.mime || ""}</div>
                          </div>
                        </div>
                      </td>

                      {/* 2. Signature */}
                      <td className="px-4 py-3.5">
                        {m.fingerprint ? (
                          <span
                            title={m.fingerprint}
                            className="inline-flex items-center gap-1 font-mono text-[10px] font-bold bg-stone-100 dark:bg-stone-800 text-stone-700 dark:text-stone-300 px-2 py-0.5 rounded border border-stone-200 dark:border-stone-700"
                          >
                            <Fingerprint size={11} className="text-blue-500 shrink-0" />
                            {m.fingerprint.slice(0, 8).toUpperCase()}
                          </span>
                        ) : (
                          <span className="text-stone-400">—</span>
                        )}
                      </td>

                      {/* 3. Type */}
                      <td className="px-4 py-3.5">
                        <span className={`px-2 py-0.5 text-[10px] font-semibold rounded ${kindTone}`}>
                          {KIND_LABEL[kind] ?? kind}
                        </span>
                      </td>

                      {/* 4. Format */}
                      <td className="px-4 py-3.5 text-stone-600 dark:text-stone-400 whitespace-nowrap">{m.format || "—"}</td>

                      {/* 5. Schema Definition — semi=keys, structured=auto/editable, unstructured=none */}
                      <td className="px-4 py-3.5 whitespace-nowrap">
                        <Link
                          href={schemaDef.href as any}
                          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-semibold border transition-colors ${schemaDef.tone === "purple"
                              ? "bg-purple-50 text-purple-700 border-purple-200 hover:bg-purple-100 dark:bg-purple-950 dark:text-purple-300 dark:border-purple-800"
                              : schemaDef.tone === "blue"
                                ? "bg-blue-50 text-blue-700 border-blue-200 hover:bg-blue-100 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-800"
                                : "bg-stone-50 text-stone-700 border-stone-200 hover:bg-stone-100 dark:bg-stone-800 dark:text-stone-300 dark:border-stone-700"
                            }`}
                        >
                          <Braces size={10} /> {schemaDef.label}
                        </Link>
                      </td>

                      {/* 6. Domain */}
                      <td className="px-4 py-3.5">
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800">
                          {m.domain || "General"}
                        </span>
                      </td>

                      {/* 7. Source */}
                      <td className="px-4 py-3.5 text-stone-500 font-mono text-[11px] whitespace-nowrap">{m.source || "—"}</td>

                      {/* 8. Updated */}
                      <td className="px-4 py-3.5 text-stone-500 whitespace-nowrap" title={m.updated_at || undefined}>
                        {relativeTime(m.updated_at)}
                      </td>

                      {/* 9. Size */}
                      <td className="px-4 py-3.5 text-stone-600 dark:text-stone-400 whitespace-nowrap">{sizeOf(m)}</td>

                      {/* 10. Classification */}
                      <td className="px-4 py-3.5">
                        <span
                          className={`px-2 py-0.5 text-[10px] font-semibold rounded-full capitalize ${sensitive
                              ? "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300"
                              : "bg-stone-100 text-stone-700 dark:bg-stone-800 dark:text-stone-300"
                            }`}
                        >
                          {m.sensitivity || "normal"}
                        </span>
                      </td>

                      {/* Actions */}
                      <td className="px-4 py-3.5 text-right whitespace-nowrap">
                        <div className="inline-flex items-center gap-1.5">
                          <Link
                            href={`/dashboard/catalogue/${m.id}` as any}
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 rounded-lg hover:bg-blue-100 transition-colors shadow-xs"
                          >
                            Explore <ArrowRight size={13} />
                          </Link>
                          <button
                            onClick={() => setToDelete(m)}
                            title="Delete this asset and everything derived from it"
                            aria-label={`Delete ${m.label || m.table || "asset"}`}
                            className="inline-flex items-center justify-center p-1.5 text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg hover:bg-red-100 dark:hover:bg-red-900 transition-colors shadow-xs"
                          >
                            <Trash2 size={13} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {toDelete && (
        <DeleteAssetDialog
          assetId={toDelete.id}
          assetName={toDelete.label || toDelete.table || "this asset"}
          onClose={() => setToDelete(null)}
          onDeleted={(res) => {
            setAssets((prev) => prev.filter((x) => x.id !== res.id));
            setFlash(
              `Deleted “${res.label || "asset"}” — ${res.nodes.toLocaleString()} nodes and ${res.edges.toLocaleString()} edges removed.`
            );
            load();               // refresh the KPI counts from the server
          }}
        />
      )}

      {collToDelete && (
        <DeleteCollectionDialog
          collectionId={collToDelete.id}
          collectionName={collToDelete.name}
          onClose={() => setCollToDelete(null)}
          onDeleted={() => {
            setFlash(`Deleted collection “${collToDelete.name}”.`);
            load();
          }}
        />
      )}

      {flash && (
        <div className="fixed bottom-6 right-6 z-[70] max-w-sm bg-stone-900 text-white text-xs px-4 py-3 rounded-xl shadow-2xl flex items-start gap-3">
          <CheckCircle2 size={15} className="text-emerald-400 shrink-0 mt-0.5" />
          <span className="flex-1">{flash}</span>
          <button onClick={() => setFlash(null)} className="text-stone-400 hover:text-white font-bold">
            ×
          </button>
        </div>
      )}
    </div>
  );
}
