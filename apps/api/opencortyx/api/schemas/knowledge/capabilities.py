"""Request and response models: capability modules."""
from pydantic import BaseModel, Field


class CapabilityUpdate(BaseModel):
    values: dict = Field(default_factory=dict)
