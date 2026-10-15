"""Request and response models: reading, authoring and searching nodes."""
import uuid
from typing import Optional

from pydantic import BaseModel, field_validator


def require_text(v, field: str) -> str:
    v = (v if isinstance(v, str) else str(v or "")).strip()
    if not v:
        raise ValueError(f"{field} is required")
    return v


def require_uuid(v, field: str) -> str:
    raw = (v if isinstance(v, str) else str(v or "")).strip()
    try:
        uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"{field} must be a node id (UUID), got {raw!r}")
    return raw


def optional_uuid(v, field: str):
    """An optional id: absent or blank is None, anything else must be a UUID (not a 500 further in)."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return require_uuid(v, field)


class NodeSpec(BaseModel):
    """A node authored by hand. Type and label are REQUIRED — the Curation form already refuses to
    submit without both, and the API accepting `{"type": "", "label": ""}` with a 200 meant any other
    client could write a nameless, typeless node that every picker then offered as a choice."""
    type: str
    label: str = ""
    properties: Optional[dict] = None
    collection_id: Optional[str] = None

    @field_validator("type")
    @classmethod
    def _type(cls, v):
        return require_text(v, "type")

    @field_validator("label")
    @classmethod
    def _label(cls, v):
        return require_text(v, "label")

    @field_validator("collection_id")
    @classmethod
    def _collection(cls, v):
        return None if v in (None, "") else require_uuid(v, "collection_id")


class NodePatch(BaseModel):
    """A partial edit: only what is sent changes. `type` is accepted and ignored — a node keeps its
    type — so existing clients that send the whole form keep working."""
    type: Optional[str] = None
    label: Optional[str] = None
    properties: Optional[dict] = None
    collection_id: Optional[str] = None

    @field_validator("label")
    @classmethod
    def _label(cls, v):
        return None if v is None else require_text(v, "label")

    @field_validator("collection_id")
    @classmethod
    def _collection(cls, v):
        return None if v in (None, "") else require_uuid(v, "collection_id")
