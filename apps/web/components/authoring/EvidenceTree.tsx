"use client";

import { useMemo, useState } from "react";
import {
  Network,
  List,
  Layers,
  Tag,
  Key,
  Calendar,
  FileText,
  Hash,
  Link as LinkIcon,
  ChevronDown,
  ChevronRight,
} from "lucide-react";
import type { DraftEvidence } from "@/lib/api/contextV2";

export interface TreeNode {
  path: string;
  nodeType: string;
  basis?: string;
  attributes: Array<{
    name: string;
    path: string;
    role: string;
    sampleValue: string;
    rawType: string;
  }>;
  children: TreeNode[];
}

const NODE_REGEX = /^(?:(.+?)\s+)?becomes a\s+([A-Za-z0-9_]+)\s+node$/i;
const EDGE_REGEX = /^(.+?)\s+-([A-Za-z0-9_]+)->\s+(.+)$/;

function getRoleBadge(role: string) {
  const r = role.toLowerCase();
  if (r.includes("identifier") || r.includes("identity") || r.includes("id")) {
    return {
      label: "ID",
      icon: Key,
      className: "bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-950/60 dark:text-blue-300 dark:border-blue-800",
    };
  }
  if (r.includes("date") || r.includes("time")) {
    return {
      label: "Date",
      icon: Calendar,
      className: "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/60 dark:text-emerald-300 dark:border-emerald-800",
    };
  }
  if (r.includes("prose") || r.includes("text") || r.includes("document")) {
    return {
      label: "Text",
      icon: FileText,
      className: "bg-purple-50 text-purple-700 border-purple-200 dark:bg-purple-950/60 dark:text-purple-300 dark:border-purple-800",
    };
  }
  if (r.includes("link") || r.includes("fk")) {
    return {
      label: "Link",
      icon: LinkIcon,
      className: "bg-cyan-50 text-cyan-700 border-cyan-200 dark:bg-cyan-950/60 dark:text-cyan-300 dark:border-cyan-800",
    };
  }
  if (r.includes("number") || r.includes("measure") || r.includes("aggregate")) {
    return {
      label: "Number",
      icon: Hash,
      className: "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/60 dark:text-amber-300 dark:border-amber-800",
    };
  }
  return {
    label: "Property",
    icon: Tag,
    className: "bg-stone-100 text-stone-600 border-stone-200 dark:bg-stone-800 dark:text-stone-300 dark:border-stone-700",
  };
}

function parseBasis(basis: string): { rawType: string; sampleValue: string } {
  const parts = basis.split(":");
  if (parts.length >= 2) {
    const rawType = parts[0].trim();
    let sampleValue = parts.slice(1).join(":").trim();
    if (sampleValue.startsWith("'") && sampleValue.endsWith("'")) {
      sampleValue = sampleValue.slice(1, -1);
    }
    return { rawType, sampleValue };
  }
  return { rawType: "", sampleValue: basis };
}

