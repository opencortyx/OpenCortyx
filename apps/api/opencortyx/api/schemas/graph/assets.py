"""Request and response models: catalogued assets: profiles, overviews and deletion."""
from typing import Optional

from pydantic import BaseModel


class OverviewEdit(BaseModel):
    exec_summary: Optional[str] = None
    business_purpose: Optional[str] = None
    nl_description: Optional[str] = None


class TagsEdit(BaseModel):
    tags: list[str]
