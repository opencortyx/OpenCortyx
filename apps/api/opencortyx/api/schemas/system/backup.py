"""Request and response models: workspace export and import."""
from pydantic import BaseModel


class ImportSpec(BaseModel):
    data: dict
    mode: str = "merge"   # merge = upsert (keep existing) · replace = wipe this org's graph first
