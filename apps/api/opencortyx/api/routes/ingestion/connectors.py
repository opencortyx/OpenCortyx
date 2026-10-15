"""Routes: saved data-source connections."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select

from opencortyx.api.schemas.ingestion.connectors import ConnectorSpec, RecipeSpec, SecretRef
from opencortyx.api.services.ingestion.connectors import (
    build_connector_config,
    connector_view,
    ensure_default_connector,
    resolve_connector_file_storage,
    resolve_table_format,
)
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.control import Connector
from opencortyx.core.security.secrets import resolve_connector_dsn
from opencortyx.ingest.sources import datalake

router = APIRouter()


@router.get("/connectors")
async def list_connectors(user: CurrentUser = Depends(require_auth)):
    async with V2Session() as db:
        await ensure_default_connector(db, user.workspace_id)   # seed the on-prem default once, lazily
        rows = (await db.execute(select(Connector).where(Connector.workspace_id == user.workspace_id)
                                 .order_by(Connector.created_at.desc()))).scalars().all()
    return {"connectors": [connector_view(c) for c in rows]}


@router.post("/connectors")
async def create_connector(spec: ConnectorSpec, user: CurrentUser = Depends(require_write)):
    if not spec.name.strip():
        raise HTTPException(status_code=400, detail="name required")
    async with V2Session() as db:
        c = Connector(workspace_id=user.workspace_id, name=spec.name.strip(),
                      kind=(spec.kind or "postgres"), config=build_connector_config(spec))
        db.add(c)
        await db.commit()
        await db.refresh(c)
    return connector_view(c)


@router.get("/connectors/{connector_id}")
async def get_connector(connector_id: str, user: CurrentUser = Depends(require_auth)):
    async with V2Session() as db:
        c = await db.get(Connector, uuid.UUID(connector_id))
    if not c or c.workspace_id != user.workspace_id:
        raise HTTPException(status_code=404, detail="connector not found")
    return connector_view(c)


@router.put("/connectors/{connector_id}")
async def update_connector(connector_id: str, spec: ConnectorSpec,
                           user: CurrentUser = Depends(require_write)):
    async with V2Session() as db:
        c = await db.get(Connector, uuid.UUID(connector_id))
        if not c or c.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="connector not found")
        if spec.name.strip():
            c.name = spec.name.strip()
        c.kind = spec.kind or c.kind
        c.config = build_connector_config(spec, existing=c.config)
        await db.commit()
        await db.refresh(c)
    return connector_view(c)


@router.delete("/connectors/{connector_id}")
async def delete_connector(connector_id: str, user: CurrentUser = Depends(require_write)):
    async with V2Session() as db:
        res = await db.execute(delete(Connector).where(
            Connector.id == uuid.UUID(connector_id), Connector.workspace_id == user.workspace_id))
        await db.commit()
    return {"status": "ok", "deleted": res.rowcount or 0}


@router.post("/connectors/{connector_id}/test")
async def test_connector(connector_id: str, user: CurrentUser = Depends(require_auth)):
    """Resolve the connector's secret + DSN and probe the source — returns table count or a clean 400."""
    import anyio

    from opencortyx.ingest.structured.ingest import _sync_introspect
    async with V2Session() as db:
        c = await db.get(Connector, uuid.UUID(connector_id))
        if not c or c.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="connector not found")
        kind = (c.config or {}).get("kind") or c.kind
        if kind in datalake.LAKE_STORAGE:        # storage connector → confirm creds resolve
            try:
                from opencortyx.core.security.secrets import resolve_secret
                if (c.config or {}).get("secret") is not None:
                    resolve_secret((c.config or {}).get("secret"))
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"credentials failed to resolve: {e}")
            return {"ok": True, "tables": 0, "note": "storage credentials OK — attach a Delta/Iceberg/Hudi connector to browse tables."}
        if kind in datalake.DATALAKE_FORMATS or kind in datalake.DOC_FORMATS:  # format/files connector
            storage_cfg, fmt, directory = await resolve_table_format(db, c)
    if kind in datalake.DOC_FORMATS:             # files connector → count ingestible files
        try:
            files = await anyio.to_thread.run_sync(lambda: datalake.list_files(storage_cfg, directory))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"lake file listing failed: {e}")
        return {"ok": True, "tables": len(files or []), "note": f"{len(files or [])} file(s) found to ingest as documents."}
    if kind in datalake.DATALAKE_FORMATS:
        try:
            tables = await anyio.to_thread.run_sync(lambda: datalake.introspect(storage_cfg, fmt, directory))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"lake read failed: {e}")
        return {"ok": True, "tables": len(tables or [])}
    try:
        dsn = resolve_connector_dsn(c.config or {})
        if not dsn:
            raise RuntimeError("connector has no resolvable connection")
        tables = await anyio.to_thread.run_sync(lambda: _sync_introspect(dsn, (c.config or {}).get("schema")))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"connection failed: {e}")
    return {"ok": True, "tables": len(tables or [])}


