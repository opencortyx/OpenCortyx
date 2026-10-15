"""Helpers every task module uses: run reporting, anchors, post-ingest follow-ups."""
import uuid
from opencortyx.core.worker.celery_app import GIVE_UP_HOOKS
from opencortyx.core.worker.utils import async_to_sync
from opencortyx.core.observability import get_logger
from opencortyx.domain.control import Ingestion
from opencortyx.core.db import V2Session
from opencortyx.core.substrate import NodeRepo
from opencortyx.tasks.graph_tree import schedule_tree_index

log = get_logger(__name__)


def _fail_run(run_id: str, reason: str) -> None:
    """A task the worker gave up on fails its run with the reason, instead of hanging at 'running'."""
    from opencortyx.ingest.runs.progress import ProgressReporter
    async_to_sync(ProgressReporter(run_id).finish)("failed", error=reason)


GIVE_UP_HOOKS.append(_fail_run)


def _doc_plan_from_features(features):
    """Simple upload feature toggles (csv) → ingest plan. None → default full pipeline.
    Negative flags trim per-doc LLM cost for bulk/benchmark ingests:
      no_assets        — skip the Asset Profile sections (~9 calls/doc, catalogue-UI only)
    What else runs is named, not toggled: extractors in `plan["templates"]` or pipeline stages."""
    if not features:
        return None
    f = {x.strip() for x in str(features).split(",") if x.strip()}
    return {"asset_sections": "no_assets" not in f}


def _reporter(run_id):
    """Build a ProgressReporter for a run id (or a no-op reporter when there's no run)."""
    from opencortyx.ingest.runs.progress import ProgressReporter
    return ProgressReporter(uuid.UUID(run_id)) if run_id else None


async def _close_ingestion(ingestion_id: str | None, status: str, run_id: str | None = None) -> None:
    """Release a scheduled ingestion after its job finishes.

    The tick sets `status = "running"` before dispatching and skips anything already running, so a job
    that never writes the status back wedges that ingestion FOREVER — it fires once and is then skipped
    on every later tick. The file-sync task closes itself out because it is handed its `ingestion_id`;
    the structured DB re-introspect (`v2_ingest_sql`) historically was not, which is exactly how a
    scheduled DBMS ingestion ended up running only once. Passing the id and calling this closes it.

    Best-effort and never raises: failing to update bookkeeping must not fail the ingest itself."""
    if not ingestion_id:
        return
    try:
        from datetime import datetime, timezone
        async with V2Session() as db:
            ing = await db.get(Ingestion, uuid.UUID(str(ingestion_id)))
            if not ing:
                return
            ing.status = status
            ing.last_run_at = datetime.now(timezone.utc)
            if run_id:
                ing.last_run_id = str(run_id)
            await db.commit()
    except Exception as e:                     # noqa: BLE001
        log.warning("ingestion.status_writeback_failed", ingestion_id=str(ingestion_id), error=str(e))


async def _apply_anchors(workspace_id: str, res: dict, anchor_cfg: list | None,
                         path: str, collection_id=None) -> None:
    """Attach this asset to the nodes its path says it belongs to.

    Best-effort by design: an anchor is an ADDITION to a successful ingestion, so a bad rule or a
    missing target must never fail the file. Rules are validated at save time (router), which is
    where a malformed glob should be reported — here it can only be per-file noise.

    Results are recorded on `res` so the run reports what was linked and, more importantly, what was
    NOT: an unresolved derived name means a folder is named differently from the entity it refers to,
    and that is invisible unless counted.
    """
    if not anchor_cfg:
        return
    sid = (res or {}).get("source_id")
    if not sid:
        return
    try:
        from opencortyx.ingest.extraction import anchors as _anchors
        rules = _anchors.parse_rules(anchor_cfg)
        async with V2Session() as db:
            out = await _anchors.apply(db, workspace_id=uuid.UUID(workspace_id), asset_node_id=uuid.UUID(str(sid)),
                                       path=path, rules=rules, collection_id=collection_id)
            await db.commit()
        res["anchors"] = out
    except Exception as e:                     # noqa: BLE001
        res["anchors"] = {"error": str(e)[:200]}
        log.warning("anchors.rules_failed", path=str(path), error=str(e))


def _anchor_opts(opts: dict | None, filename: str) -> tuple:
    """Anchor rules + the path to match them on, out of a task's free-form options blob.

    Rules ride inside the existing `opts` / `overrides` JSON rather than on new task parameters:
    these tasks are dispatched from four places (device upload, connector, sync, beat) and adding
    positional arguments would break messages already sitting on the queue across a worker restart.

    `asset_path` falls back to the filename, which is right for a device upload — there is no folder
    layout, so a rule can only match on the name — and is overridden by callers that DO know the
    source path (connector/lake runs), because a folder rule is meaningless against a bare name.
    """
    opts = opts or {}
    return (opts.get("anchors") or None), (opts.get("asset_path") or filename)


