# Changelog

## 0.2.0-beta.1 - unreleased

Data and knowledge are stored apart ([ADR-006](docs/adr/006-data-and-knowledge-apart.md),
[design](docs/knowledge-layer.md)). A new workspace keeps its documents, tables and records as Delta
tables beside the graph; the graph holds what was extracted from them, and every extracted fact
points at the chunk, row or record it came from. Questions about rows - how many, how much, which
one has the most - are answered with SQL over all the rows, with the query and the data's version
shown, instead of counting whatever retrieval returned. Live databases, MongoDB and a customer's
own lake tables are read in place, never copied.

### Upgrading
- **A Delta workspace keeps records out of the graph**: a table with no pipeline reads as its schema
  (rows answer through SQL), and a JSON / YAML / XML file with no mapping keeps its records in Delta
  without a node per record or nested field. A pipeline's row mode or a mapping still makes nodes.
- `opencortyx update`. A database migration (`0032_data_plane`) adds the `data_asset`, `evidence` and
  `source_key` tables and the workspace setting `data_store`.
- **Existing workspaces keep their data in the graph** (`data_store = graph`), working as before, and
  `opencortyx doctor` says so. This store is deprecated: while in beta, the way to the new one is
  Settings > Workspace > Data store = delta, then a reset and a re-ingest of the workspace's files.
  New workspaces start on `delta`.
- A new volume, `data_store`, holds the Delta tables. `bin/opencortyx backup` includes it; a backup
  from before it restores without it.
- New settings, all optional: `DATA_ROOT` (/data/store), `DUCKDB_EXTENSION_DIR`,
  `DATA_DUCKDB_MEMORY_LIMIT` (512MB) / `DATA_DUCKDB_THREADS` (2) for question-time SQL,
  `DATA_DUCKDB_INGEST_MEMORY_LIMIT` (1GB) / `DATA_DUCKDB_INGEST_THREADS` for reading files at ingest,
  `DATA_QUERY_TIMEOUT_SECONDS` (30), `DATA_ADAPTER_MAX_ROWS` (1,000,000).
- The image carries the DuckDB extensions (delta, iceberg, httpfs, aws, azure, postgres, mysql,
  sqlite): reading a lake or attaching a database no longer downloads anything at query time.

### Added
- **Published records connected to what text says about them** (`resolution/mentions.py`): the
  Detector finds the field that names a record ("Named by" in the Mapper) and published records are
  labelled by it; an entity read from text whose name names exactly one published record is drawn
  SAME_AS it - a person named in a ticket meets the customer of that name. A record's own free text,
  read when its upload asks, hangs what it mentions off the record itself.
- **Mapper: "Re-detect this mapping"** takes what the Detector finds now for one mapping as a new
  version (the previous one kept under Versions), keeping "Publish to graph". The list's "Detect
  from data" button is gone: detection runs after each upload, and what goes into the graph is chosen
  in Add data.
- **`EMBEDDING_WORKER_CONCURRENCY`** (default 1): more embedding processes for large imports; each
  holds the model, so raise `EMBEDDING_WORKER_MEM_LIMIT` with it.
