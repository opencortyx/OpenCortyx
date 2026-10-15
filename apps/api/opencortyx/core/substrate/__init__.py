"""The graph write path: NodeRepo and the machinery that makes a write accountable.

These five modules are one responsibility, not five. `repo.NodeRepo` is the only way the
application reads and writes the graph; the rest exist so that a write which does not land is
visible rather than lost:

    repo.py         NodeRepo — merge-not-replace upsert, audit stamps, date links, the search surfaces
    validation.py   what the ontology permits, checked before the write
    document.py     what a node SAYS — the one definition shared by every reader and writer
    quarantine.py   durable capture + replay of writes that could not land (an edge whose endpoint
                    does not exist yet is replayed, never dropped)
    accounting.py   counts attempted vs written, so a lost write shows up as a number

`from ...core.substrate import NodeRepo` is unchanged: this was a single module until the
supporting pieces outgrew living beside it, and splitting the file should not move the import.
"""
from opencortyx.core.substrate.repo import (
    APP_MEMORY_SOURCE, AUDIT_VERSION, CONVERSATION_FLAG, DELETED, NON_EVIDENCE_FLAG, NodeRepo,
    in_scope, window_open,
)

__all__ = ["NodeRepo", "in_scope", "window_open", "APP_MEMORY_SOURCE", "AUDIT_VERSION", "CONVERSATION_FLAG",
           "DELETED", "NON_EVIDENCE_FLAG"]
