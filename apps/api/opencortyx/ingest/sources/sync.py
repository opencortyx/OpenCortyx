"""Change-detection sync engine — keep the graph in step with a live source (add / update / remove).

The individual-developer contract: point an ingestion at a folder (or lake) once, then have the graph
STAY correct as files change — without re-ingesting the world each time. The strategy is
connector-aware but the shape is uniform:

  1. `datalake.stat_files()` returns a cheap SIGNATURE per file (local: mtime+size · cloud: etag+size)
     — a stat/listing, never a content read.
  2. `compute_plan()` diffs that against the ingestion's stored MANIFEST (config["sync_state"]):
        added   → files we've never ingested
        changed → signature differs from last time
        removed → in the manifest but gone from the source
        unchanged → skip entirely (this is what makes it cheap — no full rescan)
  3. The worker ingests only added+changed, and SOFT-REMOVES the graph for removed/changed-old
     (marks __status=removed; never hard-deletes, so a transient source outage can't wipe data).
  4. The manifest is rewritten with the new signatures.

Modes (ingestion.config["mode"]): on_demand (manual), scheduled (cron via Ingestion.schedule),
realtime (every beat tick). All three call the SAME diff — only the trigger differs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional
from opencortyx.core.substrate import NodeRepo
from opencortyx.ingest.formats import catalog as formats
from opencortyx.ingest.detection import routing

# manifest entry: {"sig": "<opaque>", "source_id": "<uuid|None>", "ingested_at": "<iso>"}
MANIFEST_KEY = "sync_state"
FAILURES_KEY = "failures"          # capped list of {at, category, message, run_id}
MAX_FAILURES = 25
MODE_ON_DEMAND, MODE_SCHEDULED, MODE_REALTIME = "on_demand", "scheduled", "realtime"
VALID_MODES = (MODE_ON_DEMAND, MODE_SCHEDULED, MODE_REALTIME)
STATUS_REMOVED = "removed"

# canonical ingestion kinds — the whole app standardises on these three names.
KIND_STRUCTURED, KIND_UNSTRUCTURED, KIND_SEMISTRUCTURED = "structured", "unstructured", "semistructured"
VALID_KINDS = (KIND_STRUCTURED, KIND_UNSTRUCTURED, KIND_SEMISTRUCTURED)
# legacy → canonical aliases (older rows / UI used document + json)
_KIND_ALIASES = {"document": KIND_UNSTRUCTURED, "documents": KIND_UNSTRUCTURED, "doc": KIND_UNSTRUCTURED,
                 "json": KIND_SEMISTRUCTURED, "semi-structured": KIND_SEMISTRUCTURED,
                 "semi_structured": KIND_SEMISTRUCTURED}


def normalize_mode(mode: Optional[str]) -> str:
    m = (mode or "").strip().lower()
    return m if m in VALID_MODES else MODE_ON_DEMAND


def normalize_kind(kind: Optional[str]) -> str:
    """Map any historical/UI spelling to one of the three canonical kinds. Unknown → unstructured
    (the safe default: treat it as files)."""
    k = (kind or "").strip().lower()
    if k in VALID_KINDS:
        return k
    return _KIND_ALIASES.get(k, KIND_UNSTRUCTURED)


# file-based kinds go through the change-detection manifest; structured re-runs the DB introspection.
FILE_KINDS = (KIND_UNSTRUCTURED, KIND_SEMISTRUCTURED)
# default file extensions considered per kind when a mapping doesn't pin its own `formats`.
SEMISTRUCTURED_EXTS = ("json", "jsonl", "ndjson")
STRUCTURED_EXTS = ("csv", "tsv", "psv", "tab", "parquet", "pq", "xlsx", "xlsm", "xls")


def default_exts(kind: str):
    """The extensions a watch-folder scan considers for a kind when `formats` isn't pinned. Unstructured
    → None (all supported document types, handled downstream)."""
    if kind == KIND_SEMISTRUCTURED:
        return list(SEMISTRUCTURED_EXTS)
    if kind == KIND_STRUCTURED:
        return list(STRUCTURED_EXTS)
    return None


KIND_AUTO = "auto"


def mapping_kind(kind: Optional[str]) -> str:
    """A mapping's classification: "auto" (each file's kind is detected) or one of the three kinds."""
    return KIND_AUTO if str(kind or "").strip().lower() == KIND_AUTO else normalize_kind(kind)


def file_kind(filename: str, content: Optional[bytes] = None) -> str:
    """The kind a file is ingested as when none is forced, decided by its format (ingest.formats)."""
    return normalize_kind(routing.route_for(filename, content or None))


def auto_exts() -> list:
    """Every extension ingestion accepts: what an auto-detect watch folder scans."""
    return sorted(formats.FORMATS)


def kind_pipelines(value) -> dict:
    """An auto-detect mapping's {kind: pipeline_id}, limited to the three kinds."""
    if not isinstance(value, dict):
        return {}
    return {kind: str(pid) for kind, pid in value.items() if kind in VALID_KINDS and pid}


