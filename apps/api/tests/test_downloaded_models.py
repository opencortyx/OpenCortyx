"""Hugging Face models run on this machine: in OpenCortyx itself (`transformers`), or pulled into Ollama.

These run offline: the Hub, Ollama and the model weights are all replaced, because what is being
pinned is the routing and the refusals, not the network.
"""
import asyncio

import pytest

from opencortyx.api.routes.system.models import _ollama_name
from opencortyx.api.schemas.system.models import ModelSpec
from opencortyx.core.inference import hf_hub, llm, local_llm
from opencortyx.core.inference.registry import KNOWN_PROVIDERS, NoModelConfigured


def test_a_downloaded_model_is_a_provider_with_no_address():
    assert "transformers" in KNOWN_PROVIDERS
    spec = {"provider": "transformers", "model_id": "Qwen/Qwen2.5-0.5B-Instruct", "role": "generation"}
    assert llm._require(spec)[0] == "transformers"          # no server address is asked for


def test_complete_and_stream_run_a_downloaded_model_in_process(monkeypatch):
    seen = []

    async def fake_complete(model_id, prompt, system=None, max_tokens=256, temperature=0.2):
        seen.append(("complete", model_id))
        return "ok"

    async def fake_stream(model_id, prompt, system=None, max_tokens=256, temperature=0.2):
        seen.append(("stream", model_id))
        for t in ("o", "k"):
            yield t

    async def no_server(*a, **k):
        raise AssertionError("a downloaded model must not be sent to an HTTP endpoint")

    monkeypatch.setattr(local_llm, "complete", fake_complete)
    monkeypatch.setattr(local_llm, "stream", fake_stream)
    monkeypatch.setattr(llm, "_openai_complete", no_server)
    spec = {"provider": "transformers", "model_id": "org/tiny"}

    async def run():
        whole = await llm.complete("hi", model_spec=spec)
        parts = [c async for c in llm.stream_complete("hi", model_spec=spec)]
        return whole, "".join(parts)

    assert asyncio.run(run()) == ("ok", "ok")
    assert seen == [("complete", "org/tiny"), ("stream", "org/tiny")]


def test_a_model_that_is_not_downloaded_says_so_instead_of_downloading(monkeypatch):
    """Loading reads the cache only: a chat request must never start a multi-GB download."""
    import transformers

    calls = []

    def from_pretrained(model_id, **kw):
        calls.append(kw.get("local_files_only"))
        raise OSError("not in the cache")

    monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", from_pretrained)
    monkeypatch.setattr(local_llm, "_loaded", {})
    with pytest.raises(NoModelConfigured, match="not downloaded yet"):
        local_llm._load("org/never-downloaded")
    assert calls == [True]


def test_only_text_roles_take_a_downloaded_model():
    ModelSpec(name="q", role="generation", provider="transformers", model_id="org/m")
    ModelSpec(name="q", role="extraction", provider="transformers", model_id="org/m")
    for role in ("embedding", "rerank"):
        with pytest.raises(ValueError, match="embeddings and reranking"):
            ModelSpec(name="q", role=role, provider="transformers", model_id="org/m")


def test_only_the_files_transformers_loads_are_downloaded():
    for name in ("config.json", "model.safetensors", "model-00001-of-00002.safetensors",
                 "tokenizer.json", "tokenizer.model", "merges.txt", "chat_template.jinja"):
        assert hf_hub._wanted(name), name
    for name in ("model.gguf", "pytorch_model.bin", "onnx/model.onnx", "README.md", "consolidated.pth"):
        assert not hf_hub._wanted(name), name


class _Sibling:
    def __init__(self, name, size):
        self.rfilename, self.size = name, size


class _Info:
    def __init__(self, task, files, gated=False):
        self.id, self.pipeline_tag, self.gated = "org/m", task, gated
        self.siblings = [_Sibling(n, s) for n, s in files]


@pytest.mark.parametrize("task,files,runs,why", [
    ("text-generation", [("config.json", 1_000), ("model.safetensors", 1_000_000_000)], True, None),
    ("text-generation", [("model-q4_k_m.gguf", 900_000_000)], False, "download it into Ollama"),
    ("feature-extraction", [("model.safetensors", 500)], False, "not a text-generation one"),
])
def test_info_says_whether_a_model_can_run_in_opencortyx(monkeypatch, task, files, runs, why):
    import huggingface_hub

    monkeypatch.setattr(huggingface_hub.HfApi, "model_info",
                        lambda self, repo, files_metadata=False, token=None: _Info(task, files))
    got = hf_hub.info("org/m")
    assert got["runs_in_opencortyx"] is runs
    if why:
        assert why in got["why_not"]
    else:
        assert got["bytes"] == 1_000_001_000 and got["memory_bytes"] > got["bytes"]


