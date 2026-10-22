# OpenCortyx knowledge layer (`opencortyx`)

The whole backend. Point it at your data → get **grounded, cited, token-efficient context** for any
AI app. Three stores behind two ports, an **open ontology**, and no bespoke storage code in the
application. Full design: [`docs/architecture.md`](../../../../docs/architecture.md).

## The model — a graph, its search surfaces, and a registry
- **NebulaGraph** (`graph_store/`) holds nodes and edges. A node's type is a property, never a table:
  a Table, an Attribute and a Chunk are all `Node` records. Custom fields live in `properties`, so a
  new type needs no migration. An edge is written only when both endpoints exist; otherwise it is
  quarantined for replay, so a ghost edge is impossible.
- **Qdrant** (`vector_store/`) holds one point per searchable surface: a dense vector per attribute
  and a BM25 document per node, each carrying the fields searches filter on.
- **PostgreSQL** holds the control plane: `type_registry` (the ontology that drives extraction),
  plus `collection`, `pipeline`, `schema_mapping`, `ingest_run`, `policy`, `api_key`, `audit_log`
  and `connector`. Never a node.
- `core/substrate/` is the one gateway both stores are written through: it stamps audit fields,
  keeps the search surfaces in step with the records, and counts every write.

## Layout (by architecture layer)
Dependencies point DOWN this list. `domain` imports nothing from the application; `core` imports
nothing above it — with no exemptions. Both rules are enforced by `tests/test_module_boundaries.py`.
```
main.py · bootstrap.py · doctor.py   entry points: the API app, start-up, the health check

domain/      what a row IS — definitions only, zero application imports (Layer 0)
  base.py          V2_SCHEMA + V2Base + uuid_pk
  control.py       the control-plane tables (collection, policy, api_key, ingest_run, model, ...)
  ontology.py      TypeRegistry + the asset-type rules

core/        the runtime kernel (Layer 1) — grouped by responsibility, each with a public surface
  config/          env.py = V2Settings (static, restart to change) · runtime.py = live knobs (DB-backed)
  db.py            async engine + V2Session + get_db + create_schema_and_tables + schema_exists
  substrate/       the graph write path: repo.py (NodeRepo, the ONLY writer) · validation ·
                   document (what a node says) · quarantine (replay, never drop) · accounting
  inference/       llm · embedding · registry (resolve a model reference) · warmup
  security/        auth (JWT + API key) · scope (resolve_scope) · secrets · ratelimit
  worker/          celery_app (queues, beat, ResilientTask, GIVE_UP_HOOKS) · utils (async_to_sync)
  http.py · observability.py · contracts.py · cache.py · preflight.py

stores/      storage ports + adapters (Layer 1)
  graph/           nebulagraph | memory — nodes and edges
  vector/          qdrant | memory — the search surfaces
  graph_query/     the graph query DSL: ast · parser · executor

ontology/    the built-in ontology (Layer 2): entity kinds, Person/Organization templates, extractors

ingest/      ingestion (Layer 2) — detect → parse → walk, one package per stage
  runner.py          ingest_file(): detect the kind (or take it), run that kind's default walker
  formats/           bytes → a paradigm's model: catalog.py (all 39 extensions) + one reader per family
                       structured/ tabular · semistructured/ json_reader jsonl_reader xml_to_json
                       yaml_to_json toml_to_json ini_to_json · unstructured/ documents layout/ (readers/)
  detection/         which paradigm a file is (routing.py: route_for / describe)
  walkers/           the DEFAULT walker per kind, used when nothing is declared
  structured/        SQL and table files → Table/Attribute/row nodes (ingest, catalog, translate)
  semistructured/    JSON/XML/YAML through declared structure cards (ingest.py: the declared walker)
  unstructured/      documents: chunk, Source/Chunk nodes (ingest, chunking, simhash)
  extraction/        extractors over written nodes: entities, templates, enrich, pyextractor, anchors
  pipelines/         default pipelines, schema-signature routing, conformance, mapping migration
  runs/              one run: progress, outcome, purge
  sources/           where files come from: folder sync, directory tree, data lakes

retrieval/   understanding a question, finding evidence, answering with citations (Layers 2-4)
  orchestrator.py    runs L0→L4 · adapter.py: build_context_v2(), the contract the product calls
  contracts.py       the data model every layer speaks · experiments.py: experiment flags
  l0_understanding/  intent, constraints, entities, vocabulary, plan
  l1_candidates/     lexical + embedding + entity resolution → fusion
  l2_graph/          expansion, beam, edge scoring, aggregation
  l3_rerank/         features, cross-encoder, learned rerank, cascade (cross-encoder → LLM)
  l4_context/        budget-aware context build + optimize
  answering/         answer (grounded generation) · trust (gate, citations, ABSTAIN) · memory
  common/            text_norm · temporal — primitives every layer shares

capabilities/  knowledge-quality plugins behind a registry (versioning, contradiction, trust, abstain)
drafting/      drafters that propose extractors, mappings and pipelines from a sample
knowledge/     what people and agents do with the graph: authoring · governance (proposals) ·
               memory (agent sessions) · eval (gold suite, feedback) · graph_clusters (dashboard communities)

tasks/       Celery tasks by responsibility: ingest · sync · enrich · graph · maintenance
api/         HTTP layer, mounted at /api/v2: router.py, deps.py, and three parallel trees —
               routes/ (HTTP) · schemas/ (Pydantic models) · services/ (logic) — each grouped the same way:
               access/ · ingestion/ · graph/ · knowledge/ · system/, one module per resource
```