- **The semantic layer, in the Mapper** (`knowledge/semantic`): once an ingestion settles, the
  Detector reads the tables and records and proposes what they mean - entity types, the field that
  identifies each record (never empty, never repeated, the one other data refers to), where each type
  lives (a table's rows, an object or a list inside JSON / YAML / XML records), and the links between
  types (a field whose values are all keys of another type). Nothing about field names is assumed;
  where the data leaves a choice open, the workspace's model picks among the candidates. Types and
  links go where meaning already lives - the type registry (a type's key is its `identify_by`, a link
  a reference rule) and the Mapper (a mapping's `knowledge` section, status `auto`, or `draft` when
  unsure). In the Mapper: Detect from data, a Knowledge panel per mapping (types, identifiers, sources,
  relationships, materialize), Validate (missing sources and columns, empty or repeated identifiers,
  references to nothing, ambiguous joins), Preview, and versions with restore. Settings
  `knowledge_detect` (on) and `knowledge_publish` (off: publish detected entities to the graph).
- **One logical table per type**: fifty licence files are one table; a customer in the CRM file and
  in billing is one record with the fields of both. Questions read types; `POST /schema-mappings/publish`
  makes a type's entities graph nodes (one per identifier) with their links, on request.
- **The plan route** (`retrieval/l2_data/semantic.py`): a question about data becomes a small plan the
  model chooses from options the data offers (which table, what to compute, grouped by what, which
  date, rounding) - it never writes SQL; a compiler does. What the data can settle it settles: a
  record named by its key is that record with the records it links to; a category value in the
  question is a filter; a file named is that file. The rule planner remains as the fallback.
- **The relationship a question asks about** is chosen by the model among the graph's own edge types
  (`retrieval/l0_understanding/tools.py`), added to the rules' plan.
- **Extractors read data where it lives**: `source.data` - an asset, a column, a JSON path, a
  document's chunks or an entity type's records - without copying it into the graph first.
- **The data store** (`stores/data`): delta-rs writes, DuckDB reads, both behind one module.
  `DATA_ROOT/<workspace>/unstructured` (every document's chunks, one table),
  `structured/<asset>` (a table's rows, typed once at ingest, keyed by `_row_key`) and
  `semistructured/<asset>` (a payload's records, the whole record as JSON plus its top-level fields).
  Compacted and vacuumed by the maintenance worker every six hours.
- **Evidence on every extracted fact**: the Mapper's and extractors' nodes and edges, the
  semi-structured walker's records and row mode's rows record which chunk, row or record they rest
  on, at which version, with the quote. Citations read it first; an entity found the same as another
  shares its evidence; a fact whose every source is gone is marked stale, not deleted.
- **Documents in Delta**: chunks are rows, not nodes (no Chunk, Section, layout, table or image nodes);
  passages are searched as before and read back from Delta; extractors read the same chunks.
- **Tables in Delta, or in place**: a file's every row goes to its table whatever the reading mode, so
  a catalog-only table is answerable through SQL; a SQL connector's tables are registered live (read
  through DuckDB ATTACH, never copied); Delta / Iceberg / Hudi connectors are catalogued and registered
  where they are (their ingestion failed before); a moved Delta version or watermark column re-reads
  the source.
- **Column profiles** instead of sample values: null share, distinct count, and for a category column
  its values and counts (`value_set`), which the SQL planner filters with. Personal-data columns are
  flagged and kept out of prompts and the graph.
- **MongoDB** connector: each collection walked like a JSON file of its documents - the same KeyNodes,
  references and categorical values - and read in place. **SQL Server, Oracle, Snowflake, BigQuery,
  Redshift, ...** through an Arrow adapter.
- **The data route** (`retrieval/l2_data`): L0 routes a question about rows to SQL; the planner writes
  it from the schema only (columns, value sets, joins, defined metrics, verified examples) - by rules
  for counts, totals and rankings, which needs no model, or by the generation model; the guard allows
  one read-only SELECT over the workspace's views; DuckDB runs it over managed and external tables in
  one query. SQL wins over graph aggregation for numbers.
- **Answer outcomes**: `ANSWERED`, `PARTIAL`, `UNKNOWN` with a reason (no matching rows, the source is
  unavailable, no source has it) or `CLARIFY` (a name that means two things: the answer asks which).
  A number stated from a query result must be in its rows.
- **Data definitions** (Curation > Data definitions, `/definitions`): Metrics, Segments and verified
  questions with their SQL, each run against the data before it is saved. "Save as verified query" on
  an answer in the assistant. Glossary synonyms bring column and metric names into a question.
- **Asset pages** show the asset's own chunks, rows or records (Data tab).
- **Query decomposition** (`retrieval/decomposition/`), a module above the L0-L4 layers: a question
  one search cannot answer is searched as several, and the results are merged. A comparison of named
  things ("which was published first, RFC 9155 or RFC 9410?") is searched once per thing, so both
  land in the context; the question as asked is always searched too. Setting: Retrieval > Query
  decomposition, `off` / `comparisons` (default, no model needed) / `full` (chained questions are
  also split and bridged by the generation model, which needs a stronger model than the built-in
  Qwen3-0.6B). The `decompose` request flag, accepted and ignored until now, overrides it, and the
  response lists the sub-questions under `decomposition`.
- Optional **answer write-back** (off by default): a fact found through SQL is saved with its query as
  evidence.
- The Models page warns when a generation model is under 3B parameters: fine for short answers, not
  for SQL, routing or multi-step questions.
- `doctor` checks the data store and reports workspaces still on the graph store.
- A gold set (`apps/api/tests/goldset`): a licensing business over CSV, JSON, a live database, a lake
  table and documents, 200 questions of eleven types, and a runner reporting the resolution and
  unsupported-answer rates.

### Changed
- **A simpler product by default.** The sidebar is Assistant, Add data, Connections, Customize,
  Assets, Graph and Review. Customize holds mappings, pipelines, extractors and scheduled syncs;
  Settings holds models, users, quality checks, runs and system health; Types, hand edits and what could not be read are tabs of Review; agent
  conversations are linked from the Assistant; scheduled syncs from Connections. Settings shows four
  controls (workspace name, how strictly answers are checked, AI summaries of files, local folders);
  the engine's tuning knobs keep their defaults and can be set in `.env` as
  `OPENCORTYX_SETTING_<KEY>` (e.g. `OPENCORTYX_SETTING_ABSTAIN_THRESHOLD=0.3`), which wins over the
  database. Types shows the types in this workspace's data (the built-in catalogue on request);
  Pipelines lists the pipelines people built. Nothing is removed: every page keeps its address, each
  of these is one entry in `apps/web/lib/simple.ts`, and `NEXT_PUBLIC_FULL_UI=1` shows everything.
- **Add data is drop, choose, Add.** Two choices for every kind of file: "Extract entities and
  relationships" and "Add the whole data to the graph". A folded Custom section holds a pipeline, a
  mapping that shapes the graph (tables and JSON), extractors (documents) and tags. The per-file
  reading preview and "Use this reading and add it" are gone; the Mapper does that job when wanted.
- **The graph always gets the shape of the data**: a table's columns and a JSON file's keys with their
  types (as before), the links detection finds between them as `JOINS_TO` between those fields, and a
  document's outline - its headings, tables and figures as nodes, the text staying in Delta.
- **"Add the whole data to the graph"** puts the data itself in as nodes too, per upload: every row,
  every JSON key and value, every layout element - the data still in Delta.
- **Entities and relationships read from text without an LLM** (`ingest/extraction/text_reader.py`):
  a new Models role, Entities, runs a spaCy pipeline in-process (by default `spacy/en_core_web_sm`,
  MIT, downloaded from Hugging Face on first use). Types are the model's labels and relationships the
  sentence's verb ("Alice Smith joined Acme" gives Alice Smith -JOIN-> Acme) - nothing listed by
  hand. Documents and free-text fields are read when an upload asks; each entity and relationship
  cites its passage or row and sentence, and is matched by name to the published record it names,
  also when that record is published later.
- Version detection compares a document with documents of the same name only: compared with every
  document, chat sessions that share their headers were proposed as versions of each other by the
  hundred, and each upload read the whole workspace.
- Detection: a key is unique within each file (the same customer in two files is one customer, not a
  repeated key); a link rests on at least three distinct values; the field that names a record is a
  phrase rather than a token (a name over an email), the model deciding only a tie.

### Fixed
- **An upload is "completed" only when it is usable**: stored, embedded and resolved. A run now stays
  running at a last step, "Embedding & resolution", until the asset's resolution finishes; before, it
  said completed while its passages were still being embedded, and a question asked then found
  nothing. Resolution waits for a document's passages in the data store, not only its graph nodes. A
  run waiting more than 6 hours is closed with a note; the stale-run reaper no longer re-ingests one.
- **A document with no extractor and no pipeline is read in one pass**: it was still queued for a
  deferred extraction that found nothing to run, then repeated the post-ingest steps and resolution
  (~2.6 s of CPU per document through a chat import).
- **Detection is no longer booked by document uploads**: it reads tables and JSON only, and re-read
  every table in the workspace every two minutes during a document import.
- **Retrieval no longer discards what it found below its similarity floor**: `/context` returns the
  context marked `low_relevance`, and the answer step's relevance judge decides (fast mode, which runs
  no judge, still refuses). A question whose evidence covered every source came back empty with a best
  similarity of 0.253 against a floor of 0.28.
- An upload whose reading never reported a start (a table read as its schema) stayed `queued` at its
  last step and was never closed; a JSON upload was closed before its embeddings and resolution.
- `/dashboard` was a 404; Assets showed zeros while loading.

## 0.1.0-beta.7 - 2026-10-23

A new installation handles a large first upload: a thousand files dropped on Add data at once are
all accepted and read, and the models it needs, for search and for answers, are already there.
Before, uploads after the 30th were refused, a few hundred queued files could take the job queue
down with them, and the first upload waited on a 600 MB model download. Measured on a 16-CPU PC: 1,004 files (PDF,
HTML, Markdown, reStructuredText, text; 75 MB) sent from Add data in about a minute and a half,
all read in 26 minutes, none failed.

An upload could also stay unsearchable by meaning, with nothing saying so: updating restarted the
embedding worker mid-job, and the job sat unacknowledged for 40 minutes. Questions then found only
keyword matches. Workers now hand their jobs back when stopped, a sweep catches anything still
missed, and `opencortyx doctor` shows it.

### Upgrading
- From 0.1.0-beta.3, beta.4 or beta.6: `opencortyx update`. From 0.1.0-beta.2: by hand, as in
  [Updating](docs/installation/updating.md#from-010-beta2).
- The image no longer has Docling. Documents already read keep their graph; new ones become the same
  node types. The two Docling-only settings (PDF table structure, PDF page window) are gone, and a
  saved value for them is ignored.
- A database migration (`0031_ingest_run_batch`) runs on `opencortyx update`.
- Semi-structured files are no longer linked by guessing from `*_id` fields (REFERENCES edges). To
  link records, add a join edge in the file's mapping; saving it links what is already ingested.
- A new volume, `uploads`, holds uploaded files until their worker has read them.
- New settings, all optional: `UPLOAD_RETENTION_HOURS` (168), `RATELIMIT_UPLOAD_PER_MIN` (600),
  `REDIS_MAXMEMORY` (128mb), `EMBEDDING_WORKER_MAX_MEMORY_KB` (1500000) and the database ports below.
- An existing installation keeps its embedding model (nomic-embed-text-v1.5, 768 dimensions) and its
  vectors; only a new installation starts on all-MiniLM-L6-v2. To move an existing one, add
  `EMBED_DIMENSIONS=384` to `.env`, pick all-MiniLM-L6-v2 on the Models page and re-index.
- Files read after the update are cut at the new chunk size (800 characters): a longer paragraph
  becomes several nodes. Files already read keep their nodes.
- A workspace created after the update starts with a generation model, Qwen3-0.6B, running inside
  OpenCortyx; existing workspaces keep the models they have and can add it on the Models page. The
  image is about 1.2 GB larger, and the API and extraction worker ceilings rise to 4 GB and 3.2 GB
  (`API_MEM_LIMIT`, `EXTRACTION_WORKER_MEM_LIMIT`, `EXTRACTION_WORKER_MAX_MEMORY_KB` 2500000).

### Fixed
- The graph store scanned a workspace's whole index for every lookup of "one of these ids": an
  `IN` list on an index's second field is not served from the index by NebulaGraph. Walking one
  document's parts, checking whether it was embedded and fetching edges by id each read all 179,000
  nodes of a thousand-file workspace (1.6 s and up to ~365 MB per lookup), and a few at once took the
  graph service down. They are exact matches now, one index lookup each: 0.05 s, under 30 MB.
- Qdrant held every vector, the keyword index and the search graph in memory, and was killed 18 times
  for memory during one thousand-file upload; each time, embedding jobs failed and 50 files kept no
  vectors. Vectors, the search graph and the large payload indexes are kept on disk now, with an int8
  copy of the vectors in memory that a search reads first and then rescores: ~450 MB at rest for
  544,000 points, against more than 1 GB. An existing collection is converted on start-up; its
  memory limit is 1.5 GB (`QDRANT_MEM_LIMIT`), as the conversion peaked at 1.33 GB.
- Embedding and resolution jobs gave up after three tries 30 s apart; a store restarting is waited
  out now, as ingest already did (about 16 minutes), and the graph service's memory-pressure errors
  count as passing.
- The 10-minute embedding sweep found missing vectors by reading every node of the last day from the
  graph, the whole of a large upload, and failed on the graph service's memory. It reads what is
  still owed from the vector store instead (each node's keyword entry says whether its vectors
  landed), and takes a node only when the previous sweep also found it.
- Questions could fail all at once while others were being answered: every minute, one of them
  counted every node and edge by type for the question vocabulary (5-13 s and a few hundred MB of
  the graph service on a thousand files), and two such bursts at once passed the graph service's
  memory limit. The vocabulary is kept 15 minutes and refreshed in the background, and the graph
  service has 1.5 GB (`NEBULAGRAPH_MEM_LIMIT`).
- One question could take the graph service down: expanding from a node read all of its neighbours,
  with their properties, before keeping the first few, and the day's Date node has one edge per
  node written that day (185,000 after a thousand files). The cap is applied per node in storage
  now: 0.03 s instead of running out of memory.
- Questions took about 7.7 s, half of it fitting the results into the context: each candidate was
  compared with every other, and both texts were split into words again for each comparison (578
  candidates, ~167,000 comparisons). Each text is split once now: about 2.6 s a question.
- Embedding padded short texts to the longest in their batch: a node's label ran at the length of
  a 2,000-character passage. Texts are sorted by length first now, about twice as fast.
- Reading a document wrote its graph one element at a time, about 17 round trips to the graph store
  each, which was most of an upload's time. A document's elements and links are written in one
  batch now, and the per-node link counts of its own elements in a few bulk writes instead of one
  each: a 30 KB text document went from 26.5 s to 3.8 s, a 950-element HTML page from 16.5 s to 1.6 s.
- Embedding stalled for hours after a large upload. Each finished file asked to embed "every node
  created since it started", so with a thousand files in flight each request loaded nearly the
  whole upload, text and all, and the graph store ran out of memory. A file now embeds only the
  sources it wrote, already queued when it finished. The 10-minute sweep and `opencortyx doctor`
  find missing vectors by node id first and load only the nodes that lack them.
- Two more jobs held the graph store down after a large upload:
  - The dashboard graph's index pass read every node, text included, and every edge with its
    properties, after each upload. It reads only ids, types, labels and the ends of each edge now,
    one type at a time, and reads in full only the assets it lists.
  - Resolving an asset waited for its embeddings by checking every 15 seconds, on the same worker
    that embeds, and gave up after 10 minutes. It now starts when the embedding job finishes the
    asset's vectors, with one check half an hour later in case that job never comes.
- A passage longer than 2,000 characters (a long paragraph, a code block, a table) had no embedding
  at all and was findable by keyword only. It is embedded in overlapping chunks now, each its own
  vector, cut at sentence ends.
- A plain-text file with no headings (an RFC) was handed to extraction as one block; sections now
  close at 4,000 characters.
- The graph store refused queries once the whole Docker VM was 80% full ("Used memory hits the high
  watermark"), failing uploads during a large one. It now judges memory against its own container
  limit (`--containerized=true`).
- A file's extension decides how a document is read: a `.txt`, `.md` or `.html` file was read as
  JSON or XML when a few of its lines happened to parse as such (an RFC with a `[` line, Markdown with
  HTML tags). Only a binary signature, such as a `.txt` that is really a PDF, overrides it.
- Two workers updating the same node at once (every upload touches the day's Date node) failed a
  file with "More than one request trying to add/update/delete one edge/vertex at the same time".
  Such a conflict is retried with backoff now.
- A graph store that was unreachable for a few seconds failed every upload the worker picked up
  in that time, for good (two retries 30 s apart). An infrastructure error is now retried with
  backoff for about 16 minutes while the run shows it is waiting; a fault of the file still fails it.
- Housekeeping jobs piled up behind a long upload (49 sync ticks and 52 browse-tree passes behind one
  PDF). Scheduled ticks now expire when they could not start in time, and one browse-tree pass is
  booked at a time.
- A Python file whose first lines were plain JSON values was read as JSON Lines.
- Uploads shared the 30-a-minute limit of questions, so Add data refused every file after the 30th
  of a folder. Uploads have their own limit (600 a minute), and Add data sends three files at a time
  and retries one the server could not take yet, honouring its Retry-After.
- An uploaded file travelled to its worker inside the queue message, so a few hundred files dropped
  at once filled Redis until it was killed, losing every queued job. The file is now stored once on
  the `uploads` volume and the job carries a reference to it. Redis is configured as a queue: it
  refuses new jobs when full instead of being killed (`noeviction`, `maxmemory`) and keeps its queue
  across restarts (`appendonly`).
- Runs waiting their turn were marked failed after 30 to 45 minutes ("interrupted"), although their
  jobs were still queued: behind a large upload, hundreds of healthy files showed as failed. Only a
  run that started and went quiet is failed now; a waiting run is sent again only when the queue
  has lost it.
- Add data asked about every file of a batch separately every two seconds, and gave up after five
  minutes while reading took hours. It now reads the batch's progress in one request until done,
  and lists each file that was not added with the reason.
- Files uploaded in parallel into one batch could drop each other from its saved ingestion.
- A new workspace's reranker was a model only an `INSTALL_RERANK=1` image can run, so every question
  skipped reranking. New workspaces get `Xenova/ms-marco-MiniLM-L-12-v2`, which every image runs.
- A stopped or updated worker hands its running jobs back to the queue at once (Celery soft
  shutdown), so another worker starts them in seconds. Before, Docker's stop signal went to `sh`,
  which did not pass it on, so Celery was killed mid-job every time.
- The embedding worker runs one process instead of two threads: a thread cannot be cancelled, so
  it could not hand its job back. Its child is recycled between jobs past 1.5 GB
  (`EMBEDDING_WORKER_MAX_MEMORY_KB`).
- A fresh install reported its health as down until a generation model was connected. That is a
  warning now: uploads and search work without one; only the AI Assistant needs it.

### Added
- **Settings > Chunking**: the chunk size and overlap, the section size handed to extraction, whether
  scanned pages and images are read with OCR, and the embedding batch and run sizes (128 nodes and
  4,000 characters by default, the fastest measured).

### Changed
- **A document's metadata is read from its header**: the `Key: value` block many documents open with
  (an RFC's or a PEP's header, front matter, an HTML definition list, a PDF's header table). Its
  title, identifiers ("rfc 9304", read from the filename and the header), publication date and
  fields (status, category, authors, ...) are stored on the document's Source, and a document without
  text of its own shows them in an answer's context. Identifiers and title are names, so a question
  that names a document ("RFC 9304") finds it by exact match.
- **Readers record what a format carries outside the text**: Word and PowerPoint core properties,
  HTML `<title>` and `<meta>`, an e-book's package metadata, a notebook's metadata, Markdown front
  matter (no longer read as the first paragraph), an RST field list, RTF's info group, a workbook's
  properties, a PDF's document information. Merged with the header block: the document's own title
  and date win over a file's stored ones, which are often stale.
- **The Mapper sets how documents are read and linked.** A document mapping's reading card can set
  the paragraph split size for its documents (unset, the Chunk size setting applies; the card's size
  was never applied before, not even to sliding windows), and a new card decides what happens to the
  header: link the documents it names or not, rename fields (a renamed field renames its relation),
  drop fields.
- **A join edge in a semi-structured mapping can name a type already in the graph** ("Already in the
  graph"), matched on any of its attributes: the foreign key links records ingested before, when the
  mapping is saved, and new ones as they arrive.
- **Foreign keys are declared, not guessed.** A semi-structured file's `*_id` values were linked as
  REFERENCES to whatever node in the same file had that id. Links now come only from a mapping's join
  edges ("`customer_id` names a Customer's `id`"), which become reference rules of the resolution
  engine when the mapping is saved: records are linked across files, in either order, and the ones
  already in the graph are linked when the rule is added. Deleting the mapping removes its rules.
- **Documents are linked to the documents they name.** A header value made of identifiers
  ("Obsoletes: 8113", "Requires: 703") is a reference; the resolution engine matches it to the
  document holding that identifier and adds an edge named by the key (`OBSOLETES`, `REQUIRES`),
  whichever of the two was read first.
- **A new installation embeds with all-MiniLM-L6-v2** (384 dimensions), which the image carries,
  instead of nomic-embed-text-v1.5. Measured on this release's thousand-file corpus on CPU: 10x the
  speed (186 against 18.6 texts a second) at under a third of the memory (458 MB against 1.66 GB).
  nomic-embed-text and others stay one Download away on the Models page.
- **A new workspace answers without a model server**: it starts with Qwen3-0.6B, which the image
  carries and which runs inside the API and the extraction worker on CPU (Transformers, about 1.2 GB
  each). Before, a new workspace had no generation model until one was connected by its address.
  Its hidden reasoning passage is turned off, so answers come back without a `<think>` block. A
  larger model is one Download or one server address away on the Models page.
- **Chunk size 800 characters**, within what all-MiniLM-L6-v2 reads (256 tokens). A paragraph, list
  item or caption longer than that becomes several nodes, cut at sentence ends and linked in reading
  order; a table or code block stays one node, searched by windows of that size.
- The dashboard graph's index pass waits at least five times as long as its last pass took before
  the next, so it takes at most a sixth of the time however large the graph is.
- **Docling is replaced by one reader per format** (`ingest/formats/unstructured/layout/readers/`).
  PDFs are read with LiteParse: measured on a 34-page PDF, about 1 s instead of 32-41 s and tens of MB
  instead of 0.9-2.4 GB. Images are read by OCR (Tesseract, offline). Word and PowerPoint are read
  by their styles and slides, HTML and EPUB by their tags, Markdown, plain text, reStructuredText and
  notebooks by their structure, RTF through striprtf; legacy and OpenDocument office files (.doc,
  .ppt, .odt, .odp, .ods) and Apple iWork files are converted to PDF with LibreOffice first. Every
  reader produces the same elements as before (title, sections with levels, text, list items,
  tables with grids, pictures, page headers and footers), so documents become the same graph.
  Docling, its models (~700 MB), PyMuPDF and pypdf are gone from the image.
- New formats accepted on upload: images (.png, .jpg, .tiff, ...), .rtf, .epub, .ipynb, .odp, .ods,
  .docm, .pptm, .ppt, .pages, .key, .numbers, .xhtml.
- **Settings > Chunking > Read scanned pages with OCR** replaces the two PDF settings that only
  applied to Docling.
- The models a new workspace uses for search (the embedder, the keyword model and the reranker) ship
  in the image, so a fresh installation never downloads them.
- `GET /api/v2/uploads/batches/{batch_id}`: a batch of uploads at a glance.
- Every 10 minutes, a sweep embeds any node from the last 24 hours that is still without vectors.
- `opencortyx doctor` checks:
  - that a worker listens on every queue;
  - jobs waiting and running, warning about one held over 40 minutes;
  - that the models in use are downloaded;
  - that recent uploads have vectors;
  - which reranker each workspace uses;
  - the upload store and its free disk.
  See [Troubleshooting](docs/installation/troubleshooting.md#workers-and-search).
- `OPENCORTYX_POSTGRES_PORT`, `OPENCORTYX_QDRANT_PORT` and `OPENCORTYX_NEBULA_PORT` change the
  database ports when another server already uses them.
- `tests/integration/test_hybrid_retrieval.py`: one question aimed at each search surface (vectors,
  keywords, identifiers, misspelt names, entities, dates, types, the graph), each checking the
  answer is found and that its surface returned results.

## 0.1.0-beta.6 - 2026-10-17

Security fixes from an audit, and a sturdier Windows installer. (0.1.0-beta.5 was not published: its
web image failed to build, and its tag was withdrawn.)

### Upgrading
- From 0.1.0-beta.3 or 0.1.0-beta.4: `opencortyx update`.
- From 0.1.0-beta.2: by hand, as in [Updating](docs/installation/updating.md#from-010-beta2), with
  `v0.1.0-beta.6` as the tag.
- Editors can no longer add, change or download models; an admin does that now.
- No database migration and no new settings in this release.

### Changed
- Release images are built natively on Intel and ARM machines, then joined into one
  multi-architecture image. The ARM build ran emulated, and the web image's emulated build hung or
  failed in two releases. CI now also builds the web image on ARM before anything is tagged.

### Fixed
- An installer error closed the PowerShell window it was run in (`irm ... | iex`), taking the
  message with it. The error is now shown and the window stays open.
- A failed download left a half-written folder that the next installer run then tried to update.
  The installer removes a folder it created when the download fails.

### Security
- Managing models is admin-only. Editors could add or change models and pull or download them,
  and any signed-in user could have the server list a model server's models, which let them make
  the API contact any address (server-side request forgery) or point the generation model at
  their own server and receive the prompts sent to it. Viewing the model list stays open.
- A model run inside OpenCortyx loads safetensors weights only, never a pickled checkpoint, which
  can run code when loaded.
- `sharp` 0.35.5 (a vulnerability in its SVG library) and `next` 15.5.28.

## 0.1.0-beta.4 - 2026-10-17

Installing, updating and getting back into an installation, made dependable.

### Upgrading
- From 0.1.0-beta.3: `opencortyx update`.
- From 0.1.0-beta.2: not with `opencortyx update`; switch to the tag and start it by hand, as in
  [Updating](docs/installation/updating.md#from-010-beta2) (with `v0.1.0-beta.4` as the tag).
- No database migration and no new settings in this release.

### Added
- `opencortyx reset-password <email>`: set a new password for an account from the machine running
  OpenCortyx. A forgotten admin password used to mean starting over, since there is no default
  credential and no email recovery.

### Fixed
- Avast flagged `opencortyx.ps1` as a threat (IDP.Generic) on Windows. The Windows tooling started
  PowerShell with `-ExecutionPolicy Bypass` and then polled the API with web requests in a loop,
  which is what behaviour-based antivirus looks for. It now runs with `RemoteSigned` (local scripts
  run; unsigned scripts from the internet are refused) and waits on Docker's own health checks.
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
