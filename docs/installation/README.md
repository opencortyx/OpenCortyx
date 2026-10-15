# Installation

OpenCortyx runs as a set of Docker containers. Every platform uses the same stack; only the way you
install Docker and the model backend differ.

| Your machine | Guide | Model backend |
|---|---|---|
| Windows 10/11 | [windows.md](windows.md) | NVIDIA GPU (local vLLM), Ollama/LM Studio, or a hosted API |
| macOS (Apple Silicon or Intel) | [macos.md](macos.md) | Ollama/LM Studio or a hosted API |
| Linux desktop or workstation | [linux.md](linux.md) | NVIDIA GPU (local vLLM), Ollama, or a hosted API |
| Windows Subsystem for Linux | [wsl.md](wsl.md) | Same as Windows |
| Cloud VM or server (production) | [server.md](server.md) | Hosted API, a GPU server, or local vLLM |

## Requirements

| | Minimum | Recommended |
|---|---|---|
| Docker | Docker Desktop 4.30+ or Docker Engine 24+ with Compose v2 | latest |
| Memory available to Docker | 8 GB | 12 GB (16 GB with local vLLM) |
| Disk | 10 GB | 60 GB with local vLLM (image and model weights) |
| GPU (optional) | NVIDIA with 4 GB VRAM for the default 1.5B model | 8 GB+ VRAM for larger models |

## What the installer does

1. Checks Docker and Docker Compose.
2. Downloads the latest release into `~/opencortyx` (Windows: `%USERPROFILE%\opencortyx`).
3. Runs `bin/opencortyx init`: creates `.env`, generates unique secrets, and starts the local vLLM
   model server when it finds an NVIDIA GPU.
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
- [Models](../models.md): switch between GPU, Ollama and hosted providers
- [Updating and uninstalling](updating.md)
- [Troubleshooting](troubleshooting.md)
