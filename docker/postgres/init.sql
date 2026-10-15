-- Runs once when the Postgres volume is first created.

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";      -- UUID generation
CREATE EXTENSION IF NOT EXISTS "vector";          -- pgvector for embeddings
CREATE EXTENSION IF NOT EXISTS "pg_trgm";         -- Trigram similarity search
CREATE EXTENSION IF NOT EXISTS "btree_gin";       -- GIN index support
