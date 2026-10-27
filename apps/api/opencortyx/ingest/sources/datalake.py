"""Lakehouse table formats — read Delta / Iceberg / Hudi tables from a data-lake directory using
DuckDB as the query engine.

Composition (matches the connector UI):
  • a STORAGE connector (kind s3 | gcs | adls | local) holds the lake credentials, and
  • a TABLE-FORMAT connector (kind delta | iceberg | hudi) references that storage connector via
    `lake_connector_id`, a `directory` (which holds many tables), and a `query_engine_id`
    (a DuckDB connector).
Each immediate sub-directory of `directory` carrying the format's marker (_delta_log / metadata /
.hoodie) is one table. The query engine (DuckDB) does the actual scan:
  delta → delta_scan() · iceberg → iceberg_scan() · hudi → read_parquet() over the base files.
"""
import os
from opencortyx.core.config import settings
from opencortyx.core.security.secrets import resolve_secret

_MARKER = {"delta": "_delta_log", "iceberg": "metadata", "hudi": ".hoodie"}
DATALAKE_FORMATS = {"delta", "iceberg", "hudi"}
DOC_FORMATS = {"files"}                         # unstructured DOCUMENTS in a lake (PDF/docx/txt/…)
LAKE_STORAGE = {"s3", "gcs", "adls", "local"}
_DOC_EXTS = (".pdf", ".txt", ".md", ".markdown", ".docx", ".doc", ".html", ".htm",
             ".csv", ".json", ".pptx", ".rtf", ".epub")


def _fsspec_options(storage_cfg: dict) -> dict:
    """Map a storage connector's creds to fsspec storage_options. (Cloud file READS use fsspec —
    s3fs/adlfs/gcsfs — not DuckDB. Auth shapes differ per backend.)"""
    provider = (storage_cfg.get("kind") or "local").lower()
    opts = storage_cfg.get("options") or {}
    sec = storage_cfg.get("secret")
    secret_val = resolve_secret(sec) if sec is not None else ""
    if provider == "s3":
        if (opts.get("auth") or "") == "credential_chain":
            return {}                            # s3fs uses the ambient AWS credential chain
        return {"key": opts.get("access_key_id"), "secret": secret_val,
                "client_kwargs": {"region_name": opts.get("region") or "us-east-1"}}
    if provider == "gcs":
        return {}                                # gcsfs: ambient creds / GOOGLE_APPLICATION_CREDENTIALS
    if provider == "adls":
        auth = (opts.get("auth") or "").lower()
        if auth == "service_principal":
            return {"account_name": opts.get("account_name"), "tenant_id": opts.get("tenant_id"),
                    "client_id": opts.get("client_id"), "client_secret": secret_val}
        if auth == "managed_identity":
            return {"account_name": opts.get("account_name"), "anon": False}
        return {"connection_string": secret_val}   # SAS / connection string
    return {}


def translate_local_path(p: str) -> str:
    """Map a host path to its mounted path inside the worker/api container, if configured.
    The worker is a container, so a host path (e.g. C:\\Users\\me\\docs) is only readable when that
    host folder is bind-mounted in. Set LOCAL_FS_HOST_ROOT (e.g. C:/Users/me) + LOCAL_FS_MOUNT
    (e.g. /host) in the env; a path under the host root is rewritten to the mount. No-op otherwise."""
    host = settings.LOCAL_FS_HOST_ROOT.strip()
    mount = settings.LOCAL_FS_MOUNT.strip()
    if not p or not host or not mount:
        return p
    norm = p.replace("\\", "/")
    h = host.replace("\\", "/").rstrip("/")
    if norm.lower().startswith(h.lower()):
        rest = norm[len(h):].lstrip("/")
        return f"{mount.rstrip('/')}/{rest}" if rest else mount.rstrip("/")
    return p


def list_roots() -> list:
    """The top of the server's filesystem, as browsable paths — the picker's starting point.

    Cross-platform by construction: Windows gets its drive letters (C:/, D:/ …), POSIX gets "/". No
    path is ever hardcoded, so the same build works on a developer's Windows laptop, a macOS machine
    and a Linux server.

    A bind-mount (LOCAL_FS_MOUNT) takes priority when one is configured, because in a CONTAINER the
    real filesystem is the container's own — /app, /bin, /boot — which is neither the user's data nor
    anything they can ingest. Running natively there is no mount and no translation: the server sees
    the user's disks directly, which is what "watch this folder" means in production."""
    mount = local_fs_root()
    if mount:
        return [mount]
    if os.name == "nt":
        import string
        return [f"{d}:/" for d in string.ascii_uppercase if os.path.isdir(f"{d}:/")]
    return ["/"]


