"""Captured conversations in the graph — one writer, real participants, linked to what they used.

Against the in-memory graph and vector stores: the real `capture_exchange`, `actors`, `save_session`
and graph-tree code, no database, no model. What is pinned:

  * one Session per chat thread (its id IS the thread id), one rolling Session per caller per day
    without a thread — never a second copy of the same chat
  * who took part is a node, not a string: AppUser -STARTED-> Session -HANDLED_BY-> AppAgent,
    and an agent's key -OWNED_BY-> the person who issued it, only when that person is recorded
  * each exchange is one Turn: PART_OF its session, FOLLOWS the previous one, REFERENCES every
    node the answer rests on that exists and is knowledge — never a missing id, a malformed one,
    another conversation's memory, or the session itself
  * participants are app memory and never evidence; counting a type respects the query's scope
  * the Save button enriches a captured session without destroying what capture recorded
  * the graph view then places a chat under the asset it used instead of floating beside it
"""
import uuid
from datetime import datetime, timezone

import pytest

from opencortyx.core.substrate import NodeRepo, in_scope
from opencortyx.core.substrate import repo as repo_mod
from opencortyx.knowledge import actors, authoring, graph_tree, memory
from opencortyx.stores.graph.adapters.memory import MemoryGraphStore
from opencortyx.stores.graph.factory import set_graph_store
from opencortyx.stores.vector.adapters.memory import MemoryVectorStore
from opencortyx.stores.vector.factory import set_vector_store


@pytest.fixture
def org():
    return uuid.uuid4()


@pytest.fixture(autouse=True)
def stores(monkeypatch):
    """Fresh in-memory stores; no keyword model, no embedding queue, no tree-rebuild task."""
    g, v = MemoryGraphStore(), MemoryVectorStore()
    set_graph_store(g)
    set_vector_store(v)

    async def _no_sparse(texts):
        return [None for _ in texts]

    monkeypatch.setattr(repo_mod, "sparse_embed", _no_sparse)
    monkeypatch.setattr(repo_mod, "_change_listeners", [])
    monkeypatch.setattr(authoring, "_enqueue_embed", lambda *a, **k: None)
    yield g
    set_graph_store(None)
    set_vector_store(None)


PERSON = actors.Actor(kind="user", id=str(uuid.uuid4()), name="ada@example.com", email="ada@example.com")


def web(person=PERSON) -> actors.Participants:
    return actors.Participants(user=person, agent=actors.assistant(), actor_type="user", actor_id=person.id)


async def _knowledge(org, label="coffee-brewing.txt", type="Chunk", **props):
    node = await NodeRepo().upsert_node(workspace_id=org, type=type, label=label,
                                        properties={"text": label, **props})
    return node.id


async def _edges(org, type=None):
    rows = await NodeRepo().edges(org)
    return [e for e in rows if type is None or e.type == type]


async def _ask(org, who, key, q="How long is an espresso shot?", **kw):
    return await memory.capture_exchange(org, who=who, key=key, tool="assistant_chat", query=q,
                                         output=kw.pop("output", "About 25 seconds."), **kw)


# ── one session per conversation ──────────────────────────────────────────────────────────────────

async def test_a_chat_is_one_session_whose_id_is_the_thread_id(org):
    chat = uuid.uuid4()
    key = memory.session_key(chat_id=chat, prefix="assistant", actor_id=PERSON.id)
    first = await _ask(org, web(), key, title="How long is an espresso shot?")
    second = await _ask(org, web(), key, q="And a ristretto?")

    assert first["session_id"] == second["session_id"] == str(chat)
    sessions = await NodeRepo().find(org, exact_types=["Session"])
    assert [str(s.id) for s in sessions] == [str(chat)]
    p = sessions[0].properties
    assert sessions[0].label == "How long is an espresso shot?"          # named by its first question
    assert p["captured"] is True and p["actor_type"] == "user" and p["actor_id"] == PERSON.id
    assert p["user"] == "ada@example.com" and p["agent"] == actors.ASSISTANT_NAME
    assert in_scope(sessions[0], "conversations") and not in_scope(sessions[0], "knowledge")


