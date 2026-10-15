"""Request and response models: authoring edges and relationships."""
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator

from opencortyx.api.schemas.graph.nodes import require_text, require_uuid


class EdgeSpec(BaseModel):
    """An edge authored by hand. Endpoint ids are validated HERE, at the boundary: the handler called
    `uuid.UUID(spec.source_id)` directly, so a malformed or empty id raised ValueError inside the
    endpoint and the client got a 500 for what is a 422 — a bad request, not a broken server."""
    source_id: str
    target_id: str
    type: str = "RELATES_TO"
    properties: Optional[dict] = None

    @field_validator("source_id", "target_id")
    @classmethod
    def _endpoint(cls, v, info):
        return require_uuid(v, info.field_name)

    @field_validator("type")
    @classmethod
    def _type(cls, v):
        return require_text(v, "type")

    @model_validator(mode="after")
    def _two_nodes(self):
        return no_self_loop(self)


def no_self_loop(spec):
    """An edge connects two different nodes. A self-loop was accepted and then never shown when the
    node was read back, so the user saw success and an edge that did not exist for them."""
    if spec.source_id == spec.target_id:
        raise ValueError("an edge must connect two different nodes")
    return spec


class RelationshipSpec(BaseModel):
    source_id: str
    target_id: str
    relation: str

    @field_validator("source_id", "target_id")
    @classmethod
    def _endpoint(cls, v, info):
        return require_uuid(v, info.field_name)

    @field_validator("relation")
    @classmethod
    def _relation(cls, v):
        return require_text(v, "relation")

    @model_validator(mode="after")
    def _two_nodes(self):
        return no_self_loop(self)
