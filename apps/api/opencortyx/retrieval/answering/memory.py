"""Conversation context for the answer path: rewrite a follow-up into a standalone question from the
turns before it.

The turns are read from the chat thread (`knowledge.chat_history`), the conversation's one source of
truth. This module used to keep its own copy as graph `Turn` nodes under a `Session` keyed by the
thread id, a second record of the same chat beside the one `knowledge.memory.capture_exchange`
writes. Two writers for one conversation drifted: the copies differed in shape, neither linked to
the other, and both floated unconnected in the graph view. Recording a chat into the graph is
capture's job alone; this module only reads.
"""
import re
import uuid

from opencortyx.core.inference.llm import complete

#: Inline citation markers a generator may append, e.g. "[Type#<uuid>]" or "[Project: X]".
_CITE_MARK = re.compile(r"\[[^\]\n]{0,80}?[#:][^\]\n]{0,80}?\]")


def _clean_turn(text: str) -> str:
    """A turn as PROSE — citation markers and reasoning tags removed."""
    t = re.sub(r"<think>.*?</think>", " ", text or "", flags=re.I | re.S)
    t = _CITE_MARK.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


async def recent_turns(workspace_id: uuid.UUID, conv_id, limit: int = 6) -> list:
    """The conversation's last turns before the current question, oldest→newest, as [(role, text)].

    `workspace_id` is unused — a thread id is globally unique — and kept so callers need not change."""
    if conv_id is None:
        return []
    from opencortyx.knowledge import chat_history
    return await chat_history.prior_messages(conv_id, limit=limit)


async def contextualize(query: str, workspace_id: uuid.UUID, conv_id) -> str:
    """Rewrite a follow-up into a STANDALONE question using the conversation so far.

    Retrieval embeds the question as written. "Which vendor does that project use?" embeds nothing
    about Payments Core Rewrite, so no retriever — dense, lexical, or graph — can find the project,
    and the engine abstains on a question it had already answered the subject of one turn earlier.
    Putting prior turns INTO the context does not fix that: by then retrieval has already run and
    returned the wrong nodes. The reference has to be resolved BEFORE the query is embedded.

    Returns the original query unchanged when there is no history or the rewrite is unusable, so a
    failed rewrite can only cost a follow-up its resolution, never corrupt a standalone question.
    """
    hist = await recent_turns(workspace_id, conv_id, limit=6)
    if not hist:
        return query
    convo = "\n".join(f"{r}: {_clean_turn(t)[:300]}" for r, t in hist)
    try:
        from opencortyx.core.inference.registry import resolve_model
        out = await complete(
            f"Conversation so far:\n{convo}\n\nFollow-up question: {query}\n\n"
            "Rewrite the follow-up as a STANDALONE question by replacing every pronoun and "
            "reference ('it', 'he', 'she', 'that project', 'those') with the specific name it "
            "refers to. Change nothing else. If it is already standalone, repeat it verbatim. "
            "Reply with the question only.",
            system="You rewrite follow-up questions into self-contained ones.",
            max_tokens=120, temperature=0.0,
            model_spec=await resolve_model(workspace_id, "utility"))
    except Exception:
        return query
    out = (out or "").strip().strip('"').split("\n")[0].strip()
    # guard against a model that explains itself instead of answering
    if not out or len(out) > 400 or not out.endswith("?"):
        return query
    return out
