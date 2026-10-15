# Models

OpenCortyx uses a model for each of these roles:

- **Generation** (answers, planning), **Extraction** and **Utility**: any OpenAI-compatible endpoint
  or hosted provider.
- **Embedding**: `fastembed` inside the API and workers, on CPU. No GPU or server needed.
- **Rerank**: a local cross-encoder, or the generation model when the cross-encoder is not installed.

Models are chosen in the dashboard under **Models**, not in `.env`. For each role, **Add** a model
(provider, Model ID, Base URL, API key) and tick **Make default**; the default is what that role
uses everywhere, and API keys are stored encrypted. **Test** checks that the server answers, and
the **Model server** panel shows which names the server actually serves. `bin/opencortyx doctor`
checks the default generation and embedding models from the command line.

A new workspace starts with built-in rows for every role. They point at the local vLLM server, so
without an NVIDIA GPU, edit the generation, extraction and utility rows first.

## Local GPU with vLLM

Requires an NVIDIA GPU and the NVIDIA container toolkit (on Windows, Docker Desktop with the WSL2
backend). `bin/opencortyx init` enables the vLLM server when it detects a GPU, by setting
`COMPOSE_PROFILES=gpu` in `.env`. The built-in rows already point at it:

| Field | Value |
|---|---|
| Provider | vLLM |
| Base URL | `http://vllm:8000/v1` |
| Model ID | `opencortyx-llm` (the name vLLM serves the model under) |

vLLM loads one model, chosen when the server starts. To serve a different one, set these in `.env`
and run `bin/opencortyx up`:

- `VLLM_MODEL`: the Hugging Face repository to download (default `Qwen/Qwen2.5-1.5B-Instruct-AWQ`).
  About 1.5B parameters (4-bit) fits 4 GB, 7-8B (4-bit) fits 8-12 GB. Set `HUGGING_FACE_HUB_TOKEN`
  for gated models.
- `VLLM_GPU_FRACTION`: GPU memory vLLM may reserve. `bin/opencortyx up` sizes it from free memory.
- `LLM_MAX_MODEL_LEN`: the context window (default 8192). Larger values use more GPU memory.

The first start downloads the weights into the `model_cache` volume.

## Ollama, LM Studio or another local server

| Field | Value |
|---|---|
| Provider | OpenAI-compatible |
| Base URL | `http://host.docker.internal:11434/v1` (LM Studio: port `1234`) |
| Model ID | the name from `ollama list` |

The server must listen on an address reachable from containers. With Docker Desktop (macOS,
Windows) the default works; on Linux set `OLLAMA_HOST=0.0.0.0`. Keep the `/v1` suffix.

Ollama's default context window (2048 or 4096 tokens, depending on the version) is smaller than the
8192 tokens OpenCortyx sends, and Ollama cuts off longer prompts without an error. Unlike vLLM,
Ollama does not report its window, so OpenCortyx cannot detect this. Either raise Ollama's window
with `OLLAMA_CONTEXT_LENGTH=8192` (newer versions), or create a model with it built in (any
version):

```bash
printf 'FROM llama3.2\nPARAMETER num_ctx 8192\n' > Modelfile
ollama create llama3.2-8k -f Modelfile     # then use llama3.2-8k as the Model ID
```

## Hosted providers

Choose OpenAI, Anthropic, Groq or HuggingFace Inference as the provider, enter the API key and the
Model ID (for example `gpt-4o-mini`), and leave Base URL empty. For Together, Azure and other
OpenAI-compatible APIs, choose OpenAI-compatible and enter the provider's Base URL.

## Embeddings

The default is `nomic-ai/nomic-embed-text-v1.5` on `fastembed` (768 dimensions). Changing the
embedding model requires re-indexing existing data, and a model with a different dimension also
needs `EMBED_DIMENSIONS` in `.env` to match.

## Reranker

A local cross-encoder reranker improves retrieval ranking. It adds about 200 MB to the API image,
so it is built in only with `INSTALL_RERANK=1` in `.env` and `bin/opencortyx up --build`.
