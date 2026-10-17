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

## 2. Optional: a local model server

To run the language model on this PC, install [Ollama](https://ollama.com/download) or
[LM Studio](https://lmstudio.ai/) and connect it on the **Models** page after installing (see
[Models](../models.md)). A hosted API or Hugging Face Inference needs nothing installed.

## 3. Install OpenCortyx

In PowerShell:

```powershell
irm https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.ps1 | iex
```

If script execution is blocked, download `install.ps1`, read it, then mark it as trusted and run it:

```powershell
Unblock-File .\install.ps1
powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\install.ps1
```

OpenCortyx's scripts never turn PowerShell's script checks off (`-ExecutionPolicy Bypass`): they run
with `RemoteSigned`, which allows local scripts and refuses unsigned ones from the internet.

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
