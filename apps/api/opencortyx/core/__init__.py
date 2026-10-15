"""The runtime kernel: what every capability is built FROM, and never the other way round.

Grouped by responsibility rather than by file, each group with its own public surface:

    config/      static settings (env.py) and the live-tunable knobs (runtime.py)
    db.py        the async engine, sessions, and schema DDL
    substrate/   the graph write path — NodeRepo, validation, quarantine, accounting
    inference/   models: llm, embedding, the registry that resolves them, warm-up
    security/    auth, secrets at rest, rate limiting
    worker/      the Celery app and the async bridge

Nothing here may import `ingest`, `retrieve`, `capabilities`, `ontology` or `bootstrap`: `core` is
the bottom of the runtime, and a kernel that reaches up into its callers cannot be reasoned about,
tested or replaced in isolation. `tests/test_module_boundaries.py` enforces that, with a
shrink-only allowlist for the modules still being untangled.
"""
