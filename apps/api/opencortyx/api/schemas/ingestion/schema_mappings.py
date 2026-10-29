"""Request and response models: schema mappings and their conformance."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator

#: Kept in step with api.services.schema_mappings.MAPPING_PARADIGMS (asserted by a test).
MAPPING_PARADIGMS = ("structured", "semistructured", "unstructured")
#: `auto`: written by the Detector and in use; `draft`: proposed, waiting for a person.
MAPPING_STATUSES = ("active", "auto", "draft", "paused", "disabled")


def _paradigm(v):
    if v not in MAPPING_PARADIGMS:
        raise ValueError(f"paradigm must be one of {', '.join(MAPPING_PARADIGMS)}, got {v!r}")
    return v


def _status(v):
    if v not in MAPPING_STATUSES:
        raise ValueError(f"status must be one of {', '.join(MAPPING_STATUSES)}, got {v!r}")
    return v


class SchemaMappingSpec(BaseModel):
    name: str
    paradigm: str = "semistructured"

    _p = field_validator("paradigm")(classmethod(lambda cls, v: _paradigm(v)))
    source_type: str = ""
    payload: dict = Field(default_factory=dict)
    summary: dict = Field(default_factory=dict)
    status: str = "active"

    _s = field_validator("status")(classmethod(lambda cls, v: _status(v)))


class SchemaMappingPatch(BaseModel):
    name: Optional[str] = None
    paradigm: Optional[str] = None

    _p = field_validator("paradigm")(classmethod(lambda cls, v: None if v is None else _paradigm(v)))
    source_type: Optional[str] = None
    payload: Optional[dict] = None
    summary: Optional[dict] = None
    status: Optional[str] = None

    _s = field_validator("status")(classmethod(lambda cls, v: None if v is None else _status(v)))