@router.post("/connectors/from-recipe")
async def connector_from_recipe(spec: RecipeSpec, user: CurrentUser = Depends(require_write)):
    """Create a connector from a DataHub-style YAML recipe (source.type + source.config). A
    `password: ${VAR}` becomes an env secret reference; a literal becomes an inline (encrypted) secret."""
    try:
        import yaml
        doc = yaml.safe_load(spec.recipe) or {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid YAML: {e}")
    src = (doc.get("source") or {}) if isinstance(doc, dict) else {}
    conf = src.get("config") or {}
    kind = (src.get("type") or "postgres").lower()
    host, port = conf.get("host"), conf.get("port")
    hp = conf.get("host_port")
    if hp and not host:
        host = str(hp).split(":")[0]
        port = port or (int(str(hp).split(":")[1]) if ":" in str(hp) else None)
    pw = conf.get("password")
    secret = None
    if isinstance(pw, str) and pw.startswith("${") and pw.endswith("}"):
        secret = SecretRef(provider="env", var=pw[2:-1])
    elif pw:
        secret = SecretRef(provider="inline", value=pw)
    norm_kind = _recipe_kind(kind)
    placeholder = {"HOST", "DATABASE", "USER"}          # what an export writes for an unset field
    host = None if host in placeholder else host
    conf = {k: (None if v in placeholder else v) for k, v in conf.items()}
    cspec = ConnectorSpec(name=(spec.name or f"{kind} source"), kind=norm_kind,
                          host=host, port=port, database=conf.get("database"),
                          username=conf.get("username"), schema_name=conf.get("schema"), secret=secret,
                          connection_string=conf.get("connection_string"), directory=conf.get("directory"),
                          lake_connector_id=conf.get("lake_connector_id"))
    async with V2Session() as db:
        c = Connector(workspace_id=user.workspace_id, name=cspec.name, kind=cspec.kind,
                      config=build_connector_config(cspec))
        db.add(c)
        await db.commit()
        await db.refresh(c)
    return connector_view(c)


def _recipe_kind(kind: str) -> str:
    """A recipe's `source.type` → a connector kind. Our own kinds pass through; DataHub's SQL source
    names map to ours. Collapsing everything else to postgres turned an exported S3 or local-folder
    connector into a postgres one on re-import."""
    from opencortyx.api.schemas.ingestion.connectors import connector_kinds
    k = (kind or "").lower()
    if k in connector_kinds():
        return k
    for alias, ours in (("mysql", "mysql"), ("mariadb", "mysql"), ("sqlite", "sqlite"), ("mssql", "mssql"),
                        ("sqlserver", "mssql"), ("postgres", "postgres")):
        if alias in k:
            return ours
    raise HTTPException(status_code=400, detail=f"recipe source.type {kind!r} is not a connector kind")


@router.get("/connectors/{connector_id}/recipe")
async def connector_recipe(connector_id: str, user: CurrentUser = Depends(require_auth)):
    """Export a connector as a DataHub-style YAML recipe. The password is rendered as a reference
    (env var / key-vault name / placeholder) — the real secret is NEVER emitted."""
    async with V2Session() as db:
        c = await db.get(Connector, uuid.UUID(connector_id))
    if not c or c.workspace_id != user.workspace_id:
        raise HTTPException(status_code=404, detail="connector not found")
    cfg = c.config or {}
    sec = cfg.get("secret") or {}
    prov = sec.get("provider") or "inline"
    pw_ref = ("${" + (sec.get("var") or "DB_PASSWORD") + "}") if prov == "env" \
        else (f"<azure-key-vault:{sec.get('secret_name')}>" if prov in ("azure_keyvault", "azure", "keyvault")
              else "${DB_PASSWORD}")
    config = {"host_port": f"{cfg.get('host') or 'HOST'}:{cfg.get('port') or (3306 if 'mysql' in (cfg.get('kind') or '') else 5432)}",
              "database": cfg.get("database") or "DATABASE",
              "username": cfg.get("username") or "USER", "password": pw_ref}
    if not cfg.get("host"):                              # a file / lake connector has no server to name
        config = {k: v for k, v in config.items() if k not in ("host_port", "database", "username", "password")}
    for k in ("directory", "lake_connector_id"):
        if cfg.get(k):
            config[k] = cfg[k]
    if cfg.get("schema"):
        config["schema"] = cfg["schema"]
    try:
        import yaml
        text = yaml.safe_dump({"source": {"type": cfg.get("kind") or "postgres", "config": config}},
                              sort_keys=False)
    except Exception:
        text = (f"source:\n  type: {cfg.get('kind') or 'postgres'}\n  config:\n"
                + "".join(f"    {k}: {v}\n" for k, v in config.items()))
    return {"recipe": text}


@router.get("/connectors/{connector_id}/browse")
async def browse_connector(connector_id: str, schema: Optional[str] = None,
                           user: CurrentUser = Depends(require_auth)):
    """DataHub-style catalogue browse for the ingestion picker: schemas → tables & views (+ columns),
    fast (no profiling). Credentials resolve server-side from the connector's secret store."""
    import anyio

    from opencortyx.ingest.structured.ingest import _sync_browse
    async with V2Session() as db:
        c = await db.get(Connector, uuid.UUID(connector_id))
        if not c or c.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="connector not found")
        kind = (c.config or {}).get("kind") or c.kind
        cfg_schema = (c.config or {}).get("schema") or (c.config or {}).get("schema_name") or None
        tf = None
        if kind in datalake.DATALAKE_FORMATS or kind in datalake.DOC_FORMATS:
            tf = await resolve_table_format(db, c)
    if tf is not None:                       # lake table-format / files connector → browse the directory
        storage_cfg, fmt, directory = tf
        try:
            if kind in datalake.DOC_FORMATS:    # files → list documents as a single "schema"
                files = await anyio.to_thread.run_sync(lambda: datalake.list_files(storage_cfg, directory))
                schemas = [{"schema": "files", "tables": [{"name": n, "type": "file", "columns": []} for n, _ in files]}]
            else:
                schemas = await anyio.to_thread.run_sync(lambda: datalake.browse(storage_cfg, fmt, directory))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"lake browse failed: {e}")
        return {"schemas": schemas}
    try:
        dsn = resolve_connector_dsn(c.config or {})
        if not dsn:
            raise RuntimeError("connector has no resolvable connection")
        schemas = await anyio.to_thread.run_sync(lambda: _sync_browse(dsn, schema or cfg_schema))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"browse failed: {e}")
    return {"schemas": schemas}


