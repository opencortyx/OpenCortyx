"""Request and response models: comments on assets."""
from pydantic import BaseModel


class CommentSpec(BaseModel):
    text: str
