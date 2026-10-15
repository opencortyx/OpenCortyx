"""Request and response models: node and relationship types."""
from typing import List, Optional

from pydantic import BaseModel, field_validator

from opencortyx.api.schemas.graph.nodes import require_text

TYPE_KINDS = ("node", "edge", "entity")
NAME_MAX = 80                      # type_registry.name is String(80): longer was a database error (500)


class TypeSpec(BaseModel):
    name: str
    kind: str                      # 'node' | 'edge' | 'entity'
    description: str
    allowed_source_types: Optional[List[str]] = None
    allowed_target_types: Optional[List[str]] = None
    fields: Optional[List[dict]] = None   # data points / attributes: [{name,type,description,required}]
    domain: Optional[str] = None          # template grouping (Person/Org domain) — preserved in schema

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = require_text(v, "name")
        if len(v) > NAME_MAX:
            raise ValueError(f"name may be at most {NAME_MAX} characters, got {len(v)}")
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in TYPE_KINDS:
            raise ValueError(f"kind must be one of {', '.join(TYPE_KINDS)}, got {v!r}")
        return v


class TypeStatusSpec(BaseModel):
    kind: str
    status: str                    # 'active' | 'candidate' | 'disabled'
