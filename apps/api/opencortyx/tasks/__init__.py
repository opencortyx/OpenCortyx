"""Celery tasks, one module per responsibility.

    ingest       one task per source kind: documents, JSON, tables, SQL, lake files
    sync         keeping an ingestion in step with its live source
    enrich       extractors, corpus enrichment, domain seeding, KPI scheduling
    graph        dashboard cluster hierarchy, embedding nodes and sources
    maintenance  integrity audit, stale-run reaping, quarantine replay
    _shared      helpers the task modules share

Every task has an explicit `name=`, so where a task lives is not part of the wire format.
"""
