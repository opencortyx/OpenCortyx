"""Who took part in a captured conversation — the person who asked and the agent that answered — and
their nodes in the graph.

A Session used to carry its participants as two strings, `agent` and `user`, so nothing in the graph
could say "every session this person started" or "everything this agent handled", and each session
floated unconnected. A participant is now a node with a stable id, written once and linked from every
session it takes part in:

    (AppUser) -[STARTED]-> (Session) -[HANDLED_BY]-> (AppAgent) -[OWNED_BY]-> (AppUser)

The types are `AppUser` and `AppAgent`, not `User` and `Agent`. The planner matches node-type names
against the question's own words, so a type called `User` would claim "how many users signed up?"
for the workspace's accounts when the question is about a customer table. The prefix keeps the
app's bookkeeping out of the corpus's vocabulary.

Both are app memory (`__memory`) and never evidence (`__internal`): the knowledge scope drops them,
and no answer can cite an account as a fact.

Identity comes from the CREDENTIAL, never from what a caller says about itself:
  * a person's login (JWT)   → the AppUser is that account; the agent is the client they used
  * an agent's API key       → the AppAgent is that key; the AppUser is whoever issued it, if recorded
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from opencortyx.core.observability import get_logger
from opencortyx.core.substrate import CONVERSATION_FLAG, NON_EVIDENCE_FLAG, NodeRepo

log = get_logger(__name__)

USER_TYPE = "AppUser"
AGENT_TYPE = "AppAgent"

#: The in-app Assistant: one built-in agent per workspace, not a key.
ASSISTANT_ID = "assistant"
ASSISTANT_NAME = "OpenCortyx Assistant"

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/actor-node")
_NAME_MAX = 200


@dataclass(frozen=True)
class Actor:
    """One participant. `id` is the account or key id; a client without a key (the built-in
    Assistant, an MCP client signed in as a person) is named `assistant` or `client:<name>`."""
    kind: str                                # "user" | "agent"
    id: str
    name: str
    email: Optional[str] = None
    owner_id: Optional[str] = None           # agent only: the app_user who issued its key

    @property
    def node_type(self) -> str:
        return USER_TYPE if self.kind == "user" else AGENT_TYPE


@dataclass(frozen=True)
class Participants:
    """Who a captured exchange is attributed to. `user` is None when no person is known — an API key
    issued before issuers were recorded."""
    user: Optional[Actor]
    agent: Actor
    actor_type: str                          # what authenticated: "user" | "agent"
    actor_id: str


def node_id(workspace_id, actor: Actor) -> uuid.UUID:
    """The participant's node id: the same for the same account in the same workspace, every time."""
    return uuid.uuid5(_NS, f"{workspace_id}:{actor.kind}:{actor.id}")


def assistant() -> Actor:
    return Actor(kind="agent", id=ASSISTANT_ID, name=ASSISTANT_NAME)


def client_agent(name: Optional[str]) -> Actor:
    """An agent with no key of its own — an MCP client a person signed into. Named by the client."""
    label = " ".join(str(name or "").split())[:_NAME_MAX] or "agent"
    return Actor(kind="agent", id=f"client:{label.lower()}", name=label)


def _clean_name(value) -> str:
    return " ".join(str(value or "").split())[:_NAME_MAX]


