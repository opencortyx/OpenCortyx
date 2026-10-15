# ADR-004: Three stores behind two ports

## Status
Accepted (2026-09-18). Supersedes [ADR-001](001-postgres-only-storage.md).

## Context
ADR-001 put the graph, the vectors, the keyword index and the catalogue in one PostgreSQL
database. That bought one backup and one transaction boundary, and it held while the graph was
small.

It stopped holding for the reasons ADR-001 named as its own risk. Graph traversal in SQL is a
recursive CTE per hop, and its cost grows with the whole table rather than with the neighbourhood
being walked, so multi-hop retrieval degraded as a function of corpus size rather than query size.
Vector search and relational writes contended for the same connection pool and the same buffer
cache, so an ingest could slow retrieval. Tuning one workload meant detuning the other.

The alternative is not "use more databases" but "stop pretending one engine is good at four jobs".

## Decision
Three stores, each doing what it is built for, and only two of them reachable from the application
as anything other than a port:

- **NebulaGraph** holds nodes and edges. Reached only through `graph_store.GraphStore`.
- **Qdrant** holds one point per searchable surface — a dense vector per attribute, a BM25
  document per node. Reached only through `vector_store.VectorStore`.
- **PostgreSQL** holds what is genuinely relational: the ontology (`type_registry`) and the
  control plane (users, API keys, collections, policies, connectors, pipelines, schema mappings,
  extraction templates, ingestion runs, audit log). Reached through SQLAlchemy, as before.

Each port has a `memory` adapter, which is what makes the layers above testable without any
infrastructure at all.

`core.substrate.NodeRepo` is the only writer. It keeps the graph and its search surfaces in step,
because the cross-store consistency that ADR-001 got from a transaction now has to be a property
of one code path instead.

## Consequences
- Traversal cost follows the neighbourhood, not the corpus. This was the point.
- Retrieval and ingestion no longer contend for one pool, and each store is tuned separately.
- A new graph or vector engine is an adapter, not a migration.
- **The transaction boundary is gone.** A write can land in one store and not the other, so
  `NodeRepo` quarantines what did not land and replays it, and `core.substrate.accounting` counts
  attempted against written so a lost write is a number rather than a silence. This machinery
  exists because of this decision and would be unnecessary under ADR-001.
- Backup and restore span three systems (`bin/opencortyx backup`), and operators run three
  services instead of one. That is the price, and it is a real one.