async def test_turns_are_numbered_and_chained_in_order(org):
    key = memory.session_key(chat_id=uuid.uuid4(), prefix="assistant", actor_id=PERSON.id)
    t0 = await _ask(org, web(), key, q="one?")
    t1 = await _ask(org, web(), key, q="two?")
    t2 = await _ask(org, web(), key, q="three?")
    assert [t0["ordinal"], t1["ordinal"], t2["ordinal"]] == [0, 1, 2]

    part_of = await _edges(org, "PART_OF")
    assert {str(e.source_id) for e in part_of} == {t0["turn_id"], t1["turn_id"], t2["turn_id"]}
    assert {str(e.target_id) for e in part_of} == {t0["session_id"]}
    follows = {(str(e.source_id), str(e.target_id)) for e in await _edges(org, "FOLLOWS")}
    assert follows == {(t0["turn_id"], t1["turn_id"]), (t1["turn_id"], t2["turn_id"])}


async def test_a_later_capture_keeps_when_the_session_started(org):
    key = memory.session_key(chat_id=uuid.uuid4(), prefix="assistant", actor_id=PERSON.id)
    sid = uuid.UUID((await _ask(org, web(), key))["session_id"])
    started = (await NodeRepo().get(org, sid)).properties["started_at"]
    await _ask(org, web(), key, q="later?")
    after = (await NodeRepo().get(org, sid)).properties
    assert after["started_at"] == started and after["ended_at"] >= started


async def test_without_a_thread_a_caller_has_one_rolling_session_per_day(org):
    other = actors.Actor(kind="user", id=str(uuid.uuid4()), name="bo@example.com")
    k1 = memory.session_key(prefix="assistant", actor_id=PERSON.id, day="2026-10-03")
    assert k1 == f"assistant:{PERSON.id}:2026-10-03"
    a = await _ask(org, web(), k1)
    b = await _ask(org, web(), k1, q="again?")
    c = await _ask(org, web(other), memory.session_key(prefix="assistant", actor_id=other.id, day="2026-10-03"))
    d = await _ask(org, web(), memory.session_key(prefix="assistant", actor_id=PERSON.id, day="2026-10-04"))
    assert a["session_id"] == b["session_id"]
    assert len({a["session_id"], c["session_id"], d["session_id"]}) == 3


def test_a_thread_id_wins_over_the_daily_key():
    chat = uuid.uuid4()
    assert memory.session_key(chat_id=chat, prefix="agent", actor_id="x", day="2026-01-01") == str(chat)
    assert memory.session_key(chat_id=None, prefix="agent", actor_id="x", day="2026-01-01") == "agent:x:2026-01-01"


# ── who took part ─────────────────────────────────────────────────────────────────────────────────

async def test_the_person_started_the_session_and_the_assistant_handled_it(org):
    res = await _ask(org, web(), memory.session_key(chat_id=uuid.uuid4(), prefix="assistant", actor_id=PERSON.id))
    sid = res["session_id"]
    user_nid = str(actors.node_id(org, PERSON))
    agent_nid = str(actors.node_id(org, actors.assistant()))

    assert {(str(e.source_id), str(e.target_id)) for e in await _edges(org, "STARTED")} == {(user_nid, sid)}
    assert {(str(e.source_id), str(e.target_id)) for e in await _edges(org, "HANDLED_BY")} == {(sid, agent_nid)}
    assert await _edges(org, "OWNED_BY") == []                    # the Assistant is not anyone's key

    user = await NodeRepo().get(org, uuid.UUID(user_nid))
    assert user.type == actors.USER_TYPE and user.label == "ada@example.com"
    assert user.properties["email"] == "ada@example.com" and user.properties["actor_id"] == PERSON.id


