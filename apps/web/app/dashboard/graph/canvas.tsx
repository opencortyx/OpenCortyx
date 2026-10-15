"use client";
// The graph canvas's building blocks, apart from what it draws (GraphTree.tsx): colours, the pan/zoom
// viewport, organic placement, and the side panel's pieces. The rules: things open IN PLACE around
// what opened them, placement is a jittered golden-angle spiral (organic, stable — nothing on screen
// ever moves), and every change is one static redraw.
import { useCallback, useEffect, useRef, useState } from "react";
import { Minus, Plus, Maximize, X } from "lucide-react";
import { contextV2Api } from "@/lib/api/contextV2";

// ── colours: one per node type, stable for the session (assigned in order of first sight) ─────────
export const PALETTE = [
  { stroke: "#10b981", fill: "#d1fae5" },
  { stroke: "#f59e0b", fill: "#fef3c7" },
  { stroke: "#3b82f6", fill: "#dbeafe" },
  { stroke: "#8b5cf6", fill: "#ede9fe" },
  { stroke: "#ec4899", fill: "#fce7f3" },
  { stroke: "#0d9488", fill: "#ccfbf1" },
  { stroke: "#f97316", fill: "#ffedd5" },
  { stroke: "#6366f1", fill: "#e0e7ff" },
  { stroke: "#84cc16", fill: "#ecfccb" },
  { stroke: "#e11d48", fill: "#ffe4e6" },
];
export type Swatch = { stroke: string; fill: string };
export type Point = { x: number; y: number };
export type View = { x: number; y: number; w: number; h: number };
export const MUTED: Swatch = { stroke: "#94a3b8", fill: "#f1f5f9" };
export const ACCENT = "#2563eb";
export const HIT = "#f59e0b";
export const R_ITEM = 62;            // footprint of one collapsed item: its circle plus room for its label
export const SPACE_X = 150, SPACE_Y = 78;  // the room one item and its label need, across and down
export const SPIRAL_STEP = 46;             // how fast the placement spiral widens
export const GOLDEN = Math.PI * (3 - Math.sqrt(5));
const FIT_MAX_ZOOM = 1.1;                  // auto-fit never magnifies more than this (screen px per unit)
export const LABEL_ZOOM = 0.85;            // screen px per layout unit above which every edge is labelled

/** A stable number in [0, 1) from a string — the jitter that makes placement look organic yet repeatable. */
export const unit = (s: string, salt: number) => {
  let h = 2166136261 ^ salt;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  h = Math.imul(h ^ (h >>> 15), 2246822507);
  return ((h ^ (h >>> 13)) >>> 0) / 4294967296;
};
/** What an edge says: its commonest relationship, "+n" more kinds, and "×n" when it stands for many. */
export const edgeLabel = (l: { edges: number; types: Record<string, number> }) => {
  const kinds = Object.entries(l.types).sort((a, b) => b[1] - a[1]);
  if (!kinds.length) return "";
  let t = kinds[0][0];
  if (kinds.length > 1) t += ` +${kinds.length - 1}`;
  if (l.edges > 1) t += ` ×${l.edges.toLocaleString()}`;
  return t;
};
export const fmt = (n: number | undefined | null) => (n ?? 0).toLocaleString();
export const clip = (s: string, n = 24) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
export const ago = (iso?: string | null) => {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
};

/** One colour per node type, assigned on first sight and kept for the session. */
export function useTypeColors() {
  const colors = useRef(new Map<string, number>());
  return useCallback((type: string): Swatch => {
    const m = colors.current;
    if (!m.has(type)) m.set(type, m.size % PALETTE.length);
    return PALETTE[m.get(type)!];
  }, []);
}

// ── placement ─────────────────────────────────────────────────────────────────────────────────────

/** Spots on a jittered golden-angle spiral around `o`, taking the first whose label clears every
 *  label already placed in `pos`. Same data, same picture; no grid, no physics. */
export function spiralSpot(pos: Map<string, Point>, o: Point, id: string): Point {
  const clear = (q: Point) => {
    for (const p of pos.values()) {
      const dx = (p.x - q.x) / SPACE_X, dy = (p.y - q.y) / SPACE_Y;
      if (dx * dx + dy * dy < 1) return false;
    }
    return true;
  };
  const a0 = unit(id, 1) * 2 * Math.PI;
  for (let k = 0; k < 5000; k++) {
    const r = SPIRAL_STEP * (1 + Math.sqrt(k)) * (0.8 + 0.4 * unit(id, k + 7));
    const a = a0 + k * GOLDEN;
    const q = { x: o.x + 1.4 * r * Math.cos(a), y: o.y + r * Math.sin(a) };
    if (clear(q)) return q;
  }
  return { x: o.x, y: o.y + SPACE_Y * (pos.size + 1) };
}

