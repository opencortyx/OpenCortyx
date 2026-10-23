"""Request and response models: file and JSON uploads, supported formats and ingestion integrity."""
from typing import Any, Optional

from pydantic import BaseModel, Field


class JsonStreamSpec(BaseModel):
    path: str                             # host path to a .json file (a top-level ARRAY of records)
    source_label: Optional[str] = None
    options: dict = Field(default_factory=dict)   # collection_id / root_type / max_*


class JsonInlineSpec(BaseModel):
    data: Any = None                      # the (already client-sliced) JSON — array or object
    source_label: str = "payload.json"
    options: dict = Field(default_factory=dict)
    batch_id: Optional[str] = None        # files sent together → ONE saved ingestion
