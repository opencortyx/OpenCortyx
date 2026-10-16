# Installation

OpenCortyx runs as a set of Docker containers. Every platform uses the same stack; only the way you
install Docker and the model backend differ.

| Your machine | Guide | Model backend |
|---|---|---|
| Windows 10/11 | [windows.md](windows.md) | Ollama/LM Studio, Hugging Face Inference, or a hosted API |
| macOS (Apple Silicon or Intel) | [macos.md](macos.md) | Ollama/LM Studio, Hugging Face Inference, or a hosted API |
| Linux desktop or workstation | [linux.md](linux.md) | Ollama, vLLM, Hugging Face Inference, or a hosted API |
| Windows Subsystem for Linux | [wsl.md](wsl.md) | Same as Windows |
| Cloud VM or server (production) | [server.md](server.md) | Hosted API or your own model server |

## Requirements

| | Minimum | Recommended |
|---|---|---|
| Docker | Docker Desktop 4.30+ or Docker Engine 24+ with Compose v2 | latest |
| Memory available to Docker | 8 GB | 12 GB |
| Disk | 10 GB | 20 GB |

A model server you run yourself (Ollama, LM Studio, vLLM) runs outside Docker's stack and needs its
own memory on top of this.

## What the installer does

1. Checks Docker and Docker Compose.
2. Downloads the latest release into `~/opencortyx` (Windows: `%USERPROFILE%\opencortyx`).
3. Runs `bin/opencortyx init`: creates `.env` and generates unique secrets.
4. Runs `bin/opencortyx up`: pulls release images (or builds them), applies database migrations,
   starts all services and waits until they are healthy.
5. Adds the `opencortyx` command to your PATH.

Then open http://localhost:3000, create the admin account, and choose your models under **Models**
(see [Models](../models.md)).

## After installing

```bash
opencortyx doctor        # checks Docker, database, model server and embeddings
opencortyx status        # service status
opencortyx logs api      # follow a service's logs
```

- [Getting started](../getting-started.md): add data, ask questions, connect an AI client
- [Models](../models.md): connect Ollama, LM Studio, vLLM, Hugging Face or a hosted provider
- [Updating and uninstalling](updating.md)
- [Troubleshooting](troubleshooting.md)