export function bboxOf(pos: Map<string, Point>): View {
  if (!pos.size) return { x: -400, y: -300, w: 800, h: 600 };
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const p of pos.values()) {
    x0 = Math.min(x0, p.x - R_ITEM); x1 = Math.max(x1, p.x + R_ITEM);
    y0 = Math.min(y0, p.y - R_ITEM); y1 = Math.max(y1, p.y + R_ITEM);
  }
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

// ── the viewport: pan by dragging, zoom with the wheel ────────────────────────────────────────────

export function usePanZoom() {
  const wrapRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const [dims, setDims] = useState({ w: 900, h: 600 });
  const [view, setView] = useState<View | null>(null);
  const drag = useRef<{ x: number; y: number; v: View; moved: boolean } | null>(null);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setDims({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setDims({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  /** Show the box `b` (layout units), padded, at the canvas's aspect ratio — never magnified past
   *  FIT_MAX_ZOOM, or a small graph (a workspace with one file) is blown up to giant circles. Items
   *  are drawn for about one layout unit per screen pixel; zooming in by hand is still free. */
  const frame = useCallback((b: View) => {
    const pad = 40;
    const w = Math.max(b.w + 2 * pad, 300), h = Math.max(b.h + 2 * pad, 200);
    const aspect = dims.w / Math.max(1, dims.h);
    const vw = Math.max(w, h * aspect, dims.w / FIT_MAX_ZOOM), vh = vw / aspect;
    setView({ x: b.x + b.w / 2 - vw / 2, y: b.y + b.h / 2 - vh / 2, w: vw, h: vh });
  }, [dims.w, dims.h]);

  const zoomBy = useCallback((f: number, cx?: number, cy?: number) => {
    setView((v) => {
      if (!v) return v;
      const px = cx ?? v.x + v.w / 2, py = cy ?? v.y + v.h / 2;
      const w = Math.min(Math.max(v.w * f, 120), 200000), h = w * (v.h / v.w);
      return { x: px - (px - v.x) * (w / v.w), y: py - (py - v.y) * (h / v.h), w, h };
    });
  }, []);

  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      if (!view) return;
      const r = el.getBoundingClientRect();
      zoomBy(e.deltaY > 0 ? 1.15 : 1 / 1.15,
        view.x + ((e.clientX - r.left) / r.width) * view.w, view.y + ((e.clientY - r.top) / r.height) * view.h);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  });

  const svgProps = {
    onPointerDown: (e: React.PointerEvent) => {
      if (e.button !== 0 || !view) return;
      drag.current = { x: e.clientX, y: e.clientY, v: view, moved: false };
    },
    onPointerMove: (e: React.PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      const dx = e.clientX - d.x, dy = e.clientY - d.y;
      if (!d.moved && Math.hypot(dx, dy) < 4) return;
      d.moved = true;
      const s = d.v.w / dims.w;
      setView({ ...d.v, x: d.v.x - dx * s, y: d.v.y - dy * s });
    },
    onPointerUp: () => { setTimeout(() => { drag.current = null; }, 0); },
    onPointerLeave: () => { drag.current = null; },
  };
  /** True while the pointer is dragging the canvas — a click then is the end of a pan, not a pick. */
  const dragging = () => !!drag.current?.moved;
  const zoom = view ? dims.w / view.w : 1;               // screen px per layout unit
  return { wrapRef, svgRef, dims, view, frame, zoomBy, svgProps, dragging, zoom };
}

/** Frame what just opened — `focus` and its children — or everything when nothing did. */
export function frameFocus(frame: (b: View) => void, all: View, pts: Point[]) {
  if (pts.length > 1) {
    const xs = pts.map((q) => q.x), ys = pts.map((q) => q.y);
    const x0 = Math.min(...xs) - R_ITEM, y0 = Math.min(...ys) - R_ITEM;
    frame({ x: x0, y: y0, w: Math.max(...xs) + R_ITEM - x0, h: Math.max(...ys) + R_ITEM - y0 });
  } else frame(all);
}

// ── pieces ─────────────────────────────────────────────────────────────────────────────────────────

export const GRID_BACKGROUND: React.CSSProperties = {
  backgroundColor: "#f8fafc",
  backgroundImage: "linear-gradient(#eef2f6 1px, transparent 1px), linear-gradient(90deg, #eef2f6 1px, transparent 1px)",
  backgroundSize: "40px 40px",
};

export function ZoomControls({ zoomBy, fit }: { zoomBy: (f: number) => void; fit: () => void }) {
  return (
    <div className="absolute right-3 top-3 flex flex-col rounded-lg border border-slate-200 bg-white overflow-hidden">
      <button onClick={() => zoomBy(1 / 1.3)} className="p-1.5 hover:bg-slate-50 text-slate-600" title="Zoom in"><Plus size={14} /></button>
      <button onClick={() => zoomBy(1.3)} className="p-1.5 hover:bg-slate-50 text-slate-600 border-t border-slate-200" title="Zoom out"><Minus size={14} /></button>
      <button onClick={fit} className="p-1.5 hover:bg-slate-50 text-slate-600 border-t border-slate-200" title="Fit to screen"><Maximize size={14} /></button>
    </div>
  );
}

export function Legend({ types, colorOf }: { types: [string, number][]; colorOf: (t: string) => Swatch }) {
  return (
    <div className="absolute left-3 bottom-3 flex flex-wrap gap-x-3 gap-y-1 max-w-[65%] bg-white/90 border border-slate-200 rounded-lg px-3 py-2">
      {types.map(([t]) => (
        <span key={t} className="flex items-center gap-1.5 text-[11px] text-slate-700">
          <span className="w-2.5 h-2.5 rounded-full border-2" style={{ background: colorOf(t).fill, borderColor: colorOf(t).stroke }} />
          {t}
        </span>
      ))}
    </div>
  );
}

export function CenterNote({ title, body, action }: { title: string; body?: string; action?: { label: string; onClick: () => void } }) {
  return (
    <div className="absolute inset-0 flex items-center justify-center p-6">
      <div className="text-center max-w-sm">
        <div className="text-sm font-semibold text-slate-800">{title}</div>
        {body && <div className="text-xs text-slate-500 mt-1.5 break-words">{body}</div>}
        {action && (
          <button onClick={action.onClick} className="mt-3 px-3 py-1.5 rounded-lg bg-blue-600 text-white text-xs font-medium hover:bg-blue-700">
            {action.label}
          </button>
        )}
      </div>
    </div>
  );
}

export function Section({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="px-5 py-4 border-b border-slate-100">
      <div className="flex items-center justify-between mb-2.5">
        <h3 className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">{title}</h3>
        {right}
      </div>
      {children}
    </section>
  );
}

export function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 px-3 py-2 min-w-0">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-sm font-semibold text-slate-900 tabular-nums mt-0.5 truncate">{value}</div>
    </div>
  );
}

