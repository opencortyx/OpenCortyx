"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Clock, Plus, Trash2, Cable, GitBranch, Zap, FolderTree, Folder, FolderPlus,
  Loader2, UploadCloud, FileText, GripVertical, Database, Braces, ArrowRight,
  ChevronDown, Copy, CheckCircle2, Table2, Eye, Search, Layers, AlertCircle, Info, Sparkles,
} from "lucide-react";
import { contextV2Api, BrowseSchema } from "@/lib/api/contextV2";

export type IngestMode = "on_demand" | "scheduled" | "realtime";
export type MappingDataKind = "auto" | "structured" | "semistructured" | "unstructured";
export type DetectedKind = Exclude<MappingDataKind, "auto">;

// Each mapping carries its OWN source: connector + pipeline + data type + (optionally) watch folders,
// file formats, directory nodes, or table/view selections.
export type Mapping = {
  connector_id: string;
  pipeline_id: string;
  kind?: MappingDataKind;
  /** Auto-detect only: the pipeline for each detected kind ("" or missing = the default pipeline). */
  pipelines?: Partial<Record<DetectedKind, string>>;
  enabled?: boolean;
  directories?: string[];
  formats?: string[];
  directory_nodes?: boolean;
  selections?: Record<string, { tables: string[]; views: string[] }>;
};

const WEEKDAYS = [["0", "Sunday"], ["1", "Monday"], ["2", "Tuesday"], ["3", "Wednesday"],
["4", "Thursday"], ["5", "Friday"], ["6", "Saturday"]] as const;

// ── cron helpers ──────────────────────────────────────────────────────────────
// Friendly recurrence ⇄ 5-field cron (min hour dom month dow), evaluated in UTC by the backend.
export function buildCron(p: { freq: string; minute: number; hour: number; dom: number; dow: string; raw: string }): string {
  const mm = Math.max(0, Math.min(59, p.minute || 0));
  const hh = Math.max(0, Math.min(23, p.hour || 0));
  switch (p.freq) {
    case "5min": return "*/5 * * * *";
    case "15min": return "*/15 * * * *";
    case "30min": return "*/30 * * * *";
    case "hourly": return `${mm} * * * *`;
    case "daily": return `${mm} ${hh} * * *`;
    case "weekly": return `${mm} ${hh} * * ${p.dow || "1"}`;
    case "monthly": return `${mm} ${hh} ${Math.max(1, Math.min(28, p.dom || 1))} * *`;
    default: return p.raw || "0 * * * *";
  }
}

// Best-effort parse of a cron back into the friendly controls (for edit mode). Unknown shapes → custom.
export function parseCron(cron?: string | null): { freq: string; minute: number; hour: number; dom: number; dow: string; raw: string } {
  const def = { freq: "hourly", minute: 0, hour: 9, dom: 1, dow: "1", raw: cron || "0 * * * *" };
  const f = (cron || "").trim().split(/\s+/);
  if (f.length !== 5) return def;
  const [mi, ho, dm, mo, dw] = f;
  const num = (s: string) => (/^\d+$/.test(s) ? parseInt(s, 10) : NaN);
  if (mo !== "*") return { ...def, freq: "custom" };
  if (/^\*\/(5|15|30)$/.test(mi) && ho === "*" && dm === "*" && dw === "*")
    return { ...def, freq: `${mi.slice(2)}min` };
  if (!isNaN(num(mi)) && ho === "*" && dm === "*" && dw === "*")
    return { ...def, freq: "hourly", minute: num(mi) };
  if (!isNaN(num(mi)) && !isNaN(num(ho)) && dm === "*" && dw === "*")
    return { ...def, freq: "daily", minute: num(mi), hour: num(ho) };
  if (!isNaN(num(mi)) && !isNaN(num(ho)) && dm === "*" && /^[0-7]$/.test(dw))
    return { ...def, freq: "weekly", minute: num(mi), hour: num(ho), dow: dw === "7" ? "0" : dw };
  if (!isNaN(num(mi)) && !isNaN(num(ho)) && !isNaN(num(dm)) && dw === "*")
    return { ...def, freq: "monthly", minute: num(mi), hour: num(ho), dom: num(dm) };
  return { ...def, freq: "custom" };
}

function humanCron(cron: string): string {
  const p = parseCron(cron);
  const t = `${String(p.hour).padStart(2, "0")}:${String(p.minute).padStart(2, "0")} UTC`;
  switch (p.freq) {
    case "5min": return "every 5 minutes";
    case "15min": return "every 15 minutes";
    case "30min": return "every 30 minutes";
    case "hourly": return `hourly at :${String(p.minute).padStart(2, "0")}`;
    case "daily": return `daily at ${t}`;
    case "weekly": return `every ${WEEKDAYS.find((d) => d[0] === p.dow)?.[1] || "week"} at ${t}`;
    case "monthly": return `monthly on day ${p.dom} at ${t}`;
    default: return `cron: ${cron}`;
  }
}