@router.get("/connectors/{connector_id}/dirs")
async def list_connector_dirs(connector_id: str, path: str = "",
                              user: CurrentUser = Depends(require_auth)):
    """Folder picker for document ingestion: list immediate sub-folders of `path` in a data lake
    (s3/gcs/adls) or on-prem (local) connector. Lets the user browse + select a folder, like the
    on-prem flow but for the lake. Local access is gated by the Settings 'Allow local file access'
    permission and host→mount path translation."""
    import anyio

    from opencortyx.core.config import runtime as rc
    async with V2Session() as db:
        c = await db.get(Connector, uuid.UUID(connector_id))
        if not c or c.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="connector not found")
        cfg = c.config or {}
        ckind = (cfg.get("kind") or c.kind or "").lower()
        if ckind in datalake.LAKE_STORAGE:                # connector IS the storage
            storage_cfg = cfg
        elif ckind in datalake.DOC_FORMATS or ckind in datalake.DATALAKE_FORMATS:
            storage_cfg, _fmt, _dir = await resolve_table_format(db, c)
            if not (path or "").strip():
                path = cfg.get("directory") or ""
        else:
            raise HTTPException(status_code=400, detail="connector does not support folder browsing")
    is_local = (storage_cfg.get("kind") or "local").lower() == "local"
    if is_local:
        try:
            await rc.load()
        except Exception:
            pass
        if not rc.allow_local_files():
            raise HTTPException(status_code=403, detail="Local file access is disabled — enable it in "
                               "Settings → Local files.")
        path = datalake.translate_local_path(path)
        if not (path or "").strip():
            roots = await anyio.to_thread.run_sync(datalake.list_roots)
            return {"path": "", "dirs": [datalake.untranslate_local_path(r) for r in roots]}
    try:
        dirs = await anyio.to_thread.run_sync(lambda: datalake.list_dirs(storage_cfg, path))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"list dirs failed: {e}")
    if is_local:
        # Answer in HOST coordinates — the path the user typed, recognises, and can retype.
        path = datalake.untranslate_local_path(path)
        dirs = [datalake.untranslate_local_path(d) for d in (dirs or [])]
    return {"path": path, "dirs": dirs}


