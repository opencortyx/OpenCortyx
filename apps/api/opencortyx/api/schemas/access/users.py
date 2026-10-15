"""Request and response models: workspace users and their roles."""
from typing import Optional

import re

from pydantic import BaseModel, field_validator

from opencortyx.api.deps import ROLES

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
STATUSES = ("active", "disabled")


def _role(v):
    """A role is one the guards know. Any other string was stored and then treated as 'not a viewer',
    which require_write lets write."""
    if v not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}, got {v!r}")
    return v


class UserCreate(BaseModel):
    email: str
    password: str
    role: str = "viewer"

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        v = (v or "").strip()
        if not _EMAIL.match(v):
            raise ValueError(f"email must be an address like name@example.com, got {v!r}")
        return v

    @field_validator("role")
    @classmethod
    def _r(cls, v):
        return _role(v)


class UserPatch(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None
    password: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _r(cls, v):
        return None if v is None else _role(v)

    @field_validator("status")
    @classmethod
    def _s(cls, v):
        if v is not None and v not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}, got {v!r}")
        return v