Schema: `bootstrap.init_v2_schema()` creates every ORM table; `alembic upgrade head` then applies what
the ORM does not declare. `alembic/versions/0001_baseline.py` squashes the original 22 migrations
(revision id kept, so existing installs are already at head); new schema changes go in new files.

## API (`/api/v2`)
`POST /auth/token` (dev login) · `POST /sources/upload` (documents) ·
`POST /structured/ingest-sql` + `/structured/enrich-native` (SQL catalogue) ·
`GET/POST /connectors` · `POST /context` · `POST /answer` · `POST /corpus/enrich` ·
`GET /structured` · `GET /graph` · `GET /stats` · `GET/POST /ontology*` · `GET/POST /collections`.

## Run
```bash
# API
uvicorn opencortyx.main:app --host 0.0.0.0 --port 8000
# Worker (note the v2 Celery app)
celery -A opencortyx.core.worker.celery_app worker -Q graphrag --loglevel=info
```
Needs PostgreSQL, NebulaGraph and Qdrant, plus a generation model connected on the Models page
(any OpenAI-compatible server, e.g. Ollama, or a hosted API). Embeddings run in-process. Cross-encoder rerank is optional
(`pip install sentence-transformers`); without it retrieval ranks by its other signals.

## Extend it (the contributor contract)
- **Custom node/edge/entity type:** `POST /ontology/types` (or add to `ontology/`). No
  migration — properties live in JSONB; the type's `description` drives extraction.
- **New retrieval surface:** add it under `retrieval/l1_candidates/` and compose it
  in `l1_candidates/composition.py`; the orchestrator fuses whatever the plan asked for. An
  unplanned surface may vote but never outvotes a planned one (see `l1_candidates/fusion.py`).
- **New kind of source:** add a walker in `ingest/walkers/` that implements `BaseWalker.ingest_file` and
  writes via `core.substrate.NodeRepo` (never write `node`/`edge` directly), register it in
  `walkers.WALKERS`, and declare its formats in `ingest/formats/catalog.py`. Uploads and folder sync reach it
  through `ingest.runner.ingest_file`; add a route in `api/routes/<resource>.py` only for new endpoints.
- **Invariants (never break):** one store; edges only via `NodeRepo` (FK-guarded, no ghosts); every
  derived node carries `source_ref` + `confidence`; no grounded context → **abstain**.

## Verify (no Docker needed)
```bash
python -m py_compile $(git ls-files 'opencortyx/**/*.py')
python -c "import importlib,pkgutil,opencortyx as p; \
[importlib.import_module(m.name) for m in pkgutil.walk_packages(p.__path__,'opencortyx.')]; \
import opencortyx.main; from sqlalchemy.orm import configure_mappers; configure_mappers(); print('ok')"
```
