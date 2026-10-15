"""Request and response models: collections."""
from typing import Optional

from pydantic import BaseModel


class CollectionSpec(BaseModel):
    name: str
    description: Optional[str] = ""
