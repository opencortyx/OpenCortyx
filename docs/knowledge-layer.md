# OpenCortyx Knowledge Layer — End-to-End Implementation

Status: agreed design, 2026-10-23 (rev. 2). Target repo: `OpenCortyx-release`. Paths are relative
to `apps/api/opencortyx/` unless stated.

---

## 1. The decisions

1. **Data and knowledge are stored separately.**
   - **Data** goes to **Delta Lake tables**, in three groups that match the three asset kinds:
     **structured**, **semi-structured** and **unstructured**. Data that already lives in a
     customer's lake or database stays there and is read in place (§2.5).
   - **Knowledge** goes to the **graph** (NebulaGraph). There are two layers of it:
     - **World layer**: the entities, facts and relationships that the **Mapper** and **custom
       extractors** produce, exactly as they do today.
     - **Data layer**: one asset node per asset (`StructuredAsset`, `SemiStructuredAsset`,
       `UnstructuredAsset`), its attributes (columns and fields) with descriptions, keys and join
       edges, and metric definitions.
   - **Control data** goes to **Postgres**: the asset registry, evidence pointers, the identity
     map, runs. No source data is kept here.
2. **Document text never enters the graph.** That means no chunk nodes and no layout-element
   nodes. Records stay in the graph as they are today: `KeyNode`s, the edges named after them,
   reference edges and categorical value nodes (§2.3). Full payloads and rows stay in Delta.
3. **Every piece of world knowledge carries evidence.** That's a pointer to the exact chunk, row
   or record it came from.
4. **Writing and reading use different engines.** `delta-rs` (`deltalake`) writes, and DuckDB
   reads. Both are used from **one module only** (`stores/data/`).
5. **Embeddings stay in Qdrant.** Chunk text lives in Delta, and Qdrant is only an index.
6. **"Not found" means unknown, not false.**
7. **Everything else in OpenCortyx stays the same and keeps working 100%.** This plan changes
   *where data is stored* and *what the answer flow can reach*. It adds nothing that requires
   users to change how they work, and it removes no feature.

### 1.1 What must keep working, unchanged

Every item below must behave exactly as it does today after every phase. If a phase breaks one,
that phase doesn't ship.

| Area | Must keep working |
|---|---|
| **Connectors and ingestion** | Every connector (files mount, upload, SQL databases, MongoDB (new), S3/GCS/ADLS, Delta/Iceberg/Hudi lakes, APIs, collections), every supported format, ingest runs and progress, re-ingest, versioning, purge, tags |
| **Pipelines** | Pipeline builder, Step-1 config (catalog / row mode), chunking settings, stage DAG, filters, saved pipelines |
| **Mapper** | Type cards, id fields, labels, relationships, reference rules, foreign keys, document metadata card, per-mapping paragraph size |
| **Extraction** | Custom extractors, extraction templates, Python extractors, anchors, enrichment over existing nodes, categorical value promotion |
| **Graph** | `KeyNode`s, declared types, `Attribute` nodes, `HAS_<KEY>` / reference / FK edges, date nodes, the central node, session capture, resolution engine and its review page, ontology / type registry |
| **UI** | Dashboard, graph tree view, asset pages (they show chunks/rows from Delta instead of nodes), search, chat, review, ontology, Models page, settings, auth and first-run setup |
| **APIs** | Every existing REST endpoint and MCP tool keeps its request and response shape. `/context` and the answer endpoints return the same block fields, and passages arrive as blocks with the same shape chunk nodes have today (id, text, source, page, score, provenance). |
| **Operations** | `bin/opencortyx` (install, up, update, regression, doctor), backup/restore (now including `DATA_ROOT`), memory budget, beta/stable channels |
| **Quality** | Answer quality on the 1000-file bench and the regression suite is equal to or better than today |

### 1.2 How "100% working" is enforced

