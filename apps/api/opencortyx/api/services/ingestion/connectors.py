"""Services: saved data-source connections."""
import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select

from opencortyx.api.schemas.ingestion.connectors import ConnectorSpec
from opencortyx.core.db import V2Session
from opencortyx.domain.control import Connector


def build_connector_config(spec: "ConnectorSpec", existing: Optional[dict] = None) -> dict:
    """Merge a spec into a connector config. Secrets are encrypted/encoded at rest — never plaintext."""
    from opencortyx.core.security.secrets import encrypt_secret
    cfg = dict(existing or {})
    cfg["kind"] = (spec.kind or cfg.get("kind") or "postgres")
    for f in ("host", "database", "username"):
        v = getattr(spec, f)
        if v is not None:
            cfg[f] = v
    if spec.port is not None:
        cfg["port"] = spec.port
    if spec.schema_name is not None:
        cfg["schema"] = spec.schema_name
    if spec.connection_string is not None:
        cfg["connection_string"] = encrypt_secret(spec.connection_string)
    for f in ("options", "lake_connector_id", "directory", "query_engine_id"):
        v = getattr(spec, f)
        if v is not None:
            cfg[f] = v
    if spec.secret is not None:
        s = {k: v for k, v in spec.secret.dict().items() if v is not None}
        if (s.get("provider") or "inline") == "inline" and s.get("value"):
            s["value"] = encrypt_secret(s["value"])     # encrypt inline password at rest
        cfg["secret"] = s
    return cfg


def connector_view(c) -> dict:
    """Safe public view — NEVER exposes the secret value or the resolved DSN."""
    cfg = c.config or {}
    sec = cfg.get("secret") or {}
    return {"id": str(c.id), "name": c.name, "kind": cfg.get("kind") or c.kind,
            "host": cfg.get("host"), "port": cfg.get("port"), "database": cfg.get("database"),
            "username": cfg.get("username"), "schema": cfg.get("schema"),
            "secret_provider": sec.get("provider") or ("inline" if cfg.get("connection_string") else None),
            "has_connection_string": bool(cfg.get("connection_string")),
            "options": cfg.get("options"), "lake_connector_id": cfg.get("lake_connector_id"),
            "directory": cfg.get("directory"), "query_engine_id": cfg.get("query_engine_id"),
            "is_default": bool(cfg.get("is_default")),
            "created_at": c.created_at.isoformat() if c.created_at else None}


async def ensure_default_connector(db, workspace_id) -> bool:
    """Every workspace gets a default ON-PREM (local files) connector, seeded lazily on first list so
    the individual-developer path — point at a folder and go — needs zero connector setup. Idempotent:
    keyed by config.is_default, created once and never duplicated. Returns True if it seeded. Commits."""
    has_default = (await db.execute(select(Connector.id).where(
        Connector.workspace_id == workspace_id,
        Connector.config["is_default"].astext == "true"))).first()
    if has_default is not None:
        return False
    db.add(Connector(workspace_id=workspace_id, name="On-prem files (local)", kind="local",
                     config={"kind": "local", "is_default": True}))
    await db.commit()
    return True


async def resolve_table_format(db, tf):
    """For a table-format connector (delta/iceberg/hudi): return (storage_cfg, fmt, directory) by
    resolving its referenced lake (S3/GCS/ADLS) storage connector. `local` lakes (or an absolute
    directory) need no storage connector."""
    cfg = tf.config or {}
    fmt = (cfg.get("kind") or "delta").lower()
    directory = cfg.get("directory") or ""
    storage_cfg = {"kind": "local"}
    lake_id = cfg.get("lake_connector_id")
    if lake_id:
        lc = await db.get(Connector, uuid.UUID(lake_id))
        if not lc or lc.workspace_id != tf.workspace_id:
            raise RuntimeError("referenced data-lake connector not found")
        storage_cfg = lc.config or {}
    return storage_cfg, fmt, directory


async def resolve_connector_file_storage(connector_id: str, path: str, workspace_id):
    """Shared storage resolution for the connector file pickers: resolve the connector to a storage
    config + starting path, applying the local-file permission gate and host→mount translation.
    Returns (storage_cfg, path)."""
    from opencortyx.core.config import runtime as rc
    from opencortyx.ingest.sources import datalake
    async with V2Session() as db:
        c = await db.get(Connector, uuid.UUID(connector_id))
        if not c or c.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="connector not found")
        cfg = c.config or {}
        ckind = (cfg.get("kind") or c.kind or "").lower()
        if ckind in datalake.LAKE_STORAGE:
            storage_cfg = cfg
        elif ckind in datalake.DOC_FORMATS or ckind in datalake.DATALAKE_FORMATS:
            storage_cfg, _fmt, _dir = await resolve_table_format(db, c)
            if not (path or "").strip():
                path = cfg.get("directory") or ""
        else:
            raise HTTPException(status_code=400, detail="connector does not expose files")
    if (storage_cfg.get("kind") or "local").lower() == "local":
        try:
            await rc.load()
        except Exception:
            pass
        if not rc.allow_local_files():
            raise HTTPException(status_code=403, detail="Local file access is disabled — enable it in "
                               "Settings → Local files.")
        path = datalake.translate_local_path(path)
    if not (path or "").strip():
        raise HTTPException(status_code=400, detail="This connector has no folder set — give it a "
                            "directory (or pass ?path=) so the file listing has somewhere to look.")
    return storage_cfg, path
