"use client";

/**
 * The declared graph — the picture of the RULES, not of the data.
 *
 * It shows the vocabulary — one box per type a rule produces, one arrow per relationship — rather than
 * instances produced from a sample. It is the thing you argue about before there is any data.
 *
 * WHAT IT DRAWS IS DECIDED IN declaredShape.ts, NOT HERE. This component used to work out the shape
 * itself, and got it wrong in a way that matters: it drew every rule including the ones set to Ignore,
 * and drew only explicit edge rules — never the containment the rules carry — so on a real mapping it
 * showed 13 types and 2 arrows for a schema that compiles to 4 types and a 3-edge chain. The shape now
 * mirrors the compiler line for line, and this file only lays it out and handles the gestures.
 *
 * Ported from the previous builder's canvas, which panned and zoomed an auto-laid-out arc of circles
 * but could not actually be arranged. Here a node is dragged where you want it, the layout is
 * remembered with the mapping, and dragging from a node's handle onto another node IS how an edge gets
 * declared. Positions live in `editor.canvas`, so they travel with the mapping and survive a reload; a
 * type with no saved position is placed by `autoLayout`.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Maximize2, ZoomIn, ZoomOut, Crosshair, MousePointer2 } from "lucide-react";
import type { EdgeRule, KeyRow } from "./model";
import { ownsNode } from "./model";
import {
  autoLayout, buildDeclaredShape, HANDLE_R, NODE_H, NODE_W, PAD,
  type CanvasPos,
} from "./declaredShape";

export type { CanvasPos } from "./declaredShape";

type Drag =
  | { kind: "node"; type: string; dx: number; dy: number }
  | { kind: "pan"; x: number; y: number }
  | { kind: "edge"; from: string; x: number; y: number }
  | null;

export default function DeclaredCanvas({
  rows, edges, positions, sel, onSelect, onMove, onConnect, onDeleteEdge,
}: {
  rows: KeyRow[];
  edges: EdgeRule[];
  positions: Record<string, CanvasPos>;
  sel: { kind: "node" | "edge"; index: number };
  onSelect: (s: { kind: "node" | "edge"; index: number }) => void;
  onMove: (next: Record<string, CanvasPos>) => void;
  onConnect: (fromType: string, toType: string) => void;
  onDeleteEdge: (index: number) => void;
}) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState<CanvasPos>({ x: 0, y: 0 });
  const [drag, setDrag] = useState<Drag>(null);
  const [ghost, setGhost] = useState<CanvasPos | null>(null);
  const [hover, setHover] = useState<string | null>(null);

  /** Every rule that names a type, with its index — resolves the panel's selection to a box. */
  const nodeRows = useMemo(
    () => rows.map((r, i) => ({ r, i })).filter((x) => x.r.kind === "node" && x.r.node_type),
    [rows]
  );

  const shape = useMemo(() => buildDeclaredShape(rows, edges), [rows, edges]);
  const cards = shape.nodes;
  const declared = useMemo(() => cards.map((c) => c.type), [cards]);
  const known = useMemo(() => new Set(declared), [declared]);

  /**
   * Attributes per type, unioned across every ACTIVE rule that declares it, and only attributes that
   * are themselves kept — an attribute set to Ignore is not a property the node will have, so counting
   * it would put a number on the box that ingestion never produces.
   */
  const attrsByType = useMemo(() => {
    const attrRows = rows.filter(
      (a) => a.kind === "attribute" && (a.becomes ?? a.role) !== "ignore" && a.role !== "skip"
    );
    const out = new Map<string, { attrs: Set<string>; ids: Set<string> }>();
    rows.forEach((r) => {
      if (!ownsNode(r) || !r.node_type) return;
      let e = out.get(r.node_type);
      if (!e) { e = { attrs: new Set(), ids: new Set() }; out.set(r.node_type, e); }
      const pre = r.path ? `${r.path}.` : "";
      for (const a of attrRows) {
        const rest = a.path.startsWith(pre) ? a.path.slice(pre.length) : null;
        if (rest === null || !rest.length || rest.includes(".") || rest.includes("[]")) continue;
        e.attrs.add(a.path);
        if (a.role === "id" || a.becomes === "id") e.ids.add(a.path);
      }
    });
    return out;
  }, [rows]);

  /** Selecting any rule in the panel lights up the box for its type. */
  const selType = useMemo(
    () => (sel.kind === "node" ? nodeRows.find((x) => x.i === sel.index)?.r.node_type || null : null),
    [sel, nodeRows]
  );

  /** Where a type sits when nobody has dragged it. Recomputed when the shape changes. */
  const auto = useMemo(() => autoLayout(shape), [shape]);

  const posOf = useCallback(
    (type: string) => positions[type] || auto[type] || { x: PAD + NODE_W / 2, y: PAD },
    [positions, auto]
  );

  /**
   * The viewBox is fitted to the content, so a mapping with thirty types is visible rather than cropped
   * — but it is FROZEN while a node is being dragged, because a box moving toward the edge would
   * otherwise grow the box it is being measured against and the whole scene would crawl under the cursor.
   */
  const viewRef = useRef({ x: 0, y: 0, w: 680, h: 380 });
  const view = useMemo(() => {
    if (drag?.kind === "node" || !cards.length) return viewRef.current;
    const pts = declared.map((t) => positions[t] || auto[t] || { x: PAD, y: PAD });
    const minX = Math.min(...pts.map((p) => p.x)) - NODE_W / 2 - PAD;
    const maxX = Math.max(...pts.map((p) => p.x)) + NODE_W / 2 + PAD;
    const minY = Math.min(...pts.map((p) => p.y)) - NODE_H / 2 - PAD;
    const maxY = Math.max(...pts.map((p) => p.y)) + NODE_H / 2 + PAD;
    return { x: minX, y: minY, w: Math.max(680, maxX - minX), h: Math.max(380, maxY - minY) };
  }, [declared, positions, auto, drag?.kind, cards.length]);
  viewRef.current = view;

  /** Screen → canvas coordinates, correct under any pan/zoom. */
  const toCanvas = useCallback((clientX: number, clientY: number): CanvasPos => {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const pt = svg.createSVGPoint();
    pt.x = clientX; pt.y = clientY;
    const ctm = svg.getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    const p = pt.matrixTransform(ctm.inverse());
    return { x: (p.x - pan.x) / zoom, y: (p.y - pan.y) / zoom };
  }, [pan, zoom]);

  useEffect(() => {
    if (!drag) return;
    const move = (e: PointerEvent) => {
      if (drag.kind === "pan") {
        setPan({ x: e.clientX - drag.x, y: e.clientY - drag.y });
        return;
      }
      const c = toCanvas(e.clientX, e.clientY);
      if (drag.kind === "node") {
        onMove({ ...positions, [drag.type]: { x: Math.round(c.x - drag.dx), y: Math.round(c.y - drag.dy) } });
      } else {
        setGhost(c);
      }
    };
    const up = (e: PointerEvent) => {
      if (drag.kind === "edge") {
        const c = toCanvas(e.clientX, e.clientY);
        const target = cards.find(({ type }) => {
          const p = posOf(type);
          return Math.abs(c.x - p.x) <= NODE_W / 2 && Math.abs(c.y - p.y) <= NODE_H / 2;
        })?.type;
        if (target && target !== drag.from) onConnect(drag.from, target);
      }
      setDrag(null); setGhost(null);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
  }, [drag, toCanvas, positions, onMove, onConnect, cards, posOf]);

  const startNodeDrag = (e: React.PointerEvent, type: string, p: CanvasPos) => {
    e.stopPropagation();
    const c = toCanvas(e.clientX, e.clientY);
    setDrag({ kind: "node", type, dx: c.x - p.x, dy: c.y - p.y });
  };

  const edgeLine = (a: CanvasPos, b: CanvasPos) => {
    // leave the source on its right handle and arrive on the target's left, so arrows never
    // disappear underneath a box
    const x1 = a.x + NODE_W / 2, y1 = a.y;
    const x2 = b.x - NODE_W / 2, y2 = b.y;
    const mx = (x1 + x2) / 2;
    return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
  };

  const autoArrange = () => {
    onMove({ ...autoLayout(shape) });
    setPan({ x: 0, y: 0 }); setZoom(1);
  };

  if (!cards.length) {
    return (
      <p className="px-3.5 py-10 text-center text-sm text-stone-500">
        {nodeRows.length ? (
          <>
            All {nodeRows.length} node rule{nodeRows.length === 1 ? " is" : "s are"} set to Ignore or Embed,
            so this mapping produces no nodes of its own — there is nothing to draw.
          </>
        ) : (
          <>No node rules yet. Add one, or propose rules from a sample — then drag them into shape here.</>
        )}
      </p>
    );
  }

  const plural = (n: number, one: string, many = `${one}s`) => (n === 1 ? one : many);

  return (
    <div>
      <div className="flex items-center gap-1.5 px-3.5 py-2 border-b border-stone-100">
        <MousePointer2 size={13} className="text-stone-400" />
        <span className="text-[11.5px] text-stone-500">
          Drag a box to arrange · drag from its right handle onto another box to declare an edge
        </span>
        <div className="ml-auto flex items-center gap-1">
          <button type="button" onClick={() => setZoom((z) => Math.max(0.4, +(z - 0.15).toFixed(2)))}
            title="Zoom out" className="p-1.5 rounded text-stone-600 hover:bg-stone-100"><ZoomOut size={14} /></button>
          <span className="text-[11px] tabular-nums text-stone-500 w-10 text-center">{Math.round(zoom * 100)}%</span>
          <button type="button" onClick={() => setZoom((z) => Math.min(2.2, +(z + 0.15).toFixed(2)))}
            title="Zoom in" className="p-1.5 rounded text-stone-600 hover:bg-stone-100"><ZoomIn size={14} /></button>
          <button type="button" onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }); }}
            title="Reset view" className="p-1.5 rounded text-stone-600 hover:bg-stone-100"><Crosshair size={14} /></button>
          <button type="button" onClick={autoArrange}
            title="Arrange automatically" className="p-1.5 rounded text-stone-600 hover:bg-stone-100"><Maximize2 size={14} /></button>
        </div>
      </div>

      <svg ref={svgRef} viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
        style={{ height: Math.min(560, Math.max(360, view.h)), touchAction: "none" }}
        // select-none: without it a drag across the canvas sweeps a text selection over every label
        // it passes, so arranging the graph leaves the picture streaked blue and looks broken.
        className={`block w-full bg-stone-50/60 select-none ${drag?.kind === "pan" ? "cursor-grabbing" : "cursor-grab"}`}
        onPointerDown={(e) => setDrag({ kind: "pan", x: e.clientX - pan.x, y: e.clientY - pan.y })}
        role="img" aria-label="Declared graph — drag to arrange, drag from a handle to connect">
        <defs>
          <marker id="dc-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M0,0 L8,4 L0,8 z" fill="#a8a29e" />
          </marker>
          <pattern id="dc-grid" width="24" height="24" patternUnits="userSpaceOnUse">
            <path d="M24 0 L0 0 0 24" fill="none" stroke="#e7e5e4" strokeWidth="1" />
          </pattern>
        </defs>
        <rect x={view.x} y={view.y} width={view.w} height={view.h} fill="url(#dc-grid)" />

        <g transform={`translate(${pan.x},${pan.y}) scale(${zoom})`}>
          {shape.edges.map((e, i) => {
            const a = posOf(e.from);
            const b = posOf(e.to);
            const isJoin = e.kind === "join";
            const on = e.ruleIndex !== undefined && sel.kind === "edge" && sel.index === e.ruleIndex;
            const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
            // A rule-backed arrow opens its edge rule; containment from nesting alone opens the child's
            // node rule, which is where that relationship is actually declared.
            const select = (ev: React.PointerEvent) => {
              ev.stopPropagation();
              if (e.ruleIndex !== undefined) onSelect({ kind: "edge", index: e.ruleIndex });
              else if (e.rowIndex !== undefined) onSelect({ kind: "node", index: e.rowIndex });
            };
            return (
              <g key={`${e.from}|${e.label}|${e.to}|${i}`} onPointerDown={select} className="cursor-pointer">
                <path d={edgeLine(a, b)} fill="none" markerEnd="url(#dc-arrow)"
                  stroke={on ? "hsl(160 60% 30%)" : isJoin ? "#d97706" : "#a8a29e"}
                  strokeWidth={on ? 2.4 : 1.6}
                  strokeDasharray={isJoin ? "" : "5 3"} />
                <rect x={mid.x - (e.label.length * 3.3 + 8)} y={mid.y - 18} rx={4}
                  width={e.label.length * 6.6 + 16} height={15}
                  fill="#fff" stroke={on ? "hsl(160 60% 30%)" : "#e7e5e4"} />
                <text x={mid.x} y={mid.y - 7} textAnchor="middle" fontSize="9" fontWeight="600"
                  fill={isJoin ? "#b45309" : "#57534e"}>{e.label}</text>
                {on && e.ruleIndex !== undefined && (
                  <g transform={`translate(${mid.x + (e.label.length * 3.3 + 16)},${mid.y - 10})`}
                     onPointerDown={(ev) => { ev.stopPropagation(); onDeleteEdge(e.ruleIndex!); }} className="cursor-pointer">
                    <circle r="7" fill="#fff" stroke="#dc2626" />
                    <path d="M-2.5,-2.5 L2.5,2.5 M2.5,-2.5 L-2.5,2.5" stroke="#dc2626" strokeWidth="1.4" />
                  </g>
                )}
              </g>
            );
          })}

          {ghost && drag?.kind === "edge" && (() => {
            if (!known.has(drag.from)) return null;
            const a = posOf(drag.from);
            return <path d={`M${a.x + NODE_W / 2},${a.y} L${ghost.x},${ghost.y}`}
              stroke="hsl(160 60% 30%)" strokeWidth="2" strokeDasharray="4 3" fill="none" />;
          })()}

          {cards.map(({ type, rowIndex: i, rules }) => {
            const p = posOf(type);
            const on = selType === type;
            const label = type.length > 16 ? `${type.slice(0, 15)}…` : type;
            const sum = attrsByType.get(type);
            const attrs = sum ? sum.attrs.size : 0;
            const ids = sum ? sum.ids.size : 0;
            return (
              <g key={type} data-node-type={type} transform={`translate(${p.x},${p.y})`}
                 className={drag?.kind === "node" && drag.type === type ? "cursor-grabbing" : "cursor-move"}
                 onPointerDown={(e) => { startNodeDrag(e, type, p); onSelect({ kind: "node", index: i }); }}
                 onPointerEnter={() => setHover(type)} onPointerLeave={() => setHover(null)}>
                <rect x={-NODE_W / 2} y={-NODE_H / 2} width={NODE_W} height={NODE_H} rx={9}
                  fill="#fff" stroke={on ? "hsl(160 60% 30%)" : "#d6d3d1"} strokeWidth={on ? 2.2 : 1.3} />
                <text x={0} y={-2} textAnchor="middle" fontSize="12" fontWeight="600" fill="#1c1917">{label}</text>
                <text x={0} y={12} textAnchor="middle" fontSize="9" fill="#78716c">
                  {ids ? `${ids} id · ` : ""}{attrs} attr{rules > 1 ? ` · ${rules} rules` : ""}
                </text>
                {/* the connect handle: this is how an edge is declared */}
                <circle cx={NODE_W / 2} cy={0} r={HANDLE_R}
                  fill={hover === type || (drag?.kind === "edge" && drag.from === type) ? "hsl(160 60% 30%)" : "#fff"}
                  stroke="hsl(160 60% 30%)" strokeWidth="1.6"
                  className="cursor-crosshair"
                  onPointerDown={(e) => { e.stopPropagation(); setDrag({ kind: "edge", from: type, x: 0, y: 0 }); }} />
              </g>
            );
          })}
        </g>
      </svg>

      <div className="flex flex-wrap gap-x-4 gap-y-1 px-3.5 py-2 border-t border-stone-200 bg-stone-50/70 text-[11.5px] text-stone-500">
        <span>
          <b className="text-stone-800 font-semibold tabular-nums">{cards.length}</b> {plural(cards.length, "type")}
          {shape.activeRules !== cards.length && (
            <> from <b className="text-stone-800 font-semibold tabular-nums">{shape.activeRules}</b> rules</>
          )}
        </span>
        <span>
          <b className="text-stone-800 font-semibold tabular-nums">{shape.edges.length}</b> {plural(shape.edges.length, "edge")}
        </span>
        {shape.ignoredRules > 0 && (
          <span title="Rules set to Ignore produce nothing at ingest, so they are not drawn.">
            <b className="text-stone-700 font-semibold tabular-nums">{shape.ignoredRules}</b> {plural(shape.ignoredRules, "rule")} set
            to Ignore — not drawn, not ingested
          </span>
        )}
        {shape.embeddedRules > 0 && (
          <span title="Embedded rules are ingested, but their fields fold into the parent node.">
            <b className="text-stone-700 font-semibold tabular-nums">{shape.embeddedRules}</b> embedded into
            {shape.embeddedRules === 1 ? " its" : " their"} parent
          </span>
        )}
        <span className="inline-flex items-center gap-1.5">
          <svg width="20" height="6"><line x1="0" y1="3" x2="20" y2="3" stroke="#d97706" strokeWidth="2" /></svg>
          matched by value
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="20" height="6"><line x1="0" y1="3" x2="20" y2="3" stroke="#a8a29e" strokeWidth="2" strokeDasharray="5 3" /></svg>
          containment
        </span>
      </div>

      {shape.dangling.length > 0 && (
        <div className="px-3.5 py-2.5 border-t border-stone-200 bg-amber-50 text-[12px] text-amber-800">
          <div className="font-semibold flex items-center gap-1.5">
            <AlertTriangle size={12} />
            {shape.dangling.length} {plural(shape.dangling.length, "rule names", "rules name")} a type that is not drawn
          </div>
          <ul className="mt-1 space-y-0.5 text-amber-700">
            {shape.dangling.map((d, i) => (
              <li key={i}>
                <span className="font-mono">{d.from} → {d.to}</span>
                <span className="text-amber-600"> ({d.label})</span>: {d.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
