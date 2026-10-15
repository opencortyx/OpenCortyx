# Install on Windows

Tested on Windows 11 with Docker Desktop (WSL 2 backend).

## 1. Install Docker Desktop

1. Install [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) and keep
   **Use the WSL 2 based engine** enabled.
2. Start Docker Desktop and wait until it reports that the engine is running.
3. Give Docker enough memory. With the WSL 2 backend, memory is set in `%USERPROFILE%\.wslconfig`:

   ```ini
   [wsl2]
   memory=12GB
   ```

   Then run `wsl --shutdown` and start Docker Desktop again.

## 2. Optional: NVIDIA GPU

To run the language model locally on your GPU:

1. Install the current [NVIDIA driver](https://www.nvidia.com/Download/index.aspx) for Windows. It
   includes WSL support; do not install a driver inside WSL.
2. Check that containers can see the GPU:

   ```powershell
   docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
   ```

The installer detects the GPU and enables the local vLLM model server. On a GPU with 4 GB of memory
the default 1.5B model fits; the CLI sizes vLLM's memory share to what is free when it starts.

Without a GPU, see [Models](../models.md) to use Ollama, LM Studio or a hosted API.

## 3. Install OpenCortyx

In PowerShell:

```powershell
irm https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.ps1 | iex
```

If script execution is blocked, download the script and run it with a one-time bypass:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```

The first start downloads images and models and can take 10-20 minutes. When it finishes, open
http://localhost:3000.

## Using the CLI

Open a new terminal after installing so `opencortyx` is on your PATH:

```powershell
opencortyx status
opencortyx doctor
opencortyx logs api
```

From a clone, run `bin\opencortyx` (PowerShell or cmd) or `bin/opencortyx` (Git Bash).

## Windows-specific notes

- **Ports in use:** if 3000, 8000 or 8001 are taken, set `OPENCORTYX_WEB_PORT`, `OPENCORTYX_API_PORT`
  or `OPENCORTYX_MCP_PORT` in `.env` and run `opencortyx up`.
- **Dashboard unreachable after a restart:** Docker Desktop occasionally loses port forwarding after
  `wsl --shutdown` or sleep. Run `opencortyx restart web`.
- **Ollama on Windows:** install Ollama for Windows; containers reach it at
  `http://host.docker.internal:11434/v1`, which is the installer's default.
- **Local files:** to ingest files from your disk, set `OPENCORTYX_FILES_ROOT=C:/Users/you/Documents`
  in `.env` and run `opencortyx up`.
