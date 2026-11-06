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
        if not str(mech.get("pattern") or "").strip():
            raise ValueError("a pattern extractor needs a pattern")
        try:
            re.compile(str(mech.get("pattern") or ""))
        except re.error as e:
            raise ValueError(f"the pattern is not a valid regular expression: {e}")
    if ref == "unstructured.dictionary" and not [t for t in (mech.get("terms") or []) if str(t).strip()]:
        raise ValueError("a word list needs at least one word")
    if ref == "unstructured.ner":
        check_model_config(mech)


_TYPE = r"[A-Za-z][A-Za-z0-9_]*"
_FIELD = r"[A-Za-z][A-Za-z0-9_]*"


def check_model_config(mech: dict) -> None:
    """The model method's form config (see `_model_records`): every name usable as a type, a field or
    a relationship, none twice, and every link's ends a kind this extractor creates."""
    import re
    from opencortyx.ingest.extraction.templates import RESERVED_FIELDS, _fold
    kinds = mech.get("kinds")
    if kinds is None:
        return                                          # saved before the form: one kind the model names
    if not isinstance(kinds, list) or not kinds:
        raise ValueError("say what it creates: at least one kind of thing")
    fixed, by_model = set(), 0

    def fields_ok(rows, where):
        seen = set()
        for f in rows or []:
            key = str((f or {}).get("key") or "").strip()
            if not re.fullmatch(_FIELD, key):
                raise ValueError(f"{where}: a field name is letters, digits and _ and starts with a letter ({key or 'empty'})")
            if key.lower() in RESERVED_FIELDS:
                raise ValueError(f"{where}: {key!r} is used by every thing; name the field something else")
            if key.lower() in seen:
                raise ValueError(f"{where}: the field {key!r} is listed twice")
            seen.add(key.lower())

    for k in kinds:
        t = str((k or {}).get("type") or "").strip()
        if not t:
            by_model += 1
        elif not re.fullmatch(_TYPE, t):
            raise ValueError(f"a kind's name is letters, digits and _ and starts with a letter ({t})")
        elif _fold(t) in fixed:
            raise ValueError(f"the kind {t} is listed twice")
        else:
            fixed.add(_fold(t))
        if not (k or {}).get("fields_by_model"):
            fields_ok((k or {}).get("fields"), t or "the kind the model names")
    if by_model > 1:
        raise ValueError("one kind the model names is enough: it names each thing's type itself")
    fields_ok(mech.get("input_fields"), "on the input")

    def end_ok(end):
        if end in ("*", None, ""):
            return
        if end == "~":
            if not by_model:
                raise ValueError("a connection names a thing the model names, but no kind is the model's to name")
            return
        if _fold(str(end)) not in fixed:
            raise ValueError(f"a connection names {end}, which this extractor does not create")
    for group, needs_name in (("links", False), ("pairs", True)):
        for x in mech.get(group) or []:
            rel = str((x or {}).get("relation") or "").strip()
            if needs_name and not rel:
                raise ValueError("a connection made whenever both are found needs a relationship name")
            if rel and not re.fullmatch(_TYPE, rel):
                raise ValueError(f"a relationship name is letters, digits and _ and starts with a letter ({rel})")
            end_ok((x or {}).get("from"))
            end_ok((x or {}).get("to"))
    if (mech.get("vocabulary") or "known") not in ("known", "open", "only"):
        raise ValueError("vocabulary is known, open or only")


class TemplateSpec(BaseModel):
    name: str
    source: Optional[dict] = None
    mechanism: dict = Field(default_factory=dict)
    emit: dict = Field(default_factory=dict)
    #: the extractor being edited (a rename keeps it one extractor); absent = by name
    id: Optional[str] = None
    #: a new extractor: refused if the name is taken, instead of replacing that one
    new: bool = False

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
    #: try it on a file in the workspace instead: its first input, read as the extractor reads it
    sample_asset: Optional[str] = None

    @model_validator(mode="after")
    def _mechanism(self):
        if not str(self.name or "").startswith("builtin:"):
            check_mechanism(self.mechanism)
        return self


class TemplateStatusPatch(BaseModel):
    status: str                                   # active | disabled
