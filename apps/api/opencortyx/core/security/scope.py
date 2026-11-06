"""Access scope: which collections a caller may read, and up to what sensitivity - enforced at ONE place.

THE RULE
  A Policy (domain/control.py) gives a GROUP read access to one collection (or all of them) up to a
  sensitivity. A person's groups are their role plus the groups on their account; an API key's are
  its role plus the groups on the key.
    * A workspace with no policies has no access control: everyone reads everything at "normal"
      (as before). Questions never read "confidential" or "restricted" records unless a policy
      allows it - that was already so, and still is.
    * Once a workspace has ANY policy, it is deny-by-default: a caller reads the collections its
      groups' policies name, and records in no collection (workspace-wide knowledge). A caller no
      policy names reads only the latter.
    * Admins read every collection (they manage the data), at the highest sensitivity a policy
      gives them, else "normal" for questions; browsing is unrestricted for them.

WHERE IT IS ENFORCED
  `Scope.allows(node)` is applied by NodeRepo to every node it READS while a scope is bound
  (core/substrate/repo.py): get, get_many, find, neighbours, descendants, searches. So a graph hop,
  an entity lookup, a browse page and a search all obey the same rule, and a route nobody remembered
  to scope is scoped anyway. Writes are not filtered. Background jobs run with no scope.
    * `require_auth` binds the caller's scope for the request (`bind_request`).
    * Retrieval binds the question's scope on top of it (`using`), the two intersected.
    * The data route (SQL over tables) leaves out the columns above the caller's sensitivity from
      the views themselves (retrieval/l2_data/schema.py), so `SELECT *` cannot return them.
  A node the caller may not see is NOT FOUND, never "forbidden": the answer to "is X here?" is
  the same whether X is missing or out of reach.

  A merged record (resolution/master.py) is as restricted as its most restricted member, and lists
  every member collection in `__collections`: only a caller who may read all of them sees it.

Policies and groups are read at most every `_TTL` seconds per workspace / user, and changes made in
this process take effect at once (`forget`); other processes follow within `_TTL`.
"""
from __future__ import annotations

import contextlib
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from opencortyx.domain.control import SENSITIVITY_ORDER, Policy

_TTL = 30.0
#: The collection id no collection has: a scope that allows NO collection still filters, where an
#: empty list would read as "no filter" in the code that takes `collection_ids`.
NO_COLLECTION = uuid.UUID(int=0)


@dataclass(frozen=True)
class Scope:
    collections: Optional[frozenset] = None     # None: every collection
    max_sensitivity: str = "restricted"

    @property
    def level(self) -> int:
        return SENSITIVITY_ORDER.get(self.max_sensitivity, 1)

    @property
    def unrestricted(self) -> bool:
        return self.collections is None and self.level >= max(SENSITIVITY_ORDER.values())

    def allows(self, node) -> bool:
        if node is None:
            return False
        if SENSITIVITY_ORDER.get(getattr(node, "sensitivity", None) or "normal", 1) > self.level:
            return False
        if self.collections is None:
            return True
        props = getattr(node, "properties", None) or {}
        members = props.get("__collections")
        if members:
            return all(str(c) in self.collections for c in members)
        col = getattr(node, "collection_id", None)
        return col is None or str(col) in self.collections

    def intersect(self, other: Optional["Scope"]) -> "Scope":
        if other is None:
            return self
        if self.collections is None:
            cols = other.collections
        elif other.collections is None:
            cols = self.collections
        else:
            cols = self.collections & other.collections
        return Scope(collections=cols, max_sensitivity=min(
            (self.max_sensitivity, other.max_sensitivity), key=lambda s: SENSITIVITY_ORDER.get(s, 1)))

    def collection_ids(self) -> Optional[list]:
        """For code that takes `collection_ids`: None (all), or a list that is never empty."""
        if self.collections is None:
            return None
        return [uuid.UUID(c) for c in sorted(self.collections)] or [NO_COLLECTION]

    def sensitivities(self) -> list[str]:
        return [name for name, level in SENSITIVITY_ORDER.items() if level <= self.level]


_current: ContextVar[Optional[Scope]] = ContextVar("access_scope", default=None)


def current() -> Optional[Scope]:
    return _current.get()


def bind(scope: Optional[Scope]) -> None:
    """Set the scope for the rest of this task (a request) - no reset, as `usage.bind`."""
    _current.set(scope)


@contextlib.contextmanager
def using(scope: Optional[Scope]):
    """Narrow the current scope for the code inside (intersected, never widened)."""
    cur = _current.get()
    token = _current.set(scope.intersect(cur) if scope is not None else cur)
    try:
        yield _current.get()
    finally:
        _current.reset(token)