export function parseEvidenceTree(evidence: DraftEvidence[], defaultEntityName?: string): {
  isTree: boolean;
  roots: TreeNode[];
  relationships: DraftEvidence[];
  unattached: DraftEvidence[];
} {
  const nodeMap = new Map<string, TreeNode>();
  const relationships: DraftEvidence[] = [];
  const unattached: DraftEvidence[] = [];
  const nodeItems: { path: string; nodeType: string; basis?: string }[] = [];
  const attrItems: DraftEvidence[] = [];

  for (const item of evidence) {
    const nodeMatch = item.claim.match(NODE_REGEX);
    const edgeMatch = item.claim.match(EDGE_REGEX);
    if (nodeMatch) {
      const path = (item.path ?? nodeMatch[1] ?? "").trim();
      const nodeType = nodeMatch[2];
      nodeItems.push({ path, nodeType, basis: item.basis });
    } else if (edgeMatch) {
      relationships.push(item);
    } else if (item.claim.includes("→")) {
      attrItems.push(item);
    } else {
      unattached.push(item);
    }
  }

  if (nodeItems.length === 0) {
    // If all items are `column → role`, synthesize a Table entity node
    const isColumnList = attrItems.length > 0 && attrItems.every((a) => a.claim.includes("→"));
    if (isColumnList) {
      const rootNode: TreeNode = {
        path: "table",
        nodeType: defaultEntityName || "Table Entity",
        basis: "table rows become entity nodes",
        attributes: attrItems.map((attr) => {
          const fieldName = (attr.path || attr.claim.split("→")[0] || "").trim();
          const role = attr.claim.split("→")[1].trim();
          const { rawType, sampleValue } = parseBasis(attr.basis);
          return { name: fieldName, path: fieldName, role, sampleValue, rawType };
        }),
        children: [],
      };
      return { isTree: true, roots: [rootNode], relationships, unattached };
    }
    return { isTree: false, roots: [], relationships: [], unattached: evidence };
  }

  // Populate map
  for (const n of nodeItems) {
    nodeMap.set(n.path, {
      path: n.path,
      nodeType: n.nodeType,
      basis: n.basis,
      attributes: [],
      children: [],
    });
  }

  const sortedNodePaths = Array.from(nodeMap.keys()).sort((a, b) => b.length - a.length);

  // Distribute attributes to the closest enclosing node
  for (const attr of attrItems) {
    const attrPath = (attr.path || attr.claim.split("→")[0] || "").trim();
    let assigned = false;
    for (const np of sortedNodePaths) {
      if (
        attrPath.startsWith(np) &&
        (attrPath.length === np.length || attrPath[np.length] === "." || attrPath[np.length] === "[")
      ) {
        let fieldName = attrPath.slice(np.length).replace(/^[.\[\]]+/, "");
        if (!fieldName) fieldName = attrPath;
        const role = attr.claim.includes("→") ? attr.claim.split("→")[1].trim() : attr.claim;
        const { rawType, sampleValue } = parseBasis(attr.basis);

        nodeMap.get(np)!.attributes.push({
          name: fieldName,
          path: attrPath,
          role,
          sampleValue,
          rawType,
        });
        assigned = true;
        break;
      }
    }
    if (!assigned) {
      // If there is only ONE node and this is a column attribute, attach to that node
      if (sortedNodePaths.length === 1 && (attr.claim.includes("→") || attr.path)) {
        const np = sortedNodePaths[0];
        const fieldName = attrPath.includes(".") ? attrPath.split(".").pop()! : attrPath;
        const role = attr.claim.includes("→") ? attr.claim.split("→")[1].trim() : attr.claim;
        const { rawType, sampleValue } = parseBasis(attr.basis);
        nodeMap.get(np)!.attributes.push({
          name: fieldName,
          path: attrPath,
          role,
          sampleValue,
          rawType,
        });
        assigned = true;
      } else {
        unattached.push(attr);
      }
    }
  }

  // Connect parent-child node relationships
  const roots: TreeNode[] = [];
  for (const [path, node] of nodeMap.entries()) {
    let parentFound = false;
    for (const parentPath of sortedNodePaths) {
      if (
        parentPath !== path &&
        path.startsWith(parentPath) &&
        (path[parentPath.length] === "." || path[parentPath.length] === "[")
      ) {
        nodeMap.get(parentPath)!.children.push(node);
        parentFound = true;
        break;
      }
    }
    if (!parentFound) {
      roots.push(node);
    }
  }

  return { isTree: roots.length > 0, roots, relationships, unattached };
}