// ── ScheduleControl ─────────────────────────────────────────────────────────
export function ScheduleControl({ mode, schedule, onMode, onSchedule, accent = "teal" }: {
  mode: IngestMode; schedule: string; onMode: (m: IngestMode) => void; onSchedule: (cron: string) => void;
  accent?: "teal" | "emerald" | "violet";
}) {
  const parsed = useMemo(() => parseCron(schedule), [schedule]);
  const [freq, setFreq] = useState(parsed.freq);
  const lastPushed = useRef(schedule);
  useEffect(() => {
    if (schedule !== lastPushed.current) {
      setFreq(parseCron(schedule).freq);
      lastPushed.current = schedule;
    }
  }, [schedule]);

  const p = { ...parsed, freq };
  const push = (cron: string) => { lastPushed.current = cron; onSchedule(cron); };
  const set = (patch: Partial<typeof p>) => push(buildCron({ ...p, ...patch }));
  const changeFreq = (f: string) => {
    setFreq(f);
    if (f !== "custom") push(buildCron({ ...p, freq: f }));  // custom → keep cron, user edits raw next
  };
  const ring = accent === "emerald" ? "border-emerald-400 bg-emerald-50/60"
    : accent === "violet" ? "border-violet-400 bg-violet-50/60"
    : "border-teal-400 bg-teal-50/60";

  return (
    <div>
      <label className="text-xs font-bold text-stone-500 uppercase tracking-wider flex items-center gap-1.5 mb-2">
        <Clock size={12} /> Keep in sync
      </label>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        {([["on_demand", "On-demand", "Runs only when you click Run or Sync."],
        ["scheduled", "Scheduled", "Recurring — hourly, daily, weekly, monthly."],
        ["realtime", "Real-time", "Watches for changes and syncs continuously."]] as const).map(([v, label, sub]) => (
          <button key={v} type="button" onClick={() => onMode(v)}
            className={`text-left border rounded-lg p-2.5 transition-colors ${mode === v ? ring : "border-stone-200 hover:border-stone-300"}`}>
            <div className="text-sm font-bold text-stone-800">{label}</div>
            <div className="text-[11px] text-stone-500 mt-0.5">{sub}</div>
          </button>
        ))}
      </div>

      {mode === "scheduled" && (
        <div className="mt-3 border border-stone-200 rounded-lg p-3 bg-stone-50/60 space-y-3">
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-[11px] font-bold text-stone-500 uppercase tracking-wider">Frequency</label>
              <select value={p.freq} onChange={(e) => changeFreq(e.target.value)}
                className="w-full mt-1 border border-stone-200 rounded-lg px-2.5 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-primary/20">
                <option value="5min">Every 5 minutes</option>
                <option value="15min">Every 15 minutes</option>
                <option value="30min">Every 30 minutes</option>
                <option value="hourly">Hourly</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
                <option value="custom">Custom (cron)</option>
              </select>
            </div>

            {p.freq === "hourly" && (
              <NumberField label="At minute" value={p.minute} min={0} max={59} onChange={(n) => set({ minute: n })} />
            )}
            {p.freq === "weekly" && (
              <div>
                <label className="text-[11px] font-bold text-stone-500 uppercase tracking-wider">On day</label>
                <select value={p.dow} onChange={(e) => set({ dow: e.target.value })}
                  className="w-full mt-1 border border-stone-200 rounded-lg px-2.5 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-primary/20">
                  {WEEKDAYS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </div>
            )}
            {p.freq === "monthly" && (
              <NumberField label="Day of month" value={p.dom} min={1} max={28} onChange={(n) => set({ dom: n })} />
            )}
          </div>

          {(p.freq === "daily" || p.freq === "weekly" || p.freq === "monthly") && (
            <div className="grid grid-cols-2 gap-2">
              <NumberField label="Hour (UTC)" value={p.hour} min={0} max={23} onChange={(n) => set({ hour: n })} />
              <NumberField label="Minute" value={p.minute} min={0} max={59} onChange={(n) => set({ minute: n })} />
            </div>
          )}

          {p.freq === "custom" && (
            <div>
              <input value={schedule} onChange={(e) => push(e.target.value)}
                placeholder="min hour dom month dow  (e.g. 0 0 * * *  = every night · */2 * * * *  = every 2 min)"
                className="w-full border border-stone-200 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/20" />
              <p className="text-[10px] text-stone-400 mt-1">5 fields: minute hour day-of-month month day-of-week (UTC). Supports <code>*</code>, <code>*/n</code>, <code>a-b</code>, <code>a,b,c</code>.</p>
            </div>
          )}

          <div className="flex items-center justify-between text-[11px] text-stone-500">
            <span>Runs <b className="text-stone-700">{humanCron(schedule)}</b>. Only new/changed/removed items are processed.</span>
            <code className="font-mono text-stone-400">{schedule}</code>
          </div>
        </div>
      )}

      {mode === "realtime" && (
        <p className="text-[11px] text-stone-400 mt-2 flex items-center gap-1.5">
          <Zap size={12} className="text-amber-500" /> Change detection runs every ~30s. Only the delta is ingested; unchanged items are skipped, deleted ones are soft-removed.
        </p>
      )}
    </div>
  );
}

function NumberField({ label, value, min, max, onChange }: {
  label: string; value: number; min: number; max: number; onChange: (n: number) => void;
}) {
  return (
    <div>
      <label className="text-[11px] font-bold text-stone-500 uppercase tracking-wider">{label}</label>
      <input type="number" min={min} max={max} value={value}
        onChange={(e) => onChange(Math.max(min, Math.min(max, parseInt(e.target.value || "0", 10))))}
        className="w-full mt-1 border border-stone-200 rounded-lg px-2.5 py-2 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-primary/20" />
    </div>
  );
}

// ── MappingsEditor (N:N connector → pipeline with per-source data types) ─────
const FILE_KINDS = ["local", "s3", "gcs", "adls", "files", "sharepoint"];
const SEMI_KINDS = ["api", "rest", "json", "webhook"];

const DETECTED_KINDS: { kind: DetectedKind; label: string; icon: any }[] = [
  { kind: "unstructured", label: "Documents", icon: FileText },
  { kind: "structured", label: "Tables", icon: Database },
  { kind: "semistructured", label: "JSON and records", icon: Braces },
];

function pipelineFits(pipelineKind: string | undefined, kind: DetectedKind): boolean {
  if (kind === "structured") return pipelineKind === "structured" || !pipelineKind;
  if (kind === "semistructured") return pipelineKind === "semistructured";
  return pipelineKind === "document" || pipelineKind === "unstructured";
}

function PipelineSelect({ label, value, onChange, kind, pipelines }: {
  label: string;
  value: string;
  onChange: (id: string) => void;
  kind: DetectedKind;
  pipelines: { id: string; name: string; kind?: string }[];
}) {
  const compatible = pipelines.filter((p) => pipelineFits(p.kind, kind));
  const other = pipelines.filter((p) => !compatible.includes(p));
  return (
    <div className="relative">
      <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full appearance-none border border-stone-200 rounded-xl pl-3 pr-8 py-2 text-sm text-stone-800 font-medium bg-white hover:border-stone-300 focus:outline-none focus:ring-2 focus:ring-teal-500/20 focus:border-teal-500 truncate cursor-pointer shadow-2xs">
        <option value="">Default pipeline</option>
        {compatible.length > 0 && (
          <optgroup label="Compatible pipelines">
            {compatible.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </optgroup>
        )}
        {other.length > 0 && (
          <optgroup label="Other pipelines">
            {other.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </optgroup>
        )}
      </select>
      <ChevronDown size={15} className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-stone-400" />
    </div>
  );
}

export function detectDefaultKind(connectorKind?: string): MappingDataKind {
  const k = (connectorKind || "").toLowerCase();
  if (FILE_KINDS.some((f) => k.includes(f))) return "auto";
  if (SEMI_KINDS.some((s) => k.includes(s))) return "semistructured";
  return "structured";
}

function DataTypeSelect({
  value,
  onChange,
}: {
  value: MappingDataKind;
  onChange: (k: MappingDataKind) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    if (open) document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [open]);

  const configs: Record<
    MappingDataKind,
    { label: string; icon: any; iconBox: string }
  > = {
    auto: {
      label: "Auto-detect",
      icon: Sparkles,
      iconBox: "bg-amber-50 text-amber-600 border border-amber-100",
    },
    structured: {
      label: "Force Structured",
      icon: Database,
      iconBox: "bg-emerald-50 text-emerald-600 border border-emerald-100",
    },
    semistructured: {
      label: "Force Semi-Structured",
      icon: Braces,
      iconBox: "bg-purple-50 text-purple-600 border border-purple-100",
    },
    unstructured: {
      label: "Force Unstructured",
      icon: FileText,
      iconBox: "bg-blue-50 text-blue-600 border border-blue-100",
    },
  };

  const current = configs[value] || configs.auto;
  const CurrentIcon = current.icon;

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between border border-stone-200 rounded-xl px-3 py-2 bg-white hover:border-stone-300 text-sm font-medium text-stone-800 transition-colors focus:outline-none focus:ring-2 focus:ring-teal-500/20 cursor-pointer shadow-2xs"
      >
        <div className="flex items-center gap-2 truncate">
          <span className={`p-1 rounded-md shrink-0 ${current.iconBox}`}>
            <CurrentIcon size={14} />
          </span>
          <span className="truncate">{current.label}</span>
        </div>
        <ChevronDown size={15} className={`text-stone-400 shrink-0 ml-1 transition-transform duration-150 ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-52 bg-white border border-stone-200 rounded-xl shadow-lg z-50 py-1 divide-y divide-stone-100">
          {(["auto", "structured", "semistructured", "unstructured"] as MappingDataKind[]).map((k) => {
            const cfg = configs[k];
            const Icon = cfg.icon;
            const isSelected = value === k;
            return (
              <button
                key={k}
                type="button"
                onClick={() => {
                  onChange(k);
                  setOpen(false);
                }}
                className={`w-full text-left px-3 py-2 text-xs flex items-center gap-2 hover:bg-stone-50 transition-colors cursor-pointer ${
                  isSelected ? "bg-stone-50 font-bold text-stone-900" : "text-stone-700"
                }`}
              >
                <span className={`p-1 rounded-md shrink-0 ${cfg.iconBox}`}>
                  <Icon size={13} />
                </span>
                <span>{cfg.label}</span>
                {isSelected && <CheckCircle2 size={13} className="ml-auto text-emerald-600" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function MappingsEditor({
  mappings,
  onChange,
  connectors,
  pipelines,
  connectorLabel,
  newPipelineHref,
  onNewPipeline,
  pendingFiles,
  onPickFiles,
  onClearFiles,
}: {
  mappings: Mapping[];
  onChange: (m: Mapping[]) => void;
  connectors: { id: string; name: string; kind: string; [key: string]: any }[];
  pipelines: { id: string; name: string; kind?: string; stage_count: number }[];
  connectorLabel?: string;
  newPipelineHref?: string;
  onNewPipeline?: () => void;
  pendingFiles?: Record<number, File[]>;
  onPickFiles?: (i: number, files: File[]) => void;
  onClearFiles?: (i: number) => void;
}) {
  const initialFirstConn = connectors[0]?.id || "";
  const initialFirstKind = detectDefaultKind(connectors[0]?.kind);

  const rows = mappings.length
    ? mappings
    : [{ connector_id: initialFirstConn, pipeline_id: "", kind: initialFirstKind, enabled: true }];

  const [expandedRows, setExpandedRows] = useState<Record<number, boolean>>({});
  const [draggedIdx, setDraggedIdx] = useState<number | null>(null);
  const [localPendingFiles, setLocalPendingFiles] = useState<Record<number, File[]>>({});

  // Schema browsing cache & state per connector
  const [schemasByConn, setSchemasByConn] = useState<Record<string, BrowseSchema[]>>({});
  const [schemaLoadingByConn, setSchemaLoadingByConn] = useState<Record<string, boolean>>({});
  const [schemaErrByConn, setSchemaErrByConn] = useState<Record<string, string>>({});
  const [searchByRow, setSearchByRow] = useState<Record<number, string>>({});

  // Folder browsing state
  const [browseKey, setBrowseKey] = useState<string | null>(null);
  const [bPath, setBPath] = useState("");
  const [bDirs, setBDirs] = useState<string[]>([]);
  const [bLoading, setBLoading] = useState(false);
  const [bErr, setBErr] = useState("");

  const handleInternalPickFiles = (i: number, files: File[]) =>
    setLocalPendingFiles((m) => ({ ...m, [i]: [...(m[i] || []), ...files] }));
  const handleInternalClearFiles = (i: number) =>
    setLocalPendingFiles((m) => { const n = { ...m }; delete n[i]; return n; });

  useEffect(() => {
    if (mappings.length === 0 && connectors.length) {
      onChange([{
        connector_id: connectors[0].id,
        pipeline_id: "",
        kind: detectDefaultKind(connectors[0].kind),
        enabled: true,
      }]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mappings.length, connectors.length]);

  const setRow = (i: number, patch: Partial<Mapping>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  const addRow = () => {
    const defaultConn = connectors[0]?.id || "";
    const defaultKind = detectDefaultKind(connectors[0]?.kind);
    const newIdx = rows.length;
    onChange([...rows, { connector_id: defaultConn, pipeline_id: "", kind: defaultKind, enabled: true }]);
    // Auto-expand the newly added row
    setExpandedRows((prev) => ({ ...prev, [newIdx]: true }));
  };

  const removeRow = (i: number) => {
    if (rows.length > 1) {
      onChange(rows.filter((_, j) => j !== i));
      setExpandedRows((prev) => {
        const next: Record<number, boolean> = {};
        Object.entries(prev).forEach(([k, v]) => {
          const num = Number(k);
          if (num < i) next[num] = v;
          else if (num > i) next[num - 1] = v;
        });
        return next;
      });
    } else {
      onChange([{ connector_id: "", pipeline_id: "", kind: "structured", enabled: true }]);
    }
  };

  const duplicateRow = (i: number) => {
    const target = rows[i];
    const clone: Mapping = {
      ...target,
      directories: target.directories ? [...target.directories] : undefined,
      formats: target.formats ? [...target.formats] : undefined,
      selections: target.selections ? JSON.parse(JSON.stringify(target.selections)) : undefined,
    };
    const newRows = [...rows.slice(0, i + 1), clone, ...rows.slice(i + 1)];
    onChange(newRows);
    setExpandedRows((prev) => ({ ...prev, [i + 1]: true }));
  };

  const toggleExpand = (i: number) => {
    const nextState = !expandedRows[i];
    setExpandedRows((prev) => ({ ...prev, [i]: nextState }));
    const row = rows[i];
    const conn = connectors.find((c) => c.id === row?.connector_id);
    const isFile = isFileConn(conn?.kind || "");
    const rowKind = row?.kind || detectDefaultKind(conn?.kind);
    if (nextState && rowKind === "structured" && !isFile && row?.connector_id) {
      loadSchema(row.connector_id);
    }
  };

  const isFileConn = (kind: string) => FILE_KINDS.includes(kind || "");

  const handleConnectorChange = (i: number, cid: string) => {
    const conn = connectors.find((c) => c.id === cid);
    const suggestedKind = detectDefaultKind(conn?.kind);
    const currentKind = rows[i].kind || suggestedKind;
    setRow(i, {
      connector_id: cid,
      kind: currentKind,
    });
    const isFile = isFileConn(conn?.kind || "");
    if (currentKind === "structured" && !isFile && cid) {
      loadSchema(cid);
    }
  };

  const handleKindChange = (i: number, newKind: MappingDataKind) => {
    setRow(i, { kind: newKind });
    if (newKind === "auto") setExpandedRows((prev) => ({ ...prev, [i]: true }));
    const conn = connectors.find((c) => c.id === rows[i].connector_id);
    const isFile = isFileConn(conn?.kind || "");
    if (newKind === "structured" && !isFile && rows[i].connector_id) {
      loadSchema(rows[i].connector_id);
    }
  };

  // Drag and drop reordering
  const handleDragStart = (e: React.DragEvent, index: number) => {
    setDraggedIdx(index);
    e.dataTransfer.effectAllowed = "move";
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  };

  const handleDrop = (e: React.DragEvent, targetIndex: number) => {
    e.preventDefault();
    if (draggedIdx === null || draggedIdx === targetIndex) return;
    const newRows = [...rows];
    const [removed] = newRows.splice(draggedIdx, 1);
    newRows.splice(targetIndex, 0, removed);
    setDraggedIdx(null);
    onChange(newRows);
  };

  // per-row watch folders
  const dirs = (r: Mapping) => (r.directories?.length ? r.directories : [""]);
  const setDir = (i: number, di: number, v: string) =>
    setRow(i, { directories: dirs(rows[i]).map((d, j) => (j === di ? v : d)) });
  const addDir = (i: number) => setRow(i, { directories: [...dirs(rows[i]), ""] });
  const rmDir = (i: number, di: number) => setRow(i, { directories: dirs(rows[i]).filter((_, j) => j !== di) });

  // Folder browse
  const parentPath = (p: string) => {
    const parts = p.split("/").filter(Boolean);
    parts.pop();
    return parts.join("/");
  };

  const loadDirs = async (connectorId: string, path: string) => {
    if (!connectorId) return;
    setBLoading(true);
    setBErr("");
    try {
      const r: any = await contextV2Api.listConnectorDirs(connectorId, path);
      setBPath(r.path ?? path);
      setBDirs(r.dirs || []);
    } catch (e: any) {
      setBErr(e?.response?.data?.detail || e?.message || "Couldn't list folders");
      setBDirs([]);
    } finally {
      setBLoading(false);
    }
  };

  const openBrowse = (key: string, connectorId: string) => {
    setBrowseKey(key);
    setBPath("");
    loadDirs(connectorId, "");
  };

  // DB Schema browsing
  const loadSchema = async (connectorId: string) => {
    if (!connectorId || schemasByConn[connectorId]) return;
    setSchemaLoadingByConn((m) => ({ ...m, [connectorId]: true }));
    setSchemaErrByConn((m) => ({ ...m, [connectorId]: "" }));
    try {
      const res = await contextV2Api.browseConnector(connectorId);
      setSchemasByConn((m) => ({ ...m, [connectorId]: res?.schemas || [] }));
    } catch (e: any) {
      setSchemaErrByConn((m) => ({
        ...m,
        [connectorId]: e?.response?.data?.detail || e?.message || "Browse failed",
      }));
    } finally {
      setSchemaLoadingByConn((m) => ({ ...m, [connectorId]: false }));
    }
  };

  const isTableSelected = (r: Mapping, schema: string, n: string, type: "table" | "view") => {
    const e = r.selections?.[schema];
    return !!e && (type === "table" ? e.tables.includes(n) : e.views.includes(n));
  };

  const toggleTable = (rowIdx: number, schema: string, n: string, type: "table" | "view") => {
    const currentSel = rows[rowIdx].selections || {};
    const e = currentSel[schema] || { tables: [], views: [] };
    const key = type === "table" ? "tables" : "views";
    const list = e[key].includes(n) ? e[key].filter((x) => x !== n) : [...e[key], n];
    const nextSchemaSel = { ...e, [key]: list };
    const nextSel = { ...currentSel, [schema]: nextSchemaSel };
    if (!nextSel[schema].tables.length && !nextSel[schema].views.length) {
      delete nextSel[schema];
    }
    setRow(rowIdx, { selections: nextSel });
  };

  return (
    <div className="space-y-4">
      {/* Header bar */}
      <div className="flex items-center justify-between">
        <label className="text-xs font-bold text-stone-500 uppercase tracking-wider flex items-center gap-1.5">
          <Cable size={14} className="text-teal-600" />
          {connectorLabel || "Sources"} &amp; Pipelines
        </label>
        <button
          type="button"
          onClick={addRow}
          className="text-xs font-semibold text-emerald-600 hover:text-emerald-800 flex items-center gap-1 cursor-pointer"
        >
          <Plus size={13} /> Add source
        </button>
      </div>

      <div className="space-y-3">
        {rows.map((r, i) => {
          const conn = connectors.find((c) => c.id === r.connector_id);
          const isFile = isFileConn(conn?.kind || "");
          const rowKind: MappingDataKind = r.kind || detectDefaultKind(conn?.kind);
          const isExpanded = !!expandedRows[i];
          const isActive = r.enabled !== false;

          const badgeStyle = {
            auto: "bg-amber-50 text-amber-600 border border-amber-100",
            structured: "bg-emerald-50 text-emerald-600 border border-emerald-100",
            unstructured: "bg-blue-50 text-blue-600 border border-blue-100",
            semistructured: "bg-purple-50 text-purple-600 border border-purple-100",
          }[rowKind] || "bg-amber-50 text-amber-600 border border-amber-100";

          const BadgeIcon = {
            auto: Sparkles,
            structured: Database,
            unstructured: FileText,
            semistructured: Braces,
          }[rowKind] || Sparkles;

          const customKinds = Object.values(r.pipelines || {}).filter(Boolean).length;

          // Schema browsing counts for this row
          const schemas = schemasByConn[r.connector_id] || [];
          const rowSelections = r.selections || {};
          const selectedTableCount = Object.values(rowSelections).reduce(
            (n, e) => n + e.tables.length + e.views.length,
            0
          );
          const searchQuery = searchByRow[i] || "";

          return (
            <div
              key={i}
              onDragOver={handleDragOver}
              onDrop={(e) => handleDrop(e, i)}
              className={`border rounded-xl bg-white transition-all shadow-xs ${
                isActive ? "border-stone-200 hover:border-stone-300" : "border-stone-200/60 bg-stone-50/50 opacity-75"
              }`}
            >
              <div className="p-3.5 flex flex-wrap items-center gap-3">
                {/* 6-dot Drag Handle */}
                <div
                  draggable
                  onDragStart={(e) => handleDragStart(e, i)}
                  className="cursor-grab active:cursor-grabbing text-stone-300 hover:text-stone-500 shrink-0 p-1 rounded hover:bg-stone-100 transition-colors"
                  title="Drag to reorder"
                >
                  <GripVertical size={18} />
                </div>

                {/* Data type Icon badge */}
                <div className={`w-10 h-10 rounded-xl flex items-center justify-center shrink-0 ${badgeStyle}`}>
                  <BadgeIcon size={20} />
                </div>

                {/* Source Column */}
                <div className="flex-1 min-w-[160px]">
                  <label className="block text-xs font-semibold text-stone-500 mb-1">Source</label>
                  <div className="relative">
                    <select
                      value={r.connector_id}
                      onChange={(e) => handleConnectorChange(i, e.target.value)}
                      className="w-full appearance-none border border-stone-200 rounded-xl pl-3 pr-8 py-2 text-sm text-stone-800 font-medium bg-white hover:border-stone-300 focus:outline-none focus:ring-2 focus:ring-teal-500/20 focus:border-teal-500 truncate cursor-pointer shadow-2xs"
                    >
                      <option value="">Select source…</option>
                      {connectors.map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name} {c.kind ? `(${c.kind})` : ""}
                        </option>
                      ))}
                    </select>
                    <ChevronDown size={15} className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-stone-400" />
                  </div>
                </div>

                {/* Arrow */}
                <div className="mt-5 shrink-0 text-stone-400 hidden sm:block">
                  <ArrowRight size={16} />
                </div>

                {/* Pipeline Column */}
                <div className="flex-1 min-w-[180px]">
                  <label className="block text-xs font-semibold text-stone-500 mb-1">Pipeline</label>
                  {rowKind === "auto" ? (
                    <button type="button" onClick={() => setExpandedRows((prev) => ({ ...prev, [i]: true }))}
                      className="w-full text-left border border-stone-200 rounded-xl px-3 py-2 text-sm text-stone-800 font-medium bg-white hover:border-stone-300 truncate cursor-pointer shadow-2xs">
                      One per detected kind{customKinds ? ` · ${customKinds} custom` : " · defaults"}
                    </button>
                  ) : (
                    <PipelineSelect label="Pipeline" value={r.pipeline_id} kind={rowKind} pipelines={pipelines}
                      onChange={(id) => setRow(i, { pipeline_id: id })} />
                  )}
                </div>

                {/* Classification Column */}
                <div className="w-[170px] shrink-0">
                  <label className="block text-xs font-semibold text-stone-500 mb-1">Classification</label>
                  <DataTypeSelect
                    value={rowKind}
                    onChange={(newKind) => handleKindChange(i, newKind)}
                  />
                </div>

                {/* Active Switch Toggle */}
                <div className="flex items-center gap-2 mt-5 shrink-0 pl-1">
                  <button
                    type="button"
                    role="switch"
                    aria-checked={isActive}
                    onClick={() => setRow(i, { enabled: !isActive })}
                    className={`w-9 h-5 flex items-center rounded-full p-0.5 transition-colors duration-200 ease-in-out cursor-pointer ${
                      isActive ? "bg-emerald-500" : "bg-stone-300"
                    }`}
                  >
                    <div
                      className={`bg-white w-4 h-4 rounded-full shadow-md transform transition-transform duration-200 ease-in-out ${
                        isActive ? "translate-x-4" : "translate-x-0"
                      }`}
                    />
                  </button>
                  <span className="text-xs font-medium text-stone-700 min-w-[42px] select-none">
                    {isActive ? "Active" : "Inactive"}
                  </span>
                </div>

                {/* Action Buttons: Duplicate, Delete, Expand */}
                <div className="flex items-center gap-1 mt-5 shrink-0 text-stone-400">
                  <button
                    type="button"
                    title="Duplicate mapping"
                    onClick={() => duplicateRow(i)}
                    className="p-1.5 hover:text-stone-700 hover:bg-stone-100 rounded-lg transition-colors cursor-pointer"
                  >
                    <Copy size={16} />
                  </button>
                  <button
                    type="button"
                    title="Delete mapping"
                    disabled={rows.length <= 1}
                    onClick={() => removeRow(i)}
                    className="p-1.5 hover:text-red-600 hover:bg-red-50 rounded-lg disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-stone-400 transition-colors cursor-pointer"
                  >
                    <Trash2 size={16} />
                  </button>
                  <button
                    type="button"
                    title={isExpanded ? "Collapse configuration" : "Expand configuration"}
                    onClick={() => toggleExpand(i)}
                    className="p-1.5 hover:text-stone-700 hover:bg-stone-100 rounded-lg transition-colors cursor-pointer"
                  >
                    <ChevronDown
                      size={18}
                      className={`transition-transform duration-200 ${isExpanded ? "rotate-180" : ""}`}
                    />
                  </button>
                </div>
              </div>

              {/* Expandable Per-Source Configuration */}
              {isExpanded && (
                <div className="border-t border-stone-100 p-4 bg-stone-50/60 rounded-b-xl space-y-4">
                  {rowKind === "auto" && (
                    <div className="space-y-2">
                      <span className="text-xs font-bold text-stone-700 flex items-center gap-1.5">
                        <Sparkles size={13} className="text-amber-600" /> Pipeline for each detected kind
                      </span>
                      <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                        {DETECTED_KINDS.map(({ kind, label, icon: KindIcon }) => (
                          <div key={kind}>
                            <span className="text-[11px] font-semibold text-stone-500 mb-1 flex items-center gap-1">
                              <KindIcon size={12} /> {label}
                            </span>
                            <PipelineSelect label={`Pipeline for ${label.toLowerCase()}`} value={r.pipelines?.[kind] || ""}
                              kind={kind} pipelines={pipelines}
                              onChange={(id) => setRow(i, { pipelines: { ...(r.pipelines || {}), [kind]: id } })} />
                          </div>
                        ))}
                      </div>
                      <p className="text-[11px] text-stone-400">
                        Each file&apos;s kind is detected from its format, and the file runs through the pipeline chosen for that kind.
                      </p>
                    </div>
                  )}

                  {/* Case 1: Structured DB Connector -> Table & View Selection */}
                  {rowKind === "structured" && !isFile && (
                    <div className="space-y-3">
                      <div className="flex items-center justify-between">
                        <span className="text-xs font-bold text-stone-700 flex items-center gap-1.5">
                          <Table2 size={14} className="text-emerald-600" /> Tables &amp; views to ingest
                        </span>
                        {selectedTableCount > 0 && (
                          <div className="text-xs text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-md px-2.5 py-1 flex items-center gap-1.5">
                            <CheckCircle2 size={13} />
                            <span>
                              {selectedTableCount} object{selectedTableCount === 1 ? "" : "s"} selected
                            </span>
                            ·
                            <button
                              type="button"
                              onClick={() => setRow(i, { selections: {} })}
                              className="underline hover:text-emerald-900"
                            >
                              clear
                            </button>
                          </div>
                        )}
                      </div>

                      {/* Search Bar */}
                      <div className="relative">
                        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-stone-400" />
                        <input
                          value={searchQuery}
                          onChange={(e) =>
                            setSearchByRow((prev) => ({ ...prev, [i]: e.target.value }))
                          }
                          placeholder="Search tables & views…"
                          className="w-full pl-9 pr-3 py-1.5 border border-stone-200 rounded-lg text-xs bg-white focus:outline-none focus:ring-2 focus:ring-teal-500/20"
                        />
                      </div>

                      {schemaLoadingByConn[r.connector_id] ? (
                        <p className="text-xs text-stone-500 flex items-center gap-2 py-4 justify-center">
                          <Loader2 size={14} className="animate-spin text-teal-600" /> Loading schemas…
                        </p>
                      ) : schemaErrByConn[r.connector_id] ? (
                        <div className="text-xs text-red-600 bg-red-50 border border-red-100 rounded-lg p-2.5 flex items-center gap-2">
                          <AlertCircle size={14} /> {schemaErrByConn[r.connector_id]}
                        </div>
                      ) : schemas.length > 0 ? (
                        <div className="space-y-2 max-h-60 overflow-y-auto pr-1">
                          {schemas.map((sc) => {
                            const objs = sc.tables.filter(
                              (t) =>
                                !searchQuery ||
                                t.name.toLowerCase().includes(searchQuery.toLowerCase())
                            );
                            if (!objs.length) return null;
                            return (
                              <div key={sc.schema} className="border border-stone-200 rounded-lg overflow-hidden bg-white">
                                <div className="px-3 py-1.5 bg-stone-100/70 text-xs font-bold text-stone-700 flex items-center gap-1.5">
                                  <Layers size={13} className="text-stone-500" />
                                  <span>{sc.schema}</span>
                                  <span className="text-stone-400 font-normal">({objs.length})</span>
                                </div>
                                <div className="divide-y divide-stone-100">
                                  {objs.map((t) => {
                                    const selected = isTableSelected(r, sc.schema, t.name, t.type);
                                    return (
                                      <button
                                        key={t.name}
                                        type="button"
                                        onClick={() => toggleTable(i, sc.schema, t.name, t.type)}
                                        className={`w-full text-left px-3 py-1.5 flex items-center gap-2 text-xs transition-colors cursor-pointer ${
                                          selected ? "bg-emerald-50/70" : "hover:bg-stone-50"
                                        }`}
                                      >
                                        <span
                                          className={`w-3.5 h-3.5 rounded border flex items-center justify-center ${
                                            selected
                                              ? "bg-emerald-500 border-emerald-500 text-white"
                                              : "border-stone-300 bg-white"
                                          }`}
                                        >
                                          {selected && <CheckCircle2 size={11} />}
                                        </span>
                                        {t.type === "view" ? (
                                          <Eye size={13} className="text-teal-500" />
                                        ) : (
                                          <Table2 size={13} className="text-emerald-500" />
                                        )}
                                        <span className="text-stone-800 font-medium">{t.name}</span>
                                        <span className="text-[10px] text-stone-400 ml-auto">
                                          {t.columns.length} cols{t.type === "view" ? " · view" : ""}
                                        </span>
                                      </button>
                                    );
                                  })}
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <p className="text-xs text-stone-400 py-3 text-center">
                          Leave unselected to ingest all tables in this source.
                        </p>
                      )}
                    </div>
                  )}

                  {/* Case 2: Structured File/Lake Connector */}
                  {rowKind === "structured" && isFile && (
                    <div className="space-y-3">
                      <div className="flex items-center justify-between">
                        <span className="text-xs font-bold text-stone-700 flex items-center gap-1.5">
                          <FolderTree size={13} className="text-teal-600" /> Tabular watch folders (CSV · TSV · Parquet · Excel)
                        </span>
                        <button
                          type="button"
                          onClick={() => addDir(i)}
                          className="text-xs font-semibold text-teal-600 hover:text-teal-800 flex items-center gap-1"
                        >
                          <FolderPlus size={12} /> Add folder
                        </button>
                      </div>
                      {dirs(r).map((d, di) => {
                        const key = `${i}:${di}`;
                        return (
                          <div key={di} className="space-y-1.5">
                            <div className="flex items-center gap-1.5">
                              <input
                                value={d}
                                onChange={(e) => setDir(i, di, e.target.value)}
                                placeholder="/data/tables · s3://bucket/parquet · C:\Data\Exports"
                                className="flex-1 border border-stone-200 rounded-lg px-2.5 py-1.5 text-xs font-mono bg-white focus:outline-none focus:ring-2 focus:ring-teal-500/20"
                              />
                              <button
                                type="button"
                                onClick={() => (browseKey === key ? setBrowseKey(null) : openBrowse(key, r.connector_id))}
                                className="px-2.5 py-1.5 text-xs font-semibold text-teal-600 border border-stone-200 rounded-lg bg-white hover:bg-teal-50 flex items-center gap-1 shrink-0"
                              >
                                <FolderTree size={12} /> Browse
                              </button>
                              <button
                                type="button"
                                onClick={() => rmDir(i, di)}
                                disabled={dirs(r).length <= 1}
                                className="p-1.5 text-stone-400 hover:text-red-600 rounded disabled:opacity-30 shrink-0"
                              >
                                <Trash2 size={13} />
                              </button>
                            </div>
                            {browseKey === key && (
                              <div className="border border-stone-200 rounded-lg p-2.5 bg-stone-50">
                                <div className="flex items-center justify-between gap-2 mb-1.5">
                                  <div className="text-xs font-mono text-stone-600 truncate">{bPath || "(root)"}</div>
                                  <div className="flex items-center gap-2 shrink-0">
                                    <button
                                      type="button"
                                      onClick={() => loadDirs(r.connector_id, parentPath(bPath))}
                                      disabled={!bPath}
                                      className="text-xs text-teal-600 hover:underline disabled:opacity-40"
                                    >
                                      ↑ up
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => { setDir(i, di, bPath); setBrowseKey(null); }}
                                      disabled={!bPath}
                                      className="text-xs font-bold text-emerald-600 hover:underline disabled:opacity-40"
                                    >
                                      use this folder
                                    </button>
                                    <button
                                      type="button"
                                      onClick={() => setBrowseKey(null)}
                                      className="text-xs text-stone-400 hover:underline"
                                    >
                                      close
                                    </button>
                                  </div>
                                </div>
                                {bLoading ? (
                                  <p className="text-xs text-stone-400 py-1 flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> listing…</p>
                                ) : bErr ? (
                                  <p className="text-xs text-red-600 py-1">{bErr}</p>
                                ) : bDirs.length === 0 ? (
                                  <p className="text-xs text-stone-400 py-1">No sub-folders here.</p>
                                ) : (
                                  <div className="max-h-36 overflow-y-auto divide-y divide-stone-100 bg-white rounded border border-stone-200">
                                    {bDirs.map((dir) => (
                                      <button
                                        key={dir}
                                        type="button"
                                        onClick={() => { setDir(i, di, dir); loadDirs(r.connector_id, dir); }}
                                        className="w-full text-left px-2 py-1 text-xs font-mono text-stone-700 hover:bg-stone-50 flex items-center gap-1.5"
                                      >
                                        <Folder size={12} className="text-teal-500 shrink-0" /> {dir.split("/").filter(Boolean).pop()}
                                      </button>
                                    ))}
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })}
                      <input
                        value={(r.formats || []).join(", ")}
                        onChange={(e) =>
                          setRow(i, {
                            formats: e.target.value
                              .split(",")
                              .map((s) => s.trim().replace(/^\./, "").toLowerCase())
                              .filter(Boolean),
                          })
                        }
                        placeholder="formats filter, e.g. csv, parquet, xlsx (blank = all tabular)"
                        className="w-full border border-stone-200 rounded-lg px-2.5 py-1.5 text-xs bg-white focus:outline-none focus:ring-2 focus:ring-teal-500/20"
                      />
                    </div>
                  )}

                  {/* Case 3: Unstructured, Semi-Structured or Auto-detect (every supported format) */}
                  {(rowKind === "unstructured" || rowKind === "semistructured" || rowKind === "auto") && (
                    <div className="space-y-3">
                      {/* Watch Folders */}
                      <div className="space-y-2">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-bold text-stone-700 flex items-center gap-1.5">
                            <FolderTree size={13} className="text-teal-600" /> Watch folders
                          </span>
                          <button
                            type="button"
                            onClick={() => addDir(i)}
                            className="text-xs font-semibold text-teal-600 hover:text-teal-800 flex items-center gap-1 cursor-pointer"
                          >
                            <FolderPlus size={12} /> Add folder
                          </button>
                        </div>
                        {dirs(r).map((d, di) => {
                          const key = `${i}:${di}`;
                          return (
                            <div key={di} className="space-y-1.5">
                              <div className="flex items-center gap-1.5">
                                <input
                                  value={d}
                                  onChange={(e) => setDir(i, di, e.target.value)}
                                  placeholder={
                                    rowKind === "semistructured"
                                      ? "/data/json · s3://bucket/events · C:\\Logs\\API"
                                      : rowKind === "auto"
                                        ? "/data/inbox · s3://bucket/landing · C:\\Shared\\Inbox"
                                        : "/data/docs · s3://bucket/reports · C:\\Shared\\Files"
                                  }
                                  className="flex-1 border border-stone-200 rounded-lg px-2.5 py-1.5 text-xs font-mono bg-white focus:outline-none focus:ring-2 focus:ring-teal-500/20"
                                />
                                <button
                                  type="button"
                                  onClick={() => (browseKey === key ? setBrowseKey(null) : openBrowse(key, r.connector_id))}
                                  className="px-2.5 py-1.5 text-xs font-semibold text-teal-600 border border-stone-200 rounded-lg bg-white hover:bg-teal-50 flex items-center gap-1 shrink-0 cursor-pointer"
                                >
                                  <FolderTree size={12} /> Browse
                                </button>
                                <button
                                  type="button"
                                  onClick={() => rmDir(i, di)}
                                  disabled={dirs(r).length <= 1}
                                  className="p-1.5 text-stone-400 hover:text-red-600 rounded disabled:opacity-30 shrink-0 cursor-pointer"
                                >
                                  <Trash2 size={13} />
                                </button>
                              </div>
                              {browseKey === key && (
                                <div className="border border-stone-200 rounded-lg p-2.5 bg-stone-50">
                                  <div className="flex items-center justify-between gap-2 mb-1.5">
                                    <div className="text-xs font-mono text-stone-600 truncate">{bPath || "(root)"}</div>
                                    <div className="flex items-center gap-2 shrink-0">
                                      <button
                                        type="button"
                                        onClick={() => loadDirs(r.connector_id, parentPath(bPath))}
                                        disabled={!bPath}
                                        className="text-xs text-teal-600 hover:underline disabled:opacity-40"
                                      >
                                        ↑ up
                                      </button>
                                      <button
                                        type="button"
                                        onClick={() => { setDir(i, di, bPath); setBrowseKey(null); }}
                                        disabled={!bPath}
                                        className="text-xs font-bold text-emerald-600 hover:underline disabled:opacity-40"
                                      >
                                        use this folder
                                      </button>
                                      <button
                                        type="button"
                                        onClick={() => setBrowseKey(null)}
                                        className="text-xs text-stone-400 hover:underline"
                                      >
                                        close
                                      </button>
                                    </div>
                                  </div>
                                  {bLoading ? (
                                    <p className="text-xs text-stone-400 py-1 flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> listing…</p>
                                  ) : bErr ? (
                                    <p className="text-xs text-red-600 py-1">{bErr}</p>
                                  ) : bDirs.length === 0 ? (
                                    <p className="text-xs text-stone-400 py-1">No sub-folders here.</p>
                                  ) : (
                                    <div className="max-h-36 overflow-y-auto divide-y divide-stone-100 bg-white rounded border border-stone-200">
                                      {bDirs.map((dir) => (
                                        <button
                                          key={dir}
                                          type="button"
                                          onClick={() => { setDir(i, di, dir); loadDirs(r.connector_id, dir); }}
                                          className="w-full text-left px-2 py-1 text-xs font-mono text-stone-700 hover:bg-stone-50 flex items-center gap-1.5"
                                        >
                                          <Folder size={12} className="text-teal-500 shrink-0" /> {dir.split("/").filter(Boolean).pop()}
                                        </button>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              )}
                            </div>
                          );
                        })}
                        <input
                          value={(r.formats || []).join(", ")}
                          onChange={(e) =>
                            setRow(i, {
                              formats: e.target.value
                                .split(",")
                                .map((s) => s.trim().replace(/^\./, "").toLowerCase())
                                .filter(Boolean),
                            })
                          }
                          placeholder={
                            rowKind === "semistructured"
                              ? "formats filter, e.g. json, jsonl, ndjson (blank = all)"
                              : rowKind === "auto"
                                ? "formats filter, e.g. pdf, json, csv (blank = every supported format)"
                                : "formats filter, e.g. pdf, docx, txt, md (blank = all supported)"
                          }
                          className="w-full border border-stone-200 rounded-lg px-2.5 py-1.5 text-xs bg-white focus:outline-none focus:ring-2 focus:ring-teal-500/20"
                        />
                      </div>

                      {/* Keep folders as nodes */}
                      <label className="flex items-start gap-2 text-xs cursor-pointer bg-white border border-stone-200 rounded-lg p-2.5 hover:border-stone-300">
                        <input
                          type="checkbox"
                          checked={!!r.directory_nodes}
                          onChange={(e) => setRow(i, { directory_nodes: e.target.checked })}
                          className="mt-0.5 w-3.5 h-3.5 text-teal-600 border-stone-300 rounded focus:ring-teal-500/20"
                        />
                        <div className="flex-1">
                          <span className="font-bold text-stone-800 block">Keep folders as graph nodes</span>
                          <span className="block text-stone-400 text-[11px] leading-relaxed">
                            {r.directory_nodes
                              ? "Each folder becomes a Directory node — nested folders link with HAS_SUBDIRECTORY, and each file links to its folder."
                              : "Only the files are ingested. Folder hierarchy is not represented in the graph."}
                          </span>
                        </div>
                      </label>

                      {/* Bulk device upload */}
                      <div className="border-t border-dashed border-stone-200 pt-3 space-y-2">
                        <span className="text-xs font-bold text-stone-600 flex items-center gap-1.5">
                          <UploadCloud size={13} className="text-emerald-500" /> Bulk upload from this device
                        </span>
                        <div className="grid grid-cols-2 gap-2">
                          <label className="border border-dashed border-stone-200 rounded-lg p-2.5 flex flex-col items-center justify-center text-center bg-white hover:bg-stone-50 cursor-pointer relative">
                            <FileText size={16} className="text-emerald-500 mb-0.5" />
                            <span className="text-xs font-bold text-stone-700">Pick Files</span>
                            <input
                              type="file"
                              multiple
                              onChange={(e) =>
                                onPickFiles
                                  ? onPickFiles(i, Array.from(e.target.files || []))
                                  : handleInternalPickFiles(i, Array.from(e.target.files || []))
                              }
                              className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
                            />
                          </label>
                          <label className="border border-dashed border-stone-200 rounded-lg p-2.5 flex flex-col items-center justify-center text-center bg-white hover:bg-stone-50 cursor-pointer relative">
                            <FolderTree size={16} className="text-teal-500 mb-0.5" />
                            <span className="text-xs font-bold text-stone-700">Pick Folder</span>
                            <input
                              type="file"
                              multiple
                              {...({ webkitdirectory: "", directory: "" } as any)}
                              onChange={(e) =>
                                onPickFiles
                                  ? onPickFiles(i, Array.from(e.target.files || []))
                                  : handleInternalPickFiles(i, Array.from(e.target.files || []))
                              }
                              className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
                            />
                          </label>
                        </div>
                        {(pendingFiles?.[i]?.length || localPendingFiles[i]?.length || 0) > 0 && (
                          <div className="text-xs text-stone-700 bg-emerald-50 border border-emerald-200 rounded-lg px-2.5 py-1.5 flex items-center justify-between">
                            <span>
                              <b>{pendingFiles?.[i]?.length || localPendingFiles[i]?.length || 0}</b> file(s) queued for upload
                            </span>
                            <button
                              type="button"
                              className="underline text-emerald-800 font-semibold"
                              onClick={() => (onClearFiles ? onClearFiles(i) : handleInternalClearFiles(i))}
                            >
                              clear
                            </button>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-2 p-3 bg-blue-50/70 border border-blue-100/80 rounded-xl text-xs text-blue-800">
        <Info size={16} className="text-blue-500 shrink-0" />
        <span>
          You can add multiple sources and map each to a pipeline. Each source runs independently with its own configuration.
        </span>
      </div>

      {/* Footer controls */}
      <div className="flex items-center justify-between pt-1">
        <button
          type="button"
          onClick={addRow}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold text-emerald-700 bg-emerald-50 hover:bg-emerald-100 border border-emerald-200 rounded-lg transition-colors cursor-pointer"
        >
          <Plus size={14} /> Add source
        </button>
        {onNewPipeline && (
          <button
            type="button"
            onClick={onNewPipeline}
            className="text-xs font-semibold text-emerald-600 hover:text-emerald-800 flex items-center gap-1 cursor-pointer"
          >
            <Plus size={12} /> New pipeline
          </button>
        )}
      </div>
    </div>
  );
}

// Build the mappings payload for the API: drop rows without a connector; carry each mapping's own
// directories, formats, data type (kind), enabled state, and selections.
type CleanMapping = {
  connector_id: string;
  pipeline_id: string | null;
  directories?: string[];
  formats?: string[];
  directory_nodes?: boolean;
  kind?: MappingDataKind;
  pipelines?: Partial<Record<DetectedKind, string>>;
  enabled?: boolean;
  selections?: { schema: string; tables: string[]; views: string[] }[];
};

export function cleanMappings(mappings: Mapping[]): CleanMapping[] {
  return (mappings || [])
    .filter((m) => m.connector_id)
    .map((m) => {
      const row: CleanMapping = {
        connector_id: m.connector_id,
        pipeline_id: m.pipeline_id || null,
      };
      const dirs = (m.directories || []).map((d) => d.trim()).filter(Boolean);
      const fmts = (m.formats || []).filter(Boolean);
      if (dirs.length) row.directories = dirs;
      if (fmts.length) row.formats = fmts;
      if (m.directory_nodes) row.directory_nodes = true;
      if (m.kind) row.kind = m.kind;
      if (m.kind === "auto") {
        const chosen = Object.fromEntries(Object.entries(m.pipelines || {}).filter(([, id]) => id));
        row.pipeline_id = null;
        if (Object.keys(chosen).length) row.pipelines = chosen;
      }
      if (m.enabled !== undefined) row.enabled = m.enabled;
      if (m.selections && Object.keys(m.selections).length) {
        row.selections = Object.entries(m.selections).map(([schema, e]) => ({
          schema,
          tables: e.tables,
          views: e.views,
        }));
      }
      return row;
    });
}

