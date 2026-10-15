"""Request and response models: runtime performance and retrieval settings."""
from pydantic import BaseModel, Field


class ConfigUpdate(BaseModel):
    values: dict = Field(default_factory=dict)
