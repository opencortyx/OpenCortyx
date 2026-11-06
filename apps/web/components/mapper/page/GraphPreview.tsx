"use client";

/**
 * How a file's kinds look in the graph: one circle per kind, one arrow per relationship, redrawn as
 * the page is edited. Kinds read from other files that a relationship reaches are drawn dashed.
 */

export interface PreviewNode {
  name: string;
  count: number;
  event: boolean;
  inGraph: boolean;
  /** read from this file (else: another file a relationship reaches) */
  here: boolean;
  /** the line under the name, instead of the count */
  note?: string;
}
export interface PreviewLink { from: string; to: string; name: string }

const W = 680;
const R = 40;
const PER_ROW = 5;

const short = (s: string) => (s.length > 14 ? s.slice(0, 13) + "…" : s);

export default function GraphPreview({ nodes, links }: { nodes: PreviewNode[]; links: PreviewLink[] }) {
  if (!nodes.length) return null;
  const rows = Math.ceil(nodes.length / PER_ROW);
  const rowH = 170;
  const H = rows * rowH + 10;
  const pos = new Map<string, { x: number; y: number }>();
  nodes.forEach((n, k) => {
    const row = Math.floor(k / PER_ROW);
    const inRow = Math.min(PER_ROW, nodes.length - row * PER_ROW);
    const gap = W / (inRow + 1);
    pos.set(n.name, { x: gap * ((k % PER_ROW) + 1), y: row * rowH + 112 });
  });
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="How these kinds look in the graph"
         className="block w-full min-w-[540px] h-auto text-stone-500 dark:text-stone-400">
      <defs>
        <marker id="mapper-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0 0L10 5L0 10z" fill="currentColor" />
        </marker>
      </defs>
      {links.map((l, k) => {
        const a = pos.get(l.from), b = pos.get(l.to);
        if (!a || !b || l.from === l.to) return null;
        const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
        const ux = dx / d, uy = dy / d;
        // a straight line, unless a circle stands between the two: then a bow above it
        const between = Math.abs(dy) < 1 && Array.from(pos.values()).some((p) =>
          Math.abs(p.y - a.y) < 1 && p.x > Math.min(a.x, b.x) + 1 && p.x < Math.max(a.x, b.x) - 1);
        const bow = between ? -Math.min(64, d / 4) : 0;
        const x1 = a.x + ux * R, y1 = a.y + uy * R + (bow ? -R / 2 : 0);
        const x2 = b.x - ux * (R + 6), y2 = b.y - uy * (R + 6) + (bow ? -R / 2 : 0);
        const cx = (x1 + x2) / 2, cy = (y1 + y2) / 2 + bow;
        // the curve's own midpoint, the label just above it
        const lx = cx, ly = (y1 + 2 * cy + y2) / 4 - 7;
        // a name longer than the line between the circles is shortened (the full name on hover)
        const room = Math.max(4, Math.floor(Math.hypot(x2 - x1, y2 - y1) / 6.6));
        const label = l.name.length > room ? l.name.slice(0, room - 1) + "…" : l.name;
        return (
          <g key={k}>
            <path d={`M${x1} ${y1} Q${cx} ${cy} ${x2} ${y2}`} fill="none" stroke="currentColor" strokeWidth={1.4}
                  markerEnd="url(#mapper-arrow)" />
            <text x={lx} y={ly} textAnchor="middle" className="fill-stone-500 dark:fill-stone-400"
                  style={{ font: "500 10.5px ui-monospace, Consolas, monospace" }}><title>{l.name}</title>{label}</text>
          </g>
        );
      })}
      {nodes.map((n) => {
        const p = pos.get(n.name)!;
        const tone = !n.here ? "fill-stone-50 stroke-stone-300 dark:fill-stone-900 dark:stroke-stone-600"
          : n.event ? "fill-violet-50 stroke-violet-500 dark:fill-violet-950 dark:stroke-violet-400"
          : "fill-teal-50 stroke-teal-600 dark:fill-teal-950 dark:stroke-teal-400";
        return (
          <g key={n.name} opacity={n.inGraph || !n.here ? 1 : 0.45}>
            <title>{n.name}</title>
            <circle cx={p.x} cy={p.y} r={R} className={tone} strokeWidth={1.5}
                    strokeDasharray={!n.here || !n.inGraph ? "4 4" : undefined} />
            <text x={p.x} y={p.y + 2} textAnchor="middle" className="fill-stone-900 dark:fill-stone-100"
                  style={{ font: "600 12.5px system-ui, sans-serif" }}>{short(n.name)}</text>
            <text x={p.x} y={p.y + 17} textAnchor="middle" className="fill-stone-500 dark:fill-stone-400"
                  style={{ font: "10.5px system-ui, sans-serif" }}>
              {n.note ?? (!n.here ? "another file" : `${n.count.toLocaleString()}${n.inGraph ? "" : " · not in graph"}`)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
