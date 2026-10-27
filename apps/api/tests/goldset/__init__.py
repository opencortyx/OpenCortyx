"""Gold set v1: a licensing business over every kind of source, and 200 questions with computed answers.

    domain.py     the business and its sources (CSV, JSON, a live SQLite database, a lake table, documents)
    questions.py  200 questions of eleven types, each with its expected answer or outcome
    run.py        write the sources, load them through the normal ingestion paths, ask, score

The resolution rate and the unsupported-answer rate it reports are the release gates of
docs/knowledge-layer-implementation.md (section 6). Not collected by pytest: it needs a running stack.
"""
