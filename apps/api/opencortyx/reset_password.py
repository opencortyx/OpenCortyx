"""Set a new password for an account: `opencortyx reset-password <email>`.

For the administrator locked out of their own installation. There is no default credential and no
email-based recovery, so without this a forgotten admin password meant wiping the installation.
Running it needs access to the machine and its Docker, which is the same trust as reading the
database directly; it is not reachable over the API.

The password is asked for on the terminal, twice and hidden, never passed as an argument (which
would leave it in the shell history and the process list). The account is also re-enabled if it was
disabled, since a locked-out admin may have been disabled too.
"""
import asyncio
import getpass
import sys

MIN_LENGTH = 6          # the rule the API applies when an account is created or changed


async def _reset(email: str, password: str) -> bool:
    from sqlalchemy import func, select

    from opencortyx.core.db import V2Session
    from opencortyx.core.security.auth import hash_password
    from opencortyx.domain.control import User

    async with V2Session() as db:
        user = (await db.execute(
            select(User).where(func.lower(User.email) == email.lower()))).scalar_one_or_none()
        if user is None:
            return False
        user.password_hash = hash_password(password)
        user.status = "active"
        await db.commit()
        return True


async def _emails() -> list[str]:
    from sqlalchemy import select

    from opencortyx.core.db import V2Session
    from opencortyx.domain.control import User

    async with V2Session() as db:
        return [e for (e,) in (await db.execute(select(User.email).order_by(User.created_at))).all()]


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not argv[0].strip():
        print("Usage: opencortyx reset-password <email>", file=sys.stderr)
        return 2
    email = argv[0].strip()
    if not sys.stdin.isatty():
        print("Run this in a terminal: the new password is typed at a hidden prompt.", file=sys.stderr)
        return 2
    password = getpass.getpass(f"New password for {email}: ")
    if len(password) < MIN_LENGTH:
        print(f"The password must be at least {MIN_LENGTH} characters. Nothing was changed.",
              file=sys.stderr)
        return 1
    if getpass.getpass("Type it again: ") != password:
        print("The two passwords differ. Nothing was changed.", file=sys.stderr)
        return 1
    if not asyncio.run(_reset(email, password)):
        known = asyncio.run(_emails())
        print(f"No account with the email {email}. Accounts here: {', '.join(known) or 'none'}.",
              file=sys.stderr)
        return 1
    print(f"Password changed for {email}. Sign in with it now.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
