"""Services: saved, re-runnable ingestions and their runs."""
import uuid
from typing import Any, Optional

from fastapi import HTTPException
from opencortyx.ingest.sources import sync


def safe_uuid(val: Any, workspace_id: Optional[uuid.UUID] = None) -> Optional[uuid.UUID]:
    if not val or not str(val).strip():
        return None
    val_str = str(val).strip()
    try:
        return uuid.UUID(val_str)
    except (ValueError, TypeError):
        base = workspace_id or uuid.NAMESPACE_URL
        return uuid.uuid5(base, val_str)


def selections_of(cfg: dict) -> list:
    """Normalize an ingestion's config to a list of {schema, tables, views} (back-compat for the old
    single-schema shape)."""
    sels = cfg.get("selections")
    if sels:
        return sels
    if cfg.get("schema") or cfg.get("tables") or cfg.get("views"):   # legacy single-schema
        return [{"schema": cfg.get("schema"), "tables": cfg.get("tables") or [],
                 "views": cfg.get("views") or []}]
    return []


def ingestion_view(i) -> dict:
    cfg = i.config or {}
    sels = selections_of(cfg)
    obj_count = sum(len(s.get("tables") or []) + len(s.get("views") or []) for s in sels)
    failures = cfg.get("failures") or []
    return {"id": str(i.id), "name": i.name, "kind": sync.normalize_kind(i.kind),
            "connector_id": str(i.connector_id) if i.connector_id else None,
            "pipeline_id": str(i.pipeline_id) if i.pipeline_id else None,
            "mappings": sync.resolve_mappings(i),
            "selections": sels, "object_count": obj_count,
            "schemas": [s.get("schema") for s in sels if s.get("schema")],
            "directories": cfg.get("directories") or [],
            "directory_nodes": bool(cfg.get("directory_nodes")),
            "formats": cfg.get("formats") or [],
            "enrich": cfg.get("enrich", True),
            "mode": (cfg.get("mode") or "on_demand"),
            # one-time device uploads: no connector, can't be re-run — the files ARE the source
            "source": cfg.get("source") or ("connector" if i.connector_id else None),
            "files": cfg.get("files") or [],
            "run_ids": cfg.get("runs") or [],
            "paused": bool(cfg.get("paused")),
            "hibernated": bool(cfg.get("hibernated")),
            "sync_summary": {"tracked": len(cfg.get("sync_state") or {})},
            "failures": failures[-10:], "failure_count": len(failures),
            "schedule": i.schedule, "status": i.status,
            "last_run_id": i.last_run_id,
            "last_run_at": i.last_run_at.isoformat() if i.last_run_at else None,
            "created_at": i.created_at.isoformat() if i.created_at else None,
            "updated_at": i.updated_at.isoformat() if i.updated_at else None}


def clean_mappings(mappings) -> list:
    """Normalize a flexible connector↔pipeline mapping list. Each mapping carries its OWN source:
    {connector_id, pipeline_id, directories[], formats[], kind}. Drops entries without a connector;
    pipeline_id/kind are optional (kind is otherwise derived from the pipeline). An auto-detect mapping
    carries `pipelines`, one pipeline per detected kind."""
    out = []
    for m in (mappings or []):
        if not isinstance(m, dict):
            continue
        cid = m.get("connector_id")
        if not cid:
            continue
        row = {"connector_id": str(cid),
               "pipeline_id": str(m["pipeline_id"]) if m.get("pipeline_id") else None}
        dirs = [d for d in (m.get("directories") or []) if d and str(d).strip()]
        fmts = [f for f in (m.get("formats") or []) if f]
        if dirs:
            row["directories"] = dirs
        if fmts:
            row["formats"] = fmts
        # PER MAPPING, not per ingestion: one connector can feed a flat archive to one pipeline and a
        # meaningfully-nested tree to another, and only the second wants its folders in the graph.
        if m.get("directory_nodes") is not None:
            row["directory_nodes"] = bool(m["directory_nodes"])
        if m.get("kind"):
            row["kind"] = str(m["kind"])
        pipelines = sync.kind_pipelines(m.get("pipelines"))
        if pipelines:
            row["pipelines"] = pipelines
        if m.get("enabled") is not None:
            row["enabled"] = bool(m["enabled"])
        if m.get("selections") is not None:
            row["selections"] = m["selections"]
        out.append(row)
    return out


def ingestion_config(spec) -> dict:
    cfg = {"selections": spec.selections or [], "enrich": bool(spec.enrich),
           "mode": sync.normalize_mode(spec.mode)}
    if spec.directories is not None:
        cfg["directories"] = [d for d in spec.directories if d and str(d).strip()]
    if spec.directory_nodes is not None:
        cfg["directory_nodes"] = bool(spec.directory_nodes)
    if spec.formats is not None:
        cfg["formats"] = [f for f in spec.formats if f]
    if spec.mappings is not None:
        cfg["mappings"] = clean_mappings(spec.mappings)
    if spec.anchors is not None:
        # Compile the rules HERE so a malformed glob is a 400 on save, not a per-file failure
        # discovered part-way through a 10,000-file run.
        from opencortyx.ingest.extraction import anchors as _anchors
        try:
            _anchors.parse_rules(spec.anchors)
        except _anchors.AnchorRuleError as e:
            raise HTTPException(status_code=400, detail=str(e))
        cfg["anchors"] = spec.anchors
    return cfg


def run_view(r, detail=False):
    """Serialise an IngestRun for the Progress UI."""
    v = {"id": str(r.id), "kind": r.kind, "asset": r.asset, "status": r.status,
         "percent": int(r.percent or 0), "current_step": int(r.current_step or 0),
         "total_steps": int(r.total_steps or 0), "steps": r.steps or [], "error": r.error,
         "created_at": r.created_at.isoformat() if r.created_at else None,
         "updated_at": r.updated_at.isoformat() if r.updated_at else None,
         "started_at": r.started_at.isoformat() if r.started_at else None,
         "finished_at": r.finished_at.isoformat() if r.finished_at else None}
    if detail:
        v["result"] = r.result or {}
        v["source_ref"] = r.source_ref
    return v
