"""Uploaded files, stored once on a volume the API and the workers share; jobs carry a reference.

An upload used to travel to its worker INSIDE the queue message, base64-encoded: a 50 MB PDF was a
67 MB Redis entry. Redis keeps every queued message in memory, so a few hundred files dropped at once
filled its container and the kernel killed it, taking the whole queue with it. This is the claim-check
pattern: the API writes the bytes here, the message carries `ref` (`upload:sha256:<hex>`), and the
worker reads the file back. A queued job is now a few hundred bytes however large its file.

Content-addressed: the same file uploaded twice is stored once, and a reference can never point at
different bytes than it was made from. Writes go to a temporary name and are renamed into place, so a
worker never reads half a file. Files are kept UPLOAD_RETENTION_HOURS after their last upload and
removed by `sweep` (the maintenance worker's schedule), except while a queued or running ingest still
refers to them.
"""
import base64
import hashlib
import os
import time
from pathlib import Path

from opencortyx.core.config import settings

PREFIX = "upload:sha256:"


def root() -> Path:
    return Path(settings.UPLOAD_STORE)


def _path(digest: str) -> Path:
    return root() / digest[:2] / digest


def put(content: bytes) -> str:
    """Store `content`; returns its reference. Storing what is already there only renews it."""
    digest = hashlib.sha256(content).hexdigest()
    path = _path(digest)
    if path.exists():
        os.utime(path)                     # an upload renews it: the retention clock restarts
        return PREFIX + digest
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{digest}.{os.getpid()}.{time.monotonic_ns()}.part")
    with open(tmp, "wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return PREFIX + digest


def load(ref: str) -> bytes:
    """The bytes a job was given. Accepts the base64 payload older versions queued, so jobs queued
    before an update still run after it."""
    if not ref.startswith(PREFIX):
        return base64.b64decode(ref)
    digest = ref[len(PREFIX):]
    try:
        return _path(digest).read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError(
            "The uploaded file is no longer stored on the server (it was removed after "
            f"{settings.UPLOAD_RETENTION_HOURS} hours, or the uploads volume was reset). "
            "Upload it again.") from None


def digests_in(value) -> set[str]:
    """Every reference inside a job's arguments (any nesting), as digests."""
    out: set[str] = set()
    if isinstance(value, str):
        if value.startswith(PREFIX):
            out.add(value[len(PREFIX):])
    elif isinstance(value, dict):
        for v in value.values():
            out |= digests_in(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out |= digests_in(v)
    return out


def sweep(keep: set[str], older_than_hours: float | None = None) -> dict:
    """Remove stored files not renewed for `older_than_hours` and not in `keep`, and stale partial
    writes. Returns counts."""
    hours = settings.UPLOAD_RETENTION_HOURS if older_than_hours is None else older_than_hours
    cutoff = time.time() - hours * 3600
    removed = kept = freed = 0
    base = root()
    if not base.exists():
        return {"removed": 0, "kept": 0, "freed_bytes": 0}
    for shard in base.iterdir():
        if not shard.is_dir():
            continue
        for path in shard.iterdir():
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            partial = path.name.startswith(".")
            if stat.st_mtime >= (time.time() - 3600 if partial else cutoff) or path.name in keep:
                kept += 1
                continue
            try:
                path.unlink()
                removed += 1
                freed += stat.st_size
            except FileNotFoundError:
                pass
    return {"removed": removed, "kept": kept, "freed_bytes": freed}