export function TypeBars({ types, colorOf }: { types: Record<string, number>; colorOf: (t: string) => Swatch }) {
  const rows = Object.entries(types || {}).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const total = rows.reduce((a, [, n]) => a + n, 0) || 1;
  if (!rows.length) return <div className="text-xs text-slate-400">No types.</div>;
  return (
    <table className="w-full text-xs">
      <tbody>
        {rows.map(([t, n]) => (
          <tr key={t}>
            <td className="py-1 pr-2 text-slate-700 truncate max-w-[120px]" title={t}>{t}</td>
            <td className="py-1 w-full">
              <div className="h-2 rounded-full bg-slate-100 overflow-hidden">
                <div className="h-full rounded-full" style={{ width: `${(100 * n) / total}%`, background: colorOf(t).stroke }} />
              </div>
            </td>
            <td className="py-1 pl-2 text-right tabular-nums text-slate-600">{fmt(n)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function PanelHeader({ title, badge, color, onClose }: { title: string; badge: string; color: string; onClose: () => void }) {
  return (
    <div className="px-5 py-4 border-b border-slate-200 flex items-start justify-between gap-3 sticky top-0 bg-white z-10">
      <div className="min-w-0">
        <div className="text-sm font-semibold text-slate-900 break-words leading-snug">{title}</div>
        <span className="inline-flex items-center gap-1.5 mt-1.5 text-[11px] font-medium text-slate-600">
          <span className="w-2 h-2 rounded-full" style={{ background: color }} /> {badge}
        </span>
      </div>
      <button onClick={onClose} className="text-slate-400 hover:text-slate-700 p-1 rounded-md hover:bg-slate-100" aria-label="Close">
        <X size={16} />
      </button>
    </div>
  );
}

export function ToggleButton({ open, onClick, openLabel, closedLabel }: { open: boolean; onClick: () => void; openLabel: string; closedLabel: string }) {
  return (
    <div className="px-5 pt-4">
      <button onClick={onClick}
        className={`w-full px-3 py-2 rounded-lg text-xs font-semibold ${open ? "border border-slate-300 text-slate-700 hover:bg-slate-50" : "bg-blue-600 text-white hover:bg-blue-700"}`}>
        {open ? openLabel : closedLabel}
      </button>
    </div>
  );
}

/** A node's properties and relationships, read fresh when it is selected. */
export function NodePanel({ m, open, colorOf, onToggle, onClose, onGoToNode, openLabel = "Hide its neighbours",
  closedLabel = "Show its neighbours in place", extra }: {
  m: { id: string; label: string; type: string; degree: number }; open: boolean; colorOf: (t: string) => Swatch;
  onToggle: () => void; onClose: () => void; onGoToNode: (id: string) => void;
  openLabel?: string; closedLabel?: string; extra?: React.ReactNode;
}) {
  type Rel = { dir: "out" | "in"; label: string; otherId: string; other: string; otherType: string };
  const [node, setNode] = useState<any>(null);
  const [rels, setRels] = useState<Rel[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    contextV2Api.getNode(m.id, 1, true).then((r: any) => {
      if (cancelled) return;
      setNode(r.node || {});
      const g = r.graph || { nodes: [], edges: [] };
      const byId = new Map<string, any>((g.nodes || []).map((n: any) => [n.id, n]));
      const out: Rel[] = [];
      for (const e of g.edges || []) {
        const dir = e.source === m.id ? "out" : e.target === m.id ? "in" : null;
        if (!dir) continue;
        const oid = dir === "out" ? e.target : e.source;
        const o = byId.get(oid);
        out.push({ dir, label: String(e.relation || e.label || e.type || ""), otherId: oid, other: o?.label || String(oid).slice(0, 8), otherType: o?.type || "" });
      }
      setRels(out);
    }).catch(() => { if (!cancelled) { setNode({}); setRels([]); } });
    return () => { cancelled = true; };
  }, [m.id]);

  const props = Object.entries((node?.properties || {}) as Record<string, any>).filter(([k]) => !k.startsWith("__"));
  const show = (v: any) => (v === null || v === undefined || v === "" ? <span className="text-slate-400 italic">empty</span>
    : Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v));

  return (
    <div>
      <PanelHeader title={m.label || m.type} badge={m.type} color={colorOf(m.type).stroke} onClose={onClose} />
      <ToggleButton open={open} onClick={onToggle} openLabel={openLabel} closedLabel={closedLabel} />
      {extra}
      <Section title="Summary">
        <div className="grid grid-cols-2 gap-2">
          <Stat label="Links" value={rels ? fmt(rels.length) : fmt(m.degree)} />
          <Stat label="Type" value={m.type} />
        </div>
      </Section>
      <Section title="Properties">
        {node == null ? <div className="text-xs text-slate-400">Loading…</div>
          : !props.length ? <div className="text-xs text-slate-400">No properties.</div>
          : (
            <table className="w-full text-xs">
              <tbody>
                {props.map(([k, v]) => (
                  <tr key={k} className="border-t border-slate-100 first:border-t-0 align-top">
                    <td className="py-1.5 pr-3 text-slate-500 whitespace-nowrap">{k}</td>
                    <td className="py-1.5 text-slate-800 break-words">{show(v)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </Section>
      <Section title="Relationships" right={rels ? <span className="text-[11px] text-slate-400">{rels.length}</span> : undefined}>
        {rels == null ? <div className="text-xs text-slate-400">Loading…</div>
          : !rels.length ? <div className="text-xs text-slate-400">No relationships.</div>
          : (
            <table className="w-full text-xs">
              <tbody>
                {rels.slice(0, 60).map((r, i) => (
                  <tr key={i} className="border-t border-slate-100 first:border-t-0 cursor-pointer hover:bg-slate-50" onClick={() => onGoToNode(r.otherId)} title="Show it in the graph">
                    <td className="py-1.5 pr-2 w-8">
                      <span className={`text-[9px] font-bold px-1 py-0.5 rounded ${r.dir === "out" ? "bg-blue-50 text-blue-700" : "bg-emerald-50 text-emerald-700"}`}>{r.dir.toUpperCase()}</span>
                    </td>
                    <td className="py-1.5 pr-2 font-mono text-[10px] text-slate-500 max-w-[90px] truncate" title={r.label}>{r.label}</td>
                    <td className="py-1.5 text-slate-800 max-w-[150px] truncate" title={r.other}>
                      <span className="inline-flex items-center gap-1.5">
                        {r.otherType && <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: colorOf(r.otherType).stroke }} />}
                        {r.other}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </Section>
    </div>
  );
}