def local_fs_root() -> str:
    """The container path the HOST filesystem is mounted at, or "" when none is configured.

    The API runs in a container, so an unset/empty browse path lists the CONTAINER root — /app, /bin,
    /boot, /dev — which is never what "browse my files" means and exposes nothing the user can ingest.
    When a host folder is bind-mounted, browsing starts THERE instead."""
    mount = settings.LOCAL_FS_MOUNT.strip().rstrip("/")
    return mount if (mount and os.path.isdir(mount)) else ""


def untranslate_local_path(p: str) -> str:
    """Inverse of `translate_local_path`: mount path → the HOST path the user recognises.

    Without this the picker answers in container coordinates (`/host/Reports`), which is not a path
    the user can verify, retype, or match against what they see in Explorer/Finder."""
    host = settings.LOCAL_FS_HOST_ROOT.strip()
    mount = settings.LOCAL_FS_MOUNT.strip().rstrip("/")
    if not p or not host or not mount:
        return p
    norm = str(p).replace("\\", "/")
    if norm == mount or norm.startswith(mount + "/"):
        rest = norm[len(mount):].lstrip("/")
        h = host.replace("\\", "/").rstrip("/")
        return f"{h}/{rest}" if rest else h
    return p


def list_files(storage_cfg: dict, directory: str, exts=None) -> list:
    """Return [(name, full_path)] for ingestible document files under `directory` (recursive).
    Local → os.walk; cloud (s3://, abfs://, gs://) → fsspec glob.

    An EMPTY directory means "nowhere", not "everywhere": without this guard the local branch
    (`os.path.isdir("")` is False) fell through to an fsspec glob of `/**`, walking the whole
    filesystem and hanging the request. Callers must pass a real folder."""
    exts = tuple((e if e.startswith(".") else "." + e).lower() for e in (exts or _DOC_EXTS))
    base = (directory or "").rstrip("/")
    if not base:
        return []
    out = []
    if os.path.isdir(base):                       # local
        for root, _dirs, files in os.walk(base):
            for f in files:
                if f.lower().endswith(exts):
                    out.append((f, os.path.join(root, f).replace("\\", "/")))
        return sorted(out)
    import fsspec
    fs, _ = fsspec.core.url_to_fs(base, **_fsspec_options(storage_cfg))
    proto = (base.split("://", 1)[0] + "://") if "://" in base else ""
    for p in fs.glob(base + "/**"):
        sp = str(p)
        if sp.lower().endswith(exts):
            name = sp.rstrip("/").split("/")[-1]
            out.append((name, sp if sp.startswith(proto) else proto + sp))
    return sorted(out)


def list_dirs(storage_cfg: dict, path: str = "") -> list:
    """List immediate sub-directories of `path` — powers the ingestion folder picker (browse a data
    lake or on-prem tree and select a folder). Local → os.scandir; cloud (s3/gcs/adls) → fsspec ls."""
    base = (path or "").rstrip("/")
    if not base or os.path.isdir(base):                 # local filesystem
        root = base or "/"
        out = []
        try:
            for e in os.scandir(root):
                if e.is_dir():
                    out.append(e.path.replace("\\", "/"))
        except Exception:
            return []
        return sorted(out)
    import fsspec                                        # cloud bucket / prefix
    fs, _ = fsspec.core.url_to_fs(base, **_fsspec_options(storage_cfg))
    proto = (base.split("://", 1)[0] + "://") if "://" in base else ""
    out = []
    try:
        for info in fs.ls(base, detail=True):
            if (info.get("type") or "") == "directory":
                name = str(info.get("name")).rstrip("/")
                out.append(name if name.startswith(proto) else proto + name)
    except Exception:
        return []
    return sorted(out)


