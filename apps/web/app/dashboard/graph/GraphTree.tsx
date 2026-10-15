"use client";
// The dashboard graph: the workspace as a tree you open in place.
//
//   workspace → source → tag → asset → what the asset produced → any node's relationships
//
// The server answers one parent's children at a time, 100 per click; a parent with more than 1000
// children answers with groups first (an asset's parts by type, a node's relationships by kind), and
// each group pages on its own (apps/api/opencortyx/knowledge/graph_tree.py). Parts of the graph no
// asset reaches are roots of their own, beside the workspace. A node reached from two places is drawn
// once, joined to both. Nothing navigates away and nothing animates: each change is one static redraw.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { ChevronsDownUp, ExternalLink, GitFork, RefreshCw, Search, X } from "lucide-react";
import { graphTreeApi, type TreeEdge, type TreeItem, type TreePage, type TreeTop } from "@/lib/api/graphTree";
import AssetTags from "@/components/assets/AssetTags";
import {
  ACCENT, CenterNote, GRID_BACKGROUND, HIT, LABEL_ZOOM, Legend, MUTED, NodePanel, PanelHeader, type Point,
  Section, Stat, type Swatch, ToggleButton, TypeBars, ZoomControls, ago, bboxOf, clip, edgeLabel, fmt,
  frameFocus, spiralSpot, usePanZoom, useTypeColors,
} from "./canvas";

/** Fixed looks for the tree's own levels; nodes and assets are coloured by their type. */
const LEVEL: Record<string, Swatch> = {
  root: { stroke: "#0f172a", fill: "#1e293b" },
  source: { stroke: "#4f46e5", fill: "#e0e7ff" },
  tag: { stroke: "#d97706", fill: "#fef3c7" },
};
const PILL_H = 30;

type VItem = {
  id: string; item: TreeItem; depth: number; parent: string | null;
  open: boolean; loading: boolean; children: VItem[];
};
type Pair = { a: string; b: string; edges: number; types: Record<string, number>; tree: boolean };
type Loaded = TreePage;

const isPill = (k: TreeItem["kind"]) => k === "root" || k === "source" || k === "tag";
const pillW = (label: string) => Math.min(230, 28 + 7.1 * Math.min(label.length, 28));
const moreId = (key: string) => `${key}#more`;

/** `focusNodeId` opens the tree down to one node and selects it — how an asset page shows where the
 *  asset sits. `embedded` fills the parent's height and hides the page-level controls. */