function EntityNodeCard({
  node,
  depth = 0,
}: {
  node: TreeNode;
  depth?: number;
}) {
  const [expanded, setExpanded] = useState(true);
  const isArray = node.path.includes("[]");

  return (
    <div className="space-y-2">
      <div
        className="rounded-lg border border-stone-200/80 dark:border-stone-800 bg-white dark:bg-stone-900/90
                   shadow-xs transition-all overflow-hidden"
      >
        {/* Node Header */}
        <div
          className="flex items-center justify-between px-3 py-2 bg-stone-50/80 dark:bg-stone-800/40
                     border-b border-stone-100 dark:border-stone-800/70"
        >
          <div className="flex items-center gap-2 min-w-0">
            <button
              type="button"
              onClick={() => setExpanded(!expanded)}
              className="text-stone-400 hover:text-stone-600 dark:hover:text-stone-300 p-0.5"
              aria-label={expanded ? "Collapse entity" : "Expand entity"}
            >
              {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </button>
            <div className="flex items-center gap-1.5 font-semibold text-xs sm:text-sm text-stone-900 dark:text-stone-100">
              <Layers size={15} className="text-primary shrink-0" />
              <span>{node.nodeType}</span>
            </div>
            {isArray && (
              <span className="text-[10px] font-mono px-1.5 py-0.2 rounded bg-stone-200/60 dark:bg-stone-800 text-stone-500 dark:text-stone-400">
                list
              </span>
            )}
            <span className="text-[11px] font-mono text-stone-400 dark:text-stone-500 truncate hidden sm:inline">
              {node.path || "root"}
            </span>
          </div>

          <div className="text-[11px] text-stone-400 dark:text-stone-500 shrink-0">
            {node.attributes.length} attr{node.attributes.length === 1 ? "" : "s"}
            {node.children.length > 0 && ` • ${node.children.length} sub-node${node.children.length === 1 ? "" : "s"}`}
          </div>
        </div>

        {/* Node Attributes */}
        {expanded && node.attributes.length > 0 && (
          <div className="p-2.5 grid grid-cols-1 sm:grid-cols-2 gap-1.5 text-xs">
            {node.attributes.map((attr, idx) => {
              const badge = getRoleBadge(attr.role);
              const Icon = badge.icon;
              return (
                <div
                  key={idx}
                  className="flex items-center justify-between gap-2 px-2 py-1 rounded bg-stone-50/50
                             dark:bg-stone-800/20 border border-stone-100 dark:border-stone-800/50"
                >
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span className="font-mono font-medium text-stone-800 dark:text-stone-200 truncate">
                      {attr.name}
                    </span>
                    {attr.sampleValue && (
                      <span className="text-stone-400 dark:text-stone-500 truncate font-mono text-[11px]">
                        = &apos;{attr.sampleValue}&apos;
                      </span>
                    )}
                  </div>
                  <span
                    className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium border shrink-0 ${badge.className}`}
                  >
                    <Icon size={10} />
                    {badge.label}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Child Nodes (indented with visual tree line) */}
      {expanded && node.children.length > 0 && (
        <div className="relative pl-4 sm:pl-6 border-l-2 border-primary/20 ml-3 sm:ml-4 space-y-2">
          {node.children.map((child, idx) => (
            <EntityNodeCard key={idx} node={child} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

export default function EvidenceTree({
  evidence,
  defaultEntityName,
}: {
  evidence: DraftEvidence[];
  defaultEntityName?: string;
}) {
  const [view, setView] = useState<"tree" | "raw">("tree");
  const parsed = useMemo(
    () => parseEvidenceTree(evidence, defaultEntityName),
    [evidence, defaultEntityName]
  );

  if (!parsed.isTree) {
    // Standard flat list for non-hierarchical evidence (e.g. pipeline stages)
    return (
      <ul className="divide-y divide-stone-100 dark:divide-stone-800 border-t border-stone-100 dark:border-stone-800">
        {evidence.map((item, i) => (
          <li key={i} className="px-4 py-2.5 text-sm">
            <div className="text-stone-800 dark:text-stone-200">{item.claim}</div>
            <div className="text-xs text-stone-500 dark:text-stone-400 font-mono mt-0.5 break-words">
              {item.basis}
            </div>
          </li>
        ))}
      </ul>
    );
  }

  return (
    <div className="border-t border-stone-100 dark:border-stone-800 bg-stone-50/30 dark:bg-stone-900/30 p-3 sm:p-4 space-y-3">
      {/* View Switcher */}
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-semibold uppercase tracking-wider text-stone-500 dark:text-stone-400 flex items-center gap-1.5">
          <Network size={14} className="text-primary" />
          Entity Hierarchy Graph
        </div>
        <div className="inline-flex rounded-lg p-0.5 bg-stone-200/60 dark:bg-stone-800 border border-stone-200 dark:border-stone-700 text-xs">
          <button
            type="button"
            onClick={() => setView("tree")}
            className={`flex items-center gap-1 px-2 py-1 rounded-md transition-all ${
              view === "tree"
                ? "bg-white dark:bg-stone-900 text-stone-900 dark:text-stone-100 shadow-xs font-medium"
                : "text-stone-600 dark:text-stone-400 hover:text-stone-900"
            }`}
          >
            <Network size={12} /> Tree View
          </button>
          <button
            type="button"
            onClick={() => setView("raw")}
            className={`flex items-center gap-1 px-2 py-1 rounded-md transition-all ${
              view === "raw"
                ? "bg-white dark:bg-stone-900 text-stone-900 dark:text-stone-100 shadow-xs font-medium"
                : "text-stone-600 dark:text-stone-400 hover:text-stone-900"
            }`}
          >
            <List size={12} /> Raw Rules ({evidence.length})
          </button>
        </div>
      </div>

      {/* Render Tree View or Raw Rules */}
      {view === "tree" ? (
        <div className="space-y-3 pt-1">
          {parsed.roots.map((root, i) => (
            <EntityNodeCard key={i} node={root} />
          ))}

          {parsed.relationships.length > 0 && (
            <div className="mt-3 pt-2.5 border-t border-stone-200/80 dark:border-stone-800">
              <div className="text-[11px] font-semibold text-stone-600 dark:text-stone-400 mb-1.5 flex items-center gap-1.5">
                <LinkIcon size={12} className="text-primary" />
                Relationships ({parsed.relationships.length})
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
                {parsed.relationships.map((item, i) => (
                  <div
                    key={i}
                    className="flex flex-col gap-0.5 px-2.5 py-1.5 rounded-lg bg-stone-50/60 dark:bg-stone-800/30 border border-stone-200/60 dark:border-stone-800 text-xs"
                  >
                    <div className="font-mono font-medium text-stone-800 dark:text-stone-200 flex items-center gap-1.5">
                      <span className="w-1.5 h-1.5 rounded-full bg-cyan-500 shrink-0" />
                      {item.claim}
                    </div>
                    {item.basis && (
                      <div className="text-stone-400 dark:text-stone-500 font-mono text-[10px] pl-3">
                        {item.basis}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {parsed.unattached.length > 0 && (
            <div className="mt-3 pt-2 border-t border-dashed border-stone-200 dark:border-stone-800">
              <div className="text-[11px] font-medium text-stone-500 dark:text-stone-400 mb-1">
                Other decisions:
              </div>
              <ul className="space-y-1 text-xs">
                {parsed.unattached.map((item, i) => (
                  <li key={i} className="text-stone-600 dark:text-stone-400 flex items-baseline gap-1.5">
                    <span className="w-1.5 h-1.5 rounded-full bg-stone-300 dark:bg-stone-700 shrink-0 mt-1" />
                    <span>{item.claim}</span>
                    <span className="text-stone-400 dark:text-stone-500 font-mono text-[10px]">
                      ({item.basis})
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      ) : (
        <ul className="divide-y divide-stone-100 dark:divide-stone-800 rounded-lg border border-stone-200 dark:border-stone-800 bg-white dark:bg-stone-900 overflow-hidden">
          {evidence.map((item, i) => (
            <li key={i} className="px-3.5 py-2 text-xs">
              <div className="font-medium text-stone-800 dark:text-stone-200">{item.claim}</div>
              <div className="text-stone-500 dark:text-stone-400 font-mono mt-0.5 break-words text-[11px]">
                {item.basis}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