@contextlib.contextmanager
def unscoped():
    """For the platform's own work inside a request that must see every record (a deletion job
    proving nothing is left). Never around anything that returns records to the caller."""
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


def visible(nodes):
    """The nodes of `nodes` the current scope allows (all of them when none is bound)."""
    s = _current.get()
    if s is None:
        return list(nodes)
    return [n for n in nodes if s.allows(n)]


# ── policies and groups, cached ───────────────────────────────────────────────────────────────

_policies: dict = {}     # workspace -> (read_at, [Policy rows as tuples])
_groups: dict = {}       # user id -> (read_at, [group])


def forget(workspace_id=None, user_id=None) -> None:
    """Drop cached policies (of a workspace) and groups (of a user) after they change."""
    if workspace_id is None and user_id is None:
        _policies.clear()
        _groups.clear()
    if workspace_id is not None:
        _policies.pop(str(workspace_id), None)
    if user_id is not None:
        _groups.pop(str(user_id), None)


async def _workspace_policies(workspace_id) -> list[tuple]:
    key = str(workspace_id)
    hit = _policies.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    from sqlalchemy import select
    from opencortyx.core.db import V2Session
    async with V2Session() as db:
        rows = (await db.execute(select(Policy).where(Policy.workspace_id == uuid.UUID(key)))).scalars().all()
    value = [(p.group, str(p.collection_id) if p.collection_id else None, p.max_sensitivity or "normal",
              p.permission or "read") for p in rows]
    _policies[key] = (time.monotonic(), value)
    return value


async def user_groups(user) -> list[str]:
    """The caller's groups: their role, the groups on their account (or on their API key)."""
    out = [str(user.role).lower()] if getattr(user, "role", None) else []
    out += [str(g).lower() for g in (getattr(user, "groups", None) or [])]
    if getattr(user, "actor_type", "user") != "user":
        return list(dict.fromkeys(out))
    key = str(user.user_id)
    hit = _groups.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return list(dict.fromkeys(out + hit[1]))
    try:
        from opencortyx.core.db import V2Session
        from opencortyx.domain.control import User
        async with V2Session() as db:
            row = await db.get(User, user.user_id)
        groups = [str(g).lower() for g in ((row.groups if row is not None else None) or [])]
    except Exception:                                         # noqa: BLE001 - no column yet: role only
        groups = []
    _groups[key] = (time.monotonic(), groups)
    return list(dict.fromkeys(out + groups))


def _top(names) -> str:
    level = max((SENSITIVITY_ORDER.get(n, 1) for n in names), default=1)
    return next((k for k, v in SENSITIVITY_ORDER.items() if v == level), "normal")


async def resolve_scope(workspace_id: uuid.UUID, groups: list | None = None, *, admin: bool = False):
    """(allowed_collection_ids | None, max_sensitivity) for a QUESTION by a caller in `groups`.
    None means every collection; a list is never empty (`NO_COLLECTION` when nothing is allowed)."""
    rows = await _workspace_policies(workspace_id)
    if not rows:
        return None, "normal"
    mine = [r for r in rows if r[0] in set(groups or [])]
    max_sens = _top([r[2] for r in mine]) if mine else "normal"
    if admin or any(r[1] is None for r in mine):
        return None, max_sens
    return [uuid.UUID(r[1]) for r in mine if r[1]] or [NO_COLLECTION], max_sens


async def request_scope(user) -> Optional[Scope]:
    """The scope a caller browses with: None (everything) for an admin or in a workspace without
    policies; otherwise what their groups' policies allow."""
    if getattr(user, "role", None) == "admin":
        return None
    rows = await _workspace_policies(user.workspace_id)
    if not rows:
        return None
    groups = set(await user_groups(user))
    mine = [r for r in rows if r[0] in groups]
    cols = None if any(r[1] is None for r in mine) else frozenset(r[1] for r in mine if r[1])
    return Scope(collections=cols, max_sensitivity=_top([r[2] for r in mine]) if mine else "normal")


async def bind_request(user) -> None:
    """Bind the caller's scope for this request. Never raises: a scope that cannot be read is the
    narrowest one (workspace-wide records at "normal"), not no scope at all."""
    try:
        bind(await request_scope(user))
    except Exception:                                         # noqa: BLE001 - fail closed
        bind(Scope(collections=frozenset(), max_sensitivity="normal"))


def question_scope(collection_ids, max_sensitivity: str) -> Scope:
    return Scope(collections=(frozenset(str(c) for c in collection_ids) if collection_ids is not None else None),
                 max_sensitivity=max_sensitivity or "normal")


__all__ = ["NO_COLLECTION", "Scope", "bind", "bind_request", "current", "forget", "question_scope",
           "request_scope", "resolve_scope", "unscoped", "user_groups", "using", "visible"]
