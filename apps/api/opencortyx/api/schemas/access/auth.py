"""Request and response models: sign-in, token refresh and first-run setup."""
from pydantic import BaseModel


class SetupRequest(BaseModel):
    email: str
    password: str
    adopt_demo: bool = True            # keep existing demo-workspace data (PDFs/sessions)
    mode: str = "person"               # person: the workspace is you | team: a team or organization
    name: str = ""                     # your name, or the team's; blank → a made-up one


class TokenRequest(BaseModel):
    username: str                      # email (next-auth sends it as `username`)
    password: str = ""


class RefreshRequest(BaseModel):
    refresh_token: str