@router.get("/connectors/{connector_id}/tabular-files")
async def list_connector_tabular_files(connector_id: str, path: str = "",
                                       user: CurrentUser = Depends(require_auth)):
    """List the CSV / TSV / Parquet / Excel files under `path` in a file/lake connector, for the
    structured Ingestion wizard's 'From a connector' file picker. Same storage resolution + local
    permission gate as the folder browse; filtered to tabular extensions."""
    import anyio

    from opencortyx.ingest.sources import datalake
    from opencortyx.ingest.formats.structured import tabular
    storage_cfg, path = await resolve_connector_file_storage(connector_id, path, user.workspace_id)
    exts = [e.lstrip(".") for e in tabular.TABULAR_EXTS]
    try:
        listed = await anyio.to_thread.run_sync(lambda: datalake.list_files(storage_cfg, path, exts))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"list files failed: {e}")
    return {"path": path, "files": [{"name": n, "path": p} for n, p in listed]}


@router.get("/connectors/{connector_id}/document-files")
async def list_connector_document_files(connector_id: str, path: str = "", exts: str = "",
                                        user: CurrentUser = Depends(require_auth)):
    """List the DOCUMENT files under `path` in a file/lake connector. Used by the mapping engines so an
    author can see what the bound source actually holds and verify a mapping against one. `exts` is an
    optional comma-separated filter (e.g. "json" for the Semi-Structured engine); omitted → every
    ingestible document type. Same storage resolution + local permission gate as the folder browse."""
    import anyio

    storage_cfg, path = await resolve_connector_file_storage(connector_id, path, user.workspace_id)
    want = [e.strip().lstrip(".") for e in (exts or "").split(",") if e.strip()] or None
    try:   # default exts = datalake._DOC_EXTS (the ingestible document types)
        listed = await anyio.to_thread.run_sync(lambda: datalake.list_files(storage_cfg, path, want))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"list files failed: {e}")
    return {"path": path, "files": [{"name": n, "path": p} for n, p in listed]}
