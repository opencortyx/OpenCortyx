"""Request and response models: agent memory: sessions, decisions, facts and preferences."""
from typing import List, Optional

from pydantic import BaseModel


class SaveSessionSpec(BaseModel):
    session_id: Optional[str] = None       # reuse to checkpoint the SAME session (idempotent upsert)
    title: Optional[str] = None
    agent: Optional[str] = None
    project: Optional[str] = None
    participants: Optional[List[str]] = None
    summary: Optional[str] = None
    turns: Optional[List[dict]] = None     # [{role,text}] → LLM-distilled if no decisions/facts given
    decisions: Optional[List[dict]] = None
    facts: Optional[List[str]] = None
    preferences: Optional[List[str]] = None
    refs: Optional[List[str]] = None       # data node ids the session used / produced
    distill: Optional[bool] = None         # False → skip LLM distillation (live per-turn checkpoints)
    # rich session-perspective attributes stored ON the Session node (queryable + lineage-visible)
    key_points: Optional[List[str]] = None         # bullet summary of the chat
    checkpoints: Optional[List[dict]] = None       # [{label, detail, at}] — milestones / "what we built"
    highlights: Optional[List[str]] = None         # artifacts produced (files, features, decisions)
    tags: Optional[List[str]] = None
    attributes: Optional[dict] = None              # free-form — anything from the session's POV


class RememberSpec(BaseModel):
    text: str
    kind: Optional[str] = "fact"           # decision | fact | preference
    session_id: Optional[str] = None
    refs: Optional[List[str]] = None
    scope: Optional[str] = "global"


class ActivitySpec(BaseModel):
    tool: str
    query: Optional[str] = None              # query_to_axiom
    documents: Optional[List[str]] = None    # documents_retrieved (citation labels/ids)
    # The node ids behind `documents`: each that exists becomes a Turn -REFERENCES-> node edge.
    # Separate from `documents` because a display label cannot be linked and an id cannot be read.
    document_ids: Optional[List[str]] = None
    output: Optional[str] = None             # short summary for non-retrieval tools
    agent: Optional[str] = None
