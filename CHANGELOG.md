# Changelog

## Unreleased

### Added
- `opencortyx reset-password <email>`: set a new password for an account from the machine running
  OpenCortyx. A forgotten admin password used to mean starting over, since there is no default
  credential and no email recovery.

### Fixed
- The installers put the `opencortyx` command on PATH only after the stack had started, so a
  first start that failed left the command missing when it was needed to retry. It is added first,
  on macOS, Linux and Windows.
- `opencortyx update` switched to the new version but started it with the old version's code. It
  now starts the new version with its own command. Upgrading from 0.1.0-beta.2 still runs that
  version's `update`, so upgrade from it by hand: see
  [Updating](docs/installation/updating.md#from-010-beta2).

## 0.1.0-beta.3 - 2026-10-17

Models are chosen only on the Models page, and can be downloaded there for every role. The bundled
vLLM server is gone.

### Upgrading from 0.1.0-beta.2
- Do not use `opencortyx update` from 0.1.0-beta.2: switch to the tag and start it by hand, as in
  [Updating](docs/installation/updating.md#from-010-beta2). Migration `0030` converts your model
  rows; nothing to do by hand.
- Rows that pointed at the bundled vLLM server are deleted, so a workspace that used it has no
  generation model afterwards: open **Models** and add one (a server you run, Hugging Face, a
  hosted API, or a model downloaded into OpenCortyx).
- Model settings in `.env` (`LLM_PROVIDER`, `LLM_MODEL`, `EMBED_MODEL`, ...) are ignored. You can
  delete them.
- The vLLM image is no longer used: `docker image rm vllm/vllm-openai:v0.11.0` frees its disk.

### Added
- Download a model while adding it on the Models page, for every role. A text model: Ollama pulls
  a GGUF model from Hugging Face to run on your GPU, or OpenCortyx downloads a small model and runs
  it on the CPU with no other install (the new **Hugging Face model in OpenCortyx** provider). An
  embedder or reranker: any model fastembed supports, downloaded before its first use.
- fastembed rerankers (ONNX, CPU), so reranking no longer needs `INSTALL_RERANK=1`.
- The Mapper previews PDF, Word, PowerPoint and HTML files, read the way ingestion reads them:
  title, nested sections, paragraphs, lists, tables, and page headers and footers. It accepted only
  Markdown and text before.

### Changed
- Models are chosen on the **Models** page and nowhere else. `.env` no longer selects a model
  (`LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, `EMBED_PROVIDER`, `EMBED_MODEL` and the like are
  ignored), and nothing falls back to a model that was not chosen there. Each workspace calls its
  own models.
- A new workspace starts with the embedding and rerank models only. Until a generation model is
  added, the AI Assistant and extraction say that no generation model is connected.
- Ollama, LM Studio and vLLM are all connected as an **OpenAI-compatible server** with their
  address; the separate vLLM provider is merged into it. Migration `0030` converts existing rows
  and deletes the ones that pointed at the bundled server (`http://vllm:8000/v1`).
- Hugging Face Inference goes through Hugging Face's OpenAI-compatible router
  (`https://router.huggingface.co/v1`).
- **Custom Metadata** is now **Custom Extractors**, under Integration.
- **Add data** takes several files at once, and lets you choose a pipeline per kind of file; without
  a choice, the default pipeline for the file type is used.
- A new workspace no longer writes its template's types into the graph, so the catalogue and
  Lineage start empty. The start-up log no longer prints the confusing `4/4 workspaces: 0 with
  people`.
- Model names no longer carry "(built-in)".

### Removed
- The bundled vLLM server: the `vllm` service, its image, the `gpu` profile and the GPU detection
  in `bin/opencortyx init`.
- The TEI, Infinity, Cohere Rerank and Jina Rerank providers. Models that used them are deleted on
  upgrade.
- The "New semi-structured mapping" button on the empty Mapper page.

### Fixed
- Running page headers and footers were never read from documents: Docling keeps them apart from
  the body, and only the body was read. They are now `PageHeader` and `PageFooter` nodes on the
  document, for PDF and Word alike.
- A PDF's headings were all one level, so "1.1" sat beside "1" instead of inside it and the title
  was one more section. Levels now come from the section numbers, and the opening unnumbered
  heading of a numbered document is its title.
- A new document mapping showed, and then saved, sliding-window chunking, although documents are
  read by layout when a mapping says nothing. It now starts on Layout, like ingestion.
- `bin/opencortyx up --build` failed with `image ... already exists`: five services built the same
  API image at once. Only `api` builds it now; the others use it.
- Retrieval never used the reranker chosen on the Models page. It now uses the workspace's default
  reranker when the image can run it.
- The AI Assistant said "no grounded evidence" when no generation model was connected, or the model
  server was down. It now says what is wrong.
- Windows: a first install no longer stops on an expected failure under Windows PowerShell 5.1.

## 0.1.0-beta.2 - 2026-10-16

First release under the MIT license.

### Changed
- License: MIT, replacing the OpenCortyx Community License (source-available). The README notes
  the one copyleft dependency, PyMuPDF (AGPL v3), and what it means for redistributors.

### Removed
- Internal engineering notes from `docs/` (dated build log, handoff, an implementation report and the
  internal roadmap).

## 0.1.0-beta.1 - 2026-10-15

First public beta.

### Added
- `bin/opencortyx` command for macOS, Linux and Windows: `init`, `up`, `dev`, `status`, `doctor`,
  `connect`, `logs`, `backup`, `restore`, `update`, `reset`, `test`.
- One-line installers: `install.sh` and `install.ps1`.
- `compose.yaml` at the repository root with a `gpu` profile for local vLLM, a one-shot `migrate`
  service and a production web build; `compose.dev.yaml` for development.
- Support for running without an NVIDIA GPU through any OpenAI-compatible endpoint or hosted API.
- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`.
- `Release` workflow: multi-architecture images for the API, web app and MCP server published to
  GitHub Container Registry, and a GitHub release from this changelog. It runs the CI checks on the
  tagged commit first and publishes nothing if they fail.
- Release channels. `vX.Y.Z` tags are stable (`:latest` images, a normal GitHub release);
  `vX.Y.Z-beta.N` tags are beta (`:beta` images, a GitHub pre-release).
  `bin/opencortyx update --channel stable|beta` picks one and remembers it; the installers take
  `OPENCORTYX_CHANNEL=beta`, and install the newest beta while there is no stable release.
- CI lint job (ruff) and `bin/opencortyx version`.
- Documentation: installation guides for Windows, macOS, Linux, WSL and production servers, a
  how-it-works guide with diagrams, and feature guides for every dashboard page with screenshots
  (`docs/README.md` is the index).
- End-to-end tests with Playwright (`apps/web/e2e`): sign-in, every dashboard page, ingestion into
  the catalogue and a cited answer; `npm run screenshots` regenerates the documentation screenshots.
  The `E2E` workflow runs them against a full stack.
- Source on every piece of evidence: each context block and citation from the AI Assistant, the
  API and MCP names its asset (`asset`, `asset_id`), or its `origin` when it belongs to none. An
  entity found in many assets lists them ranked for the question, each with the passage that says
  it; `GET /api/v2/nodes/{id}/sources` pages through the rest. The chat shows them as a numbered
  Sources list under each answer.
- An answer's Sources list only the records it rests on, grouped by file; everything else retrieval
  found is folded under "Other evidence". Each citation carries `supports` for API and MCP clients.
- **Resolution for any node type** on Nodes & Relationships: open a type, pick the fields that
  identify a record (offered from the attributes its records carry), the scorer and thresholds, save,
  and **Re-check existing data** to match what is already in. A mapping's record types (Lead,
  Project, ...) could only be set over the API before. Types with resolution set show a link icon.
  New endpoint `POST /api/v2/resolution/types/{name}/run`; `GET /api/v2/resolution/types/{name}`
  also returns the type's attributes.
- Settings → General → **Workspace name**: yours or your organisation's. It names the top of the
  graph (seeded from `GRAPH_ROOT_NAME`; blank keeps a made-up name).
- A demo of the whole flow at the top of the README, recorded from the real product by
  `apps/web/scripts/demo/record.mjs` (which resets a workspace and drives the UI end to end).

### Security
- Web: Next.js 14.2.5 → 15.5, React 18 → 19, next-auth and `@auth/core` updated; clears 13
  critical Next.js advisories and the critical next-auth advisory. The image optimizer is disabled
  (it is not used). `lucide-react` 0.395 → 1.46 for React 19 support; web tooling pinned to Node 20.
- API: `pypdf` 4.3 → 6.18 (PDF uploads are untrusted input); Poetry and its dependencies are no
  longer shipped in the API image; `pip` and `setuptools` upgraded in both Python images.
- MCP: all dependencies pinned in `apps/mcp/requirements.lock`.
- API and MCP dependencies with published advisories updated: `docling` 2.130 → 2.139, `pypdf`
  6.18 → 6.20, `transformers` 4.57 → 5.15 (with `sentence-transformers` 3 → 6 and
  `huggingface-hub` 0.36 → 1.33), `urllib3` 2.7 → 2.8, `multidict` 6.7 → 6.9, `oauthlib` 3 → 4,
  `PyJWT` → 2.15.1. `pip-audit` reports no known vulnerabilities in either lock file.
- Access and refresh tokens are signed with PyJWT instead of the unmaintained `python-jose`, which
  also removes `ecdsa` and its unfixed advisory. Tokens issued before the change stay valid.
- The API refuses to start without a real `JWT_SECRET` instead of falling back to `changeme`.

### Changed
- A JSON record's node id is generated from where it sits (its file, path and position), never taken
  from an `id`/`*_id` field, which is now an attribute like any other. Taking ids from the data merged
  the same id across files into one node, so the file read last silently overwrote the first; whether
  two records are the same thing is now the Resolution Engine's decision. The Mapper no longer marks
  id fields or offers an Identity picker for JSON, and the `id_field` ingest option is removed.
  Tables keep their primary keys, which identify rows only within their own source.
- Lineage is a tree opened in place — workspace, sources, tags, assets, then what each asset
  contains — loaded 100 items per click. The tile map it replaces (and its layout jobs, map
  positions and `/graph/tiles|density|locate|relayout` endpoints) is removed.
- Every graph write, not only an ingest, now refreshes the graph index, so template rebuilds,
  curation edits and imports appear on the Lineage page.
- The graph moved out of PostgreSQL: nodes and edges live in **NebulaGraph** and every search surface
  in **Qdrant** (one dense vector per attribute, one BM25 document per node), behind two ports with an
  in-memory adapter for tests. PostgreSQL keeps the control plane — collections, pipelines, mappings,
  runs, users and the ontology. Migration `0022_graph_to_stores` copies an existing installation's
  graph and its stored embeddings into the two stores and drops the old tables; nothing is re-embedded.
- Catalogued tables and their columns are named for what they are: node types `MSO` → `Table` and
  `Property` → `Attribute`, edge `HAS_PROPERTY` → `HAS_ATTRIBUTE`, and properties `parent_mso_id` →
  `parent_table_id` (and the rest of the `*_mso_id` keys). The migration renames existing data,
  templates, pipelines, mappings and ontology entries.
- A node's degree is maintained as it is written rather than through a ledger and a rollup task, so
  the map is correct the moment an edge lands; the degree-rollup beat task is gone.
- `bin/opencortyx backup` and `restore` cover all three stores (they archive and replace the store
  volumes) instead of dumping PostgreSQL alone; a backup stops the stack briefly so the copy is
  consistent.
- Streaming JSON ingest treats the file as one asset with its records beneath it, instead of making
  one `Source` per record.
- Retrieval drops statements whose validity window has closed, so a fact that expired is no longer
  cited as current; a question that names a time still reaches it.
- `GET /structured` lists the newest assets first.
- Every file ingest runs through one default walker per kind (`ingest/walkers`): documents, tables and
  records. Uploads and folder sync call `ingest.runner.ingest_file`, which detects the kind unless it
  is forced; the walkers use the existing parsers and engines, so the graph they build is unchanged.
- Secrets are generated per installation and read from `.env`; nothing is hard-coded in compose.
- `bin/opencortyx up` pulls release images for the version in `VERSION` and builds only what is not
  published; `up --build` forces a build; `update` follows release tags.
- Container logs are rotated (3 × 10 MB per service); the MCP service has a health check.
- One version number (`VERSION`) shared by the API, web app and releases; CI checks they match.
- Architecture decision records rewritten to describe the current system.
- Database, user and model alias renamed from `axiom` to `opencortyx`.
- Postgres and Redis are no longer published on the host by default; web, API and MCP bind to
  `127.0.0.1`.
- The extractor studio in the dashboard is split into focused components.
- The 5,500-line API router is split into an `api` package with one module per resource in each
  layer (`routes`, `schemas`, `services`) plus shared `deps`. Endpoints, the OpenAPI document and
  route precedence are unchanged.
- README: logo, links to www.opencortyx.com and Discord, per-environment install guides and a feature
  tour. Package metadata points to the website and repository.
- The dashboard chat page is titled "AI Assistant", matching the navigation.

### Fixed
- `bin\opencortyx update` on Windows failed: the command's function had been removed.
- `bin/opencortyx update` could move a stable install back onto a beta (git's version sort ranks
  `v1.0.0-beta.1` above `v1.0.0`), or onto an older release.
- `docs/BACKUP-RESTORE.md` described a Postgres-only dump; backups cover PostgreSQL, NebulaGraph and
  Qdrant.
- mypy on `domain` and `core` (a CI step) passes.
- "Use this reading and add it" accepts the reading: the saved mapping keeps the types it showed
  (Project, Risk, …) instead of ingesting generic `KeyNode`s.
- Retrieval no longer treats a record's related records as duplicates of each other when it trims
  the context: a project's lead and its risk share the project as parent, so asking for both kept
  the risk, dropped the lead, and the answer named another project's lead.
- Resolution no longer compares `created_date` and `modified_date`, which every write stamps, so two
  records ingested on different days are not flagged as disagreeing.
- Resetting a workspace clears the resolution index and ledger, and resolution ignores index keys of
  deleted nodes; before, re-ingesting a deleted record could be linked `SAME_AS` a node that no
  longer existed.
- Auto-detect sources in Ingestion can choose a pipeline for each detected kind (documents, tables,
  records) and use watch folders and bulk upload. Previously every file of an auto-detect source was
  read as a document, and the source had no folder settings.
- JSON Lines, XML and YAML files in watch folders are decoded by their parsers; folder sync only read
  JSON before.
- Uploading a document with one of the built-in default pipelines returned 404.
- A multi-line `INTERSECT(` or `UNION(` block without its closing `)` silently swallowed the rest of a
  graph query; it is now reported as an invalid query.
- Pipeline step 1 is one reading rule for every kind: a bound mapping plus *Accept a newly detected
  shape automatically* and *Extract only*. Chunking, table and key-path settings moved to mappings and
  the default walkers; they are cleared when a pipeline is saved. *Extract only* on document pipelines
  was never applied (the backend read a different key), and structured pipelines ignored the
  auto-accept option; both now work for all three kinds.
- Renamed ingestion modules left compatibility facades that did not re-export private helpers, so run
  deletion, structured extraction stages and mapped JSON ingestion failed with `ImportError`. Callers
  import the canonical modules and the facades are removed.
- `embed_node` referenced `V2Session` and `Node` without importing them, so node embeddings after an
  authoring or memory write always failed; `authoring` logged through an undefined `logger`.
- `poetry.lock` was out of date with `qdrant-client` and `nebula3-python`, so a clean API image build
  failed. The lock is regenerated, and the unused `apps/api/requirements.txt` is removed.
- The embedding worker was killed for memory while indexing: the in-process embedder now batches by
  text size (`FASTEMBED_MAX_BATCH_CHARS`).
- Fresh installs failed at database setup: migration `0019` altered a table no migration created.
  Migration `0020` now creates `node_attr_embedding`.
- Memories saved with `/memory/remember` or the MCP `opencortyx_remember` tool were never recalled.
- `/graph/query` returned 500 for invalid queries; it now returns 400 with the parser message.
- The MCP server rejected every Host header except localhost; allowed hosts are configurable with
  `MCP_ALLOWED_HOSTS`.
- vLLM crash-looped on GPUs with little free memory; the CLI sizes `VLLM_GPU_FRACTION` from what the
  GPU actually has free.
- Retrieval crashed instead of degrading when no database session was available.
- The extraction worker's memory limit was below its working set, so extraction was repeatedly killed.
- The API's memory limit was below its working set under retrieval load; it is now 3 GiB.
- `bin/opencortyx doctor` runs its checks in a separate container instead of inside the running API.
- The MCP server README listed tools the server does not provide; it now lists the twelve it does.
- The Assets page showed placeholder values (source, format, quality, classification, update time
  and signature) instead of the asset's data. It now shows the kind, format, content fingerprint,
  size, source, sensitivity and update time recorded at ingestion.
- API integration tests for schema mappings skipped unless an API key was set; they now also run
  with the admin credentials the other integration tests use.
- End-to-end tests delete the documents they upload.

### Removed
- The PostgreSQL graph tables (`node`, `edge`, `node_attr_embedding`, `node_tile`, `degree_delta`,
  `org_graph_stat`), their triggers and functions, the `pgvector` dependency, and the HNSW build and
  search knobs that only applied to them.
- The unused `ExtractorRegistry` and the ingestion runner and walker prototypes it served, two MCP test
  scripts that required a live server, one-off benchmark scripts, and dead code found by lint.
- Legacy launch scripts, Makefile, session notes, backup copies of pages and modules, placeholder
  SDKs, unused infrastructure stubs and ad-hoc scripts.