1. **A switch per workspace during the migration.** Phases 2–4 add a workspace setting,
   `data_store: graph | delta`. The default stays `graph` (today's behaviour) until the new path
   matches it. New code runs only when the setting is `delta`. The old path is deleted in Phase 6,
   and only after parity is proven.
2. **Parity tests.** Each phase adds tests that ingest the same fixtures under both settings and
   compare:
   - the world nodes and edges written (same types, ids, labels, properties, references)
   - the citations
   - the answers on the regression questions
3. **Gates for every phase, on GitHub runners (never installed locally):**
   - unit and integration suite green
   - regression suite (`bin/opencortyx regression`) green against its defect register
   - E2E suite green
   - install test green
   - bench resolution rate not lower than the previous release
4. **No API or MCP contract change without a compatible shape.** If a response gains a field
   (e.g. `route`, `unknown_reason`), it's added, never renamed or removed.
5. **Existing installs:** while in beta, the upgrade path is reset + re-ingest. The release notes
   say so, and `doctor` reports a workspace still on the old store.

---

## 2. Where everything lives

| Store | Holds | Never holds |
|---|---|---|
| **Delta tables** (`DATA_ROOT/<workspace_id>/…`) | managed data: chunks, rows, records | knowledge, credentials |
| **Customer's lake / database** | external data (Delta, Iceberg, Hudi, Parquet, CSV, SQL DBs) read in place | — |
| **NebulaGraph** | world layer (incl. KeyNodes and record references, as today) + data layer | chunks, full rows and payloads, sample values (except `value_set`) |
| **Qdrant** | vectors: knowledge-node attributes, attribute descriptions, passages | the only copy of any text |
| **Postgres** | `data_asset`, `evidence`, `source_key`, runs, settings, users, chat | source data |

### 2.1 Delta tables: three groups

```
DATA_ROOT/<workspace_id>/
  unstructured/                  ONE table for all documents: one row per chunk (layout element)
  structured/<asset_id>/         one table per structured asset (its own columns)
  semistructured/<asset_id>/     one table per semi-structured asset (one row per record)
```

| Group | Columns |
|---|---|
| `unstructured` | asset_id, version, chunk_id, ordinal, element_type (Title / SectionHeader / Text / ListItem / Table / Figure / PageHeader / PageFooter), level, parent_chunk_id, section_path, page, bbox, text, table_grid (JSON), ingested_at |
| `structured/<id>` | the asset's own columns (types fixed at ingest) + `_row_key`, `_snapshot`, `_ingested_at` |
| `semistructured/<id>` | record_id, parent_record_id, json_path, sync_run, fetched_at, raw (JSON text), flattened top-level fields |

Why unstructured is **one** table while the other two are **one per asset**:
- All documents share the same chunk columns. One table per PDF would create thousands of tiny
  tables.
- Structured and semi-structured assets each have their own columns, so they need a table each.

Document-level metadata (title, author, date, pages, mime) isn't stored in Delta. It lives on the
`UnstructuredAsset` node (graph) and in `data_asset.metadata` (Postgres). It's small, it's
knowledge about the data, and it's read constantly.

Rules:
- **`_row_key`** is the declared or detected primary key. Failing that, it's a hash of the row's
  content plus an occurrence index. Never the row number alone.
- **CSV, Excel and other text tables are converted once, at ingest.** Types are fixed then, so
  queries never re-detect them. The original file stays where it lives (the files mount, a lake,
  or the upload store).
- **One commit per ingest job.**
- **`optimize` + `vacuum`** run on the maintenance worker on a schedule.

### 2.2 Postgres (new tables, one migration)

| Table | Purpose |
|---|---|
| `data_asset` | One row per asset: id, workspace_id, **kind** (structured / semistructured / unstructured), **storage** (managed / external), **format** (delta / iceberg / hudi / parquet / csv / json / pdf / postgres / mysql / api …), **location** (managed Delta path, or the external table / path / DSN reference), connector_id, **version_ref** (Delta version, Iceberg snapshot id, file checksum, sync watermark), name, metadata (JSON), status, tags, created_at, updated_at |
| `evidence` | One row per (knowledge node / edge / property) × supporting unit: target_id, target_kind, property, asset_id, unit_kind (chunk / row / record), unit_key, version_ref, quote, value_hash, extractor, run_id, confidence, observed_at, valid_from, valid_to. This is the fact ledger. |
| `source_key` | The identity map: asset_id, native_id, entity_type, node_id, match_method, confidence, first_seen, last_seen |

### 2.3 The graph

**World layer** (unchanged producers): the **Mapper** (declared mappings: id fields, labels,
relationships) and **custom extractors** (pipeline extractor cards, LLM templates, Python
extractors) produce entities, facts and relationships **the same way they do now**. Only three
things change:
1. Their input is read from the asset's data (managed Delta or external), or from memory during
   ingest.
2. Every emitted node, edge or fact property attaches **evidence**.
3. Document structure is no longer written: no chunk, section or layout nodes, and no
   `CONTAINS`/`NEXT` edges between them. Everything else is written as now.

**Kept in the world layer exactly as today:**

| Node / edge | Written by | Notes |
|---|---|---|
| `KeyNode` | JSON/XML/YAML walker (`ingest/semistructured/ingest.py`), tabular row mode (`ingest/structured/ingest.py`) | A record whose type wasn't declared. What it is lives under `key` ("Product"), and the label is "Product #3". A declared type is written as that type. |
| Declared-type record nodes | Mapper (type cards, `row_node_type`) | e.g. `Licence`, `Customer` |
| `HAS_<KEY>` edges | walker / row mode | Named from the semantic key (`HAS_PRODUCT`), never `HAS_KEYNODE` |
| Reference edges | walker reference resolution (`_resolve_side`: kinds `self`, `context`, `join`, position) and declared foreign keys | The referential keys between records: "order.customer_id → Customer.id" |
| Categorical value nodes | `ingest/structured/categorical.py` | Short repeatable values (`red`, `Ireland`, `at_risk`) promoted to nodes, so "which project is red?" is one hop |
| Entities / facts / relationships | custom extractors (templates, Python extractors, anchors) | |

Each of these nodes gets evidence pointing to its record (`semistructured/<id>` record_id or
`structured/<id>` `_row_key`). A `KeyNode` keeps the scalar attributes it holds today. The full
record, including nested arrays and large text, stays in Delta.

Each world node also gets **one `FROM_ASSET` edge per supporting asset**, not one per chunk or row.

**Data layer** (the schema extractors write it at ingest):

| Node | Properties |
|---|---|
| `StructuredAsset` | name, description, grain ("one row per licence"), row_count, format, storage (managed / external), location ref, asset_id |
| `SemiStructuredAsset` | name, description, format, endpoint or path, record count, id field, asset_id |
| `UnstructuredAsset` | title, author, date, mime, pages, language, reader, tags, asset_id |
| `Attribute` | **Kept** (it exists today). A column (structured) or a field (semi-structured: `path`, e.g. `$.orders[].customer_id`). Name, type, description, semantic_type, role (pk / fk / measure / dimension / date / text / category), unit, pii, null_pct, distinct_count, `maps_to` ({entity_type, attribute, role}), `value_set` (see below) |

`value_set` is the one allowed exception to "no sample values". For a non-PII category column
with few distinct values (default ≤ 50), the distinct values and their counts are stored, e.g.
`status: ACTIVE 812, EXPIRED 97, SUSPENDED 4`. Without them the SQL planner writes
`status = 'active'` and gets zero rows. Nothing is stored for identifiers, free text, dates,
numbers or PII.

**Additional data-layer nodes.** Metric is coming; the others are proposed because each one
directly raises answer accuracy:

| Node | Status | Properties | Why it's worth having |
|---|---|---|---|
| `Metric` | **coming** | name, description, expression (SQL over Attributes), grain, filters, unit, owner | "Revenue", "active customers": one agreed definition instead of the LLM guessing the formula. This is the biggest gain for SQL questions. |
| `Segment` | proposed (with Metric) | name, description, condition (SQL predicate over Attributes), owner | Reusable named filters ("active customer" = `status='ACTIVE' AND expiry_date >= today`). Metrics and questions both use them. |
| `BusinessTerm` | proposed | term, synonyms, definition, owner | Maps the users' words ("permit", "licence no.", "LIC ref") to Attributes, entity types, Metrics or Segments. Feeds L0 binding. |
| `VerifiedQuery` | proposed | question, SQL, assets used, verified_by, verified_at, result_checksum | A question → SQL pair someone checked (from the gold set or an accepted answer). Given to the SQL planner as worked examples; one of the most reliable ways to raise text-to-SQL accuracy on a specific dataset. |

| Edge | Meaning |
|---|---|
| `HAS_ATTRIBUTE` | Structured/SemiStructured asset → Attribute |
| `JOINS_TO` | Attribute → Attribute (an FK, a walker reference rule, or a detected join, with cardinality and confidence). Works across assets, including managed ↔ external. |
| `REFERENCES` | Attribute → asset (the FK target) |
| `DEFINED_ON` | Metric / Segment → Attribute(s) |
| `USES_SEGMENT` | Metric → Segment |
| `MEANS` | BusinessTerm → Attribute / Metric / Segment / entity type |
| `USES` | VerifiedQuery → Attribute / Metric / Segment |
| `FROM_ASSET` | world node (entity, `KeyNode`, record) → any asset node |

The data-layer `JOINS_TO` between Attributes is the *schema* view of a reference ("orders.customer_id
joins customers.id"). The world-layer reference edges link actual records ("Order #12 → Customer
C-88"). Both are kept: the first plans SQL, the second answers graph questions.

The three asset types replace today's `Source` / `Table` asset roots. `is_asset_node`,
`graph_tree` and provenance accept them, and a workspace's root-type setting maps onto them.

The world and data layers stay apart in retrieval. L0 decides which layer a question needs.

**Kept as-is (allowed writers outside extractors):** session capture (AppUser, AppAgent, Session,
Turn), the workspace central node, and date nodes.

### 2.4 The evidence pointer (the extractor output contract)

```
evidence: [{
  asset_id, unit_kind: chunk|row|record, unit_key, version_ref,
  quote,            # sentence / cell values / JSON fragment that supports it
  extractor, run_id, confidence, observed_at, valid_from?, valid_to?
}]
```

`NodeRepo` quarantines a world-layer write without evidence. When two sources disagree, both
evidence rows are kept and the resolution engine applies the authority and freshness rules.

### 2.5 Data that already sits in a lake or database (external assets)

Many customers already hold their data in Delta, Iceberg or Hudi tables, Parquet/CSV on
S3/GCS/ADLS, or a SQL database. **We do not copy it.**

| Asset | Storage | How it's read |
|---|---|---|
| Delta / Iceberg / Hudi table in the customer's lake | **external** | DuckDB `delta_scan` / `iceberg_scan` / parquet over the base files, using the storage connector's credentials (`ingest/sources/datalake.py`) |
| Parquet / CSV / JSON files in the customer's object store | **external** for Parquet; **managed** for CSV/JSON (converted once, so types are stable) | DuckDB through the storage connector |
| SQL database (Postgres, MySQL, SQL Server, Oracle, SQLite, …) | **external, live (default)**. A managed snapshot is opt-in per connector. | DuckDB as the query engine, never a copy (§2.7) |
| NoSQL database (MongoDB, …) | **external, live (default)** | DuckDB as the query engine through an Arrow adapter (§2.7) |
| API | **managed** (`semistructured/<id>`) | Synced snapshots, because APIs can't be queried with SQL |
| Documents in a lake (PDF, DOCX…) | file stays external; **chunks are managed** (`unstructured`) | Parsing produces our own chunks, so those are always ours |

What changes for external assets:
- **`data_asset.location`** points to the external table. **`version_ref`** stores its version
  (Delta version, Iceberg snapshot id). For sources with no versioning, such as a live SQL table,
  it stores the read watermark.
- **Evidence stays verifiable.** On lakes the evidence points to `(table, version, row key)` and
  time travel shows the exact row later. On live databases it points to `(table, row key)` plus
  `value_hash` and `observed_at`. If the row changed later, the citation says "value changed since
  extraction" instead of showing a silently different value.
- **Change detection:** the sync task compares the external `version_ref` with the stored one. A
  new Delta version or Iceberg snapshot triggers incremental re-extraction of the changed files or
  rows. A live database uses an `updated_at` watermark when one is declared.
- **Question-time SQL** builds the workspace views over managed *and* external assets in one
  DuckDB connection, so one query can join a managed CSV with a customer's Delta table. The SQL
  guard is the same.
- **Credentials** stay in the connector's secrets. DuckDB secrets are created per request and
  never written to disk or logged.
- **Unreachable source:** an answer that needs it returns `UNKNOWN(source unavailable)`. Graph
  knowledge already extracted from it is still served, with its `observed_at`.

### 2.6 Reading Delta tables with DuckDB

**Writing goes through `delta-rs`, and all reading goes through DuckDB.** Both sit behind
`stores/data/`, and no other module imports `duckdb` or `deltalake`.

| Job | Engine | How |
|---|---|---|
| Append chunks / rows / records, merge, delete an asset, schema evolution | **delta-rs** (`deltalake.write_deltalake`, `DeltaTable.merge/delete`) | One commit per ingest job; a retry on a commit conflict |
| Compaction + cleanup | **delta-rs** (`optimize.compact`, `vacuum`) | Scheduled on the maintenance worker |
| Reading a source at ingest (CSV, Excel, Parquet, ORC, Avro, lake tables, SQL DBs) | **DuckDB** | `read_csv` / `read_parquet` / `delta_scan` / `iceberg_scan` / `postgres` / `mysql` (today's `tabular.py`, `datalake.py`) |
| Profiling columns (types, null %, distinct counts, `value_set`) | **DuckDB** | Aggregate SQL over the asset's view |
| Feeding the Mapper, row mode and extractors in batches | **DuckDB** | Streamed `SELECT … ` over `delta_scan`, as Arrow record batches (the same as `iter_row_batches` today) |
| Re-running extractors over stored chunks | **DuckDB** | `delta_scan(unstructured) WHERE asset_id = ?` |
| Passage text for L1/L3 (top-N chunk ids) | **DuckDB** | One batched `WHERE chunk_id IN (…)` per question |
| Opening evidence (the chunk, row or record behind a fact) | **DuckDB** | `delta_scan` at the pinned `version_ref` (time travel) + `WHERE unit_key = ?` |
| Asset page preview (chunks, rows, records) | **DuckDB** | Paged `SELECT … LIMIT/OFFSET` |
| Question-time SQL (L2b) | **DuckDB** | Over per-workspace views (below) |
| External lake tables / DBs | **DuckDB** | The same views, pointing at the customer's location |

**How a read works:**
1. `stores/data.connect(workspace_id, purpose)` opens an **in-memory** DuckDB connection. DuckDB
   only queries; it never stores anything, so there is no `.duckdb` file.
2. The `delta` extension, plus `iceberg`, `httpfs`, `postgres` and `mysql` for external assets, is
   loaded from the copy bundled in the image. Nothing is downloaded at runtime.
3. One **view per asset** is created for that workspace only, e.g.
   `CREATE VIEW licences AS SELECT * FROM delta_scan('<DATA_ROOT>/<ws>/structured/<asset_id>')`.
   External assets get their lake scan or DB attach, with a DuckDB secret built from the
   connector for this connection only. View names come from the asset name, so generated SQL reads
   like the customer's schema.
4. The connection is limited: `memory_limit` (below the worker budget), `threads`, a query
   timeout, and read-only access. For question-time SQL, only the guarded `SELECT` runs (§4.1 L2b).
5. The connection is closed after the job or the question. Connections are cheap; views aren't
   cached across workspaces.

**Speed:**
- Filters on `asset_id`, `chunk_id`, `_row_key` and `record_id` skip files using the Delta
  file statistics.
- The chunks table is sorted (Z-ordered) by `asset_id` during compaction, so one document's chunks
  sit in a few files.
- Structured tables are sorted by `_row_key`, so evidence lookups are fast.

**Versions:** pin `duckdb`, its extensions and `deltalake` together, and test them as one set.
Don't enable Delta table features (newer protocol versions, deletion vectors, column mapping)
until the pinned DuckDB `delta` extension reads them. A parity test writes with delta-rs and reads
with DuckDB for every feature in use.

**Fallback:** if DuckDB ever can't be used, `stores/data` switches to delta-rs' own reader
(`DeltaTable.to_pyarrow_dataset()` + DataFusion SQL). The tables, views, callers and the graph
don't change.

### 2.7 Data in operational databases: DBMS and NoSQL (DuckDB as the query engine, no copy)

Often the data lives in a running database: Postgres, MySQL, SQL Server, Oracle, SQLite, or a
NoSQL store such as MongoDB. In that case **we copy nothing, not into Delta and not into DuckDB.**
DuckDB is only the **query engine**. It reaches into the database, runs the query, and keeps
nothing afterwards. All the metadata and knowledge work (schema, profiling, Attribute nodes,
Mapper, KeyNodes, reference edges, custom extractors, evidence) still happens, reading through
DuckDB in the same way as for every other asset.

**How DuckDB reaches each kind of database:**

| Database | Access | How |
|---|---|---|
| PostgreSQL, MySQL/MariaDB, SQLite | **Native DuckDB extension** (`postgres`, `mysql`, `sqlite`) | `ATTACH '<dsn>' AS src (TYPE postgres, READ_ONLY)`. DuckDB pushes column selection and filters down to the database. |
| SQL Server, Oracle, Snowflake, BigQuery, Redshift, other ODBC/ADBC sources | **Arrow adapter** | A small adapter in `stores/data/adapters/` reads through ADBC / ODBC (`connectorx` or the vendor's Arrow driver) and hands DuckDB an Arrow stream. |
| **MongoDB** | **Arrow adapter** (`pymongoarrow`) | A collection is read as an Arrow stream. Filters and the projection are translated into a Mongo `find` / aggregation pipeline, so only the needed documents leave MongoDB. |
| Other NoSQL (Elasticsearch/OpenSearch, Cassandra, DynamoDB, Couchbase…) | **Arrow adapter** (same pattern, added as needed) | One adapter per source type, each with the same three methods (below) |

DuckDB has no official MongoDB extension, which is why MongoDB goes through an adapter. Community
DuckDB extensions are used only after review, and they're bundled and pinned like the official
ones.

**Every adapter has the same three methods:**
1. `schema(source)`: tables or collections, columns or field paths, types, keys, references.
2. `scan(source, columns, filters, limit, batch_size)`: Arrow record batches with the columns,
   filters and limit pushed into the database's own query language.
3. `changes(source, since)`: changed records since a watermark. That's an `updated_at` column,
   MongoDB change streams / oplog time, or Postgres logical replication when it's enabled.
   Otherwise the adapter returns nothing and a full rescan is scheduled.

DuckDB registers the scan as a view for that one connection, so joins, aggregates and window
functions run in DuckDB, across sources. One query can join a MongoDB collection, a Postgres table,
a customer's Delta table and a managed CSV.

**What we do with an operational database:**

| Step | Relational DBMS | MongoDB / NoSQL |
|---|---|---|
| Asset | Each table/view → `StructuredAsset` (`storage = external`, `format = postgres`…) | Each collection → `SemiStructuredAsset` (documents are nested like JSON) |
| Schema and metadata | Columns, types, PKs, FKs, comments from the catalog (`information_schema`, `pg_catalog`, … through DuckDB or SQLAlchemy `inspect`, as `structured/ingest.py` does today) | Field paths and types **inferred from a sample** (`$sample`, default 1,000 docs): how often each field appears, mixed types, nested arrays, and `ObjectId` / DBRef / `*_id` fields as reference candidates |
| Profiling | null %, distinct counts, `value_set` (SQL pushed down, or sampled on large tables) | The same, over the sample |
| Data-layer nodes | `Attribute` per column; `JOINS_TO` / `REFERENCES` from FKs | `Attribute(path)` per field; `JOINS_TO` from confirmed references (`orders.customer_id → customers._id`) |
| Knowledge extraction | Step-1 catalog or row mode, Mapper and custom extractors over DuckDB batches (as today) | The walker's KeyNodes, `HAS_<KEY>` edges, reference edges and categorical values, plus the Mapper and custom extractors, over adapter batches |
| Evidence | `(asset, primary key)` + `value_hash` + `observed_at` | `(collection, _id)` + `value_hash` + `observed_at` |
| Changes | `updated_at` watermark / logical replication → incremental re-extract | Change streams / `updated_at` → incremental re-extract |
| Question-time SQL | The attached table appears as a view | The collection appears as a view; the adapter pushes the filters down |

**Rules for live databases:**
- **Read-only always.** Use read-only credentials, `READ_ONLY` attach, and no writes of any kind.
  Pointing connectors at a **read replica** is recommended in the UI.
- **Protect production:**
  - a per-connector query timeout
  - a maximum number of rows per scan
  - a maximum number of concurrent queries
  - batch size limits
  - extraction scheduled at a set time window (off-peak by default)
- **Evidence on live data can drift.** A live database has no history, so evidence stores the
  `value_hash`. If the row has since changed, the citation says "value changed since extraction"
  and the next sync re-extracts.
- **Large first scans** (millions of rows or documents) run as a background ingest with progress,
  in key ranges, and they're resumable.
- **When an operational database can't take the load,** the connector can switch to a managed
  snapshot (`structured/<id>` / `semistructured/<id>`) on a schedule. That's opt-in per
  connector, not the default.
- **Credentials** stay in connector secrets. They're turned into a DuckDB secret or adapter
  client for one connection only, never written to disk or logs.

---

## 3. Ingestion flows

Every flow ends in `tasks/_shared._post_ingest`, which handles versioning, then extraction (Mapper
+ custom extractors), then resolution, as today.

### 3.1 Unstructured (PDF, DOCX, PPTX, HTML, EPUB, MD, images…)

```
file (upload / files mount / lake) ─► checksum ─► data_asset (unchanged? stop)
   ─► layout reader (ingest/formats/unstructured/layout — unchanged)
   ─► elements ─► Delta `unstructured` (one commit)
   ─► passage embeddings ─► Qdrant (kind=passage, asset_id, chunk_id)
   ─► UnstructuredAsset node (metadata)
   ─► Mapper / custom extractors over the elements (in memory) ─► entities/facts/edges + evidence ─► graph
   ─► resolution engine
```

- `ingest/unstructured/ingest.py` stops writing Chunk, Section, layout, table and image nodes.
- `run_named_templates_on_chunks` takes chunk records (from memory at ingest, from Delta on a
  re-run) and attaches evidence.
- Changed content gets a new `version`. Old-version chunks are deleted from Delta and Qdrant, and
  their evidence rows are dropped. Entities that lose all evidence are marked `stale`.

### 3.2 Structured (CSV, TSV, Excel, Parquet, ORC, Avro, Arrow, SQL DBs, Delta/Iceberg/Hudi)

```
source ─► managed?  yes ─► DuckDB read ─► type-fixed rows ─► Delta `structured/<asset_id>`
                    no  ─► register location + version_ref (nothing copied)
   ─► introspect + profile + LLM descriptions ─► StructuredAsset / Attribute (+ value_set) / JOINS_TO / REFERENCES
   ─► Step-1 config as today: catalog only, or row mode (KeyNode / declared type per row,
      HAS_<KEY> edges, FK reference edges, categorical value nodes) ─► + evidence(row_key)
   ─► custom extractors (as today) over DuckDB batches ─► entities/facts/edges + evidence(row_key)
   ─► source_key rows (native id → node id)
   ─► resolution engine
```

- An asset in catalog-only mode produces no world nodes. It's still fully answerable through
  SQL.
- Row mode writes the same nodes and edges as today. The difference is that the rows are also in
  Delta, so a node can always open its full row, and SQL can reach columns the node doesn't
  carry.
- Sample values are used during profiling and for the LLM's descriptions. Only `value_set` (for
  eligible category columns) is stored on nodes.

### 3.3 Semi-structured (JSON, NDJSON, XML, YAML, API responses)

```
payload ─► Delta `semistructured/<asset_id>` (one row per record, json_path, raw, flattened fields)
   ─► field profile ─► SemiStructuredAsset / Attribute (path, + value_set) / JOINS_TO from reference rules
   ─► walker as today: KeyNode / declared-type nodes, HAS_<KEY> edges, reference edges
      (self / context / join / position), categorical value nodes ─► + evidence(record_id)
   ─► Mapper mappings + custom extractors (as today) ─► entities/facts/edges + evidence(record_id)
   ─► source_key, resolution engine
```

- The walker keeps building the record graph it builds now. It only adds evidence and
  `FROM_ASSET`. The full payload also lands in Delta, so nested arrays, long text and fields the
  walker skipped (noise filter, `embed_nodes` off) can still be queried.
- Reference rules declared in a mapping write two things: the world reference edges between
  records (as today) and one data-layer `JOINS_TO` between the two Attributes, so the SQL planner
  can use the same join.

- API connectors (`tasks/sync.py`) write one snapshot per sync run, using cursors and watermarks.
- A record missing from an *incomplete* sync is never treated as deleted.

### 3.4 Updates and deletes

| Event | Managed asset | External asset |
|---|---|---|
| No change | checksum equal → no-op | `version_ref` equal → no-op |
| Changed | new Delta version → re-extract → idempotent upsert → replace that asset's evidence | new version or snapshot detected → incremental re-extract of the changed rows/files |
| Deleted | delete the asset's rows from Delta, its Qdrant points, evidence and source_key rows, and its data-layer nodes; entities with no evidence are marked `stale` | the same, except the customer's data is never touched |
| Time travel | Delta version + `valid_from` / `valid_to` | lake version or snapshot where the format has one |

---

## 4. Answer / retrieval flow (edited)

Today's pipeline, `L0 → L1 → L2 → L3 → L4 → answer`, stays. There are three additions:
1. **Passages** are a candidate type.
2. **Data routes** are new: SQL over managed and external assets.
3. **Abstention** is explicit, with a reason.

```
question
   │
L0  UNDERSTAND ─► intent · mentions · constraints · time · ROUTES (weighted, several allowed)
   │              routes: KNOWLEDGE | PASSAGE | DATA_SQL | AGGREGATE | CLARIFY
   │              + entity binding (mention → node via resolution index / source_key)
   │              + schema hypotheses (which assets / Attributes / Metrics the question touches)
   │
L1  CANDIDATES ─► knowledge nodes   (existing surfaces: embedding per attribute, bm25, identifier, fuzzy)
   │              passages          (Qdrant kind=passage, dense + sparse; text from Delta `unstructured`)
   │              data-layer nodes  (asset / Attribute / Metric descriptions — feeds DATA_SQL)
   │              └─ RRF fusion, evidence kept per surface
   │
L2  GRAPH ──────► world layer: beam search / structural expansion over entities (as today)
   │              data layer:  JOIN-PATH search between the assets the question needs
   │
L2b DATA ───────► SQL planner (when the DATA_SQL / AGGREGATE routes are active)
   │              schema subgraph + join path + metrics + source_key ─► SQL
   │              ─► guard (read-only, workspace views only, LIMIT)
   │              ─► DuckDB over managed Delta + external lake/DB views
   │              ─► sanity check ─► retry ≤ 2 with the error
   │              ─► result rows + the SQL + version_refs = evidence
   │
L3  RERANK ─────► features + cross-encoder over knowledge nodes AND passages (as today)
   │
L4  CONTEXT ────► blocks with roles: fact · passage · path · table-result · schema-note
   │              every block carries its evidence pointer
   │
ANSWER ─────────► grounded generation ─► per-sentence grounding check (trust.py)
   │              outcome = ANSWERED | PARTIAL | UNKNOWN(reason) | CLARIFY(question)
   │
WRITE-BACK ─────► optional: a validated fact found via SQL/passage ─► graph + evidence (idempotent)
   │
TRACE ──────────► per-layer trace + route chosen + SQL run + assets touched (managed / external)
```

### 4.1 Layer changes in detail

**L0: `retrieval/l0_understanding/`**
- `intent.py` / `policy.py`: add the routes `PASSAGE`, `DATA_SQL` and `CLARIFY`. They're weighted
  hypotheses, several can run, and an underfilled result widens to the other routes.
- `vocabulary.py`: score asset, `Attribute`, `Metric` and `Segment` names, descriptions and
  `maps_to`, plus `BusinessTerm` synonyms through `MEANS`, to produce the schema hypotheses.
  `KeyNode` `key` values ("Product") already count as type vocabulary today, and that stays.
- `entities.py`: entity binding also checks `source_key` ("customer 101" → the canonical
  Customer).
- `CLARIFY` only fires when the key term has two or more strong, conflicting bindings. Otherwise
  the most likely reading is answered and the assumption stated.

**L1: `retrieval/l1_candidates/`**
- `embedding.py` / `lexical.py`: add the passage surface. Text is fetched from Delta for the
  top-N only, in one batched read.
- `composition.py`: give passages their own slice of the pool.
- `hop2.py`: entity spans found in passages feed pass 2, as today.

**L2: `retrieval/l2_graph/`**
- World-layer expansion is unchanged. `FROM_ASSET` replaces chunk-hops for provenance.
- New `join_path.py`: the shortest weighted path over `JOINS_TO`/`REFERENCES` between candidate
  assets, preferring declared FKs. Paths may cross managed ↔ external assets.
- `aggregate.py`: counting graph nodes (entities, `KeyNode`s, categorical values) stays. Aggregates
  over columns the graph doesn't hold, or over catalog-only assets, go to L2b. If both can answer,
  L2b wins for numbers, because it sees every row.

**L2b: new `retrieval/l2_data/`**
- `planner.py`: builds the prompt from the schema subgraph only: attributes, descriptions, units,
  `value_set`s, the join path, Metric expressions, Segment conditions, bound `source_key` values,
  and the 3–5 closest `VerifiedQuery` examples (found by embedding similarity to the question).
  Never row samples. A question that matches a Metric uses its expression as written instead of
  inventing a formula.
- `guard.py`: parses the SQL (sqlglot) and accepts only a single `SELECT` over the registered
  views. No table functions, `ATTACH`, `COPY`, `PRAGMA` or `SET`, and a `LIMIT` is added if
  missing.
- `execute.py`: `stores/data` opens a read-only DuckDB connection with only this workspace's
  views. Managed assets map to `delta_scan(DATA_ROOT/…)`. External assets map to their lake scan
  or DB attach, using per-request secrets. It runs with `memory_limit` and a timeout.
- `check.py`: an empty result means "no matching rows", not "no". An error triggers a retry with
  the error message (at most 2). A result that contradicts a graph fact → both are shown, with
  sources. An unreachable external source → `UNKNOWN(source unavailable)`.
- The output is a `table-result` block holding the SQL, the `version_ref`s read, and the rows
  (capped).

**L3: `retrieval/l3_rerank/`**: no structural change. Passages are scored on their Delta text.

**L4: `retrieval/l4_context/builder.py`**: new roles `passage`, `table-result` and `schema-note`.
Provenance comes from `evidence`. `core/substrate/provenance.py` is rewritten to read the ledger,
and keeps question-aware ranking of sources by the evidence quote.

**Answering: `retrieval/answering/`**
- `answer.py`: the model must cite block ids.
- `trust.py`: the grounding check is extended to table results (numbers in the answer must appear
  in the result rows).
- `UNKNOWN(reason)`, where the reason is one of: no source has the attribute · no matching rows ·
  the sources conflict · the source is unavailable · the question is ambiguous.
- Write-back (off by default, set per workspace): facts that passed `trust` go to the graph with
  evidence.

### 4.2 Worked examples

| Question | Route | Path through the layers | Answer |
|---|---|---|---|
| "When does licence LIC-784 expire?" | KNOWLEDGE | binding → Licence node → `expiry_date` fact, evidence on doc p.4 | "31 Dec 2027 (Licence terms.pdf, p.4)" |
| "What does the policy say about early termination?" | PASSAGE | passages → cross-encoder → passage blocks | A quote-backed summary with page cites |
| "How many active licences per region in 2026?" | DATA_SQL | schema hypotheses → join path → SQL over `structured/<licences>` | A table, the SQL, the snapshot version |
| "Revenue by licence type last quarter" (sales in the customer's Delta lake, licence types in a managed CSV) | DATA_SQL | join path managed ↔ external → one DuckDB query over both views | A table, the SQL, both version_refs |
| "Which customers with open orders in MongoDB have an expired licence?" (orders live in MongoDB, licences in the customer's Postgres) | DATA_SQL | join path across the `orders` SemiStructuredAsset ↔ the `licences` StructuredAsset → one DuckDB query; the Mongo adapter pushes `status = 'open'` down, Postgres is attached read-only | A list of customers; the SQL; both sources with `observed_at` |
| "Does customer 101 have a free pass?" (never extracted) | KNOWLEDGE → DATA_SQL | no fact → Attribute `free_pass` with `maps_to` Customer.free_pass on the API's SemiStructuredAsset → `source_key` 101 → API id → SQL | "Yes, valid until 31 Dec 2026 (Customer API, synced 2026-10-22)" |
| "Which repairs relate to ABC Properties' licences?" | KNOWLEDGE + DATA_SQL | graph ABC → HOLDS → Licences; SQL on repairs | Combined, both sources cited |
| "What is ABC's credit score?" (no source has it) | all routes | nothing in graph, attributes or passages | "UNKNOWN: no connected source holds a credit score for ABC Properties" |

### 4.3 Model requirements

| Role | Minimum |
|---|---|
| Embedding, sparse, rerank | Current defaults (MiniLM, BGE cross-encoder) |
| Custom extractors, attribute descriptions | A mid-size model |
| L0 routing, SQL planning, answering | **A strong model from the registry.** Qwen3-0.6B can't do this reliably, and the UI should warn when one is selected. |

---

## 5. Security

- **Tenant isolation:**
  - Delta paths are per workspace.
  - DuckDB views are created per request, for the caller's workspace only.
  - Connector credentials are scoped to the workspace.
  - Every Postgres query filters on `workspace_id`.
  - Graph and Qdrant keep their existing filters.
- **The SQL guard is mandatory.** DuckDB can read any file or attached database it can reach.
- **External sources are read-only.** We never write to a customer's lake or database. Connectors
  should use read-only credentials, and the UI should say so.
- **PII:** `Attribute.pii` keeps sensitive columns out of LLM prompts and graph properties unless
  the workspace allows them. No sample values go into the graph.
- **Audit:** the trace records the assets touched and the SQL executed for every answer.

---

## 6. Evaluation (how we reach and prove ~95%)

**Resolution rate** = (correct answers with valid evidence + correct UNKNOWN/CLARIFY) ÷ all
questions. A confident answer without support counts as a **failure**.

1. **Gold set per domain:** 200–500 real questions covering each type: document fact, passage
   summary, simple SQL, complex SQL, managed ↔ external join, cross-source, missing fact,
   conflicting sources, ambiguous, no-answer, unreachable source. Start from the 1000-file bench
   kit (`axiom/opencortyx-bench`).
2. **Measure per layer:**
   - route accuracy
   - entity binding
   - candidate recall
   - SQL execution and correctness
   - grounding
   - abstention correctness
   - latency
   - cost
3. **Failure loop:** sort every miss by cause, fix the cause, re-run.
4. **Release gate:** no regression in resolution rate or in the unsupported-answer rate.

---

## 7. Implementation phases

Each phase ships on its own and must pass the §1.2 gates before the next one starts. Phases 2–4
run behind `data_store = delta`, with the default left at `graph` until parity is proven.

### Phase 0: Foundation (no behaviour change)
- `stores/data/` (new): `DataStore` with append, merge, delete_asset, read, lookup(unit_key),
  optimize/vacuum, and `views(workspace)`, which builds the DuckDB views over managed Delta and
  external assets. The only importer of `deltalake` and `duckdb`. `ingest/formats/structured/tabular.py`
  and `ingest/sources/datalake.py` move their DuckDB calls behind it.
- `pyproject.toml`: add `deltalake` and `sqlglot`. Pin `duckdb` with its `delta`, `iceberg`,
  `httpfs`, `postgres`, `mysql` and `sqlite` extensions, bundled in the image.
- `stores/data/adapters/`: the adapter interface (`schema` / `scan` / `changes`) and the native
  ATTACH path for Postgres/MySQL/SQLite.
- `compose.yaml`: an `opencortyx_data` volume on api and all workers. `DATA_ROOT` goes in
  `V2Settings`.
- Migration: `data_asset`, `evidence`, `source_key`.
- `tasks/maintenance.py`: optimize + vacuum on a schedule.
- **Done:** `stores/data` tests (managed + an external Delta fixture) pass, and the full suite is
  green.

### Phase 1: Evidence contract (old and new coexist)
- Mapper and custom extractor paths (`ingest/extraction/templates.py`, `pyextractor.py`,
  `anchors.py`, the semi-structured runner): attach `evidence`.
- `core/substrate/repo.py`: write `evidence`, and quarantine world writes that have none.
- `core/substrate/provenance.py`: read `evidence` first, falling back to the graph walk.
- `resolution/`: read competing evidence instead of overwriting.
- **Done:** new ingests fill `evidence`, and citations are unchanged in the UI.

### Phase 2: Unstructured → Delta
- `ingest/unstructured/ingest.py`: elements → `unstructured`, passages → Qdrant, and an
  `UnstructuredAsset` node.
- Extractors read chunk records from memory or Delta.
- Purge and versioning through `data_asset`.
- **Done:** a PDF ingest writes only `UnstructuredAsset` plus extracted entities, and the
  regression doc questions pass.

### Phase 3: Structured → Delta / external
- `ingest/structured/ingest.py`, `ingest/formats/structured/tabular.py`,
  `ingest/sources/datalake.py`: managed rows → `structured/<id>`; external tables are registered
  in place with `version_ref`.
- Table/Attribute nodes become `StructuredAsset`/`Attribute`. Add `maps_to`, `grain`, `pii` and
  profile numbers, and drop sample values.
- The Mapper and custom extractors run over DuckDB batches as today and write `source_key`.
  Row mode keeps writing KeyNodes / declared-type nodes exactly as today.
- `tasks/sync.py`: external version detection → incremental re-extract.
- SQL database connectors switch from copying to **live through DuckDB** (`ATTACH … READ_ONLY`),
  with timeouts, row caps and the `updated_at` watermark.
- **Done:** a 1M-row CSV, an external Delta table and a live Postgres/MySQL database produce the
  same graph as today (catalog or row mode), and any fact opens its row.

### Phase 3b: NoSQL and other database adapters (live, no copy)
- `stores/data/adapters/mongodb.py` (`pymongoarrow`): sampled schema inference, `scan` with
  filter and projection pushdown, `changes` through change streams.
- A MongoDB connector kind in the connector UI, with each collection registered as a
  `SemiStructuredAsset`. The walker, Mapper and extractors run over adapter batches.
- An ADBC/ODBC adapter for SQL Server / Oracle / Snowflake / BigQuery / Redshift.
- **Done:** a MongoDB fixture (nested documents, `ObjectId` references) produces the same KeyNodes,
  reference edges and entities as the same data loaded as JSON files. A question joining MongoDB
  with Postgres answers through one DuckDB query.

### Phase 4: Semi-structured → Delta
- `ingest/semistructured/*`, `ingest/walkers/semistructured.py`, `tasks/sync.py`: records →
  `semistructured/<id>`, plus `SemiStructuredAsset` and `Attribute(path)` nodes, and reference
  rules → data-layer `JOINS_TO`.
- The walker's KeyNodes, `HAS_<KEY>` edges, reference edges and categorical value nodes are
  unchanged, apart from added evidence + `FROM_ASSET`.
- **Done:** the regression JSON fixtures produce the same KeyNodes, reference edges and entities as
  before, and every one of them opens its record from Delta.

### Phase 5: Retrieval and answering
- L0 routes and schema hypotheses, the L1 passage surface, L2 `join_path.py`, L2b
  `retrieval/l2_data/`, L4 roles, and `UNKNOWN(reason)`. Optional write-back.
- `Attribute.value_set` used by the SQL planner.
- `BusinessTerm` and `VerifiedQuery` nodes, with simple authoring in the Mapper. A verified answer
  can be saved as a `VerifiedQuery` with one click.
- **Done:** gold-set resolution rate ≥ the current baseline. Then iterate toward the target.

### Phase 5b: Metrics and segments (coming)
- `Metric` and `Segment` nodes, an authoring form in the Mapper (name, description, expression or
  condition, grain), and validation that runs the expression once on the data before saving.
- The L2b planner prefers a matching Metric over a generated formula, and the answer names the
  Metric it used.
- **Done:** gold-set SQL questions that have a defined Metric are answered with that exact
  definition.

### Phase 6: Switch over, UI and cleanup
- Flip the default to `data_store = delta`. This happens only after Phases 2–5 have passed parity
  on the regression suite, E2E and the bench.
- Graph tree: asset levels come from `data_asset` and the three asset types, and children come
  via `FROM_ASSET`. The asset page shows chunks, rows or records from Delta or the external
  source.
- Remove the dead node types, the provenance fallback, and the chunk-node and generic-row code
  paths.
- Docs, CHANGELOG, regression defect register. Beta installs reset and re-ingest.

---

## 8. What gets removed

- Chunk, Section, layout-element, table-element and image nodes, plus their CONTAINS/NEXT edges.
- `EXTRACTED_FROM` edges to chunks (replaced by `evidence` + `FROM_ASSET`).
- `Source` / `Table` as asset roots (replaced by the three asset types).
- Sample values on Attribute nodes (only `value_set` remains, for eligible category columns).

**Not removed:**
- `Attribute` nodes
- `KeyNode`s and declared-type record nodes
- `HAS_<KEY>` edges
- reference edges (self / context / join / position) and FK edges
- categorical value nodes
- the Mapper, custom extractors, extraction templates, pipelines
- the resolution engine
- session capture
- graph aggregation over graph nodes

## 9. Risks

| Risk | Mitigation |
|---|---|
| Retrieval quality drops without chunk nodes | Passage surface + `FROM_ASSET`, with the bench as the gate in Phase 5 |
| Delta commit conflicts | Blind appends are safe. Retry deletes/merges, one commit per ingest job. |
| Small files slow down reads | Scheduled optimize; unstructured is one table, not one per document |
| Generated SQL reads outside the workspace | The SQL guard + per-workspace views (mandatory) |
| External source slow, down or changed | Timeouts; `UNKNOWN(source unavailable)`; `version_ref` + `value_hash` flag changed rows |
| Customer lake uses a table feature DuckDB can't read yet | Detect it at registration and show it in the UI; offer a managed snapshot instead |
| Load on a customer's production database | Read replica recommended; timeouts, row caps, concurrency limits, off-peak extraction window; opt-in managed snapshot |
| NoSQL schema drift (fields appear and disappear) | Periodic re-sampling; new fields are added as Attributes, and missing fields are never treated as deleted data |
| Weak generation model | Registry warning |
| Worker memory | DuckDB `memory_limit` below the worker budget (`scripts/check-memory-budget.py`) |
| DuckDB licence or governance change | Everything is behind `stores/data`; DataFusion or delta-rs can read the same tables |

## 10. Open decisions

1. Whether write-back of question-time facts is on by default (proposed: off).
2. ~~The default for SQL databases~~ **Decided:** live through DuckDB, no copy (§2.7). A managed
   snapshot is opt-in per connector.
3. Where `DATA_ROOT` lives by default for cloud installs (proposed: the local volume now, an
   object-store connector later).
4. Who authors `Metric` definitions (proposed: Mapper UI, workspace admins).

---

## 11. Timeline

**Assumptions:**
- **Two engineers** working in parallel tracks, using Claude Code sessions for implementation.
- Start date Monday **26 Oct 2026**.
- Testing runs on GitHub runners.
- One week of holiday buffer at the year end.
- Each phase includes its tests and parity checks (§1.2).

With **one engineer**, multiply by about 1.6 (≈ 6–7 months). These are estimates. Re-plan after
Phase 1, when the real pace is known.

| Weeks | Dates | Track A: storage and ingestion | Track B: sources, retrieval, evaluation | Release |
|---|---|---|---|---|
| 1–2 | 26 Oct – 6 Nov | **Phase 0:** `stores/data`, Delta + DuckDB, migration, volume, bundled extensions | **Gold set v1** (200 questions, all question types) + baseline measurement on today's system; adapter spike (MongoDB, SQL Server) | — |
| 3–4 | 9 – 20 Nov | **Phase 1:** evidence contract, ledger, provenance reads | **Phase 3 (part 1):** structured assets to Delta, live Postgres/MySQL `ATTACH`, external lake registration | beta: Phases 0–1 (no behaviour change) |
| 5–7 | 23 Nov – 11 Dec | **Phase 2:** unstructured to Delta, passages in Qdrant (behind `data_store = delta`) | **Phase 3 (part 2):** profiling, `value_set`, row mode + Mapper over DuckDB, watermarks, parity tests | — |
| 8–9 | 14 – 24 Dec | **Phase 4:** semi-structured to Delta, walker evidence, reference rules → `JOINS_TO` | **Phase 3b:** MongoDB adapter, ADBC/ODBC adapter | beta: Phases 2–4 behind the switch |
| — | 25 Dec – 1 Jan | Buffer / holidays | Buffer / holidays | — |
| 10–13 | 4 – 29 Jan 2027 | **Phase 5 (data side):** passage text fetch, `FROM_ASSET` provenance, L4 roles, asset-page previews | **Phase 5 (answer side):** L0 routes, L2 join paths, L2b SQL planner + guard + checks, `UNKNOWN(reason)`, `BusinessTerm`, `VerifiedQuery` | — |
| 14–15 | 1 – 12 Feb | Parity runs on regression + E2E + bench, fixes | **Phase 5b:** `Metric` + `Segment`, authoring in the Mapper | beta: Phase 5 behind the switch |
| 16–17 | 15 – 26 Feb | **Phase 6:** flip the default, graph tree + asset pages, remove dead paths | Docs, CHANGELOG, defect register, install test, release notes (reset + re-ingest) | **v0.2.0-beta** (new storage on by default) |
| 18+ | from 1 Mar 2027 | Hardening, performance on large sources | **Accuracy loop toward 95%** per customer domain: failure analysis → fixes → re-run, about 4–8 weeks per domain | regular betas |

**Milestones:**

| Date | Milestone | Proof |
|---|---|---|
| 6 Nov 2026 | Foundation in, baseline measured | `stores/data` tests green; baseline resolution rate recorded |
| 20 Nov 2026 | Every fact carries evidence | Citations unchanged; `evidence` filled on new ingests |
| 24 Dec 2026 | All three asset kinds + live DBMS/MongoDB work behind the switch | Parity tests green for each source type |
| 12 Feb 2027 | New answer flow (routes, SQL, passages, UNKNOWN) behind the switch | Gold-set rate ≥ baseline; regression + E2E green |
| 26 Feb 2027 | New storage is the default | All §1.2 gates green; v0.2.0-beta released |
| ~Apr–May 2027 | ~95% resolution rate on the first customer domain's gold set | Measured on that gold set, with no increase in unsupported answers |

**What would move the dates:**
- Retrieval parity in Phase 5 taking longer than planned. This is the riskiest part, and the bench
  decides it.
- Customer databases or lakes using features DuckDB or an adapter can't read yet.
- No strong generation model being available to test with (needed for L0/L2b/answering).
- Gold-set questions arriving late. The accuracy loop can't start without them.
