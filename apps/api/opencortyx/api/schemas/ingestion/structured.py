"""Request and response models: structured sources: SQL databases and table files."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from opencortyx.api.schemas.graph.nodes import optional_uuid, require_uuid


class SqlIngestSpec(BaseModel):
    connection_string: Optional[str] = None
    connector_id: Optional[str] = None
    schema_name: Optional[str] = None
    enrich: bool = True
    table_mappings: Optional[dict] = None
    pipeline: Optional[list] = None        # Structured Builder stages (target tables / stages / mappings)

    _ids = field_validator("connector_id")(classmethod(lambda cls, v, info: optional_uuid(v, info.field_name)))


class ConnectorFilesIngestSpec(BaseModel):
    connector_id: str
    paths: list = Field(default_factory=list)      # explicit tabular file paths chosen in the wizard
    directories: Optional[list] = None             # server folder paths — each walked recursively for tabular files
    pipeline_id: Optional[str] = None
    collection_id: Optional[str] = None
    anchors: Optional[list] = None                 # attach/type each table by its file path

    _id = field_validator("connector_id")(classmethod(lambda cls, v, info: require_uuid(v, info.field_name)))
    _ids = field_validator("pipeline_id", "collection_id")(
        classmethod(lambda cls, v, info: optional_uuid(v, info.field_name)))


class DomainSpec(BaseModel):
    domain: str
    n_tables: int = 6
