"""Permanent deletion of an ingested ASSET — the asset node and everything that exists only
because of it.

"Everything" is defined by PROVENANCE, not by adjacency: deleting an asset must not follow a
plain relationship edge into a neighbouring asset (a foreign-key edge between two tables, an entity
shared with another document). Two provenance signals are followed, transitively:

  • `node.source_ref` — the substrate's provenance pointer (Source → chunk → claim/entity/…,
    Table → Attribute/row, Source → profile section / comment / Date-bookkeeping children).
  • `node.properties[parent_table_id | table_id | affected_table_id | target_table_id]` — derived business
    nodes from structured ingestion carry the Table id in properties instead of a source_ref.

Nodes shared across assets (a global entity with `source_ref IS NULL`) are deliberately KEPT — they
are not "of" this asset.

Beyond the graph, a full delete also clears the asset's traces elsewhere so it cannot come back and
does not leave phantoms in the UI:
  • its edges and search surfaces,
  • its Progress runs (`ingest_run`),
  • its entry in every ingestion's change-detection manifest (`ingestion.config["sync_state"]`) —
    without this a watched file stays "unchanged" forever and is never re-ingested.
"""
import uuid

from sqlalchemy import delete as sa_delete, or_, select

from opencortyx.core.db import V2Session
from opencortyx.core.substrate import NodeRepo
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

# properties keys that point BACK at an asset (derived structured nodes carry these, not source_ref)
_REF_KEYS = ("parent_table_id", "table_id", "affected_table_id", "target_table_id")
_MAX_DEPTH = 24            # provenance chains are shallow (source→chunk→claim); a cap makes cycles safe


async def collect_asset_nodes(workspace_id: uuid.UUID, asset_id: uuid.UUID) -> dict:
    """Transitive provenance closure of `asset_id`, including the asset, as {id: node}."""
    repo = NodeRepo()
    asset = await repo.get(workspace_id, asset_id)
    if asset is None:
        return {}
    closure = {str(asset.id): asset}
    by_ref: dict[str, list] = {}
    if asset.type != "Source":
        for node in await repo.find(workspace_id):
            for key in _REF_KEYS:
                value = (node.properties or {}).get(key)
                if value:
                    by_ref.setdefault(str(value), []).append(node)
    frontier = [str(asset.id)]
    for _ in range(_MAX_DEPTH):
        if not frontier:
            break
        found = await repo.find(workspace_id, source_refs=frontier)
        for ref in frontier:
            found += by_ref.get(ref, [])
        frontier = []
        for node in found:
            if str(node.id) not in closure:
                closure[str(node.id)] = node
                frontier.append(str(node.id))
    return closure


