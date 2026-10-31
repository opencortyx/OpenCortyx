/**
 * The simple product: what is hidden, renamed or folded away so a person sees only what they use.
 *
 * Nothing here deletes anything: every page still exists at its address, every setting still works
 * (the engine's knobs keep their defaults in code and can be set in `.env` as
 * OPENCORTYX_SETTING_<KEY>). To bring one thing back, remove its entry below; to bring everything
 * back at once, set FULL_UI=1 in `.env` and rebuild the web image (it becomes NEXT_PUBLIC_FULL_UI).
 */

export const FULL_UI = process.env.NEXT_PUBLIC_FULL_UI === "1";

const when = <T,>(simple: T, full: T): T => (FULL_UI ? full : simple);

/** Sidebar entries reached from elsewhere instead (Settings, Review, Add data), not from the sidebar. */
export const HIDDEN_NAV: ReadonlySet<string> = new Set(when([
  "/dashboard/curate",          // Review > Curation
  "/dashboard/schema",          // Review > Types
  "/dashboard/quarantine",      // Review > Not read
  "/dashboard/eval",            // Settings > Quality checks
  "/dashboard/mapper",          // Customize
  "/dashboard/custom-assets",   // Customize
  "/dashboard/pipelines",       // Customize
  "/dashboard/ingest",          // Customize, and Connections > Scheduled syncs
  "/dashboard/sessions",        // Assistant > Agent conversations
  "/dashboard/users",           // Settings > Users & access
  "/dashboard/models",          // Settings > Models
  "/dashboard/health",          // Settings > System health
], ["/dashboard/customize"]));         // full UI: each page is an entry of its own

/** Sidebar names people use, instead of ours. */
export const NAV_LABELS: Readonly<Record<string, string>> = when<Record<string, string>>({
  "/dashboard/chat": "Assistant",
  "/dashboard/graph": "Graph",
  "/dashboard/review": "Review",
}, {});

/** Sidebar sections in the order a person needs them: ask, add, look, check. */
export const NAV_SECTION_ORDER: readonly string[] = when(
  ["Intelligence", "Integration", "Discover", "Trust", "Administration"], []);

/** Settings tabs that are engine internals (their values: defaults, or `.env`). */
export const HIDDEN_SETTINGS_TABS: ReadonlySet<string> = new Set(when(
  ["parallelism", "chunking", "capabilities", "extraction"], []));

/** The General settings a person can judge; every other knob keeps its default (or `.env`). */
export const GENERAL_SETTINGS: ReadonlySet<string> | null = when(
  new Set(["workspace_name", "grounding_mode", "ai_overviews", "allow_local_files"]), null);

/** The four General settings, in plain words. `grounding_mode` is a switch: strict = the LLM check. */
export const SETTING_COPY: Readonly<Record<string, { label: string; help: string }>> = when<Record<string, { label: string; help: string }>>({
  workspace_name: { label: "Workspace name", help: "Your name, or your team's. It names the top of the graph." },
  grounding_mode: { label: "Check answers strictly",
                    help: "Every sentence of an answer is checked against its sources by the model. Slower; needs a capable model." },
  ai_overviews: { label: "AI summaries of files", help: "A short summary of each file when it is added. Off saves model calls." },
  allow_local_files: { label: "Read folders on this server", help: "Let connections read files from a folder on the server. Uploads do not need it." },
}, {});

/** Settings > General: the read-only compute and infrastructure readouts, and the ontology rebuild. */
export const SHOW_OPERATOR_PANELS = FULL_UI;

/** System Health: the worker restart buttons (operators use the `opencortyx` CLI). */
export const SHOW_HEALTH_RESTARTS = FULL_UI;

/** Pipelines: the built-in default pipelines ("Automatic") listed beside the ones people built. */
export const SHOW_DEFAULT_PIPELINES = FULL_UI;

/** Ingestion: one-time uploads listed as saved ingestions (they are runs, under Add data). */
export const SHOW_UPLOAD_INGESTIONS = FULL_UI;

/** Nodes & Relationships: every type in the ontology, not only the types this workspace's data has. */
export const SHOW_ALL_TYPES_BY_DEFAULT = FULL_UI;
