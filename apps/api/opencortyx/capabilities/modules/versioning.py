"""Versioning capability — SimHash near-duplicate detection across ANY datasource.

Generalises the old document-only `_detect_doc_version`: the "unit" being versioned is whatever
node the ingest produced as its source (a `Source` for documents/JSON, a dataset node for tabular).
We fingerprint the unit's canonical text, store it on the unit, then do an O(1)-style integer
Hamming compare against other units of the SAME node type in the org; a near-duplicate draws
`SUPERSEDES` (new → old) and retires the old unit + its derived nodes so retrieval ignores them.

Extract-&-wrap: the fingerprint/compare/supersede mechanics are the same as the proven doc path
(ingest/unstructured/simhash.py + the relocated detection SQL) — only the "what is the unit" step is generalised.
"""
from __future__ import annotations

import uuid

from opencortyx.capabilities.base import Capability, ConfigField, IngestContext
from opencortyx.core.substrate import NodeRepo
from opencortyx.stores.graph import Node
from opencortyx.ingest.unstructured.simhash import simhash64, hamming, to_str, from_str
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

_repo = NodeRepo()

# node types whose text we fold into a unit's fingerprint when the unit itself carries no text
# (structured datasets describe themselves via their columns; JSON records via their child nodes).
_CHILD_EDGES = ["CONTAINS", "HAS_ATTRIBUTE", "HAS_SEGMENT", "HAS_METRIC"]
# bookkeeping / volatile props that are NOT content — excluded so a re-ingest's fingerprint reflects
# the DATA, not its ingest timestamp or version counter (which would inflate the Hamming distance).
_VOLATILE_PROPS = {"native", "filename", "mime", "embedding", "language", "word_count", "checksum",
                   "created_date", "modified_date", "__audit_version", "version", "__source",
                   "ordinal", "level"}


async def _canonical_text(workspace_id: uuid.UUID, unit: Node) -> str:
    """A stable, content-first text of a unit for fingerprinting.

    Documents and JSON: the text of the unit's child chunks or records, so a lightly edited v2 stays a
    few bits from v1. Structured datasets have no child text, so their shape stands in: label, salient
    properties, and the labels and sample values of their columns, segments and metrics. Word-level
    SimHash is order-robust, so ordering does not matter."""
    kids = await _repo.find(workspace_id, source_refs=[unit.id], limit=4000)
    content = [(c.properties or {}).get("text") or (c.properties or {}).get("statement") or "" for c in kids]
    content = [t for t in content if t]
    if content:
        return "\n".join(content)
    parts: list[str] = [unit.label or ""]
    for k, v in (unit.properties or {}).items():
        if not k.startswith("__") and k not in _VOLATILE_PROPS and isinstance(v, (str, int, float)):
            parts.append(f"{k}={v}")
    for _e, n in (await _repo.neighbors(workspace_id, [unit.id], edge_types=_CHILD_EDGES, direction="out"))[:1000]:
        sv = (n.properties or {}).get("sample_values")
        parts.append(f"{n.label or ''} {sv if isinstance(sv, (list, str)) else ''}")
    parts += [c.label or "" for c in kids[:1000]]
    return "\n".join(str(x) for x in parts if str(x).strip())


