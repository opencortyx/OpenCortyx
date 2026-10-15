"""Request and response models: extraction templates and the shapes ingestion creates."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


def check_mechanism(mech: dict) -> None:
    """Refuse a mechanism the engine could never run, at save time and with the reason — rather than
    storing it and having every ingest skip it silently (an unknown `ref`, an LLM extractor with no
    prompt) or fail per chunk (Python that does not compile, a regex that does not)."""
    import re
    from opencortyx.ingest.extraction.templates import _LLM_REFS, _PRODUCIBLE
    mech = mech or {}
    ref = str(mech.get("ref") or "")
    if ref and ref not in _PRODUCIBLE:
        raise ValueError(f"unknown mechanism {ref!r}; use one of {', '.join(sorted(_PRODUCIBLE))}")
    if (ref in _LLM_REFS or not ref) and not str(mech.get("prompt") or "").strip() \
            and not mech.get("code"):
        raise ValueError("an AI-prompt extractor needs a prompt")
    if ref == "unstructured.python":
        try:
            compile(str(mech.get("code") or ""), "<extractor>", "exec")
        except SyntaxError as e:
            raise ValueError(f"the Python code does not compile: {e.msg} (line {e.lineno})")
    if ref == "unstructured.regex":
        try:
            re.compile(str(mech.get("pattern") or ""))
        except re.error as e:
            raise ValueError(f"the pattern is not a valid regular expression: {e}")


class TemplateSpec(BaseModel):
    name: str
    source: Optional[dict] = None
    mechanism: dict = Field(default_factory=dict)
    emit: dict = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("name is required")
        return v

    @model_validator(mode="after")
    def _mechanism(self):
        # A built-in's edit sends only what changed (its prompt, its model); its mechanism is the
        # catalog's and is not re-validated here.
        if not self.name.startswith("builtin:"):
            check_mechanism(self.mechanism)
        return self


class TemplatePreviewSpec(BaseModel):
    name: Optional[str] = None
    source: Optional[dict] = None
    mechanism: dict = Field(default_factory=dict)
    emit: dict = Field(default_factory=dict)
    sample_text: str = ""

    @model_validator(mode="after")
    def _mechanism(self):
        if not str(self.name or "").startswith("builtin:"):
            check_mechanism(self.mechanism)
        return self


class TemplateStatusPatch(BaseModel):
    status: str                                   # active | disabled
