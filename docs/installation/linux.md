# Install on Linux

For Ubuntu, Debian, Fedora and other distributions with Docker Engine.

## 1. Install Docker Engine and Compose

The convenience script installs Docker Engine and the Compose plugin:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
newgrp docker
docker compose version
```

For distribution packages, follow [Install Docker Engine](https://docs.docker.com/engine/install/).

## 2. Optional: Ollama on the host

Containers reach the host through Docker's bridge, not `127.0.0.1`, so Ollama must listen on all
interfaces:

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl edit ollama      # add the two lines below
#   [Service]
#   Environment="OLLAMA_HOST=0.0.0.0"
sudo systemctl restart ollama
ollama pull llama3.2
```

Keep port 11434 closed in your firewall to anything but local containers.

## 3. Install OpenCortyx

```bash
curl -fsSL https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.sh | bash
```

When it finishes, open http://localhost:3000 and choose the model under **Models**: OpenAI-compatible,
Base URL `http://host.docker.internal:11434/v1` for Ollama, or a hosted provider and its API key. See
[Models](../models.md).

## Linux-specific notes

- **Permission denied on the Docker socket:** your user is not in the `docker` group yet; log out and
  back in after `usermod`.
- **`opencortyx` not found:** add `~/.local/bin` to your PATH.
- **SELinux (Fedora, RHEL):** if Postgres cannot read `docker/postgres/init.sql`, relabel the
  repository directory with `chcon -Rt svirt_sandbox_file_t ~/opencortyx/docker`.
- **Local files:** set `OPENCORTYX_FILES_ROOT=/home/you/documents` in `.env` and run `opencortyx up`.
