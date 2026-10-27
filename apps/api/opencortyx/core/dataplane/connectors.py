"""A saved SQL connector as DuckDB attaches it: the connection string, resolved for one connection.

The connector keeps the SQLAlchemy-style URL it always has (`postgresql+psycopg2://u:p@h/db`,
`mysql+pymysql://...`, `sqlite:///path`); DuckDB's extensions take a libpq URI, a MySQL key=value
string and a file path. Nothing here is stored or logged - the result goes straight into ATTACH.
"""
from __future__ import annotations

import uuid
from typing import Optional
from urllib.parse import unquote, urlsplit


def kind_of(url: str) -> Optional[str]:
    """postgres / mysql / sqlite for a URL DuckDB can attach natively, else None (an adapter's job)."""
    scheme = (url or "").split("://", 1)[0].lower()
    base = scheme.split("+", 1)[0]
    if base in ("postgresql", "postgres"):
        return "postgres"
    if base in ("mysql", "mariadb"):
        return "mysql"
    if base == "sqlite":
        return "sqlite"
    return None


def attach_string(url: str, kind: str) -> str:
    """The ATTACH argument for `url`."""
    if kind == "sqlite":
        # sqlite:///relative or sqlite:////absolute
        return url.split(":///", 1)[1] if ":///" in url else url
    parts = urlsplit(url)
    if kind == "postgres":
        # libpq takes a URI; only the driver suffix (+psycopg2) has to go.
        return "postgresql://" + url.split("://", 1)[1]
    if kind == "mysql":
        kv = {"host": parts.hostname or "localhost", "port": str(parts.port or 3306),
              "user": unquote(parts.username or ""), "passwd": unquote(parts.password or ""),
              "db": (parts.path or "/").lstrip("/")}
        return " ".join(f"{k}={v}" for k, v in kv.items() if v)
    raise ValueError(f"{kind!r} is not attached natively")


async def attach_dsn(workspace_id, connector_id, kind: str) -> str:
    """The connector's ATTACH string, from its (decrypted) connection string."""
    from opencortyx.core.db import V2Session
    from opencortyx.core.security.secrets import resolve_connector_dsn
    from opencortyx.domain import Connector
    async with V2Session() as db:
        row = await db.get(Connector, uuid.UUID(str(connector_id)))
    if row is None or str(row.workspace_id) != str(workspace_id):
        raise LookupError("connector not found in this workspace")
    return attach_string(resolve_connector_dsn(row.config or {}), kind)
