"""Services: sign-in, token refresh and first-run setup."""
import uuid

from sqlalchemy import func, select

from opencortyx.core.db import V2Session

DEMO_ORG = uuid.uuid5(uuid.NAMESPACE_DNS, "axiom-org:demo")   # the legacy dev workspace (continuity)


def access_token_ttl_seconds() -> int:
    """How long an access token is good for, read from the same settings that mint it."""
    from opencortyx.core.config import settings as _s
    return int(_s.JWT_EXPIRE_MINUTES) * 60


async def count_users() -> int:
    from opencortyx.domain.control import User
    async with V2Session() as db:
        return (await db.execute(select(func.count()).select_from(User))).scalar() or 0