def test_start_refuses_a_model_that_cannot_run_here(monkeypatch):
    monkeypatch.setattr(hf_hub, "info", lambda repo, token=None: {
        "repo": repo, "runs_in_opencortyx": False, "why_not": "it has no safetensors weights"})
    with pytest.raises(ValueError, match="cannot run in OpenCortyx"):
        hf_hub.start("org/gguf-only")


@pytest.mark.parametrize("typed,name", [
    ("Qwen/Qwen2.5-1.5B-Instruct-GGUF", "hf.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF"),
    ("Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M", "hf.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M"),
    ("https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF", "hf.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF"),
    ("hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF", "hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF"),
    ("qwen2.5:1.5b", "qwen2.5:1.5b"),
])
def test_ollama_pulls_a_hugging_face_repository_by_its_hf_co_name(typed, name):
    assert _ollama_name(typed) == name


# ── Embedding and reranking: fastembed models, downloaded ahead of use ────────────────────────────

def test_fastembed_lists_embedders_and_rerankers_by_role():
    from opencortyx.core.inference import fastembed_models
    embedders = {m["id"]: m for m in fastembed_models.catalogue("embedding")}
    rerankers = {m["id"]: m for m in fastembed_models.catalogue("rerank")}
    assert embedders["nomic-ai/nomic-embed-text-v1.5"]["dim"] == 768
    assert "Xenova/ms-marco-MiniLM-L-6-v2" in rerankers and "Xenova/ms-marco-MiniLM-L-6-v2" not in embedders
    with pytest.raises(ValueError, match="not a fastembed reranker"):
        fastembed_models.start("rerank", "nomic-ai/nomic-embed-text-v1.5")


def test_an_embedder_must_make_vectors_the_size_of_the_index():
    from fastapi import HTTPException

    from opencortyx.api.routes.system.models import _check_dimensions
    from opencortyx.core.config import settings

    ok = "nomic-ai/nomic-embed-text-v1.5" if settings.EMBED_DIMENSIONS == 768 else None
    if ok:
        _check_dimensions(ModelSpec(name="e", role="embedding", provider="fastembed", model_id=ok))
    with pytest.raises(HTTPException, match="EMBED_DIMENSIONS=384"):
        _check_dimensions(ModelSpec(name="e", role="embedding", provider="fastembed",
                                    model_id="BAAI/bge-small-en-v1.5"))


def test_a_fastembed_reranker_scores_pairs_like_a_cross_encoder(monkeypatch):
    from opencortyx.core.inference import embedding

    calls = []

    class FakeModel:
        def rerank(self, query, docs):
            calls.append((query, list(docs)))
            return [2.0 if "Paris" in d else -2.0 for d in docs]

    ce = embedding._FastembedCrossEncoder.__new__(embedding._FastembedCrossEncoder)
    ce.model = FakeModel()
    got = ce.predict([("capital?", "Paris is the capital"), ("capital?", "Bananas are yellow"),
                      ("fruit?", "Bananas are yellow")])
    assert calls == [("capital?", ["Paris is the capital", "Bananas are yellow"]),
                     ("fruit?", ["Bananas are yellow"])]
    assert got[0] > 0.8 and got[1] < 0.2 and all(0.0 <= v <= 1.0 for v in got)


@pytest.mark.parametrize("provider,installed,used", [
    ("fastembed", False, True),
    ("sentence-transformers", False, False),     # this image cannot run it: rank without it
    ("sentence-transformers", True, True),
    ("openai_compatible", True, False),          # not an in-process reranker
])
def test_retrieval_uses_the_registry_reranker_when_it_can_run(monkeypatch, provider, installed, used):
    import uuid

    from opencortyx.core.inference import embedding, registry
    from opencortyx.retrieval.orchestrator import RetrievalWorkflow

    async def fake_resolve(workspace_id, role, model_id=None):
        assert role == "rerank"
        return {"provider": provider, "model_id": "some/reranker"}

    monkeypatch.setattr(registry, "resolve_model", fake_resolve)
    monkeypatch.setattr(embedding, "cross_encoder_installed", lambda: installed)
    orch = RetrievalWorkflow.__new__(RetrievalWorkflow)
    got = asyncio.run(orch._registry_reranker(uuid.uuid4()))
    assert got == (("some/reranker", provider) if used else (None, None))
    assert asyncio.run(orch._registry_reranker(None)) == (None, None)
