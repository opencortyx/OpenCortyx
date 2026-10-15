"""Request and response models: saved, re-runnable ingestions and their runs."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _schedule(v):
    """A schedule must be a cron the tick can read. An unreadable one was stored and then never fired,
    silently (sync.cron_due treats it as 'never due')."""
    if v is None or not str(v).strip():
        return None
    from opencortyx.ingest.sources.sync import cron_valid
    if not cron_valid(v):
        raise ValueError(f"schedule must be a 5-field cron expression (e.g. '*/30 * * * *'), got {v!r}")
    return str(v).strip()


class IngestionSpec(BaseModel):
    name: str
    connector_id: Optional[str] = None         # optional when `mappings` is supplied (N:N)
    pipeline_id: Optional[str] = None
    selections: list = Field(default_factory=list)   # [{schema, tables:[], views:[]}] — multi-schema
    enrich: bool = True
    schedule: Optional[str] = None             # cron (5-field) — used when mode="scheduled"
    mode: Optional[str] = None                 # on_demand (default) | scheduled | realtime
    kind: str = "unstructured"                 # structured | unstructured (files) | semistructured (JSON)
    directories: Optional[list] = None         # file kinds: on-prem/lake folder paths (recursive)
    formats: Optional[list] = None             # file kinds: file extensions to include; empty → all
    directory_nodes: Optional[bool] = None
    mappings: Optional[list] = None            # flexible N:N — [{connector_id, pipeline_id}]; overrides the single pair
    anchors: Optional[list] = None

    _sched = field_validator("schedule")(classmethod(lambda cls, v: _schedule(v)))


class IngestionPatch(BaseModel):
    name: Optional[str] = None
    pipeline_id: Optional[str] = None
    selections: Optional[list] = None
    enrich: Optional[bool] = None
    schedule: Optional[str] = None
    mode: Optional[str] = None
    paused: Optional[bool] = None
    directories: Optional[list] = None
    formats: Optional[list] = None
    directory_nodes: Optional[bool] = None
    mappings: Optional[list] = None

    _sched = field_validator("schedule")(classmethod(lambda cls, v: _schedule(v)))


class AnchorPreview(BaseModel):
    anchors: list = Field(default_factory=list)
    paths: list = Field(default_factory=list)      # relative paths as the connector reports them
