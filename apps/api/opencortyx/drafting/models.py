"""The typed vocabulary of authoring: what a drafter is asked, and what it hands back.

A *draft* is a proposal for a control-plane object — a schema mapping, a pipeline, an extraction
template — that the system wrote by looking at real data instead of asking a person to author it
from an empty form. Nothing here is persisted. A draft becomes real only when someone saves it
through the CRUD that already owns that object, which is why `Draft.payload` is deliberately the
EXACT shape those endpoints already accept rather than a new intermediate format: a second write
path would be a second place for the shape to drift.

Two properties make a draft reviewable rather than magic, and both are carried, not implied:

  `evidence`   why each decision was made, in terms of the sample ("role=text for `body`: averages
               412 characters over 20 records"). Without it a user can only accept or reject the
               whole thing; with it they can correct one line.
  `confidence` how much of the draft rests on measurement versus on a language model's guess. A
               LOW draft is not a worse draft — it is one that should be read before it is saved.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DraftKind(str, Enum):
    """What is being drafted. The value is the wire token and the registry key prefix."""

    MAPPING = "mapping"
    PIPELINE = "pipeline"
    EXTRACTOR = "extractor"


class Confidence(str, Enum):
    HIGH = "high"        # every decision is a measurement over the sample
    MEDIUM = "medium"    # measurements refined by a model that agreed with them
    LOW = "low"          # the model was the only source, or the sample was too thin to measure

    @classmethod
    def worst(cls, *values: "Confidence") -> "Confidence":
        order = [cls.HIGH, cls.MEDIUM, cls.LOW]
        return max(values, key=order.index) if values else cls.LOW


@dataclass(frozen=True)
class Evidence:
    """One decision and the thing in the sample that justifies it."""

    claim: str                   # "body is prose worth reading"
    basis: str                   # "averages 412 characters across 20 records"
    path: str = ""               # the key/column/field it concerns, when there is one

    def to_dict(self) -> dict:
        d = {"claim": self.claim, "basis": self.basis}
        if self.path:
            d["path"] = self.path
        return d


@dataclass
class Sample:
    """A bounded look at one source — the only thing a drafter is allowed to reason from.

    Bounded because a drafter runs inside a request: a 2 GB JSONL must cost the same as a 2 KB one.
    `truncated` says so out loud, so a drafter can lower its own confidence instead of quietly
    generalising from the first page.

    The three facets are populated according to `route`, and exactly one is normally non-empty —
    but a CSV read as text (a malformed header, say) legitimately has both `rows` and `text`, so
    they are independent fields rather than a union.
    """

    filename: str = ""
    route: str = ""                                    # formats.SEMISTRUCTURED|STRUCTURED|DOCUMENT
    format: str = ""                                   # detector's format name: JSON, CSV, PDF
    records: list[Any] = field(default_factory=list)   # semi-structured: top-level records
    columns: list[str] = field(default_factory=list)   # structured: header
    rows: list[list[Any]] = field(default_factory=list)  # structured: a few data rows
    text: str = ""                                     # unstructured: leading prose
    #: unstructured: the file itself, as the document reader opens it. A PDF's layout — which line
    #: is a heading, where a table is — is in the file, not in `text`, so a structure preview must
    #: read this. Not serialised.
    document: bytes = b""
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.records or self.rows or self.text.strip())

    def to_dict(self) -> dict:
        return {"filename": self.filename, "route": self.route, "format": self.format,
                "records": len(self.records), "columns": self.columns,
                "rows": len(self.rows), "text_chars": len(self.text),
                "truncated": self.truncated, "warnings": self.warnings}


@dataclass
class DraftRequest:
    """What a drafter is given. `goal` is the plain-English half — the only input for an extractor
    ("track which vendors we owe money to"), and an optional steer for a mapping."""

    workspace_id: uuid.UUID
    kind: DraftKind
    sample: Sample = field(default_factory=Sample)
    goal: str = ""
    #: Free-form steering a caller already knows: a bound mapping id, a paradigm override, the
    #: node type an extractor should hang off. Drafters read what they understand and ignore
    #: the rest, so a new caller-supplied hint never needs a signature change.
    context: dict[str, Any] = field(default_factory=dict)
    #: Registry model name/id for the generation call. None → the org's default for the role.
    model_ref: str | None = None
    #: When false, a drafter must return its heuristic result and make no model call at all.
    use_model: bool = True


@dataclass
class Draft:
    """A proposal. `payload` is ready to POST to the endpoint that owns `kind`, unmodified."""

    kind: DraftKind
    name: str
    payload: dict[str, Any]
    drafter: str                                       # registry name that produced it
    confidence: Confidence = Confidence.MEDIUM
    summary: str = ""                                  # one line a person reads first
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Where to POST this payload, so the client never hard-codes the mapping kind → URL table.
    save_endpoint: str = ""
    #: Set by the model that refined it, for auditing which drafts a model touched.
    model: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "name": self.name, "payload": self.payload,
                "drafter": self.drafter, "confidence": self.confidence.value,
                "summary": self.summary, "evidence": [e.to_dict() for e in self.evidence],
                "warnings": self.warnings, "save_endpoint": self.save_endpoint,
                "model": self.model}


__all__ = ["Confidence", "Draft", "DraftKind", "DraftRequest", "Evidence", "Sample"]
