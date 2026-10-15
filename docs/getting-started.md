# Getting started

## 1. Install and start

Follow the [installation guide](installation/README.md) for your system, or from a clone run `bin/opencortyx up`. When it finishes,
open http://localhost:3000.

## 2. Create your admin account

The first visit shows a setup screen. There is no default password.

## 3. Add data

- **Documents and files**: *Ingestion* → upload files or a folder (PDF, Markdown, text, JSON, CSV,
  Parquet). Sample files are in [`examples/data`](../examples/data).
- **Databases**: *Connections* → add a source, then *Ingestion* → *New structured ingestion* to pick
  tables.
- **Files on this machine**: set `OPENCORTYX_FILES_ROOT` in `.env` to a folder, run
  `bin/opencortyx up`, and choose paths under it in the local connector.

Progress is shown under *Runs*. Extraction continues in the background after upload.

## 4. Shape the knowledge

- *Mapper* defines how JSON, tables and documents become nodes and edges.
- *Custom Metadata* holds extractors and relationship rules; built-in extractors run automatically.
- *Review Queue* lists facts proposed by agents and extractors that need approval.

## 5. Ask questions

Use the *AI Assistant* in the dashboard, or the API (`POST /api/v2/answer`, docs at http://localhost:8000/docs).

## 6. Connect an AI client

Create an API key under *Users & Access*, then:

```bash
bin/opencortyx connect claude ax_your_key
```

See [apps/mcp/README.md](../apps/mcp/README.md) for other clients and the tool list.

## Troubleshooting

```bash
bin/opencortyx doctor
bin/opencortyx logs api
```

- **Model not reachable**: check `LLM_*` in `.env`; see [models.md](models.md).
- **A service keeps restarting for memory**: give Docker more memory; see [memory-budget.md](memory-budget.md).
- **Dashboard unreachable after a Docker Desktop restart** (Windows): `bin/opencortyx restart web`.

Every dashboard page is described in the [feature guides](README.md#features).
