"""Request and response models: API keys for agents and MCP clients."""
from pydantic import BaseModel, field_validator


class ApiKeyCreate(BaseModel):
    app: str = "claude"
    role: str = "editor"               # the workspace role this agent key acts as
    groups: list[str] = []             # the access groups the key reads as (core/security/scope.py)

    @field_validator("groups")
    @classmethod
    def _g(cls, v):
        from opencortyx.api.schemas.access.users import clean_groups
        return clean_groups(v)
