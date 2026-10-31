# Models

OpenCortyx uses a model for each of these roles:

- **Generation** (answers, planning), **Extraction** and **Utility**: a model server you connect.
  Extraction and Utility use the Generation model unless you give them their own.
- **Embedding**: `fastembed` inside the API and workers, on CPU. No server needed.
- **Rerank** (optional): a cross-encoder inside the API, on CPU. Without one, retrieval ranks by its
  other signals.

Models are chosen in the dashboard under **Models**, and nowhere else: not in `.env`, and OpenCortyx
does not ship or start a model server of its own. For each role, **Add** a model (provider, Model ID,
server address, API key) and tick **Make default**; the default is what that role uses everywhere,
and API keys are stored encrypted. **Test** checks that the server answers, and the **Model server**
panel shows which names the server actually serves. `bin/opencortyx doctor` checks each workspace's
generation model from the command line.

A new workspace starts with the embedding and rerank models and a small generation model,
**Qwen3-0.6B**, all running on CPU inside OpenCortyx: the Assistant and extraction work from the
first file. It is slow on long contexts and weak at arithmetic and multi-step questions; connect a
larger model below for better answers, and make it the default for generation and extraction.

## Ollama, LM Studio, vLLM or another server you run

| Field | Value |
|---|---|
| Provider | OpenAI-compatible server |
| Server address | `http://host.docker.internal:11434/v1` (Ollama) · `:1234/v1` (LM Studio) · your vLLM's `/v1` |
| Model ID | the name the server lists (`ollama list`, or pick it from the list in the form) |

Inside Docker, a server on the same computer is `host.docker.internal`, not `localhost`. The server
must listen on an address reachable from containers: with Docker Desktop (macOS, Windows) the default
works; on Linux set `OLLAMA_HOST=0.0.0.0`. Keep the `/v1` suffix.

Ollama's default context window (2048 or 4096 tokens, depending on the version) is smaller than the
8192 tokens OpenCortyx sends, and Ollama cuts off longer prompts without an error. Ollama does not
report its window, so OpenCortyx cannot detect this. Either raise Ollama's window with
`OLLAMA_CONTEXT_LENGTH=8192` (newer versions), or create a model with it built in (any version):

```bash
printf 'FROM llama3.2\nPARAMETER num_ctx 8192\n' > Modelfile
ollama create llama3.2-8k -f Modelfile     # then use llama3.2-8k as the Model ID
```

## Download a model from Hugging Face

A model that is not on this machine yet can be downloaded while you add it: on the **Models** page,
press **Add**, pick the provider, type the Model ID, and press **Download** under it. When it is
done, **Save** the model as usual. This works for every role: embedders and rerankers come from
fastembed (see [Embeddings](#embeddings) and [Reranker](#reranker)). For generation, extraction and
utility, choose where the model runs with the provider:

| | Ollama | OpenCortyx (CPU) |
|---|---|---|
| Speed | Fast; uses your GPU when it has one | Slow: a 0.5B model writes a few words a second |
| Provider | OpenAI-compatible server, with Ollama's address | Hugging Face model in OpenCortyx (CPU) |
| Install | [Ollama](https://ollama.com/download) on this computer | Nothing |
| Model | A GGUF repository, e.g. `Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M`, or an Ollama name such as `qwen2.5:1.5b` | A text-generation repository with safetensors weights, e.g. `Qwen/Qwen2.5-0.5B-Instruct` |
| Stored in | Ollama's own model folder | The `model_cache` Docker volume |

**Ollama** pulls the GGUF files from Hugging Face itself (`hf.co/<repository>`); the button reads
**Download into Ollama** when the server does not have the Model ID yet. Keep the form open until
it finishes; if it stops, download again and it resumes.

**OpenCortyx (CPU)** downloads the weights into the model cache that the API and the workers share,
and runs them with `transformers`. The form shows the download size and the memory the model needs.
Each service that generates (the API for answers, the workers for extraction) loads
its own copy, and the API's memory limit is 3 GB by default (`API_MEM_LIMIT` in `.env`), so keep to
small models. Gated models need a Hugging Face token with access; it is used for that download only
and not stored. Downloaded models are listed above the Model ID; **Delete files** frees the disk.

## Hugging Face Inference

Choose **HuggingFace Inference**, enter a Hugging Face access token (with the *Make calls to Inference
Providers* permission), and pick a Model ID from the list, for example `Qwen/Qwen3-4B-Instruct-2507`.
Requests go through Hugging Face's OpenAI-compatible router (`https://router.huggingface.co/v1`);
leave the address empty. To use your own Text Generation Inference server instead, enter its `/v1`
address.

## Other hosted providers

Choose OpenAI, Anthropic or Groq as the provider, enter the API key and the Model ID (for example
`gpt-4o-mini`), and leave the address empty. For Together, Azure and other OpenAI-compatible APIs,
choose OpenAI-compatible server and enter the provider's address.

## Embeddings

A new installation uses `sentence-transformers/all-MiniLM-L6-v2` on `fastembed` (384 dimensions),
which the image carries. An installation from before 0.1.0-beta.7 keeps `nomic-ai/nomic-embed-text-v1.5`
(768 dimensions) and its vectors. To use another, **Add** under Embedding: the provider is fastembed, and the list shows every model it can
run with its vector size; pick one and press **Download**. Changing the embedding model requires
re-indexing existing data, and a model with a different vector size also needs `EMBED_DIMENSIONS`
in `.env` to match (the form refuses one that does not).

## Reranker

A reranker reads the question and each retrieved passage together and improves the order of the
results. **Add** under Reranker, keep the provider fastembed, pick a model (for example
`Xenova/ms-marco-MiniLM-L-6-v2`, 80 MB) and press **Download**, then make it the default. Retrieval
uses the workspace's default reranker from then on. The seeded `BAAI/bge-reranker-v2-m3` row runs on
sentence-transformers, which the API image includes only when built with `INSTALL_RERANK=1` in
`.env` and `bin/opencortyx up --build`; without it that row is skipped.