async def test_participants_are_shared_across_sessions_not_copied(org):
    for _ in range(3):
        await _ask(org, web(), memory.session_key(chat_id=uuid.uuid4(), prefix="assistant", actor_id=PERSON.id))
    assert len(await NodeRepo().find(org, exact_types=[actors.USER_TYPE])) == 1
    assert len(await NodeRepo().find(org, exact_types=[actors.AGENT_TYPE])) == 1
    assert len(await _edges(org, "STARTED")) == 3 and len(await _edges(org, "HANDLED_BY")) == 3


async def test_an_agent_key_is_owned_by_the_person_who_issued_it(org):
    owner = actors.Actor(kind="user", id=str(uuid.uuid4()), name="admin@example.com")
    key_id = str(uuid.uuid4())
    who = actors.Participants(user=owner, actor_type="agent", actor_id=key_id,
                              agent=actors.Actor(kind="agent", id=key_id, name="Claude Desktop",
                                                 owner_id=owner.id))
    res = await _ask(org, who, memory.session_key(prefix="agent", actor_id=key_id))
    agent_nid, owner_nid = actors.node_id(org, who.agent), actors.node_id(org, owner)

    assert {(e.source_id, e.target_id) for e in await _edges(org, "OWNED_BY")} == {(agent_nid, owner_nid)}
    assert {(e.source_id, str(e.target_id)) for e in await _edges(org, "STARTED")} == {(owner_nid, res["session_id"])}
    s = await NodeRepo().get(org, uuid.UUID(res["session_id"]))
    assert s.label.startswith("Claude Desktop · ") and s.properties["actor_type"] == "agent"


async def test_a_key_with_no_recorded_issuer_names_no_person(org):
    """The old capture named the workspace's first admin for every agent. No issuer means none."""
    key_id = str(uuid.uuid4())
    who = actors.Participants(user=None, actor_type="agent", actor_id=key_id,
                              agent=actors.Actor(kind="agent", id=key_id, name="cursor"))
    res = await _ask(org, who, memory.session_key(prefix="agent", actor_id=key_id))
    assert await _edges(org, "STARTED") == [] and await _edges(org, "OWNED_BY") == []
    assert len(await _edges(org, "HANDLED_BY")) == 1
    s = await NodeRepo().get(org, uuid.UUID(res["session_id"]))
    assert s.properties.get("user") is None and s.properties.get("user_id") is None
    assert await NodeRepo().find(org, exact_types=[actors.USER_TYPE]) == []


async def test_a_mismatched_owner_is_not_linked(org):
    """OWNED_BY only when the session's person IS the key's issuer — never inferred across people."""
    someone = actors.Actor(kind="user", id=str(uuid.uuid4()), name="x@example.com")
    key_id = str(uuid.uuid4())
    who = actors.Participants(user=someone, actor_type="agent", actor_id=key_id,
                              agent=actors.Actor(kind="agent", id=key_id, name="bot", owner_id=str(uuid.uuid4())))
    await _ask(org, who, memory.session_key(prefix="agent", actor_id=key_id))
    assert await _edges(org, "OWNED_BY") == []


def test_participant_ids_are_stable_and_distinct():
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    a = actors.Actor(kind="user", id="same", name="n")
    assert actors.node_id(org_a, a) == actors.node_id(org_a, actors.Actor(kind="user", id="same", name="renamed"))
    assert actors.node_id(org_a, a) != actors.node_id(org_b, a)                       # per workspace
    assert actors.node_id(org_a, a) != actors.node_id(org_a, actors.Actor(kind="agent", id="same", name="n"))


def test_a_client_without_a_key_is_named_by_the_client():
    c = actors.client_agent("  Claude   Desktop ")
    assert c.name == "Claude Desktop" and c.id == "client:claude desktop" and c.kind == "agent"
    assert actors.client_agent(None).name == "agent" and actors.client_agent("").id == "client:agent"
    assert actors.client_agent("Claude Desktop").id != actors.assistant().id


async def test_participants_are_memory_and_never_evidence(org):
    await _ask(org, web(), memory.session_key(chat_id=uuid.uuid4(), prefix="assistant", actor_id=PERSON.id))
    for n in await NodeRepo().find(org, exact_types=[actors.USER_TYPE, actors.AGENT_TYPE]):
        assert not in_scope(n, "knowledge") and in_scope(n, "conversations")
        assert str(n.properties["__internal"]).lower() == "true"


