"""Request and response models: evaluation cases, runs and answer feedback."""
from typing import List, Optional

from pydantic import BaseModel, field_validator


class EvalCaseSpec(BaseModel):
    question: str

    @field_validator("question")
    @classmethod
    def _question(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("question is required")
        return v
    expect_keywords: Optional[List[str]] = None
    should_abstain: bool = False


class FeedbackSpec(BaseModel):
    query: str
    rating: str                       # up | down
    answer: Optional[str] = None
    correction: Optional[str] = None
