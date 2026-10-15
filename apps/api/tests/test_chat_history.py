"""The Assistant's chat history — what the sidebar relies on, against the live stack.

A question asked with `save_chat` lands in the asker's history with its answer; asking again with
that `chat_id` continues the same chat; the chat can be listed, searched, reopened turn by turn,
renamed and deleted; and nobody else — not even another user in the same org — can see it.

Uses `/context` (raw context mode) so no language model is needed: an empty tenant abstains at once,
and an abstain is still a turn the history must keep.
"""
import uuid

from tests.conftest import mint_token


def _ask(client, headers, q, chat_id=None):
    r = client.post("/context", headers=headers, json={"query": q, "save_chat": True, "chat_id": chat_id})
    assert r.status_code == 200, r.text
    return r.json()["chat_id"]


def test_a_saved_question_is_a_chat_that_can_be_reopened_and_continued(client, tenant_headers):
    headers, _ = tenant_headers
    cid = _ask(client, headers, "Which vendors supply the payments project?")
    assert _ask(client, headers, "And when does their contract end?", cid) == cid

    listed = client.get("/chats", headers=headers).json()
    assert [t["id"] for t in listed["items"]] == [cid]
    assert listed["items"][0]["title"] == "Which vendors supply the payments project?"
    assert listed["items"][0]["message_count"] == 4

    chat = client.get(f"/chats/{cid}", headers=headers).json()
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant", "user", "assistant"]
    assert chat["messages"][2]["text"] == "And when does their contract end?"
    assert chat["messages"][1]["meta"]["abstained"] is True   # what the badge under it renders from

    assert [t["id"] for t in client.get("/chats", headers=headers,
                                        params={"q": "contract end"}).json()["items"]] == [cid]
    assert client.get("/chats", headers=headers, params={"q": "zebra"}).json()["items"] == []

    assert client.patch(f"/chats/{cid}", headers=headers, json={"title": "Vendors"}).json()["title"] == "Vendors"
    assert client.delete(f"/chats/{cid}", headers=headers).status_code == 200
    assert client.get(f"/chats/{cid}", headers=headers).status_code == 404
    assert client.get("/chats", headers=headers).json()["items"] == []


def test_a_chat_is_private_to_the_user_who_started_it(client, tenant_headers):
    headers, workspace_id = tenant_headers
    cid = _ask(client, headers, "Private question")
    colleague = {"Authorization": f"Bearer {mint_token(workspace_id)}", "X-OpenCortyx-Capture": "client"}

    assert client.get("/chats", headers=colleague).json()["items"] == []
    assert client.get(f"/chats/{cid}", headers=colleague).status_code == 404
    assert client.delete(f"/chats/{cid}", headers=colleague).status_code == 404
    # Someone else's chat id does not let you write into it — the question starts a chat of your own.
    assert _ask(client, colleague, "Sneaking in", cid) != cid
    assert client.get(f"/chats/{cid}", headers=headers).json()["message_count"] == 2


def test_questions_are_not_saved_unless_asked(client, tenant_headers):
    headers, _ = tenant_headers
    r = client.post("/context", headers=headers, json={"query": "Not kept"})
    assert r.status_code == 200 and "chat_id" not in r.json()
    assert client.get("/chats", headers=headers).json()["items"] == []
    assert client.get(f"/chats/{uuid.uuid4()}", headers=headers).status_code == 404


async def test_follow_ups_read_the_messages_before_the_question_being_answered(_require_stack):
    """`prior_messages` is what follow-up resolution reads now that the thread is the conversation's
    only record: the question just appended is NOT history, and the window is the last N before it."""
    from opencortyx.knowledge import chat_history as ch

    org, user = uuid.uuid4(), uuid.uuid4()
    tid = await ch.open_for_question(org, user, None, "Who leads Atlas?")
    try:
        assert await ch.prior_messages(tid) == []                    # first question: no history
        await ch.append_answer(tid, "Marek Nowak.")
        await ch.open_for_question(org, user, tid, "What is its biggest risk?")
        assert await ch.prior_messages(tid) == [("user", "Who leads Atlas?"), ("assistant", "Marek Nowak.")]
        assert await ch.prior_messages(tid, limit=1) == [("assistant", "Marek Nowak.")]
        assert await ch.prior_messages(tid, limit=0) == []
        await ch.append_answer(tid, "Settlement mismatch.")          # answered: nothing is pending
        assert (await ch.prior_messages(tid))[-1] == ("assistant", "Settlement mismatch.")
        assert await ch.prior_messages("not-a-uuid") == [] and await ch.prior_messages(uuid.uuid4()) == []
    finally:
        await ch.delete_thread(org, user, tid)