def stat_files(storage_cfg: dict, directory: str, exts=None) -> list:
    """Return [(name, full_path, sig)] for ingestible files under `directory` — the CHEAP change
    signal for real-time / scheduled sync. `sig` is a small opaque string that changes iff the file
    content likely changed, WITHOUT reading the bytes:
      • local  → 'mtime_ns:size' from os.stat (a stat() per file, not a content scan)
      • cloud  → 'etag:size' (or 'mtime:size') from the fsspec listing's info dict — the same idea a
        lakehouse checkpoint uses (object version/etag), so we never re-download unchanged objects.
    Callers diff this against a stored manifest (ingest.sync.compute_plan) and only touch the delta."""
    exts = tuple((e if e.startswith(".") else "." + e).lower() for e in (exts or _DOC_EXTS))
    base = (directory or "").rstrip("/")
    out = []
    if not base:
        return out                                # no folder means no files, never "walk from here"
    if os.path.isdir(base):                       # local filesystem — os.stat, no read
        for root, _dirs, files in os.walk(base):
            for f in files:
                if not f.lower().endswith(exts):
                    continue
                full = os.path.join(root, f)
                try:
                    st = os.stat(full)
                    sig = f"{getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9))}:{st.st_size}"
                except OSError:
                    sig = ""
                out.append((f, full.replace("\\", "/"), sig))
        return sorted(out)
    if "://" not in base:
        # A local folder that is not there (a typo, an unmounted drive) is an error, not an empty
        # folder: read as empty, a sync reported "completed" every minute, and a folder that had held
        # files would have made every one of them look deleted.
        raise FileNotFoundError(f"watched folder {base} does not exist or is not mounted")
    import fsspec                                  # cloud — use listing metadata (etag/version/size)
    fs, _ = fsspec.core.url_to_fs(base, **_fsspec_options(storage_cfg))
    proto = (base.split("://", 1)[0] + "://") if "://" in base else ""
    for info in fs.find(base, detail=True).values():
        sp = str(info.get("name") or "")
        if not sp.lower().endswith(exts):
            continue
        etag = info.get("ETag") or info.get("etag") or info.get("version_id") or ""
        if not etag:                              # fall back to mtime when the store exposes no etag
            mt = info.get("mtime") or info.get("last_modified") or info.get("LastModified") or ""
            etag = str(mt)
        sig = f"{etag}:{info.get('size') or info.get('Size') or ''}"
        name = sp.rstrip("/").split("/")[-1]
        full = sp if sp.startswith(proto) else proto + sp
        out.append((name, full, sig))
    return sorted(out)


def read_file(storage_cfg: dict, path: str) -> bytes:
    """Read a file's bytes from local or cloud (fsspec)."""
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read()
    import fsspec
    with fsspec.open(path, "rb", **_fsspec_options(storage_cfg)) as fh:
        return fh.read()


def _q(s) -> str:
    return str(s or "").replace("'", "''")


def _scan(fmt: str, path: str) -> str:
    p = path.rstrip("/")
    if fmt == "delta":
        return f"delta_scan('{_q(p)}')"
    if fmt == "iceberg":
        return f"iceberg_scan('{_q(p)}')"
    return f"read_parquet('{_q(p)}/**/*.parquet', union_by_name=true)"   # hudi base files (best-effort)


def _secret_sql(provider: str, opts: dict, secret_val: str) -> str:
    """Build the DuckDB `CREATE SECRET` body for the lake's credentials. `opts.auth` selects the
    method (verified against DuckDB 1.5.3). All identifiers come from the storage connector.
      • S3:   access_key (KEY_ID/SECRET[/REGION]) | credential_chain (CHAIN[/ASSUME_ROLE_ARN]) — IAM/STS
      • GCS:  hmac (KEY_ID/SECRET) — DuckDB has no native service-account-JSON/WIF; use an HMAC key
      • ADLS: service_principal (+secret or +certificate, ACCOUNT_NAME) | sas (CONNECTION_STRING) |
              managed_identity (credential_chain, ACCOUNT_NAME)
    """
    auth = (opts.get("auth") or "").lower()
    p = []
    if provider == "s3":
        p.append("TYPE S3")
        if auth == "credential_chain":
            p.append("PROVIDER credential_chain")
            if opts.get("chain"):
                p.append(f"CHAIN '{_q(opts['chain'])}'")
            if opts.get("assume_role_arn"):
                p.append(f"ASSUME_ROLE_ARN '{_q(opts['assume_role_arn'])}'")
        else:
            p += [f"KEY_ID '{_q(opts.get('access_key_id'))}'", f"SECRET '{_q(secret_val)}'"]
        p.append(f"REGION '{_q(opts.get('region') or 'us-east-1')}'")
    elif provider == "gcs":
        p += ["TYPE GCS", f"KEY_ID '{_q(opts.get('access_key_id'))}'", f"SECRET '{_q(secret_val)}'"]
    elif provider == "adls":
        p.append("TYPE AZURE")
        if auth in ("service_principal", "sp"):
            p += ["PROVIDER service_principal",
                  f"TENANT_ID '{_q(opts.get('tenant_id'))}'", f"CLIENT_ID '{_q(opts.get('client_id'))}'"]
            if opts.get("certificate_path"):
                p.append(f"CLIENT_CERTIFICATE_PATH '{_q(opts['certificate_path'])}'")
            else:
                p.append(f"CLIENT_SECRET '{_q(secret_val)}'")
            if opts.get("account_name"):
                p.append(f"ACCOUNT_NAME '{_q(opts['account_name'])}'")
        elif auth in ("managed_identity", "credential_chain"):
            p.append("PROVIDER credential_chain")
            if opts.get("account_name"):
                p.append(f"ACCOUNT_NAME '{_q(opts['account_name'])}'")
        else:                                   # SAS / connection string
            p.append(f"CONNECTION_STRING '{_q(secret_val)}'")
    return f"CREATE OR REPLACE SECRET lake ({', '.join(p)})"


