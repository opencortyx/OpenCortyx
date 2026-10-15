"""Request and response models: drafted mappings, pipelines and extractors."""
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

#: The wire tokens of `authoring.models.DraftKind`, repeated here so a malformed `kind` is a 422
#: from FastAPI with the valid values in it, rather than an empty result the caller has to explain.
DraftKindToken = Literal["mapping", "pipeline", "extractor"]


class DraftSpec(BaseModel):
    """Draft from content already in hand — inline records, or a goal with no file at all.

    `content` is the raw text of a source (a JSON document, a CSV). It is an alternative to the
    multipart upload on the same endpoint, for a caller that already has the bytes: an agent over
    MCP, or the UI re-drafting after an edit without making the user pick the file again.
    """

    model_config = {"protected_namespaces": ()}   # `model_ref` is a real field, not model_*

    kind: DraftKindToken = "mapping"
    goal: str = ""
    content: Optional[str] = None
    filename: str = ""
    drafter: Optional[str] = None            # force one by registry name; None → every candidate
    use_model: bool = True                   # False → measured result only, no inference call
    model_ref: Optional[str] = None          # registry model name/id for the refinement call
    context: dict[str, Any] = Field(default_factory=dict)


class DraftPreviewSpec(BaseModel):
    """Re-run a draft — usually an edited one — against a sample, without saving it."""

    kind: DraftKindToken
    payload: dict[str, Any] = Field(default_factory=dict)
    sample_text: str = ""


class EvidenceView(BaseModel):
    claim: str
    basis: str
    path: str = ""


class DraftView(BaseModel):
    kind: str
    name: str
    payload: dict[str, Any]
    drafter: str
    confidence: str
    summary: str
    evidence: list[EvidenceView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    save_endpoint: str = ""
    model: str = ""


class DraftResponse(BaseModel):
    drafts: list[DraftView] = Field(default_factory=list)
    #: What was read to produce them — the route, the format and how much was sampled. Returned
    #: because "why did it think this was a CSV" is the first question a wrong draft raises.
    sample: dict[str, Any] = Field(default_factory=dict)
    #: Every drafter that could have run, so a UI can offer "try a different reading".
    available: list[dict[str, Any]] = Field(default_factory=list)
