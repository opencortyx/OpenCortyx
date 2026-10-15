"""Helpers shared by several resources: request scoping, auditing, budgets and upload limits."""

from fastapi import HTTPException

from opencortyx.domain.control import AuditLog
from opencortyx.core.db import V2Session
from opencortyx.core.config import settings

ROLES = ("admin", "editor", "viewer")
_MAX_UPLOAD_BYTES = settings.MAX_UPLOAD_BYTES   # 50 MB/file default


def guard_upload_size(content: bytes, filename: str | None) -> None:
    """Reject empty or oversized uploads with a clear 400/413 (vs a silent worker failure later)."""
    if not content:
        raise HTTPException(status_code=400, detail=f"'{filename or 'file'}' is empty — nothing to ingest.")
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"'{filename or 'file'}' is {len(content) // (1024 * 1024)} MB, over the "
                   f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit. Split it or raise MAX_UPLOAD_BYTES.")


def guard_readable(content: bytes, filename: str | None, route: str | None = None) -> dict:
    """The routing decision for an upload, or a 415 naming what the file is when no reader here can
    read it (an image, an executable, an archive). One guard for every entry point — the preview on
    Add data offered "Use this reading" for a PNG the upload would then refuse."""
    from opencortyx.ingest.formats import catalog as formats
    from opencortyx.ingest.detection import routing
    decision = routing.describe(filename or "upload", content, override=route)
    if decision["route"] not in (formats.DOCUMENT, formats.SEMISTRUCTURED, formats.STRUCTURED):
        kind = decision.get("format") or "this kind of"
        article = "an" if kind[:1].upper() in "AEIOU" else "a"
        raise HTTPException(status_code=415, detail=(
            f"'{filename or 'file'}' is {article} {kind} file, which OpenCortyx cannot read. Upload a "
            f"document, a table (CSV, Excel, Parquet …) or a JSON/XML/YAML file."))
    return decision


async def audit(workspace_id, actor, action, *, node_id=None, meta=None):
    """Record a curation/admin action (who · what · when) in the audit log. Best-effort — auditing
    must never break the write it records."""
    try:
        async with V2Session() as db:
            db.add(AuditLog(workspace_id=workspace_id, actor=(str(actor) if actor else None),
                            action=action, node_id=node_id, meta=meta or {}))
            await db.commit()
    except Exception:
        pass
