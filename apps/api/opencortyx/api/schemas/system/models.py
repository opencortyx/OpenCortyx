"""Request and response models: the model registry for generation, embeddings and reranking."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ModelSpec(BaseModel):
    model_config = {"protected_namespaces": ()}   # `model_id` is a real field, not the model_* namespace
    name: str
    role: str = "generation"               # one of core.inference.registry.MODEL_ROLES
    provider: str = "openai_compatible"    # openai_compatible (Ollama, LM Studio, vLLM, ...) | openai | anthropic | huggingface | groq | transformers | fastembed | sentence-transformers
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

    @model_validator(mode="after")
    def _text_only(self):
        from opencortyx.core.config import LOCAL_LLM_PROVIDERS
        if self.provider.lower() in LOCAL_LLM_PROVIDERS and self.role in ("embedding", "rerank"):
            raise ValueError("a downloaded Hugging Face model runs generation, extraction or "
                             "utility; embeddings and reranking use fastembed / sentence-transformers")
        return self


class ModelDiscover(BaseModel):
    model_config = {"protected_namespaces": ()}   # `model_id` is a real field, not the model_* namespace
    provider: str = "openai_compatible"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model_id: Optional[str] = None          # an existing Model row → reuse its stored key/base
    role: Optional[str] = None              # fastembed lists embedders or rerankers by role


class HFDownload(BaseModel):
    """A Hugging Face model to download into OpenCortyx's model cache."""
    repo: str                               # e.g. Qwen/Qwen2.5-0.5B-Instruct
    token: Optional[str] = None             # only for gated models; used for this download, not stored


class FastembedDownload(BaseModel):
    """A fastembed (ONNX, CPU) embedder or reranker to download ahead of use."""
    model: str                              # e.g. BAAI/bge-small-en-v1.5, Xenova/ms-marco-MiniLM-L-6-v2
    role: str = "embedding"                 # embedding | rerank


class OllamaPull(BaseModel):
    """A model for an Ollama server to pull: a Hugging Face GGUF repository or an Ollama library name."""
    base_url: str                           # the Ollama connection's address, as on the Models page
    model: str                              # Qwen/Qwen2.5-1.5B-Instruct-GGUF[:Q4_K_M] or qwen2.5:1.5b
