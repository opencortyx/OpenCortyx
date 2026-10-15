"""Request and response models: grounded context retrieval and cited answers."""
from typing import List, Optional

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


class ContextQuery(BaseModel):
    query: str
    collection_ids: Optional[List[str]] = None
    # ── per-query retrieval config (the Chat UI sends these) ──
    retrievers: Optional[List[str]] = None       # e.g. ["structured","bm25","lexical"]; None → profile
    rerank: Optional[List[str]] = None           # ["cross"] | ["cross","llm"] | [] (none); None → profile
    k: Optional[int] = Field(None, ge=0, le=200)             # per-retriever top-k
    abstain_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)  # lower = answers more
    # ── bitemporal "as-of" queries ──
    as_of: Optional[str] = None                  # valid-time: facts true as of this ISO date
    as_of_ingest: Optional[str] = None           # transaction-time: what we knew by this ISO date
    fast: Optional[bool] = None                  # low-latency mode: skip rerank + CRAG eval
    decompose: Optional[bool] = False            # break complex questions into multi-query sub-searches
    # WHAT to search — "knowledge" (your ingested data, the default), "conversations" (this app's own
    # Sessions/Turns/Decisions/Facts), or "all". See RetrieveContext.scope.
    scope: Optional[str] = None
    # Chat history (the Assistant sidebar). `save_chat` records this turn in the caller's history —
    # in `chat_id` when they own it, else in a new chat whose id comes back with the answer. A saved
    # chat is also the conversation follow-ups are resolved against ("what about the other one?").
    chat_id: Optional[str] = None
    save_chat: Optional[bool] = None

    @field_validator("query")
    @classmethod
    def _query(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("a question is required")
        return v

    @field_validator("as_of", "as_of_ingest")
    @classmethod
    def _iso(cls, v):
        if v in (None, ""):
            return None
        try:
            (datetime.fromisoformat if "T" in v else date.fromisoformat)(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"must be an ISO date such as 2026-03-31, got {v!r}")
        return v
