"""Request and response models: reusable ingestion pipelines."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator

#: The paradigms a pipeline can be for — after the aliases `normalize_kind` accepts.
PIPELINE_KINDS = ("document", "semistructured", "structured")


def _kind(v):
    from opencortyx.ingest.pipelines.defaults import normalize_kind
    k = normalize_kind(v)
    if k not in PIPELINE_KINDS:
        raise ValueError(f"kind must be one of {', '.join(PIPELINE_KINDS)}, got {v!r}")
    return k


def _stages(v):
    bad = [i for i, s in enumerate(v or []) if not isinstance(s, dict)]
    if bad:
        raise ValueError(f"every stage must be an object; stage {bad[0]} is {type(v[bad[0]]).__name__}")
    return v


class PipelineSpec(BaseModel):
    name: str
    description: str = ""
    kind: str = "document"               # document | semistructured | structured (aliases accepted)
    stages: list = Field(default_factory=list)

    _k = field_validator("kind")(classmethod(lambda cls, v: _kind(v)))
    _s = field_validator("stages")(classmethod(lambda cls, v: _stages(v)))


class PipelinePatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    kind: Optional[str] = None
    stages: Optional[list] = None

    _k = field_validator("kind")(classmethod(lambda cls, v: None if v is None else _kind(v)))
    _s = field_validator("stages")(classmethod(lambda cls, v: None if v is None else _stages(v)))