async def resolve(workspace_id, *, actor_id, actor_type: str, agent_name: Optional[str] = None) -> Participants:
    """The participants behind an authenticated call.

    `actor_type == "agent"` means `actor_id` is an api_key id. Its agent is named by the key's `app`
    (what the admin called it when issuing it) unless that is the placeholder `default`, then by
    the name the client sent. Its person is the key's issuer when one was recorded, else none.

    Otherwise `actor_id` is an app_user id: that person, using `agent_name`'s client, or the built-in
    Assistant when no client is named.

    A row that cannot be read leaves that participant with only its id; resolution never fails the
    capture it serves.
    """
    from opencortyx.core.db import V2Session
    from opencortyx.domain.control import ApiKey, User

    async def _user(db, uid) -> Optional[Actor]:
        if not uid:
            return None
        row = await db.get(User, uid)
        if row is None or str(row.workspace_id) != str(workspace_id):
            return Actor(kind="user", id=str(uid), name=str(uid))
        return Actor(kind="user", id=str(row.id), name=_clean_name(row.email) or str(row.id),
                     email=row.email)

    aid = str(actor_id)
    if actor_type == "agent":
        user, agent = None, Actor(kind="agent", id=aid, name=_clean_name(agent_name) or "agent")
        try:
            async with V2Session() as db:
                key = await db.get(ApiKey, uuid.UUID(aid))
                if key is not None and str(key.workspace_id) == str(workspace_id):
                    app = _clean_name(key.app)
                    owner = getattr(key, "created_by", None)
                    agent = Actor(kind="agent", id=aid,
                                  name=app if app and app.lower() != "default" else agent.name,
                                  owner_id=str(owner) if owner else None)
                    user = await _user(db, owner)
        except Exception as e:                                   # noqa: BLE001 — capture is best-effort
            log.warning("actors.resolve_key_failed", error=str(e)[:200])
        return Participants(user=user, agent=agent, actor_type="agent", actor_id=aid)

    agent = client_agent(agent_name) if _clean_name(agent_name) else assistant()
    user = Actor(kind="user", id=aid, name=aid)
    try:
        async with V2Session() as db:
            user = await _user(db, uuid.UUID(aid)) or user
    except Exception as e:                                       # noqa: BLE001
        log.warning("actors.resolve_user_failed", error=str(e)[:200])
    return Participants(user=user, agent=agent, actor_type="user", actor_id=aid)


def _props(actor: Actor) -> dict:
    props = {"actor_id": actor.id, "actor_kind": actor.kind, "name": actor.name,
             "text": f"{actor.node_type} {actor.name}",
             CONVERSATION_FLAG: True, NON_EVIDENCE_FLAG: True, "__source": "system"}
    if actor.email:
        props["email"] = actor.email
    return props


async def ensure(workspace_id, actor: Actor, repo: Optional[NodeRepo] = None) -> uuid.UUID:
    """The participant's node, written when it is new or its name changed, else left alone.

    Called on every captured exchange, so it reads first: rewriting an unchanged node would bump its
    audit version and add a MODIFIED_ON edge on every question asked."""
    repo = repo or NodeRepo()
    nid = node_id(workspace_id, actor)
    old = await repo.get(workspace_id, nid)
    want = _props(actor)
    # compared as text: a store may hand a boolean back as "true"
    if old is not None and old.label == actor.name and all(
            str((old.properties or {}).get(k)).lower() == str(v).lower() for k, v in want.items()):
        return nid
    await repo.upsert_node(workspace_id=workspace_id, node_id=nid, type=actor.node_type, label=actor.name,
                           properties=want)
    return nid


async def link_session(workspace_id, session_id, who: Participants, repo: Optional[NodeRepo] = None) -> dict:
    """Write the participants' nodes and their edges to one session. Idempotent: an edge between two
    nodes is one edge, however often it is written."""
    repo = repo or NodeRepo()
    agent_nid = await ensure(workspace_id, who.agent, repo)
    edges = [{"source_id": session_id, "target_id": agent_nid, "type": "HANDLED_BY"}]
    user_nid = None
    if who.user is not None:
        user_nid = await ensure(workspace_id, who.user, repo)
        edges.append({"source_id": user_nid, "target_id": session_id, "type": "STARTED"})
    owner = who.agent.owner_id
    if owner and who.user is not None and who.user.id == owner:
        edges.append({"source_id": agent_nid, "target_id": user_nid, "type": "OWNED_BY"})
    await repo.add_edges(workspace_id=workspace_id, edges=edges)
    return {"agent": str(agent_nid), "user": str(user_nid) if user_nid else None}
