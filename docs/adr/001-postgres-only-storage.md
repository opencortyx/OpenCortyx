# ADR-001: PostgreSQL as the only datastore

## Status
Superseded by [ADR-004](004-three-stores-behind-two-ports.md) (2026-09-18).

Kept because the reasoning still explains why the single-store option was taken seriously, and the
consequences listed below are exactly what was given up. Every concrete claim about the schema
is now false: there are no `node`, `edge` or `node_attr_embedding` tables, and pgvector is not used.

## Context
A context layer needs vectors, a knowledge graph, keyword search and a relational catalogue. The
usual approach runs a separate system for each (a vector database, a graph database, a search
engine), which multiplies operations, backups, security boundaries and consistency problems.

## Decision
Store everything in one PostgreSQL database with the pgvector extension, in the `opencortyx_v2`
schema:

- `node` and `edge` hold the graph. Nodes carry a JSONB `properties` document, an embedding
  column (pgvector, HNSW index) and full-text search data. Both tables are hash-partitioned.
- `type_registry` holds the ontology: which node, edge and entity types may exist.
- `node_attr_embedding` holds per-attribute embeddings for attribute-level retrieval.
- Control-plane tables (users, API keys, pipelines, schema mappings, extraction templates,
  ingestion runs, write accounting, audit log) live in the same schema.
- Redis is used only as the Celery broker and result backend; it holds no durable data.

Schema changes go through Alembic migrations, run by the one-shot `setup` service (formerly `migrate`).

## Consequences
- One backup (`bin/opencortyx backup`) captures all data, and one restore brings it back.
- Transactions cover graph, vector and catalogue writes together.
- New node shapes need no migration because properties are JSONB.
- Very large deployments depend on Postgres scaling (partitioning, HNSW tuning, read replicas)
  rather than on specialised engines.