async def detect_version(workspace_id: uuid.UUID, unit_id: uuid.UUID, max_distance: int) -> dict | None:
    """Fingerprint `unit_id` (storing `__simhash` if absent), then find the nearest other unit of the
    same node type AND the same name within `max_distance` bits. On a match the newer unit supersedes
    the older one, or the supersession is proposed for review. Best-effort; never raises.

    The same name: a new version is the same file uploaded again with changes. Compared with every
    unit of the type, short texts that share their wording (chat sessions with the same headers) were
    proposed as versions of each other by the hundred, and each upload read the whole workspace."""
    try:
        unit = await _repo.get(workspace_id, unit_id)
        if unit is None:
            return None
        fingerprint = from_str((unit.properties or {}).get("__simhash"))
        if fingerprint is None:
            fingerprint = simhash64(await _canonical_text(workspace_id, unit))
            if not fingerprint:
                return None
            await _repo.patch_properties(workspace_id, [unit.id], {"__simhash": to_str(fingerprint)}, bump_version=False)

        best, best_d = None, 65
        for other in await _repo.find(workspace_id, exact_types=[unit.type], label=unit.label):
            props = other.properties or {}
            if other.id == unit.id or props.get("superseded") in ("true", "proposed"):
                continue
            value = from_str(props.get("__simhash"))
            if value is not None and hamming(fingerprint, value) < best_d:
                best, best_d = other, hamming(fingerprint, value)
        if best is None or best_d > max_distance:
            return {"fingerprinted": True, "superseded_source": None}

        new, old = unit, best
        if old.created_at and new.created_at and old.created_at > new.created_at:
            new, old = old, new

        from opencortyx.core.config import runtime as rc
        if rc.simhash_version_review():
            await _repo.patch_properties(workspace_id, [old.id], {
                "superseded": "proposed", "superseded_by": str(new.id), "resolution_kind": "version",
                "version_distance": best_d,
                "resolution_about": f"'{new.label}' looks like a new version of '{old.label}' "
                                    f"({best_d}/64 bits differ)"})
            return {"fingerprinted": True, "proposed_supersede": str(old.id), "distance": best_d}

        await apply_version_supersede(workspace_id, new.id, old.id)
        return {"fingerprinted": True, "superseded_source": str(old.id), "distance": best_d}
    except Exception as e:
        log.warning("capability.detect_version_failed", capability="versioning", error=str(e))
        return None


async def apply_version_supersede(workspace_id: uuid.UUID, new_id: uuid.UUID, old_id: uuid.UUID) -> None:
    """Draw SUPERSEDES (new → old) and retire the old unit, its chunks or records, and what was derived
    from them; retrieval hides superseded nodes. Used by auto-apply and by an approved proposal."""
    await _repo.add_edge(workspace_id=workspace_id, source_id=new_id, target_id=old_id, type="SUPERSEDES")
    derived = await _repo.descendants(workspace_id, [old_id], max_depth=2)
    await _repo.patch_properties(workspace_id, [n.id for n in derived], {"superseded": "true"})
    await _repo.patch_properties(workspace_id, [old_id], {"superseded": "true", "superseded_by": str(new_id)})


class SimHashVersioning(Capability):
    name = "versioning"
    title = "Version Matching (SimHash)"
    group = "Versioning"
    description = ("Detect when a newly-ingested source is a near-duplicate 'v2' of an existing one "
                   "(64-bit SimHash fingerprint, tenant-scoped) and link them with SUPERSEDES so "
                   "retrieval ignores the stale version. Works for documents, tables and JSON.")
    ui_route = "/dashboard/settings?tab=capabilities"
    enable_key = "simhash_versioning"

    def config_schema(self):
        return [
            ConfigField("simhash_versioning", "Enabled", "bool", default=True,
                        help="Detect near-duplicate re-uploads across any datasource and supersede "
                             "the older version. O(1)-style integer compare — no vector scan."),
            ConfigField("simhash_max_distance", "Version match sensitivity", "int", default=5,
                        min=0, max=16,
                        help="Max bit-difference (of 64) between two fingerprints to treat the newer "
                             "as a version of the older. Lower = stricter (near-identical only); "
                             "~5 catches a lightly-edited v2; unrelated items sit ~25 bits apart."),
    ]

    async def on_source_ingested(self, ctx: IngestContext) -> dict:
        from opencortyx.core.config import runtime as rc
        if ctx.source_ref is None:
            return {"skipped": "no source unit"}
        res = await detect_version(ctx.workspace_id, ctx.source_ref, rc.simhash_max_distance())
        return res or {"skipped": "no result"}