def classify_failure(exc) -> str:
    """Bucket an exception into a failure CATEGORY so recovery can be reasoned about and reported.
    Categories mirror the operator's mental model: memory | queue | llm | api | io | db | runtime.
    Best-effort string matching — the category drives the UI badge + retry policy, not control flow."""
    name = type(exc).__name__.lower()
    msg = (str(exc) or "").lower()
    blob = f"{name} {msg}"
    if isinstance(exc, MemoryError) or "out of memory" in blob or "oom" in blob or "cuda" in blob:
        return "memory"
    if "celery" in blob or "broker" in blob or "redis" in blob or "queue" in blob or "worker" in blob:
        return "queue"
    if ("llm" in blob or "completion" in blob or "prompt" in blob
            or "token" in blob):
        return "llm"
    if ("timeout" in blob or "connection" in blob or "httpx" in blob or "http" in blob
            or "429" in blob or "502" in blob or "503" in blob or "504" in blob or "rate limit" in blob):
        return "api"
    if ("filenotfound" in name or "permission" in name or "oserror" in name or "ioerror" in name
            or "no such file" in blob or "disk" in blob):
        return "io"
    if "operationalerror" in name or "asyncpg" in blob or "sqlalchemy" in blob or "database" in blob:
        return "db"
    return "runtime"


def record_failure(cfg: dict, exc, run_id=None) -> dict:
    """Append a failure to the ingestion config's capped failure log and return the updated cfg.
    All failures are recorded (requirement: 'all failures should be recorded'). Newest last."""
    from datetime import datetime, timezone
    cfg = dict(cfg or {})
    failures = list(cfg.get(FAILURES_KEY) or [])
    failures.append({"at": datetime.now(timezone.utc).isoformat(),
                     "category": classify_failure(exc),
                     "message": (str(exc) or type(exc).__name__)[:500],
                     "run_id": str(run_id) if run_id else None})
    cfg[FAILURES_KEY] = failures[-MAX_FAILURES:]
    return cfg


MAPPINGS_KEY = "mappings"


def resolve_mappings(ingestion) -> list:
    """The flexible connector↔pipeline mapping. Returns a list of {connector_id, pipeline_id} pairs
    (either may be None). Supports 1:1, 1:N, N:1, N:N via config["mappings"], and falls back to the
    legacy single ingestion.connector_id + ingestion.pipeline_id when no mappings are set — so old
    ingestions keep working unchanged. Each pair becomes one dispatched job at run/sync time."""
    cfg = ingestion.config or {}
    # ingestion-level defaults (back-compat) — a mapping without its own dirs/formats inherits these.
    ing_dirs = [d for d in (cfg.get("directories") or []) if d and str(d).strip()]
    ing_fmts = [f for f in (cfg.get("formats") or []) if f]
    maps = cfg.get(MAPPINGS_KEY) or []
    out = []
    for m in maps:
        if not isinstance(m, dict):
            continue
        cid = m.get("connector_id")
        pid = m.get("pipeline_id")
        mdirs = [d for d in (m.get("directories") or []) if d and str(d).strip()] or ing_dirs
        mfmts = [f for f in (m.get("formats") or []) if f] or ing_fmts
        out.append({"connector_id": str(cid) if cid else None,
                    "pipeline_id": str(pid) if pid else None,
                    "directories": mdirs, "formats": mfmts,
                    "directory_nodes": (m.get("directory_nodes")
                                        if m.get("directory_nodes") is not None
                                        else cfg.get("directory_nodes")),
                    "kind": (m.get("kind") or None),   # explicit per-mapping kind (else derived from pipeline)
                    "pipelines": kind_pipelines(m.get("pipelines")),
                    "enabled": (bool(m["enabled"]) if m.get("enabled") is not None else True),
                    "selections": m.get("selections") or []})
    if out:
        return out
    return [{"connector_id": str(ingestion.connector_id) if ingestion.connector_id else None,
             "pipeline_id": str(ingestion.pipeline_id) if ingestion.pipeline_id else None,
             "directories": ing_dirs, "formats": ing_fmts, "kind": None, "pipelines": {},
             # legacy single-connector shape: the flag can only be ingestion-level here
             "directory_nodes": cfg.get("directory_nodes")}]


