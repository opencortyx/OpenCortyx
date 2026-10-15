"""Request and response models: how a node type is resolved."""
from typing import List, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _clean(values) -> list:
    return list(dict.fromkeys(str(v).strip() for v in (values or []) if str(v or "").strip()))


class ResolutionSpecIn(BaseModel):
    identify_by: List[str] = []     # attributes whose values together identify a node of the type
    compare: List[str] = []         # attributes compared on a match; empty means all of them
    ignore: List[str] = []          # attributes never compared
    enabled: bool = True
    relations: List[Literal["SAME", "SUPPORTS", "EXTENDS", "CONTRADICTS"]] = ["SAME", "SUPPORTS", "EXTENDS", "CONTRADICTS"]
    scorer: Literal["exact", "similar"] = "exact"
    auto: float = Field(0.95, ge=0.0, le=1.0)      # applied at or above this score
    review: float = Field(0.80, ge=0.0, le=1.0)    # sent for review at or above this score

    @model_validator(mode="after")
    def _gate(self):
        if self.review > self.auto:
            raise ValueError("the review threshold cannot be above the automatic one")
        return self

    @field_validator("identify_by", "compare", "ignore")
    @classmethod
    def _names(cls, v):
        names = _clean(v)
        if any(len(n) > 200 for n in names):
            raise ValueError("attribute names are at most 200 characters")
        return names


class ResolutionDecideIn(BaseModel):
    choice: Literal["keep", "use_new"]      # keep the existing value, or take the newer record's
