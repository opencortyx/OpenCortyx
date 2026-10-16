"""models come only from the Model Registry — the bundled vLLM server and its rows are gone

OpenCortyx no longer ships a model server, and nothing resolves a model from `.env` any more: a
workspace's registry rows are the only source. So:

1. Rows that point at the bundled server (`http://vllm:8000/...`, or provider `vllm` with no address
   of its own, which resolved to it) are deleted. They were seeded automatically, never chosen, and
   name a server that no longer exists. A workspace left without a generation model says so and asks
   for one on the Models page.
2. Every other `vllm` row is an OpenAI-compatible server the user runs themselves: its provider
   becomes `openai_compatible`, its address and model are kept.
3. Model names lose the "(built-in ...)" suffix the seeded rows carried; the provider column already
   says where a model runs.
4. Leftover `ollama` rows get the same treatment as when Ollama was retired: embedding rows become
   the in-process fastembed model (same nomic weights, same 768 dimensions), the rest become
   `openai_compatible` against Ollama's OpenAI API (`.../v1`); one with no address is deleted.
5. Rows of the retired TEI, Infinity, Cohere Rerank and Jina Rerank providers are deleted: nothing
   can call them any more, and the role they filled shows as not connected until another is chosen.

Idempotent. Downgrade does nothing: the deleted rows pointed at a server this version cannot start.
"""
from alembic import op

revision = "0030_registry_only_models"
down_revision = "0029_workspace"
branch_labels = None
depends_on = None

S = "opencortyx_v2"


def upgrade() -> None:
    op.execute(f"""
        DELETE FROM {S}.model
        WHERE base_url LIKE 'http://vllm:%'
           OR (lower(provider) = 'vllm' AND COALESCE(base_url, '') = '')""")
    op.execute(f"UPDATE {S}.model SET provider = 'openai_compatible' WHERE lower(provider) = 'vllm'")

    op.execute(f"""
        UPDATE {S}.model
        SET provider = 'fastembed', base_url = NULL,
            model_id = CASE WHEN model_id IN ('nomic-embed-text', 'nomic-embed-text:latest')
                            THEN 'nomic-ai/nomic-embed-text-v1.5' ELSE model_id END
        WHERE lower(provider) = 'ollama' AND role = 'embedding'""")
    op.execute(f"DELETE FROM {S}.model WHERE lower(provider) = 'ollama' AND COALESCE(base_url, '') = ''")
    op.execute(f"""
        UPDATE {S}.model
        SET provider = 'openai_compatible',
            base_url = CASE WHEN rtrim(base_url, '/') LIKE '%/v1' THEN rtrim(base_url, '/')
                            ELSE rtrim(base_url, '/') || '/v1' END
        WHERE lower(provider) = 'ollama'""")
    op.execute(f"DELETE FROM {S}.model WHERE lower(provider) IN ('tei', 'infinity', 'cohere', 'jina')")

    op.execute(fr"""
        UPDATE {S}.model SET name = regexp_replace(name, '\s*\(built-in[^)]*\)\s*$', '')
        WHERE name ~ '\(built-in[^)]*\)\s*$'""")


def downgrade() -> None:
    pass
