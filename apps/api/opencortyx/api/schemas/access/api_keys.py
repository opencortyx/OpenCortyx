"""Request and response models: API keys for agents and MCP clients."""
from pydantic import BaseModel


class ApiKeyCreate(BaseModel):
    app: str = "claude"
    role: str = "editor"               # the workspace role this agent key acts as