def _roll_up_anchors(res: dict, done: dict) -> None:
    """Fold one file's anchor outcome into a multi-file run's totals.

    Counted, not just logged: the numbers that matter are how many links were made, how many names
    resolved to nothing, and what the files were TYPED as — all
    of which are invisible per-file in a run over thousands of them."""
    a = (res or {}).get("anchors") or {}
    if a.get("edges"):
        done["anchor_edges"] = done.get("anchor_edges", 0) + a["edges"]
    if a.get("typed_as"):
        typed = done.setdefault("anchor_typed", {})
        typed[a["typed_as"]] = typed.get(a["typed_as"], 0) + 1
    if a.get("unresolved"):
        done["anchor_unresolved"] = done.get("anchor_unresolved", 0) + len(a["unresolved"])


async def _apply_anchors_sql(workspace_id: str, res: dict, anchor_cfg: list | None) -> None:
    """Anchor the TABLES a database ingestion produced.

    A SQL source has no file path, so the rules match on `schema/table` — which is the layout a
    database actually has, and reads the same way a folder rule does (`hr/** → HR Person`,
    `finance/ledger → as_type: Ledger`). Without this, anchors worked for every ingestion kind
    EXCEPT a database connector, which is the one place a catalogue most obviously belongs to a
    known owner.

    Scoped to the tables of THIS run via `source_key`, so a re-run of one connector never re-anchors
    another's tables. Best-effort, like the file path: anchors are an addition to a successful
    ingestion and must not fail it.
    """
    if not anchor_cfg:
        return
    res = res or {}
    src_key = res.get("source_key") or ((res.get("introspect") or {}).get("source_key")
                                        if isinstance(res.get("introspect"), dict) else None)
    if not src_key:
        return
    try:
        from opencortyx.ingest.extraction import anchors as _anchors
        from opencortyx.domain.ontology import ASSET_TYPES
        rules = _anchors.parse_rules(anchor_cfg)
        totals = {"edges": 0, "typed": 0, "tables": 0, "unresolved": 0}
        tables = await NodeRepo().find(uuid.UUID(workspace_id), types=list(ASSET_TYPES), source_key=str(src_key))
        async with V2Session() as db:
            for m in tables:
                p = m.properties or {}
                path = "/".join(x for x in [p.get("schema_name") or "", p.get("table_name") or m.label] if x)
                out = await _anchors.apply(db, workspace_id=uuid.UUID(workspace_id), asset_node_id=m.id,
                                           path=path, rules=rules)
                totals["tables"] += 1 if out["matched_rules"] else 0
                totals["edges"] += out["edges"]
                totals["typed"] += 1 if out["typed_as"] else 0
                totals["unresolved"] += len(out["unresolved"])
            await db.commit()
        res["anchors"] = totals
    except Exception as e:                     # noqa: BLE001
        res["anchors"] = {"error": str(e)[:200]}
        log.warning("anchors.rules_failed", source=str(src_key), kind="sql", error=str(e))


async def _post_ingest(workspace_id: str, res: dict, kind: str):
    """Datasource-agnostic post-ingest capability dispatch (Versioning + Contradiction Detection +
    Resolution) over the source just ingested. `kind` = document | structured | semistructured.
    Reads the unit node id from `res['source_id']`; no-ops when the pipeline exposes no single source
    (e.g. multi-table SQL). Best-effort — never raises, never aborts an ingest."""
    # Book the tree re-index FIRST — before the source_id gate below, so multi-table SQL runs (which
    # expose no single source node yet still add plenty of nodes) also reach the dashboard graph.
    schedule_tree_index(workspace_id)
    try:
        from opencortyx.capabilities.dispatch import run_post_ingest
        sid = (res or {}).get("source_id")
        if not sid:
            return None
        out = await run_post_ingest(uuid.UUID(str(workspace_id)), uuid.UUID(str(sid)), kind)
        if isinstance(res, dict):
            res["capabilities"] = out
        # Last step of the asset's lifecycle: versioning ran above; its nodes, attributes and
        # statements are resolved against the organization's graph once its embeddings exist.
        from opencortyx.tasks.resolution import v2_resolve_asset
        job = v2_resolve_asset.delay(str(workspace_id), str(sid))
        if isinstance(res, dict):
            res["resolution"] = {"task_id": job.id}
        return out
    except Exception as e:
        log.warning("capabilities.post_ingest_failed", error=str(e))
        return None


_FILE_SETTING_KEYS = ("delimiter", "has_header", "quote", "escape", "encoding", "skip_rows", "sheet")


async def _merge_mapping_file_settings(workspace_id: str, mapping_ids: list, overrides: dict | None):
    """Fold a bound mapping's saved `file_settings` into this run's parse overrides.

    The run wins where it specifies something (the Ingestion Hub's own file panel, say); the mapping
    fills in the rest. Best-effort — a missing or malformed mapping must never break an ingest."""
    if not mapping_ids:
        return overrides
    try:
        import uuid as _uuid
        from opencortyx.domain.control import SchemaMapping
        merged = dict(overrides or {})
        for mid in mapping_ids:
            async with V2Session() as db:
                m = await db.get(SchemaMapping, _uuid.UUID(str(mid)))
            if not m or str(m.workspace_id) != str(workspace_id):
                continue
            fs = ((m.payload or {}).get("editor") or {}).get("file_settings") or {}
            for k in _FILE_SETTING_KEYS:
                if k in fs and k not in merged:
                    merged[k] = fs[k]
        return merged or overrides
    except Exception as e:
        log.warning("tabular.mapping_settings_unreadable", error=str(e))
        return overrides
