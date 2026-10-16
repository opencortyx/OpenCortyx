# Troubleshooting

Start with:

```bash
opencortyx doctor
opencortyx status
opencortyx logs <service>     # api, worker, extraction-worker, web, mcp, postgres
```

## Installation

| Symptom | Fix |
|---|---|
| `Docker is not running` | Start Docker Desktop or `sudo systemctl start docker`, then rerun the installer |
| `docker: command not found` while Docker Desktop is running (macOS) | The CLI is not on `PATH`. See [macos.md](macos.md#docker-command-line) |
| `Docker Compose v2 is required` although Docker Desktop is installed (macOS) | The plugin links in `~/.docker/cli-plugins` point to the installer disk image. Relink them as shown in [macos.md](macos.md#docker-command-line) |
| `POSTGRES_PASSWORD is not set` | Run `opencortyx init` in the installation directory |
| Port 3000, 8000 or 8001 already in use | Set `OPENCORTYX_WEB_PORT`, `OPENCORTYX_API_PORT` or `OPENCORTYX_MCP_PORT` in `.env`, then `opencortyx up` |
| Build fails with `ERESOLVE` during `npm ci` | You changed web dependencies with a newer npm; reinstall them with Node 20 (`apps/web/.nvmrc`) |

## Model server

| Symptom | Fix |
|---|---|
| `doctor`: model `…` not served | The generation model's Model ID on the **Models** page must match a name the server serves (the **Model server** panel lists them). For Ollama run `ollama list` |
| `doctor` or the AI Assistant: no generation model is connected | Add one on the **Models** page and tick **Make default**. A new workspace starts without one |
| `doctor`: LLM unreachable, local Ollama | Linux: Ollama must listen on `0.0.0.0` (see [linux.md](linux.md)). All platforms: the server address on the **Models** page uses `host.docker.internal`, not `localhost`, and ends with `/v1` |
| A model row still points at `http://vllm:8000/v1` | The bundled vLLM server was removed; upgrading deletes those rows. Add the model server you use on the **Models** page |
| With Ollama, long documents extract poorly or answers ignore part of the context | Ollama's context window is smaller than the 8192 tokens OpenCortyx sends and it drops the rest of the prompt. Raise it to 8192 (see [models.md](../models.md#ollama-lm-studio-vllm-or-another-server-you-run)) |
| Answers say there is not enough information | Ingestion or extraction may still be running (see **Runs** in the dashboard), or the question is outside your data |

## Memory

| Symptom | Fix |
|---|---|
| A service restarts repeatedly and `opencortyx logs <service>` ends in `Killed`, or `docker inspect` shows `OOMKilled` | Give Docker more memory, raise that service's `*_MEM_LIMIT` in `.env`, or lower worker concurrency. See [memory-budget.md](../memory-budget.md#setting-the-limits) |
| Whole machine slows down during ingestion | Reduce `EXTRACTION_WORKER_CONCURRENCY` and `CELERY_WORKER_CONCURRENCY` |
| An upload stays "Waiting to start…" | Another upload holds the ingest worker. `opencortyx logs worker` showing `signal 9 (SIGKILL)` means a file keeps running it out of memory: after 2 such attempts the run fails with the reason, and **Settings → Danger Zone → Clear Task Queue Only** frees it at once |
| A document says "extracting…" for a long time | Its LLM stages run in the background on the extraction worker, one call at a time per model slot. `ollama ps` shows the model; on a Mac see [macOS](macos.md) for `OLLAMA_NUM_PARALLEL` |

## Dashboard and MCP

| Symptom | Fix |
|---|---|
| Dashboard unreachable after sleep or `wsl --shutdown` (Windows) | `opencortyx restart web` |
| Login loops back to the login page behind a proxy | `PUBLIC_WEB_URL` must be the exact public URL, including `https://` |
| MCP client gets `421 Invalid Host header` | Add the hostname the client uses to `MCP_ALLOWED_HOSTS` |
| MCP client gets `401` | Create an API key under **Users & Access** and send it as `X-API-Key` |

## Getting help

Open an issue with the output of `opencortyx doctor` and the relevant logs. Remove API keys and
passwords before sharing logs.