def _connect(storage_cfg: dict, fmt: str):
    """A DuckDB connection with the format extension loaded + the lake's credentials registered."""
    from opencortyx.stores.data import DataStoreError, connect
    provider = (storage_cfg.get("kind") or storage_cfg.get("provider") or "local").lower()
    exts = []
    if provider != "local":
        exts.append("httpfs")
    if provider == "adls":
        exts.append("azure")
    if provider in ("s3", "gcs", "r2"):
        exts.append("aws")                       # needed for credential_chain / STS
    if fmt in ("delta", "iceberg"):
        exts.append(fmt)
    # The image carries these extensions (stores/data/duck.py), so nothing is downloaded here.
    try:
        con = connect(extensions=exts, purpose="ingest")
    except DataStoreError as e:
        raise RuntimeError(str(e)) from e

    if provider == "local":
        return con
    sec = storage_cfg.get("secret")
    secret_val = resolve_secret(sec) if sec is not None else ""
    opts = storage_cfg.get("options") or {}
    try:
        con.execute(_secret_sql(provider, opts, secret_val))
    except Exception as e:
        raise RuntimeError(f"could not register {provider} credentials: {e}")
    return con


def _list_tables(con, fmt: str, directory: str):
    """Return [(name, full_path)] for each table directory under `directory`."""
    marker = _MARKER.get(fmt, "_delta_log")
    base = directory.rstrip("/")
    # local filesystem (fast, exact)
    if os.path.isdir(base):
        out = []
        for d in sorted(os.listdir(base)):
            p = os.path.join(base, d)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, marker)):
                out.append((d, p.replace("\\", "/")))
        return out
    # cloud / remote → DuckDB glob, keep dirs that contain the format marker
    try:
        rows = con.execute(
            f"SELECT DISTINCT regexp_replace(file, '/{marker}.*$', '') AS t "
            f"FROM glob('{_q(base)}/*/{marker}/**')").fetchall()
    except Exception:
        rows = []
    out = []
    for (t,) in rows:
        nm = (t or "").rstrip("/").split("/")[-1]
        if nm:
            out.append((nm, t))
    return out


def browse(storage_cfg: dict, fmt: str, directory: str) -> list:
    """DataHub-style browse: one 'schema' (the directory) → its tables + column names."""
    con = _connect(storage_cfg, fmt)
    try:
        tables = _list_tables(con, fmt, directory)
        entries = []
        for name, path in tables:
            try:
                cols = [c[0] for c in con.execute(f"DESCRIBE SELECT * FROM {_scan(fmt, path)}").fetchall()]
            except Exception:
                cols = []
            entries.append({"name": name, "type": "table", "columns": cols})
        label = directory.rstrip("/").split("/")[-1] or fmt
        return [{"schema": f"{fmt}:{label}", "tables": entries}]
    finally:
        con.close()


def introspect(storage_cfg: dict, fmt: str, directory: str, only_tables=None, sample_n: int = 5) -> list:
    """Per-table metadata in the SAME shape as the SQL introspector, so the structured ingest path
    can write Table/Attribute nodes from lake tables unchanged."""
    con = _connect(storage_cfg, fmt)
    sel = set(only_tables) if only_tables else None
    out = []
    try:
        for name, path in _list_tables(con, fmt, directory):
            if sel and name not in sel:
                continue
            scan = _scan(fmt, path)
            try:
                desc = con.execute(f"DESCRIBE SELECT * FROM {scan}").fetchall()   # (name,type,null,key,default,extra)
            except Exception as e:
                out.append({"table": name, "schema": fmt, "columns": [], "row_count": 0,
                            "error": str(e)})
                continue
            try:
                rc = con.execute(f"SELECT count(*) FROM {scan}").fetchone()[0]
            except Exception:
                rc = None
            cols = []
            for c in desc:
                cols.append({"name": c[0], "type": str(c[1]), "primary_key": False,
                             "foreign_key": False, "nullable": True,
                             "references_table": None, "references_column": None})
            out.append({"table": name, "schema": fmt, "row_count": rc, "is_view": False,
                        "columns": cols, "path": path})
        return out
    finally:
        con.close()