def has_watch_folders(ingestion) -> bool:
    """True if any mapping (or the ingestion itself) points at folders to WATCH — i.e. it's a
    file-scan sync (per-file change detection), not a database re-introspection. A structured
    ingestion with watch folders (CSV/Parquet) uses the file-sync path; a structured DB connector
    (no folders) uses the re-introspect path."""
    cfg = ingestion.config or {}
    if [d for d in (cfg.get("directories") or []) if d and str(d).strip()]:
        return True
    for m in (cfg.get(MAPPINGS_KEY) or []):
        if isinstance(m, dict) and [d for d in (m.get("directories") or []) if d and str(d).strip()]:
            return True
    return False


def is_file_sync(ingestion) -> bool:
    """Whether run/sync/tick should route this ingestion through the file change-detection engine
    (v2_sync_ingestion) vs the structured DB re-introspect. File kinds always do; structured does
    only when it has watch folders."""
    k = normalize_kind(ingestion.kind)
    return k in FILE_KINDS or has_watch_folders(ingestion)


def compute_plan(manifest: dict, current: list) -> dict:
    """Diff a fresh listing against the stored manifest.

    `manifest` = {path: {sig, source_id, ...}} (may be empty on first run).
    `current`  = [(name, path, sig)] from datalake.stat_files().
    Returns {added, changed, removed, unchanged} where each item is a dict
    {name, path, sig, source_id?}. `removed` carries the old source_id to soft-remove.
    """
    manifest = manifest or {}
    seen = set()
    added, changed, unchanged = [], [], []
    for name, path, sig in current:
        seen.add(path)
        prior = manifest.get(path)
        if prior is None:
            added.append({"name": name, "path": path, "sig": sig})
        elif str(prior.get("sig") or "") != str(sig or "") or not sig:
            # sig differs, or the store gave us no signal (empty sig) → treat as changed to be safe
            changed.append({"name": name, "path": path, "sig": sig,
                            "source_id": prior.get("source_id")})
        else:
            unchanged.append({"name": name, "path": path, "sig": sig,
                              "source_id": prior.get("source_id")})
    removed = [{"path": p, "name": p.rstrip("/").split("/")[-1],
                "source_id": (e or {}).get("source_id")}
               for p, e in manifest.items() if p not in seen]
    return {"added": added, "changed": changed, "removed": removed, "unchanged": unchanged}


def spare_unreadable(plan: dict, unreadable: list) -> list:
    """Take out of `plan["removed"]` every file under a folder that could not be listed, and return
    them. A folder that cannot be read (unmounted, renamed, a network blip) says nothing about its
    files: counted as removed, one outage would soft-remove everything ever synced from it. They are
    carried forward unchanged and judged again on the next pass that can read the folder."""
    roots = [r.rstrip("/") + "/" for r in unreadable if r]
    kept = [it for it in plan["removed"] if any(it["path"].startswith(r) for r in roots)]
    plan["removed"] = [it for it in plan["removed"] if it not in kept]
    return kept


