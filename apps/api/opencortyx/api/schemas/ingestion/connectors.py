"""Request and response models: saved data-source connections."""
from typing import Optional

from pydantic import BaseModel, field_validator


def connector_kinds() -> set:
    """Every kind a connector can be: the SQL dialects the DSN builder knows, file databases, and the
    data-lake storages and table formats the lake readers handle."""
    from opencortyx.core.security.secrets import FILE_DIALECTS, SQL_DIALECTS
    from opencortyx.ingest.sources import datalake
    return (set(SQL_DIALECTS) | set(FILE_DIALECTS) | set(datalake.LAKE_STORAGE)
            | set(datalake.DATALAKE_FORMATS) | set(datalake.DOC_FORMATS))


class SecretRef(BaseModel):
    provider: str = "inline"               # inline | env | azure_keyvault
    value: Optional[str] = None            # inline password (encrypted at rest)
    var: Optional[str] = None              # env var name
    vault_url: Optional[str] = None        # Azure Key Vault URL (optional → default)
    secret_name: Optional[str] = None      # Azure Key Vault secret name


class ConnectorSpec(BaseModel):
    name: str
    kind: str = "postgres"                 # postgres|mysql|sqlite|… | s3|gcs|adls (lake) | delta|iceberg|hudi (format)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        k = (v or "").strip().lower()
        kinds = connector_kinds()
        if k not in kinds:
            raise ValueError(f"unknown connector kind {v!r}; use one of {', '.join(sorted(kinds))}")
        return k
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    schema_name: Optional[str] = None
    secret: Optional[SecretRef] = None
    connection_string: Optional[str] = None   # raw/recipe DSN alternative (may contain ${ENV}/${SECRET})
    # ── data lake / table format ──
    options: Optional[dict] = None            # lake storage options (e.g. {access_key_id, region})
    lake_connector_id: Optional[str] = None   # table-format → the S3/GCS/ADLS storage connector
    directory: Optional[str] = None           # table-format → the path that holds many tables
    query_engine_id: Optional[str] = None     # table-format → the query-engine connector (DuckDB)


class RecipeSpec(BaseModel):
    name: Optional[str] = None
    recipe: str                            # DataHub-style YAML
