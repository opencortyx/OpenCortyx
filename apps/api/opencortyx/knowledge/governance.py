"""Which ontology types an organization has switched off.

"Off" on the Nodes & Relationships page means hidden from the graph AND skipped by extraction.
Extraction reads the set once per run through a short cache that a status change clears.
"""
import time

from opencortyx.domain.ontology import visible_types

_GATE_CACHE: dict = {}          # workspace_id → {"exp", "off"}: each organization's own set


async def disabled_types(db, workspace_id) -> set:
    """{(kind, name)} of this organization's ontology types switched off. Cached ~30 s."""
    key, now = str(workspace_id), time.time()
    hit = _GATE_CACHE.get(key)
    if not hit or hit["exp"] <= now:
        rows = await visible_types(db, workspace_id)
        hit = {"exp": now + 30, "off": {(r.kind, r.name) for r in rows if r.status == "disabled"}}
        _GATE_CACHE[key] = hit
    return hit["off"]


def bust_gate_cache(workspace_id=None) -> None:
    """Drop one organization's cached set, or every organization's."""
    if workspace_id is None:
        _GATE_CACHE.clear()
    else:
        _GATE_CACHE.pop(str(workspace_id), None)