export default function GraphTree({ focusNodeId, embedded = false }: { focusNodeId?: string; embedded?: boolean }) {
  const [top, setTop] = useState<TreeTop | null>(null);
  const [pages, setPages] = useState<Record<string, Loaded>>({});
  const pagesRef = useRef<Record<string, Loaded>>({});
  const inflight = useRef(new Map<string, Promise<Loaded | null>>());
  const [opened, setOpened] = useState<string[]>([]);
  const openedRef = useRef<string[]>([]);
  openedRef.current = opened;
  const [pinned, setPinned] = useState<Record<string, TreeItem[]>>({});
  const [extraRoots, setExtraRoots] = useState<TreeItem[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [hits, setHits] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<TreeItem[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const { wrapRef, svgRef, dims, view, frame, zoomBy, svgProps, dragging, zoom } = usePanZoom();
  const colorOf = useTypeColors();

  // ── data ──────────────────────────────────────────────────────────────────────────────────────
  const loadTop = useCallback(async () => {
    try { setTop(await graphTreeApi.top()); setError(null); }
    catch (e: any) { setError(e?.response?.data?.detail || e?.message || "Could not load the graph"); }
  }, []);
  useEffect(() => { loadTop(); }, [loadTop]);

  const storePage = (key: string, p: Loaded) => {
    pagesRef.current = { ...pagesRef.current, [key]: p };
    setPages(pagesRef.current);
  };

  /** The first page of what `key` opens into, fetched once. */
  const ensurePage = useCallback((key: string): Promise<Loaded | null> => {
    if (pagesRef.current[key]) return Promise.resolve(pagesRef.current[key]);
    const running = inflight.current.get(key);
    if (running) return running;
    const p = graphTreeApi.children(key, 0).then((d) => {
      const loaded = d;
      d.items.forEach((i) => { if (!isPill(i.kind)) colorOf(i.type); });
      storePage(key, loaded);
      return loaded;
    }).catch((e: any) => {
      setError(e?.response?.data?.detail || e?.message || "Could not open that");
      return null;
    }).finally(() => { inflight.current.delete(key); });
    inflight.current.set(key, p);
    return p;
  }, [colorOf]);

  /** The next 100 of what `key` opens into, added to what is on screen. */
  const loadMore = useCallback(async (key: string) => {
    const cur = pagesRef.current[key];
    if (!cur || inflight.current.has(key)) return;
    const p = graphTreeApi.children(key, cur.next).then((d) => {
      const have = new Set(cur.items.map((i) => i.id));
      d.items.forEach((i) => { if (!isPill(i.kind)) colorOf(i.type); });
      storePage(key, { ...cur, items: [...cur.items, ...d.items.filter((i) => !have.has(i.id))],
        edges: [...cur.edges, ...d.edges], total: d.total, next: d.next });
      return pagesRef.current[key];
    }).catch(() => null).finally(() => { inflight.current.delete(key); });
    inflight.current.set(key, p);
    await p;
  }, [colorOf]);

  /** Read everything on screen again — the first screen and every open level — keeping what is open. */
  const refresh = useCallback(async () => {
    pagesRef.current = {};
    inflight.current.clear();
    setPages({});
    await loadTop();
    await Promise.all(openedRef.current.map((k) => ensurePage(k)));
  }, [loadTop, ensurePage]);

  const run = top?.run;
  const indexing = !!run && (run.status === "queued" || run.status === "running");
  // While an index pass runs, check back; when it lands, read everything on screen again.
  useEffect(() => {
    if (!indexing) return;
    const t = setInterval(async () => {
      try {
        const d = await graphTreeApi.top();
        if (d.run && d.run.status !== "queued" && d.run.status !== "running") refresh();
      } catch { /* keep waiting */ }
    }, 3000);
    return () => clearInterval(t);
  }, [indexing, refresh]);

  // ── what is visible: the first screen, with every opened item's children around it ─────────────
  const openedSet = useMemo(() => new Set(opened), [opened]);
  const tree = useMemo(() => {
    const visible = new Map<string, VItem>();
    const keyToId = new Map<string, string>();
    const links: [string, string][] = [];
    const walk = (item: TreeItem, depth: number, parent: string | null): VItem | null => {
      if (parent) links.push([parent, item.id]);
      if (visible.has(item.id)) return null;                  // drawn once, joined to every parent
      const v: VItem = { id: item.id, item, depth, parent, open: false, loading: false, children: [] };
      visible.set(item.id, v);
      keyToId.set(item.key, item.id);
      if (openedSet.has(item.key)) {
        const page = pages[item.key];
        if (!page) v.loading = true;
        else {
          v.open = true;
          const kids = [...page.items];
          for (const p of pinned[item.key] || []) if (!kids.some((k) => k.id === p.id)) kids.push(p);
          for (const k of kids) {
            const c = walk(k, depth + 1, v.id);
            if (c) v.children.push(c);
          }
          const more = page.total - page.next;
          if (more > 0) {
            const m: TreeItem = { id: moreId(item.key), key: item.key, kind: "group", label: `+${fmt(more)} more`,
              type: "__more", count: more, size: null, openable: true };
            const mv: VItem = { id: m.id, item: m, depth: depth + 1, parent: v.id, open: false, loading: false, children: [] };
            visible.set(m.id, mv);
            links.push([v.id, m.id]);
            v.children.push(mv);
          }
        }
      }
      return v;
    };
    const roots: VItem[] = [];
    for (const it of [...(top?.items || []), ...extraRoots]) {
      const v = walk(it, 0, null);
      if (v) roots.push(v);
    }
    return { roots, visible, keyToId, links };
  }, [top, extraRoots, pages, openedSet, pinned]);

  // ── edges: every real relationship whose two ends are on screen, plus tree links where there is none
  const pairs = useMemo(() => {
    const out = new Map<string, Pair>();
    const add = (a: string, b: string, type: string | null) => {
      if (a === b || !tree.visible.has(a) || !tree.visible.has(b)) return;
      const [x, y] = a < b ? [a, b] : [b, a];
      const e = out.get(`${x}|${y}`) ?? { a: x, b: y, edges: 0, types: {}, tree: true };
      if (type) {
        e.tree = false;
        e.edges += 1;
        e.types[type] = (e.types[type] || 0) + 1;
      }
      out.set(`${x}|${y}`, e);
    };
    const seen = new Set<string>();
    for (const key of opened) for (const e of pages[key]?.edges || []) {
      const id = `${e.source}|${e.target}|${e.relation || e.type}`;
      if (seen.has(id)) continue;
      seen.add(id);
      add(e.source, e.target, e.relation || e.type);
    }
    for (const [a, b] of tree.links) add(a, b, null);
    return [...out.values()];
  }, [tree, pages, opened]);

  // ── layout: organic and stable (see canvas.tsx). The first screen puts the workspace in the middle
  // and the unconnected parts around it; each opened item's children go around it.
  const posRef = useRef(new Map<string, Point>());
  const layout = useMemo(() => {
    const pos = posRef.current;
    for (const id of [...pos.keys()]) if (!tree.visible.has(id)) pos.delete(id);
    const queue = [...tree.roots];
    for (const v of tree.roots) if (!pos.has(v.id)) pos.set(v.id, pos.size ? spiralSpot(pos, { x: 0, y: 0 }, v.id) : { x: 0, y: 0 });
    while (queue.length) {
      const v = queue.shift()!;
      const at = pos.get(v.id)!;
      for (const k of v.children) {
        if (!pos.has(k.id)) pos.set(k.id, spiralSpot(pos, at, k.id));
        queue.push(k);
      }
    }
    return { abs: new Map(pos), bbox: bboxOf(pos) };
  }, [tree]);

  const fit = useCallback(() => frame(layout.bbox), [frame, layout.bbox]);
  const shapeKey = useMemo(() => [...tree.visible.keys()].join(","), [tree]);
  useEffect(() => {
    const v = focus ? tree.visible.get(focus) : undefined;
    frameFocus(frame, layout.bbox, v ? [v, ...v.children].map((k) => layout.abs.get(k.id)).filter((q): q is Point => !!q) : []);
  }, [shapeKey, dims.w, dims.h]);   // eslint-disable-line react-hooks/exhaustive-deps

  // ── actions ───────────────────────────────────────────────────────────────────────────────────
  const open = useCallback(async (item: TreeItem) => {
    setOpened((xs) => (xs.includes(item.key) ? xs : [...xs, item.key]));
    setFocus(item.id);
    await ensurePage(item.key);
  }, [ensurePage]);
  const close = useCallback((item: TreeItem) => {
    setOpened((xs) => xs.filter((x) => x !== item.key));
    setFocus(tree.visible.get(item.id)?.parent ?? null);
  }, [tree]);
  const isOpen = (item: TreeItem) => openedSet.has(item.key);

  const onItemClick = (v: VItem) => {
    const it = v.item;
    if (it.type === "__more") { setFocus(v.parent); loadMore(it.key); return; }
    const wasSelected = selected === v.id;
    setSelected(v.id);
    if (!it.openable) return;
    if (isOpen(it)) { if (wasSelected || isPill(it.kind) || it.kind === "group" || it.kind === "bundle") close(it); }
    else open(it);
  };

  /** Open every level on the way to a node, and make sure the node itself is drawn. */
  const reveal = useCallback(async (nodeId: string, fallback?: TreeItem) => {
    let loc;
    try { loc = await graphTreeApi.locate(nodeId); } catch { loc = null; }
    if (!loc) {
      // no asset reaches it: it sits beside the workspace, like the other unconnected parts
      if (fallback && !tree.visible.has(fallback.id)) setExtraRoots((xs) => (xs.some((x) => x.id === fallback.id) ? xs : [...xs, fallback]));
      return fallback?.id ?? null;
    }
    for (const k of loc.path) await ensurePage(k);
    setOpened((xs) => [...xs, ...loc.path.filter((k) => !xs.includes(k))]);
    const pin = (parentKey: string, it: TreeItem) => {
      if (pagesRef.current[parentKey]?.items.some((i) => i.id === it.id)) return;
      setPinned((p) => ((p[parentKey] || []).some((i) => i.id === it.id) ? p : { ...p, [parentKey]: [...(p[parentKey] || []), it] }));
    };
    pin(loc.path[2], loc.asset);
    if (loc.path.length > 3) pin(loc.path[3], loc.node);
    return loc.node.id;
  }, [ensurePage, tree]);

  const goTo = useCallback(async (nodeId: string, fallback?: TreeItem) => {
    const id = tree.visible.has(nodeId) ? nodeId : await reveal(nodeId, fallback);
    if (!id) return;
    setSelected(id);
    setHits(new Set([id]));
    setFocus(tree.visible.get(id)?.parent ?? id);
  }, [tree, reveal]);

  const runSearch = useCallback(async () => {
    const q = query.trim();
    if (!q) { setResults(null); setHits(new Set()); return; }
    setSearching(true);
    setSelected(null);
    try { setResults((await graphTreeApi.search(q, 30)).items); }
    catch { setResults([]); }
    finally { setSearching(false); }
  }, [query]);
  const clearSearch = () => { setQuery(""); setResults(null); setHits(new Set()); };

  // Open on the requested node once the first screen is in.
  const focusedOnce = useRef(false);
  useEffect(() => {
    if (!focusNodeId || focusedOnce.current || !top) return;
    focusedOnce.current = true;
    (async () => {
      const id = await reveal(focusNodeId);
      if (!id) return;
      setSelected(id);
      setHits(new Set([id]));
      const loc = await graphTreeApi.locate(focusNodeId).catch(() => null);
      if (loc) await open(loc.node);
    })();
  }, [focusNodeId, top, reveal, open]);

  const collapseAll = () => { setFocus(null); setOpened([]); setPinned({}); setExtraRoots([]); setSelected(null); setHits(new Set()); };
  const rebuild = useCallback(async () => {
    try { await graphTreeApi.rebuild(); } catch { /* the run status says what happened */ }
    loadTop();
  }, [loadTop]);

  // ── drawing helpers ───────────────────────────────────────────────────────────────────────────
  const items = useMemo(() => {
    const out: VItem[] = [];
    const walk = (v: VItem) => { out.push(v); v.children.forEach(walk); };
    tree.roots.forEach(walk);
    return out;
  }, [tree]);
  const selectedV = selected ? tree.visible.get(selected) ?? null : null;
  const incident = useMemo(() => {
    const s = new Set<string>();
    if (!selected) return s;
    for (const e of pairs) { if (e.a === selected) s.add(e.b); if (e.b === selected) s.add(e.a); }
    return s;
  }, [pairs, selected]);
  const styleOf = (it: TreeItem): Swatch =>
    it.kind === "bundle" || it.kind === "group" ? MUTED : (LEVEL[it.kind] ?? colorOf(it.type));
  const circleR = (it: TreeItem) => (it.type === "__more" ? 8 : it.kind === "asset" ? 13
    : it.kind === "group" || it.kind === "bundle" ? 10 + Math.min(10, 3 * Math.log10(1 + (it.count || 0)))
    : 9 + Math.min(7, Math.log2(1 + (it.count || 0))));
  const typesOnScreen = useMemo(() => {
    const c: Record<string, number> = {};
    for (const v of items) if (!isPill(v.item.kind) && v.item.kind !== "group" && v.item.kind !== "bundle") c[v.item.type] = (c[v.item.type] || 0) + 1;
    return Object.entries(c).sort((a, b) => b[1] - a[1]).slice(0, 8);
  }, [items]);
  const sublabel = (v: VItem): string => {
    const it = v.item;
    if (it.type === "__more") return "click to load the next 100";
    if (v.open && pages[it.key]?.total === 0) return "nothing inside";
    if (it.kind === "root") return `${fmt(it.count)} assets · ${fmt(it.size)} sources`;
    if (it.kind === "source" || it.kind === "tag") return `${fmt(it.count)} ${it.count === 1 ? "asset" : "assets"}`;
    if (it.kind === "group" || it.kind === "bundle") return fmt(it.count);
    if (it.kind === "root_part") return `${fmt(it.size)} nodes · not from any asset`;
    if (it.kind === "asset") return it.type;
    return "";
  };

  const empty = !!top && !top.assets && !top.roots_total && !extraRoots.length;

  return (
    <div className={`${embedded ? "h-full" : "h-[calc(100vh-6rem)] border border-slate-200 rounded-xl shadow-sm"} bg-white flex flex-col overflow-hidden`}>
      {/* header */}
      <div className="flex items-center justify-between gap-4 px-5 py-3 border-b border-slate-200">
        <div className="min-w-0">
          <h1 className="text-base font-semibold tracking-tight text-slate-900 flex items-center gap-2">
            <GitFork size={16} className="text-blue-600" /> Knowledge Graph
          </h1>
          <p className="text-xs text-slate-500 mt-0.5 truncate">
            {run?.status === "ok"
              ? `${fmt(top?.assets)} assets · ${fmt(top?.sources)} sources · ${fmt(run.nodes)} nodes · indexed ${ago(run.finished_at)}`
              : indexing ? "Indexing…" : "Sources, tags and assets — open anything in place"}
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <form className="relative" onSubmit={(e) => { e.preventDefault(); runSearch(); }}>
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
            <input value={query} onChange={(e) => { setQuery(e.target.value); if (!e.target.value) clearSearch(); }}
              placeholder="Search assets and nodes…" aria-label="Search the graph"
              className="w-56 pl-8 pr-7 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/20 focus:border-blue-400" />
            {query && (
              <button type="button" onClick={clearSearch} className="absolute right-1.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-700" aria-label="Clear search">
                <X size={13} />
              </button>
            )}
          </form>
          <button onClick={collapseAll} title="Collapse everything"
            className="h-8 w-8 flex items-center justify-center rounded-lg border border-slate-200 text-slate-600 hover:bg-slate-50">
            <ChevronsDownUp size={14} />
          </button>
          <button onClick={rebuild} title="Re-index sources, tags and assets now"
            className="h-8 w-8 flex items-center justify-center rounded-lg border border-slate-200 text-slate-600 hover:bg-slate-50">
            <RefreshCw size={14} className={indexing ? "text-blue-600" : ""} />
          </button>
        </div>
      </div>

      <div className="flex-1 flex min-h-0">
        {/* canvas */}
        <div ref={wrapRef} className="flex-1 relative min-w-0 overflow-hidden" style={GRID_BACKGROUND}>
          {error && !top ? (
            <CenterNote title="Could not load the graph" body={error} action={{ label: "Retry", onClick: loadTop }} />
          ) : !top ? (
            <CenterNote title="Loading…" />
          ) : empty ? (
            indexing ? <CenterNote title="Indexing…" body="Listing sources, tags and assets. This page updates when it is ready." />
              : run?.status === "error" ? <CenterNote title="Indexing failed" body={run.error || ""} action={{ label: "Try again", onClick: rebuild }} />
              : <CenterNote title="Nothing to show yet" body="Add some data — the graph is indexed automatically after each change." action={{ label: "Index now", onClick: rebuild }} />
          ) : view && (
            <svg ref={svgRef} width={dims.w} height={dims.h} viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
              className="block select-none touch-none" role="img" aria-label="Knowledge graph"
              style={{ cursor: dragging() ? "grabbing" : "default" }} {...svgProps}
              onClick={() => { if (!dragging()) setSelected(null); }}>
              {/* edges: plain first, the selection's on top */}
              {[false, true].map((layer) => pairs.map((l) => {
                const on = !!selected && (l.a === selected || l.b === selected);
                if (on !== layer) return null;
                const pa = layout.abs.get(l.a), pb = layout.abs.get(l.b);
                if (!pa || !pb) return null;
                const faded = !!selected && !on;
                return (
                  <line key={`${l.a}-${l.b}`} x1={pa.x} y1={pa.y} x2={pb.x} y2={pb.y}
                    stroke={on ? ACCENT : faded ? "#e5e9ef" : "#cbd5e1"}
                    strokeWidth={(on ? 2.5 : l.tree ? 1.25 : 1 + Math.min(2, Math.log10(1 + l.edges))) / Math.max(zoom, 0.25)}
                    strokeDasharray={l.tree && !on ? "5 5" : undefined}>
                    <title>{l.tree ? "contains" : `${fmt(l.edges)} edge${l.edges === 1 ? "" : "s"}: ${Object.entries(l.types).map(([t, n]) => `${t} ${n}`).join(", ")}`}</title>
                  </line>
                );
              }))}

              {/* edge labels: along the line, upright; the selection's always, the rest once readable */}
              {pairs.map((l) => {
                if (l.tree) return null;
                const on = !!selected && (l.a === selected || l.b === selected);
                if (!on && (zoom < LABEL_ZOOM || !!selected)) return null;
                const pa = layout.abs.get(l.a), pb = layout.abs.get(l.b);
                if (!pa || !pb) return null;
                const len = Math.hypot(pb.x - pa.x, pb.y - pa.y);
                const text = edgeLabel(l);
                const fs = 10.5 / Math.max(zoom, 0.2);
                if (!text || len * zoom < text.length * 6.2 + 40) return null;   // no room on screen
                let angle = (Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180) / Math.PI;
                if (angle > 90) angle -= 180;
                if (angle < -90) angle += 180;
                return (
                  <text key={`lbl-${l.a}-${l.b}`} transform={`translate(${(pa.x + pb.x) / 2},${(pa.y + pb.y) / 2}) rotate(${angle})`}
                    textAnchor="middle" dy={fs * 0.35} fontSize={fs} fontWeight={on ? 600 : 500}
                    fill={on ? ACCENT : "#64748b"} pointerEvents="none"
                    style={{ paintOrder: "stroke", stroke: "#f8fafc", strokeWidth: 4 / Math.max(zoom, 0.2) }}>
                    {text}
                  </text>
                );
              })}

              {/* items */}
              {items.map((v) => {
                const p = layout.abs.get(v.id);
                if (!p) return null;
                const it = v.item;
                const sel = selected === v.id;
                const hit = hits.has(v.id);
                const faded = !!selected && !sel && !incident.has(v.id) && !hit;
                const s = styleOf(it);
                const more = it.type === "__more";
                const sub = sublabel(v);
                const title = `${it.label}${sub ? ` · ${sub}` : ""}${it.openable && !more ? ` — click to ${v.open ? "close" : "open"}` : ""}`;
                if (isPill(it.kind)) {
                  const w = pillW(it.label);
                  const dark = it.kind === "root";
                  return (
                    <g key={v.id} transform={`translate(${p.x},${p.y})`} opacity={faded ? 0.5 : 1} className="cursor-pointer"
                      onClick={(e) => { if (dragging()) return; e.stopPropagation(); onItemClick(v); }}>
                      <title>{title}</title>
                      {sel && <rect x={-w / 2 - 6} y={-PILL_H / 2 - 6} width={w + 12} height={PILL_H + 12} rx={PILL_H / 2 + 6} fill="none" stroke={ACCENT} strokeOpacity={0.25} strokeWidth={6} />}
                      {hit && !sel && <rect x={-w / 2 - 5} y={-PILL_H / 2 - 5} width={w + 10} height={PILL_H + 10} rx={PILL_H / 2 + 5} fill="none" stroke={HIT} strokeWidth={3} />}
                      <rect x={-w / 2} y={-PILL_H / 2} width={w} height={PILL_H} rx={PILL_H / 2}
                        fill={s.fill} stroke={sel ? ACCENT : s.stroke} strokeWidth={2}
                        strokeDasharray={it.kind === "tag" && it.auto ? "4 3" : undefined} />
                      <text textAnchor="middle" dy={4.5} fontSize={12.5} fontWeight={600} fill={dark ? "#f8fafc" : "#1e293b"}>
                        {clip(it.label, 28)}
                      </text>
                      {sub && (
                        <text y={PILL_H / 2 + 15} textAnchor="middle" fontSize={10.5} fill="#64748b"
                          style={{ paintOrder: "stroke", stroke: "#f8fafc", strokeWidth: 4 }}>
                          {v.loading ? "opening…" : sub}
                        </text>
                      )}
                    </g>
                  );
                }
                const r = circleR(it);
                return (
                  <g key={v.id} transform={`translate(${p.x},${p.y})`} opacity={faded ? 0.5 : 1} className="cursor-pointer"
                    onClick={(e) => { if (dragging()) return; e.stopPropagation(); onItemClick(v); }}>
                    <title>{title}</title>
                    {hit && !sel && <circle r={r + 7} fill="none" stroke={HIT} strokeWidth={4} strokeOpacity={0.9} />}
                    {sel && <circle r={r + 8} fill="none" stroke={ACCENT} strokeOpacity={0.2} strokeWidth={7} />}
                    {it.kind === "asset" ? (
                      <rect x={-r} y={-r} width={2 * r} height={2 * r} rx={5} fill={sel ? ACCENT : s.fill} stroke={sel ? ACCENT : s.stroke} strokeWidth={2.5} />
                    ) : (
                      <circle r={sel ? r + 2 : r} fill={sel ? ACCENT : s.fill} stroke={sel ? ACCENT : s.stroke}
                        strokeWidth={2.5} strokeDasharray={it.kind === "group" || it.kind === "bundle" || it.kind === "root_part" ? "3 3" : undefined} />
                    )}
                    {it.openable && !more && !sel && !v.open && !v.loading && <circle r={2.5} fill={s.stroke} />}
                    {v.open && <line x1={-4} y1={0} x2={4} y2={0} stroke={sel ? "#ffffff" : s.stroke} strokeWidth={2} />}
                    {v.loading && <text textAnchor="middle" dy={4} fontSize={11} fill={s.stroke}>…</text>}
                    <text y={r + 16} textAnchor="middle" fontSize={more ? 11.5 : 12.5} fontWeight={sel || hit ? 600 : 500}
                      fill={sel ? ACCENT : more ? "#475569" : "#1e293b"} style={{ paintOrder: "stroke", stroke: "#f8fafc", strokeWidth: 4 }}>
                      {clip(it.label)}
                    </text>
                    {sub && !more && (
                      <text y={r + 30} textAnchor="middle" fontSize={10.5} fill="#64748b"
                        style={{ paintOrder: "stroke", stroke: "#f8fafc", strokeWidth: 4 }}>
                        {clip(sub, 34)}
                      </text>
                    )}
                  </g>
                );
              })}
            </svg>
          )}

          {!!items.length && !empty && (
            <>
              <ZoomControls zoomBy={zoomBy} fit={fit} />
              <Legend types={typesOnScreen} colorOf={colorOf} />
              <div className="absolute right-3 bottom-3 text-[11px] text-slate-500 bg-white/90 border border-slate-200 rounded-lg px-2.5 py-1.5 max-w-[34%]">
                Click to open in place · 100 at a time · scroll to zoom, drag to pan
              </div>
            </>
          )}
        </div>

        {/* side panel */}
        <aside className="w-[360px] shrink-0 border-l border-slate-200 bg-white overflow-y-auto hidden md:block">
          {selectedV && (selectedV.item.kind === "asset" || selectedV.item.kind === "node" || selectedV.item.kind === "root_part") ? (
            <NodePanel key={selectedV.id}
              m={{ id: selectedV.item.id, label: selectedV.item.label, type: selectedV.item.type, degree: selectedV.item.count || 0 }}
              open={selectedV.open || selectedV.loading} colorOf={colorOf}
              onToggle={() => (isOpen(selectedV.item) ? close(selectedV.item) : open(selectedV.item))}
              onClose={() => setSelected(null)} onGoToNode={(id) => goTo(id)}
              openLabel={selectedV.item.kind === "asset" ? "Close this asset" : "Hide its relationships"}
              closedLabel={selectedV.item.kind === "asset" ? "Open: what it contains and links to" : "Show its relationships in place"}
              extra={selectedV.item.kind === "asset" ? <AssetExtras assetId={selectedV.item.id} onChanged={async () => { const id = selectedV.item.id; await refresh(); await reveal(id); setSelected(id); setFocus(id); }} /> : undefined} />
          ) : selectedV && selectedV.item.type !== "__more" ? (
            <LevelPanel key={selectedV.id} v={selectedV} page={pages[selectedV.item.key]} colorOf={colorOf}
              styleOf={styleOf} open={selectedV.open || selectedV.loading}
              onToggle={() => (isOpen(selectedV.item) ? close(selectedV.item) : open(selectedV.item))}
              onMore={() => loadMore(selectedV.item.key)} onClose={() => setSelected(null)}
              onPick={(it) => { setSelected(it.id); setFocus(it.id); }} />
          ) : (
            <OverviewPanel top={top} results={results} searching={searching} colorOf={colorOf}
              onPick={(it) => goTo(it.id, it)} onPickRoot={(id) => setSelected(id)} />
          )}
        </aside>
      </div>
    </div>
  );
}

// ── panels ─────────────────────────────────────────────────────────────────────────────────────────

function OverviewPanel({ top, results, searching, colorOf, onPick, onPickRoot }: {
  top: TreeTop | null; results: TreeItem[] | null; searching: boolean; colorOf: (t: string) => Swatch;
  onPick: (it: TreeItem) => void; onPickRoot: (id: string) => void;
}) {
  const root = top?.items.find((i) => i.kind === "root");
  const parts = (top?.items || []).filter((i) => i.kind === "root_part");
  return (
    <div>
      <div className="px-5 py-4 border-b border-slate-200">
        <div className="text-sm font-semibold text-slate-900">{root?.label || "Workspace"}</div>
        <div className="text-xs text-slate-500 mt-0.5">
          {root?.mode === "team" ? "Team workspace" : root?.mode === "person" ? "Personal workspace" : "Workspace"}
          {" · "}click it to see its sources
        </div>
      </div>
      {(results || searching) && (
        <Section title="Search results" right={results ? <span className="text-[11px] text-slate-400">{fmt(results.length)}</span> : undefined}>
          {searching ? <div className="text-xs text-slate-400">Searching…</div>
            : !results!.length ? <div className="text-xs text-slate-400">No matches.</div>
            : (
              <table className="w-full text-xs">
                <tbody>
                  {results!.map((it) => (
                    <tr key={it.id} className="border-t border-slate-100 first:border-t-0 cursor-pointer hover:bg-slate-50" onClick={() => onPick(it)} title="Show it in the graph">
                      <td className="py-1.5 pr-2 max-w-[190px] truncate text-slate-800" title={it.label}>{it.label || "—"}</td>
                      <td className="py-1.5">
                        <span className="inline-flex items-center gap-1 text-slate-600">
                          <span className="w-1.5 h-1.5 rounded-full" style={{ background: colorOf(it.type).stroke }} />
                          {it.kind === "asset" ? `${it.type} (asset)` : it.type}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
        </Section>
      )}
      <Section title="Summary">
        <div className="grid grid-cols-2 gap-2">
          <Stat label="Assets" value={fmt(top?.assets)} />
          <Stat label="Sources" value={fmt(top?.sources)} />
          <Stat label="Nodes" value={fmt(top?.run?.nodes)} />
          <Stat label="Unconnected parts" value={fmt(top?.roots_total)} />
        </div>
      </Section>
      {!!parts.length && (
        <Section title="Not from any asset">
          <p className="text-xs text-slate-500 mb-2">Parts of the graph no asset reaches — authored by hand, or imported without a file. Each is a root of its own.</p>
          <table className="w-full text-xs">
            <tbody>
              {parts.map((it) => (
                <tr key={it.id} onClick={() => onPickRoot(it.id)} className="border-t border-slate-100 cursor-pointer hover:bg-slate-50">
                  <td className="py-1.5 pr-2 max-w-[190px] truncate text-slate-800" title={it.label}>{it.label}</td>
                  <td className="py-1.5 text-right tabular-nums text-slate-600">{fmt(it.size)} nodes</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}
    </div>
  );
}

/** A level of the tree (workspace, source, tag, group, the unconnected bundle): what it holds, a page at a time. */
function LevelPanel({ v, page, colorOf, styleOf, open, onToggle, onMore, onClose, onPick }: {
  v: { item: TreeItem }; page?: TreePage; colorOf: (t: string) => Swatch; styleOf: (it: TreeItem) => Swatch;
  open: boolean; onToggle: () => void; onMore: () => void; onClose: () => void; onPick: (it: TreeItem) => void;
}) {
  const it = v.item;
  const what: Record<string, string> = {
    root: it.type && it.type !== "Workspace" ? `${it.type} · workspace` : "Workspace", source: "Source", tag: it.auto ? "Tag (file extension)" : "Tag", group: "Group", bundle: "Unconnected parts",
  };
  const child: Record<string, string> = { root: "sources", source: "tags", tag: "assets", group: "items", bundle: "parts" };
  const types = useMemo(() => {
    const c: Record<string, number> = {};
    for (const x of page?.items || []) if (!isPill(x.kind)) c[x.type] = (c[x.type] || 0) + 1;
    return c;
  }, [page]);
  return (
    <div>
      <PanelHeader title={it.label} badge={what[it.kind] || it.kind} color={styleOf(it).stroke} onClose={onClose} />
      <ToggleButton open={open} onClick={onToggle} openLabel="Close" closedLabel={`Open in place (${fmt(it.count)} ${child[it.kind] || ""})`} />
      <Section title="Summary">
        <div className="grid grid-cols-2 gap-2">
          <Stat label={it.kind === "root" || it.kind === "source" || it.kind === "tag" ? "Assets" : "Items"} value={fmt(it.count)} />
          <Stat label="Read" value={page ? `${fmt(page.next)} of ${fmt(page.total)}` : "—"} />
        </div>
        {page?.grouped && <p className="text-xs text-slate-500 mt-2">More than 1,000 — grouped first. Open a group to page through it.</p>}
      </Section>
      {page && !!Object.keys(types).length && it.kind !== "root" && it.kind !== "source" && (
        <Section title="Types"><TypeBars types={types} colorOf={colorOf} /></Section>
      )}
      {page && (
        <Section title={`Contents`} right={<span className="text-[11px] text-slate-400">{fmt(page.items.length)} shown</span>}>
          <table className="w-full text-xs">
            <tbody>
              {page.items.slice(0, 200).map((x) => (
                <tr key={x.id} className="border-t border-slate-100 first:border-t-0 cursor-pointer hover:bg-slate-50" onClick={() => onPick(x)}>
                  <td className="py-1.5 pr-2 max-w-[200px] truncate text-slate-800" title={x.label}>{x.label || "—"}</td>
                  <td className="py-1.5 text-right tabular-nums text-slate-500">{x.count != null ? fmt(x.count) : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {page.next < page.total && (
            <button onClick={onMore} className="mt-2 w-full px-3 py-1.5 rounded-lg border border-slate-200 text-xs font-medium text-slate-700 hover:bg-slate-50">
              Load the next {fmt(Math.min(100, page.total - page.next))}
            </button>
          )}
        </Section>
      )}
    </div>
  );
}

/** An asset's tags, editable here, and the way to its page. */
function AssetExtras({ assetId, onChanged }: { assetId: string; onChanged: () => void }) {
  return (
    <Section title="Tags">
      <AssetTags assetId={assetId} onSaved={onChanged} />
      <Link href={`/dashboard/catalogue/${assetId}` as any}
        className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium text-blue-600 hover:underline">
        <ExternalLink size={12} /> Open the asset page
      </Link>
    </Section>
  );
}