def _cron_field_match(field: str, value: int) -> bool:
    """Match one standard cron field against a value. Supports '*', '*/n', 'a-b', 'a,b,c', and lists
    of those. Deliberately small (no external croniter dep) — minute granularity is enough here."""
    field = (field or "*").strip()
    for part in field.split(","):
        part = part.strip()
        if part in ("*", "?"):
            return True
        if part.startswith("*/"):
            try:
                if value % int(part[2:]) == 0:
                    return True
            except ValueError:
                pass
            continue
        if "-" in part:
            try:
                lo, hi = (int(x) for x in part.split("-", 1))
                if lo <= value <= hi:
                    return True
            except ValueError:
                pass
            continue
        try:
            if int(part) == value:
                return True
        except ValueError:
            pass
    return False


def cron_valid(expr: Optional[str]) -> bool:
    """Whether a 5-field cron expression uses only what `_cron_field_match` understands."""
    import re
    fields = str(expr or "").split()
    part = r"(\*|\?|\*/\d+|\d+(-\d+)?)"
    return len(fields) == 5 and all(re.fullmatch(rf"{part}(,{part})*", f) for f in fields)


def hibernation_allows(cfg: dict, now, backoff: float) -> bool:
    """Whether a HIBERNATED ingestion may run again: only once `backoff` seconds have passed since it
    failed. This decides for every mode, the schedule included: a `* * * * *` ingestion whose folder
    is missing otherwise failed again every minute, and the backoff never applied."""
    ha = (cfg or {}).get("hibernated_at")
    try:
        when = datetime.fromisoformat(ha) if ha else None
    except (TypeError, ValueError):
        when = None
    if when is None:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds() >= backoff


def cron_due(expr: Optional[str], now) -> bool:
    """True if a 5-field cron expression (min hour dom month dow) matches `now` (a UTC datetime), to
    the minute. Blank/invalid → never due (the ingestion just won't fire on a schedule). dow: 0/7=Sun.
    Called once per beat tick per scheduled ingestion, so the beat interval should be <= 60s."""
    if not expr or not str(expr).strip():
        return False
    fields = str(expr).split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    wd = (now.weekday() + 1) % 7   # Python Mon=0 → cron Sun=0
    return (_cron_field_match(minute, now.minute) and _cron_field_match(hour, now.hour)
            and _cron_field_match(dom, now.day) and _cron_field_match(month, now.month)
            and (_cron_field_match(dow, wd) or _cron_field_match(dow, 7 if wd == 0 else wd)))


async def _derived_ids(workspace_id: uuid.UUID, source_id: uuid.UUID) -> list:
    """The source node and everything derived from it, two levels down: chunks, summaries and profile
    sections, then the entities, claims and keywords extracted from those."""
    derived = await NodeRepo().descendants(workspace_id, [source_id], max_depth=2)
    return [source_id] + [n.id for n in derived]


async def soft_remove_source(workspace_id: uuid.UUID, source_id) -> int:
    """Mark a source and its derived nodes removed: kept, but excluded from retrieval. Reversible,
    so a transient source outage never deletes data. Returns the number of nodes touched."""
    if not source_id:
        return 0
    try:
        sid = source_id if isinstance(source_id, uuid.UUID) else uuid.UUID(str(source_id))
    except (ValueError, TypeError):
        return 0
    ids = await _derived_ids(workspace_id, sid)
    await NodeRepo().patch_properties(workspace_id, ids, {
        "__status": STATUS_REMOVED, "__removed_at": datetime.now(timezone.utc).isoformat()})
    await unsupersede_predecessors(workspace_id, ids)
    return len(ids)


async def unsupersede_predecessors(workspace_id, gone_ids) -> int:
    """Restore the nodes these departing nodes had superseded. Returns the count restored.

    Retrieval hides superseded nodes, and re-ingesting near-duplicate content supersedes the older
    copy. When the newer copy goes away the older one must become current again, or it stays hidden
    with `superseded_by` pointing at nothing. A node superseded by something else stays retired.
    """
    if not gone_ids:
        return 0
    repo = NodeRepo()
    gone = {str(i) for i in gone_ids}
    restore = [n.id for n in await repo.find(workspace_id, status="superseded")
               if str((n.properties or {}).get("superseded_by") or "").strip('"') in gone]
    return await repo.patch_properties(workspace_id, restore, remove=("superseded", "superseded_by"))


