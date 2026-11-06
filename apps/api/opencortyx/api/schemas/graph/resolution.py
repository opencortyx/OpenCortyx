"""Request and response models: how a node type is resolved."""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


def _clean(values) -> list:
    return list(dict.fromkeys(str(v).strip() for v in (values or []) if str(v or "").strip()))


_NAME_LISTS = ("identify_by", "compare", "ignore", "distinguish")


def _attribute_names(values) -> list:
    names = _clean(values)
    if any(len(n) > 200 for n in names):
        raise ValueError("attribute names are at most 200 characters")
    return names


class ResolutionSpecIn(BaseModel):
    identify_by: List[str] = []     # attributes whose values together identify a node of the type
    compare: List[str] = []         # attributes compared on a match; empty means all of them
    ignore: List[str] = []          # attributes never compared
    enabled: bool = True
    relations: List[Literal["SAME", "SUPPORTS", "EXTENDS", "CONTRADICTS"]] = ["SAME", "SUPPORTS", "EXTENDS", "CONTRADICTS"]
    scorer: Literal["exact", "similar"] = "exact"
    auto: float = Field(0.95, ge=0.0, le=1.0)      # applied at or above this score
    review: float = Field(0.80, ge=0.0, le=1.0)    # sent for review at or above this score
    distinguish: List[str] = []     # "must agree": differing values mean two different things
    corroborate: Literal["default", "never", "similar", "always"] = "default"
    conflict_review: Optional[bool] = None         # None: the workspace setting
    name_scope: Literal["workspace", "asset"] = "workspace"

    @model_validator(mode="after")
    def _gate(self):
        if self.review > self.auto:
            raise ValueError("the review threshold cannot be above the automatic one")
        return self

    @field_validator(*_NAME_LISTS)
    @classmethod
    def _names(cls, v):
        return _attribute_names(v)


class ResolutionDecideIn(BaseModel):
    choice: Literal["keep", "use_new"]      # keep the existing value, or take the newer record's


class LabelIn(BaseModel):
    """A person's answer for two nodes: the same thing, or different. `apply` also acts on it: a
    "different" for two joined nodes splits them, a "same" joins them."""
    a_id: str
    b_id: str
    same: bool
    note: Optional[str] = Field(None, max_length=500)
    apply: bool = True


class LabelSnapshotIn(BaseModel):
    label: Optional[str] = Field(None, max_length=500)
    properties: Dict[str, Any] = {}
    asset_id: Optional[str] = None


class LabelImportItem(BaseModel):
    node_type: str = Field(..., min_length=1, max_length=80)
    a: LabelSnapshotIn
    b: LabelSnapshotIn
    same: bool
    note: Optional[str] = Field(None, max_length=500)


class LabelImportIn(BaseModel):
    items: List[LabelImportItem] = Field(..., max_length=50_000)
    named: bool = False             # the records are named things (an extractor's entities)


class EvaluateIn(BaseModel):
    node_type: str = Field(..., min_length=1, max_length=80)
    overrides: Optional[Dict[str, Any]] = None   # what-if settings, the names ResolutionSpecIn uses

    @field_validator("overrides")
    @classmethod
    def _known_settings(cls, v):
        """Each what-if setting is checked as the saved rule would be, so a typo or an out-of-range
        value is refused instead of being dropped and scored as if it were the current rule. The
        review-above-automatic check needs the saved rule too, so it runs after the merge."""
        if not v:
            return v
        from typing import Annotated
        from pydantic import TypeAdapter
        fields = ResolutionSpecIn.model_fields
        unknown = sorted(set(v) - set(fields))
        if unknown:
            raise ValueError(f"unknown settings: {', '.join(unknown)}")
        out = {}
        for key, value in v.items():
            f = fields[key]
            value = TypeAdapter(Annotated[f.annotation, f]).validate_python(value)
            out[key] = _attribute_names(value) if key in _NAME_LISTS else value
        return out


class SplitIn(BaseModel):
    member_id: str


class MergeIn(BaseModel):
    a_id: str
    b_id: str
    note: Optional[str] = Field(None, max_length=500)
