import { apiClient } from "./client";

// The browse tree the dashboard graph draws (apps/api/opencortyx/knowledge/graph_tree.py):
// workspace → source → tag → asset → what the asset produced → any node's relationships.
// Every call returns one parent's children, one page (at most 100); past 1000 children a parent
// returns groups instead, and each group pages on its own. Items carry an opaque `key` that names
// what they open into — send it back as is.

export type TreeKind = "root" | "root_part" | "bundle" | "source" | "tag" | "asset" | "node" | "group";

export interface TreeItem {
  id: string;             // the node id for nodes and assets (one node is drawn once), else the key
  key: string;
  kind: TreeKind;
  label: string;
  type: string;
  count: number | null;   // assets under a source/tag, members of a group, links of a node
  size: number | null;    // nodes in an unconnected part
  openable: boolean;
  rel?: "part" | "linked";            // under an asset: made from it, or linked to it
  auto?: boolean;                     // a tag nobody gave: the file extension
  types?: Record<string, number>;     // an unconnected part's node types
  edge_type?: string;
  direction?: "out" | "in";
  node_id?: string;                   // the root: its central node (id = the workspace's workspace_id)
  mode?: "person" | "team";           // the root: what the workspace stands for
}

export interface TreeEdge { source: string; target: string; type: string; relation?: string | null }

export interface TreePage {
  items: TreeItem[];
  edges: TreeEdge[];
  total: number;
  offset: number;
  limit: number;
  next: number;           // where the next page starts: counts relationships, not distinct items
  grouped: boolean;
}

export interface TreeRun {
  status: "queued" | "running" | "ok" | "error";
  nodes?: number | null; assets?: number | null; roots?: number | null;
  duration_ms?: number | null; error?: string | null; finished_at?: string | null;
}

export interface TreeTop {
  run: TreeRun | null;
  items: TreeItem[];
  assets: number;
  sources: number;
  roots_total: number;
}

export interface TreeLocation { path: string[]; asset: TreeItem; node: TreeItem }

export const graphTreeApi = {
  top: () => apiClient.get<TreeTop>("/api/v2/graph/tree").then((r) => r.data),
  children: (key: string, offset = 0) =>
    apiClient.get<TreePage>("/api/v2/graph/tree/children", { params: { key, offset } }).then((r) => r.data),
  locate: (nodeId: string) =>
    apiClient.get<TreeLocation>(`/api/v2/graph/tree/locate/${nodeId}`).then((r) => r.data),
  search: (q: string, limit = 20) =>
    apiClient.get<{ items: TreeItem[] }>("/api/v2/graph/tree/search", { params: { q, limit } }).then((r) => r.data),
  rebuild: () => apiClient.post("/api/v2/graph/tree/rebuild").then((r) => r.data),
  tags: (assetId: string) =>
    apiClient.get<{ tags: string[]; default: string }>(`/api/v2/assets/${assetId}/tags`).then((r) => r.data),
  setTags: (assetId: string, tags: string[]) =>
    apiClient.put<{ tags: string[]; default: string }>(`/api/v2/assets/${assetId}/tags`, { tags }).then((r) => r.data),
};