async def test_an_unchanged_participant_is_not_rewritten(org):
    nid = await actors.ensure(org, PERSON)
    version = (await NodeRepo().get(org, nid)).properties["__audit_version"]
    await actors.ensure(org, PERSON)
    assert (await NodeRepo().get(org, nid)).properties["__audit_version"] == version
    await actors.ensure(org, actors.Actor(kind="user", id=PERSON.id, name="ada@new.example"))
    renamed = await NodeRepo().get(org, nid)
    assert renamed.label == "ada@new.example" and renamed.properties["__audit_version"] > version


# ── what an exchange references ───────────────────────────────────────────────────────────────────

async def test_a_turn_references_the_knowledge_its_answer_used(org):
    chunk, record = await _knowledge(org), await _knowledge(org, "row 7", type="Record")
    res = await _ask(org, web(), memory.session_key(chat_id=uuid.uuid4(), prefix="assistant", actor_id=PERSON.id),
                     documents=["coffee-brewing.txt"], node_ids=[str(chunk), record])
    refs = {(str(e.source_id), e.target_id) for e in await _edges(org, "REFERENCES")}
    assert refs == {(res["turn_id"], chunk), (res["turn_id"], record)}
    turn = await NodeRepo().get(org, uuid.UUID(res["turn_id"]))
    assert turn.properties["documents_retrieved"] == ["coffee-brewing.txt"]
    assert turn.properties["query"] == turn.properties["query_to_axiom"] == "How long is an espresso shot?"


async def test_references_skip_what_is_missing_malformed_repeated_or_not_knowledge(org):
    chunk = await _knowledge(org)
    key = memory.session_key(chat_id=uuid.uuid4(), prefix="assistant", actor_id=PERSON.id)
    earlier = await _ask(org, web(), key)
    res = await _ask(org, web(), key, node_ids=[
        str(uuid.uuid4()),            # deleted since, or never existed
        "Chunk#275af418",             # a display label, not an id
        None, "", 42,
        str(chunk), str(chunk),       # repeated
        earlier["turn_id"],           # another exchange: memory, not knowledge
        earlier["session_id"],        # the session itself
    ])
    assert res["references"] == [str(chunk)]
    assert len(await _edges(org, "REFERENCES")) == 1


async def test_references_are_capped(org):
    ids = [await _knowledge(org, f"chunk {i}") for i in range(memory.MAX_REFERENCES + 5)]
    res = await _ask(org, web(), memory.session_key(chat_id=uuid.uuid4(), prefix="assistant", actor_id=PERSON.id),
                     node_ids=ids, documents=[f"d{i}" for i in range(50)])
    assert len(res["references"]) == memory.MAX_REFERENCES
    assert len(res["documents_retrieved"]) == memory.MAX_DOCUMENTS


async def test_an_exchange_with_no_answer_or_evidence_is_still_recorded(org):
    """An abstain is a turn too: what was asked, and that nothing grounded it."""
    res = await memory.capture_exchange(org, who=web(), tool="assistant_chat", query="Unknown?", output=None,
                                        key=memory.session_key(chat_id=uuid.uuid4(), prefix="assistant",
                                                               actor_id=PERSON.id))
    turn = await NodeRepo().get(org, uuid.UUID(res["turn_id"]))
    assert turn.properties["answer"] is None and turn.properties["text"] == "Unknown?"
    assert res["references"] == [] and await _edges(org, "REFERENCES") == []


# ── the Save button ───────────────────────────────────────────────────────────────────────────────

