# Install on macOS

Works on Apple Silicon and Intel Macs. The language model runs in Ollama or LM Studio on your Mac,
on Hugging Face Inference, or through a hosted API.

## 1. Install Docker Desktop

1. Install [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/) for
   your chip.
2. In **Settings → Resources**, set **Memory** to at least 8 GB (12 GB recommended) and apply.
3. Start Docker Desktop and check that the command line works:

   ```bash
   docker compose version
   ```

   If `docker` is not found or `docker compose` is an unknown command, see
   [Docker command line](#docker-command-line) below.

## 2. Choose a model backend

**Ollama (local, free):**

```bash
brew install ollama
ollama serve            # or start the Ollama app
ollama pull llama3.2
```

Ollama gives models a small context window by default (2048 or 4096 tokens, depending on the
version) and silently cuts off longer prompts. OpenCortyx sends prompts of up to 8192 tokens, so
raise the window to 8192:

- **Ollama versions that list `OLLAMA_CONTEXT_LENGTH` in `ollama serve --help`:**

  ```bash
  launchctl setenv OLLAMA_CONTEXT_LENGTH 8192   # then quit and reopen the Ollama app
  ```

  If you run `ollama serve` in a terminal instead, start it as
  `OLLAMA_CONTEXT_LENGTH=8192 ollama serve`.

- **Any version:** create a copy of the model with the larger window. It reuses the downloaded
  weights, so nothing is downloaded again:

  ```bash
  printf 'FROM llama3.2\nPARAMETER num_ctx 8192\n' > Modelfile
  ollama create llama3.2-8k -f Modelfile
  ```

  Then use `llama3.2-8k` as the Model ID (see step 4).

**On a Mac with 16 GB or less, run Ollama with one request at a time.** Ollama reserves a full
context window for every parallel slot, so a 7B model at 8192 tokens grew to 11 GB, the Mac ran out
of memory beside Docker, and requests stopped being answered. With one slot the same model takes
about 6 GB:

```bash
launchctl setenv OLLAMA_NUM_PARALLEL 1       # then quit and reopen Ollama
```

Then lower **Settings → Parallelism → Parallel extraction LLM calls** to 1 or 2, so OpenCortyx does
not queue more requests than the model server can serve. `ollama ps` shows the model's size.

Check that only one Ollama server runs. The Ollama app, a Homebrew service and a hand-made launch
agent each start `ollama serve`; the second one fails with `address already in use`, and settings
given to it do nothing. `launchctl list | grep -i ollama` lists them.

Any model you already have works too; `ollama list` shows the names.

**LM Studio (local, free):** download a model in LM Studio and start its local server (port 1234).

**Hosted API:** OpenAI, Anthropic, Groq, Azure or Together, with an API key. See
[Models](../models.md).

## 3. Install OpenCortyx

```bash
curl -fsSL https://raw.githubusercontent.com/opencortyx/OpenCortyx/main/install.sh | bash
```

From a clone, run `bin/opencortyx up`. Add `--build` to build the images from your checkout
instead of pulling the release images.

When it finishes, open http://localhost:3000.

## 4. Choose the model

Open **Settings > Models** in the dashboard. A new workspace answers with a small built-in model
(Qwen3-0.6B on CPU); for better and faster answers, **Add** one under Generation and tick **Make
default** (extraction and utility use it too):

| Field | Ollama | LM Studio |
|---|---|---|
| Provider | OpenAI-compatible | OpenAI-compatible |
| Server address | `http://host.docker.internal:11434/v1` | `http://host.docker.internal:1234/v1` |
| Model ID | e.g. `llama3.2-8k` | the model name LM Studio shows |

For a hosted API, pick the provider and enter its API key instead. **Test** confirms the server
answers.

## Docker command line

Docker Desktop can run while its command line is still broken. Two cases:

- **`docker: command not found`:** the CLI is not on your `PATH`. In Docker Desktop, open
  **Settings → Advanced** and choose a CLI installation option, or add it yourself:
  `echo 'export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"' >> ~/.zshrc`.
- **`docker: unknown command: docker compose`** (the installer says
  `Docker Compose v2 is required`): the plugin links in `~/.docker/cli-plugins` point to the Docker
  installer disk image (`/Volumes/Docker/...`) instead of the installed app. This happens when Docker
  was first started from the mounted `.dmg`. Point them at the app:

  ```bash
  cd ~/.docker/cli-plugins
  for f in *; do ln -sfn "/Applications/Docker.app/Contents/Resources/cli-plugins/$f" "$f"; done
  docker compose version
  ```

## macOS-specific notes

- **Apple Silicon:** images are built for `arm64`, so everything runs natively.
- **Ollama reachability:** Docker Desktop forwards `host.docker.internal` to your Mac, so the default
  `http://host.docker.internal:11434/v1` works without changing Ollama's settings.
- **Memory with Ollama:** Ollama runs outside Docker and uses memory of its own. A 7B model with an
  8192-token window takes about 9 GB while it is loaded, on top of what Docker has. On a 16 GB Mac,
  use a 3B model (such as `llama3.2`) or a hosted API. Ollama unloads an idle model after 5 minutes.
- **Provider `ollama`** is no longer a provider name. Use OpenAI-compatible with Ollama's URL.
- **`opencortyx` not found:** the installer links the command into `~/.local/bin`. Add it to your
  shell profile: `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc`.
- **Local files:** add `OPENCORTYX_FILES_ROOT=/Users/you/Documents` to `.env` and run `opencortyx up`.
  Docker Desktop must be allowed to share that folder (**Settings → Resources → File sharing**).
