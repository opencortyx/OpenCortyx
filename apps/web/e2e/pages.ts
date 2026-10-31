import { HIDDEN_NAV, NAV_LABELS } from "../lib/simple";

/** Every dashboard page, in navigation order. */
const PAGES = [
  { section: "Discover", name: "Assets", path: "/dashboard/catalogue", slug: "assets" },
  { section: "Discover", name: "Lineage", path: "/dashboard/graph", slug: "lineage" },
  { section: "Trust", name: "Curation", path: "/dashboard/curate", slug: "curation" },
  { section: "Trust", name: "Nodes & Relationships", path: "/dashboard/schema", slug: "nodes-relationships" },
  { section: "Trust", name: "Review Queue", path: "/dashboard/review", slug: "review-queue" },
  { section: "Trust", name: "Evaluations", path: "/dashboard/eval", slug: "evaluations" },
  { section: "Trust", name: "Quarantine", path: "/dashboard/quarantine", slug: "quarantine" },
  { section: "Integration", name: "Add data", path: "/dashboard/add", slug: "add-data" },
  { section: "Integration", name: "Connections", path: "/dashboard/connectors", slug: "connections" },
  { section: "Integration", name: "Mapper", path: "/dashboard/mapper", slug: "mapper" },
  { section: "Integration", name: "Custom Extractors", path: "/dashboard/custom-assets", slug: "custom-extractors" },
  { section: "Integration", name: "Pipelines", path: "/dashboard/pipelines", slug: "pipelines" },
  { section: "Integration", name: "Ingestion", path: "/dashboard/ingest", slug: "ingestion" },
  { section: "Integration", name: "Runs", path: "/dashboard/progress", slug: "runs" },
  { section: "Intelligence", name: "AI Assistant", path: "/dashboard/chat", slug: "ai-assistant" },
  { section: "Intelligence", name: "Conversations", path: "/dashboard/sessions", slug: "conversations" },
  { section: "Administration", name: "Users & Access", path: "/dashboard/users", slug: "users-access" },
  { section: "Administration", name: "Models", path: "/dashboard/models", slug: "models" },
  { section: "Administration", name: "System Health", path: "/dashboard/health", slug: "system-health" },
  { section: "Administration", name: "Settings", path: "/dashboard/settings", slug: "settings" },
] as const;

/**
 * Each page with `nav`: the label its sidebar link shows, or null when the sidebar reaches it from
 * another page (lib/simple.ts decides both, as it does for the sidebar itself).
 */
export const DASHBOARD_PAGES = PAGES.map((p) => ({
  ...p, nav: HIDDEN_NAV.has(p.path) ? null : (NAV_LABELS[p.path] ?? p.name),
}));
