"""Who is behind a credential — `actors.resolve`, with the database replaced by a fake session.

Identity comes from what authenticated, never from what a caller says: a key names its agent and
the person who issued it; a person's login names that person and the client they used. A row that
cannot be read must cost the capture its detail, never the caller their answer.
"""
import uuid
from types import SimpleNamespace

import pytest

from opencortyx.core import db as db_mod
from opencortyx.domain.control import ApiKey, User
from opencortyx.knowledge import actors

ORG = uuid.uuid4()


class _FakeDB:
    def __init__(self, rows, fail=False):
        self.rows, self.fail = rows, fail

    async def __aenter__(self):
        if self.fail:
            raise ConnectionError("database down")
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, key):
        return self.rows.get((model, str(key)))


@pytest.fixture
def rows(monkeypatch):
    table: dict = {}
    monkeypatch.setattr(db_mod, "V2Session", lambda: _FakeDB(table))
    return table


def _user(rows, email="ada@example.com", org=ORG):
    u = SimpleNamespace(id=uuid.uuid4(), workspace_id=org, email=email)
    rows[(User, str(u.id))] = u
    return u


def _key(rows, app="Claude Desktop", created_by=None, org=ORG):
    k = SimpleNamespace(id=uuid.uuid4(), workspace_id=org, app=app, created_by=created_by)
    rows[(ApiKey, str(k.id))] = k
    return k


async def test_a_key_names_its_agent_and_the_person_who_issued_it(rows):
    owner = _user(rows, "admin@example.com")
    key = _key(rows, created_by=owner.id)
    who = await actors.resolve(ORG, actor_id=key.id, actor_type="agent", agent_name="ignored client name")
    assert who.actor_type == "agent" and who.actor_id == str(key.id)
    assert who.agent == actors.Actor(kind="agent", id=str(key.id), name="Claude Desktop", owner_id=str(owner.id))
    assert who.user.id == str(owner.id) and who.user.email == "admin@example.com"


async def test_a_key_without_a_recorded_issuer_has_no_person(rows):
    _user(rows, "first-admin@example.com")          # exists, and must NOT be guessed as the owner
    key = _key(rows, created_by=None)
    who = await actors.resolve(ORG, actor_id=key.id, actor_type="agent")
    assert who.user is None and who.agent.owner_id is None


async def test_the_placeholder_app_name_falls_back_to_what_the_client_sent(rows):
    key = _key(rows, app="default")
    assert (await actors.resolve(ORG, actor_id=key.id, actor_type="agent", agent_name="Cursor")).agent.name == "Cursor"
    assert (await actors.resolve(ORG, actor_id=key.id, actor_type="agent")).agent.name == "agent"


async def test_a_key_from_another_workspace_is_not_trusted(rows):
    stranger = _user(rows, "x@other.example", org=uuid.uuid4())
    key = _key(rows, app="Theirs", created_by=stranger.id, org=uuid.uuid4())
    who = await actors.resolve(ORG, actor_id=key.id, actor_type="agent", agent_name="client")
    assert who.agent.name == "client" and who.user is None


async def test_an_issuer_from_another_workspace_is_kept_only_as_an_id(rows):
    stranger = _user(rows, "x@other.example", org=uuid.uuid4())
    key = _key(rows, created_by=stranger.id)
    who = await actors.resolve(ORG, actor_id=key.id, actor_type="agent")
    assert who.user.id == str(stranger.id) and who.user.email is None and who.user.name == str(stranger.id)


async def test_a_person_using_the_web_app_is_handled_by_the_assistant(rows):
    person = _user(rows)
    who = await actors.resolve(ORG, actor_id=person.id, actor_type="user")
    assert who.agent == actors.assistant()
    assert who.user.name == "ada@example.com" and who.actor_type == "user"


async def test_a_person_through_an_mcp_client_is_handled_by_that_client_not_the_assistant(rows):
    person = _user(rows)
    who = await actors.resolve(ORG, actor_id=person.id, actor_type="user", agent_name="Claude Desktop")
    assert who.agent.id == "client:claude desktop" and who.agent.name == "Claude Desktop"
    assert who.user.email == "ada@example.com"


async def test_an_unknown_person_keeps_their_id(rows):
    uid = uuid.uuid4()
    who = await actors.resolve(ORG, actor_id=uid, actor_type="user")
    assert who.user.id == str(uid) and who.user.name == str(uid) and who.user.email is None


async def test_a_database_failure_degrades_to_ids_and_never_raises(monkeypatch):
    monkeypatch.setattr(db_mod, "V2Session", lambda: _FakeDB({}, fail=True))
    key_id, uid = uuid.uuid4(), uuid.uuid4()
    agent = await actors.resolve(ORG, actor_id=key_id, actor_type="agent", agent_name="bot")
    assert agent.agent.name == "bot" and agent.user is None
    person = await actors.resolve(ORG, actor_id=uid, actor_type="user")
    assert person.user.id == str(uid) and person.agent == actors.assistant()


async def test_a_malformed_actor_id_degrades_and_never_raises(rows):
    who = await actors.resolve(ORG, actor_id="not-a-uuid", actor_type="agent", agent_name="bot")
    assert who.agent.id == "not-a-uuid" and who.user is None
    person = await actors.resolve(ORG, actor_id="not-a-uuid", actor_type="user")
    assert person.user.id == "not-a-uuid"
