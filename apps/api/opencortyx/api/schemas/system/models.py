"""Request and response models: the model registry for generation, embeddings and reranking."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ModelSpec(BaseModel):
    model_config = {"protected_namespaces": ()}   # `model_id` is a real field, not the model_* namespace
    name: str
    role: str = "generation"               # one of core.inference.registry.MODEL_ROLES
    provider: str = "vllm"                 # vllm | openai | openai_compatible | anthropic | huggingface | fastembed | tei | …
    model_id: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    params: dict = Field(default_factory=dict)
    is_default: bool = False

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        from opencortyx.core.inference.registry import MODEL_ROLES
        if v not in MODEL_ROLES:
            raise ValueError(f"role must be one of {', '.join(MODEL_ROLES)}, got {v!r}")
        return v

    @field_validator("provider")
    @classmethod
    def _provider(cls, v):
        from opencortyx.core.inference.registry import KNOWN_PROVIDERS
        if (v or "").lower() not in KNOWN_PROVIDERS:
            raise ValueError(f"provider must be one of {', '.join(sorted(KNOWN_PROVIDERS))}, got {v!r}")
        return v


class ModelDiscover(BaseModel):
    model_config = {"protected_namespaces": ()}   # `model_id` is a real field, not the model_* namespace
    provider: str = "vllm"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model_id: Optional[str] = None          # an existing Model row → reuse its stored key/base
