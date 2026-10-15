"""Request and response models: glossary terms, synonyms, business rules and business context."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from opencortyx.api.schemas.graph.nodes import optional_uuid, require_text


def _required(*fields):
    """Curated text is the whole point of the item: a blank term or definition is refused, not stored."""
    return field_validator(*fields)(classmethod(lambda cls, v, info: require_text(v, info.field_name)))


class GlossarySpec(BaseModel):
    term: str
    definition: str
    applies_to: Optional[str] = None                       # legacy single target
    targets: list[str] = Field(default_factory=list)       # attach to any node(s)
    scope: Optional[str] = None                            # global | collection | node
    status: Optional[str] = None                           # draft | approved | deprecated
    owner: Optional[str] = None
    source: Optional[str] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    examples: Optional[list[str]] = None

    _text = _required("term", "definition")


class RuleSpec(BaseModel):
    title: str
    content: str
    applies_to: Optional[str] = None
    targets: list[str] = Field(default_factory=list)
    severity: str = "info"                                 # must | should | info
    kind: Optional[str] = None                             # definition | calculation | policy | quality | freshness
    scope: Optional[str] = None
    status: Optional[str] = None
    owner: Optional[str] = None
    source: Optional[str] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None

    _text = _required("title", "content")


class SynonymSpec(BaseModel):
    term: str
    target_id: Optional[str] = None
    targets: list[str] = Field(default_factory=list)
    kind: Optional[str] = None                             # acronym | abbreviation | former_name | variant | translation
    scope: Optional[str] = None
    status: Optional[str] = None
    owner: Optional[str] = None
    source: Optional[str] = None

    _text = _required("term")


class BusinessContextSpec(BaseModel):
    title: str
    content: str
    scope: str = "org"                                     # org | collection
    collection_id: Optional[str] = None
    always_on: bool = True
    kind: str = "business_context"
    owner: Optional[str] = None
    source: Optional[str] = None

    _text = _required("title", "content")
    _collection = field_validator("collection_id")(classmethod(lambda cls, v, info: optional_uuid(v, info.field_name)))