async def test_saving_a_captured_chat_adds_its_summary_and_keeps_what_capture_recorded(org):
    chunk = await _knowledge(org)
    chat = uuid.uuid4()
    key = memory.session_key(chat_id=chat, prefix="assistant", actor_id=PERSON.id)
    res = await _ask(org, web(), key, node_ids=[chunk])

    out = await memory.save_session(org, session_id=str(chat), title="Espresso", distill=False,
                                    summary="Shots take about 25 seconds.", facts=["Espresso ≈ 25 s"],
                                    turns=[{"role": "user", "text": "How long is an espresso shot?"},
                                           {"role": "assistant", "text": "About 25 seconds."}])
    assert out["session_id"] == str(chat) and out["turns"] == 0 and out["facts"] == 1

    sessions = await NodeRepo().find(org, exact_types=["Session"])
    assert [str(s.id) for s in sessions] == [str(chat)]                 # no second copy of the chat
    assert sessions[0].properties["summary"] == "Shots take about 25 seconds."
    assert sessions[0].properties["captured"] is True
    assert await NodeRepo().get(org, uuid.UUID(res["turn_id"])) is not None   # captured turn survives
    assert len(await _edges(org, "REFERENCES")) == 1 and len(await _edges(org, "STARTED")) == 1

    # capture keeps going after a save, and does not undo it
    await _ask(org, web(), key, q="And a lungo?")
    s = await NodeRepo().get(org, chat)
    assert s.properties["summary"] == "Shots take about 25 seconds." and s.properties["status"] == "saved"


async def test_saving_an_uncaptured_session_still_replaces_its_turns(org):
    """Unchanged for a session capture never wrote (capture switched off, or a client's own save)."""
    sid = str(uuid.uuid4())
    await memory.save_session(org, session_id=sid, distill=False, summary="s",
                              turns=[{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}])
    out = await memory.save_session(org, session_id=sid, distill=False, summary="s",
                                    turns=[{"role": "user", "text": "only"}])
    assert out["turns"] == 1
    assert len(await NodeRepo().find(org, exact_types=["Turn"])) == 1


# ── where it sits in the graph view ───────────────────────────────────────────────────────────────

async def _roots(org):
    repo = NodeRepo()
    nodes = [n for n in await repo.find(org) if n.type not in graph_tree.CALENDAR_TYPES]
    edges = [e for e in await repo.edges(org) if e.type not in graph_tree.BOOKKEEPING_EDGES]
    return graph_tree.unconnected_parts(nodes, edges)


async def test_a_chat_that_used_an_asset_sits_under_it_not_beside_the_workspace(org):
    source = await NodeRepo().upsert_node(workspace_id=org, type="Source", label="coffee-brewing.txt",
                                          properties={"filename": "coffee-brewing.txt"})
    chunk = await NodeRepo().upsert_node(workspace_id=org, type="Chunk", label="c1", source_ref=source.id,
                                         properties={"text": "25 seconds"})
    await _ask(org, web(), memory.session_key(chat_id=uuid.uuid4(), prefix="assistant", actor_id=PERSON.id),
               node_ids=[chunk.id])
    assert await _roots(org) == []


async def test_chats_that_used_nothing_form_one_part_per_person_not_one_per_chat(org):
    for _ in range(4):
        await _ask(org, web(), memory.session_key(chat_id=uuid.uuid4(), prefix="assistant", actor_id=PERSON.id))
    roots = await _roots(org)
    assert len(roots) == 1
    assert roots[0]["types"]["Session"] == 4 and roots[0]["types"]["Turn"] == 4


# ── counting respects the question's scope ────────────────────────────────────────────────────────

async def test_counting_a_type_respects_the_scope(org):
    from opencortyx.retrieval.l2_graph import aggregate as agg
    await NodeRepo().upsert_node(workspace_id=org, type="Session", label="ingested", properties={"text": "s"})
    await _ask(org, web(), memory.session_key(chat_id=uuid.uuid4(), prefix="assistant", actor_id=PERSON.id))

    spec = agg.AggregationSpec(function="count", target_type="Session")
    spec.grounded = True
    assert (await agg.execute(org, spec, scope="knowledge")).scalar == 1.0
    assert (await agg.execute(org, spec, scope="conversations")).scalar == 1.0
    assert (await agg.execute(org, spec, scope="all")).scalar == 2.0
    assert (await agg.execute(org, spec)).scalar == 2.0                # unscoped callers: unchanged
