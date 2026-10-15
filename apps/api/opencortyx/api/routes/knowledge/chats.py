"""Routes: the Assistant's chat history — the conversations in its sidebar.

Turns are written by the answer endpoints (`save_chat` / `chat_id` on the question), so these only
list, open, rename and delete. Every chat is private to the user who started it.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from opencortyx.core.security.auth import CurrentUser, require_auth
from opencortyx.knowledge import chat_history

router = APIRouter()


class ChatSpec(BaseModel):
    title: Optional[str] = None


@router.get("/chats")
async def list_chats(q: str = "", limit: int = 50, offset: int = 0,
                     user: CurrentUser = Depends(require_auth)):
    """The caller's chats, most recently active first. `q` searches titles and what was said."""
    return await chat_history.list_threads(user.workspace_id, user.user_id, q=q, limit=limit, offset=offset)


@router.post("/chats")
async def create_chat(spec: ChatSpec, user: CurrentUser = Depends(require_auth)):
    return await chat_history.create_thread(user.workspace_id, user.user_id, spec.title)


@router.get("/chats/{chat_id}")
async def get_chat(chat_id: str, user: CurrentUser = Depends(require_auth)):
    """One chat with every turn, each answer carrying what was shown under it."""
    t = await chat_history.get_thread(user.workspace_id, user.user_id, chat_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return t


@router.patch("/chats/{chat_id}")
async def rename_chat(chat_id: str, spec: ChatSpec, user: CurrentUser = Depends(require_auth)):
    t = await chat_history.rename_thread(user.workspace_id, user.user_id, chat_id, spec.title or "")
    if t is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return t


@router.delete("/chats/{chat_id}")
async def delete_chat(chat_id: str, user: CurrentUser = Depends(require_auth)):
    if not await chat_history.delete_thread(user.workspace_id, user.user_id, chat_id):
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"deleted": True}
