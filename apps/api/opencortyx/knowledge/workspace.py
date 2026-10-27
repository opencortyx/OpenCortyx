"""A workspace's identity: its name, its mode, and the central node its graph hangs from.

A workspace IS an `workspace_id` — every row and every node is scoped by it. This module gives that id a
name a person reads, and says what it stands for: in `person` mode the workspace is someone, in
`team` mode it is a team or an organization. The central node is that someone or something, an
ordinary graph node whose id IS the workspace_id, so anything workspace-wide can hang from it without a
lookup, and the top of the graph is a real node rather than a label.

The node is written here and nowhere else. A mode switch retypes it (Person ↔ Organization) and a
rename relabels it; everything attached to it stays attached, because its id never changes.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert as pg_insert

from opencortyx.core.db import V2Session
from opencortyx.core.substrate import NodeRepo
from opencortyx.domain import Workspace

MODES = ("person", "team")
#: Where a workspace's data is written: `graph` (chunk and row nodes, as before) or `delta` (Delta
#: tables through stores/data, the graph holding knowledge only).
DATA_STORES = ("graph", "delta")
#: The central node's type in each mode.
CENTRAL_TYPES = {"person": "Person", "team": "Organization"}
#: Stamped on the central node, so it is told apart from a Person or Organization read from data.
CENTRAL_KEY = "__workspace"
NAME_MAX = 120

_ADJECTIVES = ("Curious", "Quiet", "Brave", "Clever", "Gentle", "Lucky", "Nimble", "Patient",
               "Bright", "Calm", "Bold", "Merry", "Swift", "Wise", "Cosy", "Keen")
_ANIMALS = ("Otter", "Heron", "Badger", "Lynx", "Owl", "Fox", "Panda", "Falcon",
            "Walrus", "Koala", "Marten", "Puffin", "Ibex", "Gecko", "Tapir", "Wren")


class InvalidWorkspace(ValueError):
    """A submitted name or mode the workspace cannot take."""


@dataclass(frozen=True)
class Profile:
    workspace_id: uuid.UUID
    name: str
    mode: str
    data_store: str = "delta"

    @property
    def central_type(self) -> str:
        return CENTRAL_TYPES[self.mode]

    @property
    def display_name(self) -> str:
        """The name to show: the one given, else a friendly one made up from the workspace_id — the same
        on every restart, different between workspaces."""
        return self.name or made_up_name(self.workspace_id)


def made_up_name(workspace_id) -> str:
    h = hashlib.sha256(str(workspace_id).encode()).digest()
    return f"{_ADJECTIVES[h[0] % len(_ADJECTIVES)]} {_ANIMALS[h[1] % len(_ANIMALS)]}"


def clean_name(name) -> str:
    if name is None:
        return ""
    if not isinstance(name, str):
        raise InvalidWorkspace(f"name: expected text, got {name!r}")
    name = " ".join(name.split())
    if len(name) > NAME_MAX:
        raise InvalidWorkspace(f"name: at most {NAME_MAX} characters")
    return name


def clean_mode(mode) -> str:
    if mode not in MODES:
        raise InvalidWorkspace(f"mode: {mode!r} is not one of {list(MODES)}")
    return mode


def clean_data_store(value) -> str:
    if value not in DATA_STORES:
        raise InvalidWorkspace(f"data_store: {value!r} is not one of {list(DATA_STORES)}")
    return value


def _default(workspace_id) -> Profile:
    """A workspace with no row yet — one made before this table, or by a test. `GRAPH_ROOT_NAME` is
    the install's seed for a name, as it was for the setting this replaced."""
    from opencortyx.core.config.env import settings
    return Profile(workspace_id=workspace_id, name=clean_name(settings.GRAPH_ROOT_NAME or "")[:NAME_MAX], mode="person",
                   data_store="delta")


async def get_profile(workspace_id) -> Profile:
    org = uuid.UUID(str(workspace_id))
    async with V2Session() as db:
        row = await db.get(Workspace, org)
    if row is None:
        return _default(org)
    store = getattr(row, "data_store", None)
    return Profile(workspace_id=org, name=row.name or "", mode=row.mode if row.mode in MODES else "person",
                   data_store=store if store in DATA_STORES else "delta")


async def data_store(workspace_id) -> str:
    """`graph` or `delta`: which path this workspace's ingest writes through."""
    return (await get_profile(workspace_id)).data_store


async def save_profile(workspace_id, *, name=None, mode=None, data_store=None) -> tuple[Profile, dict]:
    """Write the fields given (None = leave as it is), then bring the central node in line.
    Returns (profile, {field: {"from", "to"}} for what actually changed). Raises InvalidWorkspace,
    writing nothing, on a bad value."""
    before = await get_profile(workspace_id)
    after = Profile(workspace_id=before.workspace_id,
                    name=before.name if name is None else clean_name(name),
                    mode=before.mode if mode is None else clean_mode(mode),
                    data_store=before.data_store if data_store is None else clean_data_store(data_store))
    changed = {k: {"from": getattr(before, k), "to": getattr(after, k)}
               for k in ("name", "mode", "data_store") if getattr(before, k) != getattr(after, k)}
    async with V2Session() as db:
        vals = {"name": after.name, "mode": after.mode, "data_store": after.data_store}
        stmt = pg_insert(Workspace).values(workspace_id=after.workspace_id, **vals)
        await db.execute(stmt.on_conflict_do_update(index_elements=["workspace_id"], set_=vals))
        await db.commit()
    await ensure_central_node(after.workspace_id, after)
    return after, changed


async def ensure_central_node(workspace_id, profile: Profile | None = None):
    """The workspace's central node, created or brought in line with the profile. Idempotent, and
    a no-op write when it already matches."""
    profile = profile or await get_profile(workspace_id)
    org = profile.workspace_id
    repo = NodeRepo()
    props = {"name": profile.display_name, "mode": profile.mode, CENTRAL_KEY: True}
    node = await repo.get(org, org)
    if node is None:
        return await repo.upsert_node(node_id=org, workspace_id=org, type=profile.central_type,
                                      label=profile.display_name, properties=props, date_link=False)
    current = node.properties or {}
    if (node.type == profile.central_type and node.label == profile.display_name
            and all(current.get(k) == v for k, v in props.items())):
        return node
    # `save`, not `upsert_node`: an upsert keeps an existing node's type, and a mode switch is
    # exactly a change of type.
    node.type, node.label = profile.central_type, profile.display_name
    node.properties = {**current, **props}
    return await repo.save(node)


def is_central(node) -> bool:
    return node is not None and str(node.id) == str(node.workspace_id)
