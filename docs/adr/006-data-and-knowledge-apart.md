# ADR-006: Data and knowledge are stored apart

## Status
Accepted (2026-10-23), implemented in 0.2.0. Extends [ADR-004](004-three-stores-behind-two-ports.md).
The full design is [docs/knowledge-layer.md](../knowledge-layer.md).

## Context
Every chunk of every document and every row of every table became a graph node. The graph then held
two different things - the knowledge extracted from the data, and the data itself - and paid for
both: a thousand files were 185,000 nodes, most of them chunk text the graph only stored; a table of
a million rows was a million nodes, or (catalog mode) none of its rows were answerable at all; and a
counting question was answered by counting whatever retrieval happened to return.

## Decision
- **Data** lives in **Delta Lake tables** under `DATA_ROOT/<workspace>/`: one `unstructured` table
  for every document's chunks, one table per structured asset, one per semi-structured asset. Data
  already in a customer's lake or database stays there and is read in place.
- **Knowledge** stays in the **graph**: what the Mapper and the extractors produce (entities, facts,
  relationships, KeyNodes, reference edges, categorical values) and the data layer (assets, their
  Attributes with value sets, joins, Metrics, Segments, verified queries).
- **Evidence** joins them: every extracted fact points at the chunk, row or record it came from, at
  the version it was read (`evidence`, in PostgreSQL).
- **delta-rs writes, DuckDB reads**, both behind `stores/data` and nowhere else. Embeddings stay in
  Qdrant, an index over passages whose text is in Delta.
- Questions about rows are answered with **SQL** (`retrieval/l2_data`), guarded to one read-only
  SELECT over the workspace's views.

## Consequences
- The graph holds knowledge only; its size follows what was extracted, not how much text there is.
- Every number a data question gets comes from all the rows, with the SQL and versions shown.
- Every fact can be opened at its source, at the version it was read from.
- A workspace from before 0.2.0 keeps its data as graph nodes (`data_store = graph`, deprecated) until
  it is reset and re-ingested; both paths work, and `doctor` reports the old one.
- Two more engines to pin together (DuckDB with its extensions, delta-rs), tested as one set
  (`tests/test_data_store.py`).