def _breakdown(nodes) -> dict:
    """{node type: count} over the closure — what the confirm dialog shows the user."""
    out: dict = {}
    for node in nodes:
        out[node.type or "?"] = out.get(node.type or "?", 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


async def _purge_runs(workspace_id, asset_id, source_key) -> int:
    """Drop the asset's Progress history. `source_ref` on a run is the source_id (documents/JSON) or
    the source_key (structured). A source_key is shared by every table of one connection, so it is
    only used once no node carries it any more (i.e. the last table of that source was deleted).

    Runs written before the executors recorded a source ref have `source_ref IS NULL`, so they are
    also matched on the `source_id` / `source_key` their stored result carries — otherwise deleting
    the asset would leave an unattributable phantom on the Progress tab."""
    from opencortyx.domain import IngestRun
    refs = {str(asset_id)}
    try:
        if source_key and not await NodeRepo().count(workspace_id, source_key=str(source_key)):
            refs.add(str(source_key))
        async with V2Session() as db:
            ref_list = list(refs)
            res = await db.execute(sa_delete(IngestRun).where(
                IngestRun.workspace_id == workspace_id, or_(
                    IngestRun.source_ref.in_(ref_list),
                    IngestRun.result["source_id"].astext.in_(ref_list),
                    IngestRun.result["source_key"].astext.in_(ref_list))).returning(IngestRun.id))
            gone = [str(r) for (r,) in res.all()]
            await db.commit()
        # The upload ingestions that listed these runs would otherwise point at nothing and read
        # RUNNING forever on the Ingestion page.
        from opencortyx.ingest.runs.progress import forget_upload_runs
        await forget_upload_runs(workspace_id, gone)
        return len(gone)
    except Exception:
        return 0


async def _purge_manifest(workspace_id, asset_id) -> int:
    """Forget the asset in every saved ingestion's sync manifest, so a watched source re-ingests it
    as `added` instead of skipping it as `unchanged`."""
    from opencortyx.domain import Ingestion
    from opencortyx.ingest.sources.sync import MANIFEST_KEY
    dropped = 0
    try:
        async with V2Session() as db:
            rows = (await db.execute(select(Ingestion).where(Ingestion.workspace_id == workspace_id))).scalars().all()
            for ing in rows:
                cfg = dict(ing.config or {})
                man = cfg.get(MANIFEST_KEY) or {}
                if not isinstance(man, dict):
                    continue
                kept = {p: e for p, e in man.items()
                        if str((e or {}).get("source_id") or "") != str(asset_id)}
                if len(kept) != len(man):
                    dropped += len(man) - len(kept)
                    cfg[MANIFEST_KEY] = kept
                    ing.config = cfg              # reassign — JSONB needs a new object to be flushed
            if dropped:
                await db.commit()
            return dropped
    except Exception:
        return dropped


async def preview_asset_delete(workspace_id: uuid.UUID, asset_id: uuid.UUID) -> dict | None:
    """What a delete WOULD remove. None when the asset doesn't exist in this org."""
    closure = await collect_asset_nodes(workspace_id, asset_id)
    if not closure:
        return None
    asset = closure[str(asset_id)]
    edges = await NodeRepo().edges(workspace_id, node_ids=list(closure))
    return {"id": str(asset_id), "label": asset.label, "type": asset.type,
            "nodes": len(closure), "edges": len(edges), "by_type": _breakdown(closure.values())}


async def purge_asset_full(workspace_id: uuid.UUID, asset_id: uuid.UUID) -> dict | None:
    """Delete the asset permanently. Idempotent; None when the asset doesn't exist in this org."""
    closure = await collect_asset_nodes(workspace_id, asset_id)
    if not closure:
        return None
    repo = NodeRepo()
    asset = closure[str(asset_id)]
    source_key = (asset.properties or {}).get("source_key")
    n_edges = len(await repo.edges(workspace_id, node_ids=list(closure)))
    # published records read from this asset alone: FROM_ASSET into it and into no other asset
    backed = await repo.edges(workspace_id, target_ids=[asset_id], types=["FROM_ASSET"])
    recs = list({e.source_id for e in backed})
    elsewhere = {e.source_id for e in (await repo.edges(workspace_id, source_ids=recs, types=["FROM_ASSET"]) if recs else [])
                 if e.target_id != asset_id}
    only_here = [r for r in recs if r not in elsewhere and str(r) not in closure]
    try:
        from opencortyx.ingest.sources.sync import unsupersede_predecessors
        await unsupersede_predecessors(workspace_id, list(closure))
    except Exception as _e:                           # noqa: BLE001 - never block the delete itself
        log.warning("asset_purge.unsupersede_failed", asset_id=str(asset_id), error=repr(_e))
    n_nodes = await repo.delete_nodes(workspace_id, list(closure))
    runs = await _purge_runs(workspace_id, asset_id, source_key)
    manifest = await _purge_manifest(workspace_id, asset_id)
    data = await _purge_data(workspace_id, asset_id)
    # what the asset alone made, outside its own subtree: its published records, the named things no
    # other asset still states, and a cluster's master left with no member
    gone = only_here + list(data.pop("orphans", []) or [])
    masters = list({e.target_id for e in (await repo.edges(workspace_id, source_ids=gone, types=["MEMBER_OF"]) if gone else [])})
    extra = await repo.delete_nodes(workspace_id, gone) if gone else 0
    if masters:
        kept = {e.target_id for e in await repo.edges(workspace_id, target_ids=masters, types=["MEMBER_OF"])}
        empty = [m for m in masters if m not in kept]
        extra += await repo.delete_nodes(workspace_id, empty) if empty else 0
    return {"deleted": True, "id": str(asset_id), "label": asset.label, "type": asset.type,
            "nodes": n_nodes + extra, "edges": n_edges, "runs": runs, "manifest_entries": manifest,
            "data": data, "records_and_entities": extra, "by_type": _breakdown(closure.values())}


async def _purge_data(workspace_id, asset_id) -> dict:
    """The asset on the data plane: its managed rows or chunks, its search passages, its evidence and
    source keys, and its registry row. A customer's own external data is never touched. Entities
    other assets still back keep their evidence. One left with none is marked stale - or, when it is
    a named thing a reader or an extractor made (`__identity: entity`), returned as an orphan for
    the caller to delete: nothing states it any more."""
    from opencortyx.core.dataplane import assets, evidence
    try:
        row = await assets.get(workspace_id, asset_id)
        if row is None:
            return {}
        lost = await evidence.drop_for_asset(workspace_id, asset_id)
        if row.kind == "unstructured":
            from opencortyx.ingest.unstructured import dataplane as _udata
            await _udata.drop_passages(workspace_id, asset_id)
        await assets.remove(workspace_id, asset_id)
        stale = await evidence.without_evidence(workspace_id, lost)
        ids = [uuid.UUID(str(i)) for i in stale]
        named = {n.id for n in await NodeRepo().get_many(workspace_id, ids)
                 if n is not None and (n.properties or {}).get("__identity") == "entity"} if ids else set()
        orphans = [i for i in ids if i in named]
        keep = [i for i in ids if i not in named]
        if keep:
            await NodeRepo().patch_properties(workspace_id, keep, {"__stale": True})
        return {"kind": row.kind, "storage": row.storage, "stale": len(keep), "orphans": orphans}
    except Exception as e:                                    # noqa: BLE001 - the graph delete stands
        log.warning("asset_purge.data_failed", asset_id=str(asset_id), error=repr(e))
        return {"error": str(e)[:200]}
