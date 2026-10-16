"""Semi-structured pipeline RUNTIME — ingest any JSON through a pipeline authored before the
data existed.

Flow for one uploaded JSON + one pipeline (kind="semistructured"):

  1. STRUCTURE  — the existing auto walker (ingest_json) builds the hierarchical node/edge graph
                  exactly as today (Sessions, Turns, attributes, NEXT chains, REFERENCES).
  2. MATCH      — each top-level record is matched to one of the pipeline's declared structures
                  (schema.match_structure, overlap-based). Unknown shapes: auto-accepted when the
                  pipeline's `auto_accept` toggle is on (heuristic roles + one LLM scout call,
                  persisted back onto the pipeline as a draft card) — else parked & counted.
  3. READ       — the matched structure's text/date roles turn the record into reading units
                  (assemble.units_for), which are chunked into ordinary Chunk nodes: same type,
                  same `text` property, same embeddings, `valid_from` from date-role keys,
                  linked CONTAINS from the JSON's Source node.
  4. EXTRACT    — the pipeline's remaining stages run over those chunks through the SAME DAG
                  executor documents use (_run_dag): built-in + custom extractors, call groups,
                  parallel LLM calls — nothing re-implemented.

This module OWNS the semi-structured flow; it calls into existing machinery (walker, parsing,
NodeRepo, DAG executor) but no existing module needs to know it exists beyond the task dispatch.
"""
import json
import uuid

from opencortyx.core.db import V2Session
from opencortyx.ingest.semistructured import schema as ss
from opencortyx.ingest.semistructured.assemble import units_for
from opencortyx.core.substrate.repo import _q_capture
from opencortyx.core.config import settings
from opencortyx.core.observability import get_logger
from opencortyx.domain.control import SchemaMapping
from opencortyx.core.substrate import NodeRepo
from opencortyx.ingest.semistructured.registry import register_structures

log = get_logger(__name__)

PARK_BYTES_BUDGET = 32 * 1024 * 1024

STRUCTURES_STAGE = "json_structures"     # the pseudo-stage that carries the structure cards

EXTRACT_NODE_BATCH = settings.EXTRACT_NODE_BATCH
#: per-stage filter knobs (same names the document/semi builders write) — see
#: ingest_docs._apply_stage_filters, which is the ONE implementation all three paradigms use.
_STAGE_FILTER_KEYS = ("include_keys", "exclude_keys", "min_words", "max_words")


def structures_config(stages: list) -> dict:
    """The pipeline's structure config: {"auto_accept": bool, "structures": [...]}. Missing →
    auto-accept everything (zero-config default for Personal mode)."""
    for s in stages or []:
        if isinstance(s, dict) and s.get("type") == STRUCTURES_STAGE:
            return dict(s.get("config") or {})
    return {"auto_accept": True, "structures": []}


def bound_mapping_ids(cfg: dict) -> list:
    """The schema mappings a stage config binds, newest multi-binding form first. A pipeline may bind
    SEVERAL mappings (`mapping_ids`); `mapping_id` is the older single-binding form and is still read so
    pipelines saved before multi-binding keep working."""
    ids = [str(x) for x in (cfg or {}).get("mapping_ids") or [] if x]
    single = (cfg or {}).get("mapping_id")
    if single and str(single) not in ids:
        ids.append(str(single))
    return ids


def _payload_from_editor(mapping) -> dict:
    """A bound mapping's payload with its `structures` RE-DERIVED from the editor rule model.

    `structures` is a compiled artefact; `editor` is what the author drew and the only thing the engine
    page round-trips. Reading the compiled copy meant a mapping stayed frozen at whatever the
    translator understood ON THE DAY IT WAS SAVED — so a mapping saved before the translator learned to
    read the canvas's `edgeRules` kept walking `Record -CONTAINS_ITEMS-> Item` while its own picture
    said `Order -HAVE_ITEM-> Item`, with no way to tell from the UI which one would run. Re-deriving
    here is also what makes the pipeline builder's promise true: *"the pipeline reads its shape from it
    at ingest, so edits in the engine take effect without re-saving."*

    Shapes the editor does NOT describe — the ones auto-accept discovered and wrote back — are kept:
    they were never the editor's to produce, and dropping them would un-learn every shape the mapping
    has met. Falls back to the stored copy on any translate error; a mapping must still run.
    """
    payload = dict(mapping.payload or {})
    editor = payload.get("editor")
    if not editor:
        return payload
    try:
        from opencortyx.ingest.semistructured.translate import translate_editor
        fresh = translate_editor(editor, getattr(mapping, "name", "") or "")
    except Exception as e:                       # noqa: BLE001 — never lose an ingest to a translate bug
        log.warning("semistructured.mapping_retranslate_failed",
                    mapping_id=str(getattr(mapping, "id", "?")), error=str(e))
        return payload
    fresh_structures = fresh.pop("structures", None) or []
    if not fresh_structures:
        return payload
    seen = {(s.get("fingerprint") or s.get("name")) for s in fresh_structures}
    kept = [s for s in (payload.get("structures") or [])
            if (s.get("fingerprint") or s.get("name")) not in seen]
    payload.update(fresh)                        # reading rules, pattern rules, strict, id patterns
    payload["structures"] = fresh_structures + kept
    return payload


async def resolve_structures_config(workspace_id, stages: list) -> dict:
    """Structure config for a run — the piece that lets a pipeline CONSUME saved schema mappings by
    reference. Normally the inline json_structures stage config; but when that stage only carries
    mapping references (no inline `structures`), load each referenced SchemaMapping's payload and use
    the UNION of their structures. The returned dict keeps the refs so auto-accepted shapes are written
    back to the mappings (not the pipeline). Falls back to the inline/auto config on any miss."""
    cfg = structures_config(stages)
    mapping_ids = bound_mapping_ids(cfg)
    if mapping_ids and not cfg.get("structures"):
        import uuid as _uuid
        merged: dict = {}
        structures: list = []
        origins: dict = {}                       # structure fingerprint/name → the mapping it came from
        seen: set = set()
        resolved: list = []
        for mid in mapping_ids:
            try:
                async with V2Session() as db:
                    m = await db.get(SchemaMapping, _uuid.UUID(str(mid)))
                if m is None or m.workspace_id != workspace_id or not (m.payload or {}):
                    continue
                payload = _payload_from_editor(m)
                for st in payload.pop("structures", None) or []:
                    # de-dupe by fingerprint (falling back to name) so two mappings declaring the same
                    # shape don't double it
                    key = (st or {}).get("fingerprint") or (st or {}).get("name") or repr(st)
                    if key in seen:
                        continue
                    seen.add(key)
                    structures.append(st)
                    origins[str(key)] = str(mid)
                for k, v in payload.items():     # non-structure keys: first mapping wins
                    merged.setdefault(k, v)
                resolved.append(str(mid))
            except Exception as e:               # noqa: BLE001 — one bad ref must not sink the run
                log.warning("semistructured.mapping_resolve_failed", mapping_id=str(mid), error=str(e))
        if structures or resolved:
            merged["structures"] = structures
            if "auto_accept" in cfg:
                merged["auto_accept"] = cfg["auto_accept"]
            if "strict" in cfg:
                merged["strict"] = cfg["strict"]
            if "extract_only" in cfg:
                merged["extract_only"] = cfg["extract_only"]
            merged["mapping_ids"] = resolved
            merged["structure_origins"] = origins
            if len(resolved) == 1:
                merged["mapping_id"] = resolved[0]   # keep the single-ref field for existing write-back
            return merged
    return cfg


def runnable_stages(stages: list) -> list:
    """The DAG stages minus the structures pseudo-stage (which is config, not an executor)."""
    return [s for s in (stages or []) if not (isinstance(s, dict) and s.get("type") == STRUCTURES_STAGE)]


def _stage_template_names(stype: str) -> list:
    """The extractor template name a stage represents, for NODE-targeted extraction.
    Returns [] for the reading-rule and structure stages — those never run per-node."""
    if stype in ("chunking", STRUCTURES_STAGE):
        return []
    return [stype] if stype else []               # a builtin: or custom template name runs as itself


def _breakdown_to_counts(bd: dict) -> dict:
    """Extraction breakdown → the ingest result's counters.

    `extracted_node_types` / `extracted_edge_types` are the GENERIC truth: whatever was created, by
    type, whether or not anyone anticipated it.
    """
    return {
        "rels": int(bd.get("edges") or 0),
        "extracted_node_types": dict(bd.get("node_types") or {}),
        "extracted_edge_types": dict(bd.get("edge_types") or {}),
    }


_CAT_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/categorical-value")


async def _promote_categoricals(workspace_id, created_by_type: dict, *, collection_id=None) -> int:
    """Write the categorical promotions for the nodes this walk created. Returns edges written.

    The value node is keyed on (attribute-type, value) and NOT on the record, so every record with
    `status=red` links to the SAME `red` node. That shared node is what turns an attribute filter
    into a traversal.
    """
    from opencortyx.ingest.structured.categorical import plan_promotions

    ids = [uuid.UUID(i) for group in created_by_type.values() for i in (group or [])]
    if not ids:
        return 0
    repo = NodeRepo()
    written = 0
    for node, attr, value, ntype, etype in plan_promotions(await repo.get_many(workspace_id, ids)):
        vid = uuid.uuid5(_CAT_NS, f"{workspace_id}:{ntype}:{value.lower()}")
        await repo.upsert_node(workspace_id=workspace_id, node_id=vid, type=ntype, label=value,
            properties={"name": value, "text": f"{ntype} {value}", "__categorical": "true"},
            collection_id=collection_id)
        await repo.add_edge(workspace_id=workspace_id, source_id=node.id, target_id=vid, type=etype)
        written += 1
    return written


async def _made_by(workspace_id, node_type: str, names: list, collection_id=None) -> list:
    """Ids of nodes of `node_type` PRODUCED by any of these extractor cards.

    `__template` is stamped on emitted nodes, `__by` on resolver-minted identity nodes (which carry
    no `__template` on purpose). Between them every extractor output says who made it."""
    wanted = set(names)
    nodes = await NodeRepo().find(workspace_id, types=[node_type],
                                  collection_ids=[collection_id] if collection_id else None)
    return [n.id for n in nodes
            if (n.properties or {}).get("__template") in wanted or (n.properties or {}).get("__by") in wanted]


def stage_node_targets(stages: list) -> dict:
    """Read each stage's `config.input_nodes` ([{node_type, attrs:[...]}]) → {node_type:
    {extractors:set, input_attrs:set}}. This is where the user says 'run THIS extractor over THESE
    nodes reading THESE attributes' — chosen in the Add-Step editor from the structures' vocabulary."""
    out: dict = {}
    for s in (stages or []):
        if not isinstance(s, dict):
            continue
        inodes = (s.get("config") or {}).get("input_nodes") or []
        names = _stage_template_names(s.get("type"))
        if not inodes or not names:
            continue
        cfg = s.get("config") or {}
        for inp in inodes:
            nt = (inp or {}).get("node_type")
            if not nt:
                continue
            agg = out.setdefault(nt, {"extractors": set(), "input_attrs": set(), "filters": [],
                                      "mech": {}, "producers": set()})
            agg["extractors"].update(names)
            for _pk in ("from_extractor", "from_stage"):
                _pv = str((inp or {}).get(_pk) or "").strip()
                if _pv:
                    agg["producers"].add(_pv)
            for _k in ("via", "from_type", "to_type", "relation"):
                _v = str(cfg.get(_k) or "").strip()
                if _v:
                    for _n in names:
                        agg["mech"].setdefault(_n, {})[_k] = _v
            agg["input_attrs"].update(a for a in (inp.get("attrs") or []) if a)
            if any(cfg.get(k) not in (None, "", [], 0) for k in _STAGE_FILTER_KEYS):
                agg["filters"].append({k: cfg.get(k) for k in _STAGE_FILTER_KEYS if k in cfg})
    return out


def _dag_stages(stages: list) -> list:
    """Stages that run over CHUNKS in the DAG — everything except node-targeted extractor stages
    (those with input_nodes run per-node instead, so they must not also run over chunks)."""
    return [s for s in (stages or [])
            if not (isinstance(s, dict) and (s.get("config") or {}).get("input_nodes")
                    and _stage_template_names(s.get("type")))]


def _reading_targets(structures: list, created_by_type: dict) -> dict:
    """{node_type: {attribute names}} — WHERE the prose a pipeline stage should read actually lives.

    A structure declares where the prose IS (a text-role attribute, or a prose Document node). The
    walker may materialise that prose either as its own node (`Document.text`) or as a property of
    the node above it (`Turn.content`) — depending on shape, depth and the walk spec. So resolve each
    readable PATH to the deepest declared node the walk really created, and read the remainder of the
    path off that node. Matching on the declared type alone silently found nothing whenever the two
    disagreed, which left every extractor stage with no input."""
    out: dict = {}
    for s in structures or []:
        rows = [(k.get("path") or "", k.get("node_type") or "Record")
                for k in (s.get("keys") or [])
                if k.get("kind") == "node" and (k.get("decision") or "node") not in ("skip", "embed")]
        rows.sort(key=lambda r: len(r[0]), reverse=True)      # deepest declared node wins
        for p in ss.readable_paths(s):
            for npath, ntype in rows:
                if npath and not (p == npath or p.startswith(npath + ".")):
                    continue
                if not created_by_type.get(ntype):
                    continue                                  # not in this walk → try its parent
                rest = p[len(npath):].lstrip(".") if npath else p
                attr = rest.split(".")[-1].replace("[]", "") if rest else "text"
                out.setdefault(ntype, set()).add(attr or "text")
                break
    return out


def _summary_of(structures: list) -> dict:
    """Denormalized {targets, nodes, edges} for a mapping's hub row — computed from the graph its
    declared structures will build. Mirrors router._mapping_summary (kept local to avoid a cycle)."""
    from opencortyx.ingest.semistructured.registry import structure_types
    targets, nodes, edges = [], 0, 0
    for st in structures or []:
        for r in structure_types(st):
            if r.get("kind") == "node":
                nodes += 1
                if r.get("name") and r["name"] not in targets:
                    targets.append(r["name"])
            elif r.get("kind") == "edge":
                edges += 1
    return {"targets": targets, "nodes": nodes, "edges": edges}


def _merge_by_fingerprint(existing: list, incoming: list) -> list:
    """Merge structure cards by fingerprint — a re-detected shape updates in place, never duplicates."""
    by_fp = {st.get("fingerprint"): st for st in (existing or [])}
    for st in incoming or []:
        by_fp[st.get("fingerprint")] = st
    return list(by_fp.values())


async def _merge_structures_into_mapping(workspace_id, mapping_id, structures: list) -> None:
    """Auto-accept WRITE-BACK: merge newly-discovered structures into the SchemaMapping the pipeline
    references (by fingerprint), refresh its summary, and re-register its types so the engine/hub
    reflect them and the next run matches instantly. Best-effort."""
    if not structures:
        return
    try:
        import uuid as _uuid
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified
        async with V2Session() as db:
            m = (await db.execute(select(SchemaMapping).where(
                SchemaMapping.id == _uuid.UUID(str(mapping_id))).with_for_update())).scalar_one_or_none()
            if not m or m.workspace_id != workspace_id:
                return
            payload = dict(m.payload or {})
            payload["structures"] = _merge_by_fingerprint(payload.get("structures") or [], structures)
            m.payload = payload
            m.summary = _summary_of(payload["structures"])
            flag_modified(m, "payload")
            flag_modified(m, "summary")
            await register_structures(db, workspace_id, structures)
            await db.commit()
    except Exception as e:                        # noqa: BLE001 — write-back is best-effort
        log.warning("semistructured.auto_accept_writeback_failed", mapping_id=str(mapping_id), error=str(e))


async def _upsert_auto_mapping(workspace_id, source_label, structures: list) -> None:
    """When a pipeline references NO mapping, still surface auto-discovered shapes: find-or-create an
    'auto' SchemaMapping for this source and merge the structures into it — so a brand-new JSON shape
    shows up in the Semi-Structured engine + hub (auto-accept: new shape → saved mapping). Best-effort."""
    if not structures:
        return
    try:
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified
        name = f"Auto — {source_label}" if source_label else "Auto — JSON"
        async with V2Session() as db:
            m = (await db.execute(select(SchemaMapping).where(
                SchemaMapping.workspace_id == workspace_id, SchemaMapping.paradigm == "semistructured",
                SchemaMapping.name == name).with_for_update())).scalar_one_or_none()
            if m is None:
                m = SchemaMapping(workspace_id=workspace_id, name=name, paradigm="semistructured",
                                  source_type="JSON", payload={"auto_accept": True, "structures": []},
                                  summary={}, status="auto")
                db.add(m)
                await db.flush()
            payload = dict(m.payload or {})
            payload["structures"] = _merge_by_fingerprint(payload.get("structures") or [], structures)
            payload.setdefault("auto_accept", True)
            m.payload = payload
            m.summary = _summary_of(payload["structures"])
            flag_modified(m, "payload")
            flag_modified(m, "summary")
            await register_structures(db, workspace_id, structures)
            await db.commit()
    except Exception as e:                        # noqa: BLE001 — best-effort
        log.warning("semistructured.auto_mapping_upsert_failed", error=str(e))


async def _persist_structures_to_pipeline(pipeline_id, workspace_id, cfg: dict) -> None:
    """Write auto-accepted structure cards back onto the Pipeline row so they appear as draft cards in
    the builder (re-ingests then match them instantly, and humans can refine roles). Backward-compat
    for pipelines that carry inline structures rather than referencing a saved mapping."""
    try:
        from sqlalchemy import select
        from sqlalchemy.orm.attributes import flag_modified
        from opencortyx.domain.control import Pipeline
        async with V2Session() as db:
            p = (await db.execute(select(Pipeline).where(Pipeline.id == uuid.UUID(str(pipeline_id)))
                                  .with_for_update())).scalar_one_or_none()
            if not p or p.workspace_id != workspace_id:
                return
            new_stages = list(p.stages or [])
            for s in new_stages:
                if isinstance(s, dict) and s.get("type") == STRUCTURES_STAGE:
                    merged = dict(s.get("config") or {})
                    merged["structures"] = _merge_by_fingerprint(
                        merged.get("structures") or [], cfg.get("structures") or [])
                    merged.update({k: v for k, v in cfg.items() if k != "structures"})
                    s["config"] = merged
                    break
            else:
                new_stages.insert(0, {"id": "structures", "type": STRUCTURES_STAGE, "config": cfg})
            p.stages = new_stages
            flag_modified(p, "stages")
            # Mode A (auto-detect) discovers types at ingest → publish them to the ontology too,
            # so an auto-accepted shape's node types are immediately bindable by extractors.
            await register_structures(db, workspace_id, cfg.get("structures") or [])
            await db.commit()
    except Exception as e:                        # noqa: BLE001 — persistence is best-effort
        log.warning("semistructured.auto_accepted_persist_failed", error=str(e))


async def _persist_structures(pipeline_id, workspace_id, stages: list, cfg: dict, source_label=None) -> None:
    """Persist auto-accepted structures. Routing:
      • pipeline REFERENCES mapping(s) → merge each shape back into the SchemaMapping it came from
        (`_mapping_id`); newly-discovered shapes go to the first bound mapping.
      • otherwise → keep the pipeline-stage draft write-back AND surface the shapes as an auto mapping
        so a new JSON shape shows up in the Semi-Structured engine + hub."""
    structures = cfg.get("structures") or []
    bound = bound_mapping_ids(cfg)
    if bound:
        # group by originating mapping so a shape is written back where it was authored; a shape
        # discovered during this run has no origin yet and goes to the first bound mapping
        origins = cfg.get("structure_origins") or {}
        by_mapping: dict = {}
        for st in structures:
            key = str((st or {}).get("fingerprint") or (st or {}).get("name") or repr(st))
            by_mapping.setdefault(str(origins.get(key) or bound[0]), []).append(st)
        for mid, sts in by_mapping.items():
            await _merge_structures_into_mapping(workspace_id, mid, sts)
        return
    if pipeline_id:
        await _persist_structures_to_pipeline(pipeline_id, workspace_id, cfg)
    await _upsert_auto_mapping(workspace_id, source_label, structures)


#: The name the record-oriented parsers give the synthetic wrapper they put around a top-level
#: array (json_parsers.JsonParser, JsonLinesParser, config_parsers multi-document YAML).
_RECORD_COLLECTION_KEY = "records"


def _unwrap_record_collection(data):
    """`{"records": [...]}` from a top-level-array file → the list of records itself.

    The JSON parsers wrap a top-level array so the plain walker builds siblings under a parent
    instead of one node holding a list attribute. Semi-structured ingestion must see THROUGH that
    wrapper, because every consumer downstream reads the payload as "a record, or a list of
    records" and the wrapper is neither — it is one dict whose keys are all `records[]...`:

      • the SIGNATURE GATE scored the wrapper against cards whose paths are relative to a record.
        The overlap is ~0, so a correct card was rejected and EVERY record was dropped before the
        walk, while the run reported "Completed 100%". Measured on a 2-conversation LoCoMo file:
        the gate saw 1 record and matched 0; the same card scores 0.946 against a real element.
      • the WALK is offset the same way — ingest_json walks a LIST as one record per element (what
        a card is authored against) but a DICT as a single record, so every declared path would
        have needed a `records[]` prefix nobody typed.

    Unwrapped in ONE place, at the entry to the runner, so the gate, the walk and the match loop
    cannot disagree about what a record is. The plain (Mode B) walk is deliberately untouched: it
    has no card to satisfy, and its graph shape is already established.
    """
    if (isinstance(data, dict) and len(data) == 1
            and isinstance(data.get(_RECORD_COLLECTION_KEY), list)):
        return data[_RECORD_COLLECTION_KEY]
    return data


async def run_semistructured(workspace_id: uuid.UUID, data, *, pipeline_stages: list,
                             source_label: str, collection_id=None, pipeline_id=None,
                             options: dict | None = None, reporter=None,
                             checksum: str | None = None) -> dict:
    """Ingest a parsed JSON payload through a semi-structured pipeline. Returns counts.

    `checksum` is the sha256 of the RAW uploaded bytes. Without it a re-upload of an UNCHANGED file
    re-walks the whole corpus: the nodes converge (ids are deterministic) but every one of them is
    touched, so the run stamps a fresh `MODIFIED_ON` edge across the entire graph. Measured on a
    re-run of a 1,309-node LoCoMo ingest: nodes identical, edges 2,617 -> 3,926. The plain JSON route
    has always passed it; the pipeline route never did, so exactly the path a user ingests through
    was the one that could not tell "same file again" from "new data".
    """
    from opencortyx.ingest.pipelines.defaults import normalize_stages
    from opencortyx.ingest.semistructured.ingest import ingest_json, _JNS
    from opencortyx.ingest.unstructured.ingest import (
        _apply_stage_filters, _run_dag, _stage_kind, _mark_ingested_ok)

    data = _unwrap_record_collection(data)

    # canonical stage vocabulary, whatever the caller sent — the structure card must be findable as
    # `type: "json_structures"`, otherwise the run silently falls back to bare auto-detect.
    pipeline_stages = normalize_stages(pipeline_stages)
    opts = options or {}
    # resolve the structure config: inline stage, OR a saved SchemaMapping the pipeline references
    # (mapping_id) — this is how a pipeline consumes a schema mapping without an inline copy.
    cfg = await resolve_structures_config(workspace_id, pipeline_stages)
    structures: list = ss.normalize_structures(cfg.get("structures") or [])
    if structures:
        cfg = {**cfg, "structures": structures}
    from opencortyx.ingest.pipelines import signature as _sig
    bound_mapping = bool(bound_mapping_ids(cfg))
    # Unmatched records go to the default walker unless a mapping is bound with auto-accept off.
    auto_structure = _sig.auto_create_on(cfg, bound=bound_mapping)
    structure_only = _sig.extract_only_on(cfg)     # "Extract only": one record's structure, no values
    walk_spec = ss.compile_walk_spec(structures)
    # stage-level knobs that live OUTSIDE the cards: pattern rules (P23) + configurable id keys (P3).
    # They ride on the walk spec so the walker applies them even to shapes no card covers.
    rules = [r for r in (cfg.get("rules") or []) if isinstance(r, dict)]
    id_patterns = [str(p).strip() for p in (cfg.get("id_patterns") or []) if str(p).strip()]
    from opencortyx.ingest.pipelines.conformance import strict_suppressed
    strict = bool(cfg.get("strict"))
    suppress = strict_suppressed(strict, (walk_spec or {}).get("edge_types", {}).values())
    # Per-RECORD rule sets: they decide from the data (a discriminator field's value, an element being
    # null), so they ride on the walk spec rather than the structure's per-path keys.
    _runtime = {k: cfg.get(k) for k in ("discriminators", "array_filters", "edge_attributes")
                if cfg.get(k)}
    if rules or id_patterns or suppress or strict or _runtime:
        walk_spec = dict(walk_spec or {})
        if strict:
            walk_spec["strict"] = True
        if rules:
            walk_spec["rules"] = rules
        if id_patterns:
            walk_spec["id_patterns"] = id_patterns
        if suppress:
            walk_spec["suppress_edges"] = sorted(suppress)
        walk_spec.update(_runtime)
    has_custom = any((s.get("status") or "auto") != "auto" for s in structures)
    stages = runnable_stages(pipeline_stages)

    ignored_records = 0
    if structures and not auto_structure:
        _records = data if isinstance(data, list) else [data]
        _kept = [r for r in _records
                 if not isinstance(r, dict) or ss.match_structure(r, structures)[0] is not None]
        ignored_records = len(_records) - len(_kept)
        if ignored_records:
            data = _kept if isinstance(data, list) else None
        if not _kept:                          # nothing in this payload matches → ingest nothing
            if reporter is not None:
                await reporter.start(1)
                await reporter.step(1, f"Ignored — no record matches the bound schema mapping "
                                       f"({ignored_records} record(s))", "completed", 0)
            return {"status": "ignored", "source": source_label, "records": len(_records),
                    "matched": 0, "ignored": ignored_records, "nodes": 0, "edges": 0,
                    "reason": "no record matched a bound schema mapping and auto-accept is off"}
    runnable = sum(1 for s in stages if isinstance(s, dict) and _stage_kind(s.get("type")) not in (None, "chunk"))

    if reporter is not None:
        await reporter.start(2 + runnable)
        await reporter.step(1, "Walk JSON → graph", "running")

    # ── 1) STRUCTURE: the existing walker, untouched (collect_ids → per-NODE attachment below) ──
    walk = await ingest_json(
        workspace_id, data, source_label=source_label, collection_id=collection_id,
        root_type=opts.get("root_type"), max_depth=int(opts.get("max_depth", 6)),
        max_array=int(opts.get("max_array", 500)),
        max_records=(1 if structure_only else int(opts.get("max_records", 100000))),
        embed_nodes=bool(opts.get("embed_nodes", False)),
        mapping=(opts.get("mapping") or None),     # explicit per-request mapping still wins per field
        walk_spec=walk_spec, auto_zip=not has_custom,
        collect_ids=True, checksum=checksum, replace=bool(opts.get("replace")))
    created_by_type: dict = walk.pop("created_by_type", {}) or {}
    source_id = uuid.uuid5(_JNS, f"{workspace_id}:source:{source_label}")   # the walker's Source node
    if reporter is not None:
        await reporter.step(1, "Walk JSON → graph", "completed", walk.get("nodes"))
        await reporter.step(2, "Match structures & read text", "running")

    records = data if isinstance(data, list) else [data]
    _park_bytes, _park_truncated = [0], [False]   # per-run parked-retention budget
    try:
        from opencortyx.core import observability as _obs
        _obs.INGEST_RECORDS.labels(paradigm="semistructured", outcome="seen").inc(len(records))
        if ignored_records:
            _obs.INGEST_RECORDS.labels(paradigm="semistructured", outcome="ignored").inc(ignored_records)
            _obs.DQ_FAILURES.labels(reason="no_matching_structure").inc(ignored_records)
    except Exception:                                      # noqa: BLE001 — never fail an ingest
        pass
    counts = {"records": len(records), "matched": 0, "parked": 0, "structures_added": 0,
              "ignored": ignored_records,      # filtered out before the walk by the signature gate
              "units": 0, "chunks": 0, "attached_extracted": 0, "node_attached_extracted": 0}
    added_new = False
    for ri, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        structure, _score = ss.match_structure(rec, structures)
        if structure is None:
            if not auto_structure:
                counts["parked"] += 1
                try:
                    from opencortyx.core import observability as _obs
                    _obs.INGEST_RECORDS.labels(paradigm="semistructured", outcome="parked").inc()
                    _obs.DQ_FAILURES.labels(reason="parked_unreadable").inc()
                except Exception:                          # noqa: BLE001
                    pass
                if _park_bytes[0] < PARK_BYTES_BUDGET:
                    _blob = {"record": rec, "record_index": ri, "source_id": str(source_id),
                             "pipeline_id": str(pipeline_id or "")}
                    _park_bytes[0] += len(json.dumps(_blob, default=str))
                    await _q_capture(workspace_id, "record", _blob,
                                     reason="no_matching_structure", asset=source_label)
                elif not _park_truncated[0]:
                    # Say it ONCE, and say it loudly: from here on the payloads are not being kept.
                    # `counts['parked']` stays exact, and the source asset remains the recoverable copy.
                    _park_truncated[0] = True
                    await _q_capture(workspace_id, "record",
                                     {"source_id": str(source_id), "budget_bytes": PARK_BYTES_BUDGET,
                                      "note": "further parked records for this run were NOT retained; "
                                              "re-ingest the source after declaring a structure"},
                                     reason="park_retention_truncated", retryable=False,
                                     asset=source_label)
                continue
            structure = ss.build_structure(rec, text_min_chars=cfg.get("auto_text_min_chars"),
                                           embed_small=bool(cfg.get("auto_embed_small")))
            from opencortyx.core.inference.registry import resolve_model
            structure = await ss.scout_structure(                         # one optional LLM refine
                [rec], structure, await resolve_model(workspace_id, "utility"))
            ss.apply_auto_rules(structure, skip_nodes=cfg.get("auto_skip_nodes"),
                                skip_attrs=cfg.get("auto_skip_attrs"))   # auto-mode exclusion rules
            ss.apply_rules(structure, rules)                             # pattern rules (P23)
            structures.append(structure)
            counts["structures_added"] += 1
            added_new = True
        counts["matched"] += 1
        try:
            from opencortyx.core import observability as _obs
            _obs.INGEST_RECORDS.labels(paradigm="semistructured", outcome="matched").inc()
        except Exception:                                  # noqa: BLE001
            pass
        counts["units"] += len(units_for(rec, structure))

    if added_new:                                  # draft cards show up in the builder / engine + hub
        cfg["structures"] = structures
        await _persist_structures(pipeline_id, workspace_id, pipeline_stages, cfg, source_label=source_label)

    # 'Keys only' → the structure/shape is now in the graph + registered; skip text reading + extraction.
    if structure_only:
        await _mark_ingested_ok(workspace_id, source_id)
        if reporter is not None:
            await reporter.step(2, "Keys only — skipped text extraction", "completed", walk.get("nodes"))
        return {"status": "ok", "source_id": str(source_id), "structure_only": True, **walk, **counts}

    node_ext: dict = {}            # node_type → {extractors:set, input_attrs:set, filters:[cfg…]}

    def _merge_target(ntype, names, inputs, filters=None, mech=None, producers=None):
        agg = node_ext.setdefault(ntype, {"extractors": set(), "input_attrs": set(), "filters": [],
                                          "producers": set(),
                                          "mech": {}})
        agg["extractors"].update(names)
        agg["input_attrs"].update(inputs)
        agg["filters"].extend(filters or [])
        agg["producers"].update(producers or ())
        for _n, _ov in (mech or {}).items():
            agg["mech"].setdefault(_n, {}).update(_ov)

    # (i) STAGE-driven targets — 'run this extractor over these nodes/attributes', chosen in the
    #     Add-Step editor. This is the primary manual path (works across a pipeline's many structures).
    for ntype, agg in stage_node_targets(pipeline_stages).items():
        _merge_target(ntype, agg["extractors"], agg["input_attrs"], agg.get("filters"),
                      agg.get("mech"), agg.get("producers"))
    # (ii) extractors attached to a structure card's keys.
    for s in structures:
        for k in (s.get("keys") or []):
            if k.get("kind") != "node" or (k.get("decision") or "node") in ("skip", "embed"):
                continue
            ntype = k.get("node_type") or "Record"
            explicit = [e for e in (k.get("extractors") or []) if e]
            text_attrs = ss.node_text_attrs(s, k.get("path") or "")
            if explicit:
                _merge_target(ntype, explicit, (list(k.get("input_attrs") or []) or text_attrs))
    for s in structures:
        for k in (s.get("keys") or []):
            if k.get("kind") != "attribute":
                continue
            names = [e for e in (k.get("extractors") or []) if e]
            path = k.get("path") or ""
            if not names or not path:
                continue
            owner = ss.owning_node_type(s, path)
            if owner:
                _merge_target(owner, names, [path.rsplit(".", 1)[-1]])

    pairs: list = []                              # (node_id, text) → the pipeline's DAG stages
    text_types = _reading_targets(structures, created_by_type)
    if text_types:
        for ntype, attrs in text_types.items():
            if ntype in node_ext:                 # per-node extraction already reads this type
                continue
            ids = [uuid.UUID(i) for i in (created_by_type.get(ntype) or [])]
            if not ids:
                continue
            for n in await NodeRepo().get_many(workspace_id, ids):
                p = n.properties or {}
                txt = "\n".join(f"{a}: {p[a]}" for a in sorted(attrs)
                                if str(p.get(a) or "").strip()) or str(p.get("text") or "")
                if txt.strip():
                    pairs.append((n.id, txt))
    counts["read_nodes"] = len(pairs)

    if any(isinstance(s, dict) and s.get("type") == "promote_categoricals" for s in (stages or [])):
        try:
            counts["categoricals"] = await _promote_categoricals(
                workspace_id, created_by_type, collection_id=collection_id)
        except Exception as e:                    # noqa: BLE001 — enrichment must never fail an ingest
            log.warning("semistructured.categorical_promotion_failed", error=str(e))

    if reporter is not None:
        await reporter.step(2, "Match structures & read text", "completed", len(pairs))

    from opencortyx.ingest.extraction.templates import run_named_templates_on_chunks
    if node_ext:

        _bd: dict = {}                        # shared extraction breakdown → real result counters

        from opencortyx.ingest.extraction.entities import (
            ENTITY_COLUMN_TO_FIELD, ENTITY_FIELD_TO_COLUMN)
        _ALIAS = {**ENTITY_FIELD_TO_COLUMN, **ENTITY_COLUMN_TO_FIELD}
        _COLS = tuple(ENTITY_FIELD_TO_COLUMN.values()) + ("type",)

        def _field(_n, _p, a):
            v = _p.get(a)
            if v in (None, "") and a in _COLS:
                v = getattr(_n, a, None)
            if v in (None, ""):
                alt = _ALIAS.get(a)
                if alt:
                    v = _p.get(alt)
                    if v in (None, "") and alt in _COLS:
                        v = getattr(_n, alt, None)
            return v

        async def _extract_over(ntype, agg, rows) -> int:
            n_pairs = []
            for n in rows:
                p = n.properties or {}
                txt = ("\n".join(f"{a}: {_field(n, p, a)}" for a in sorted(agg["input_attrs"])
                                 if _field(n, p, a) not in (None, ""))
                       if agg["input_attrs"] else str(p.get("text") or ""))
                if txt.strip():
                    n_pairs.append((n.id, txt))
            for f in (agg.get("filters") or []):
                n_pairs = await _apply_stage_filters(workspace_id, n_pairs, f)
            if not n_pairs:
                return 0
            try:
                return await run_named_templates_on_chunks(
                    workspace_id, sorted(agg["extractors"]), n_pairs, collection_id, breakdown=_bd,
                    mech_overrides=agg.get("mech") or None)
            except Exception as e:                # noqa: BLE001
                log.warning("semistructured.node_extraction_failed", node_type=str(ntype), error=str(e))
                return 0

        emitted_targets = {}                      # input types produced by a prior stage → phase 2

        async def _extract_batched(ntype, agg, ids) -> int:
            """Run one type's extractors over its nodes in EXTRACT_NODE_BATCH-sized slices.

            The rows, the composed pairs and the per-batch results all go out of scope between
            slices, so the worker's peak is set by the batch and not by the size of the corpus.
            See EXTRACT_NODE_BATCH for the failure this exists to prevent.
            """
            done, seen = 0, 0
            for _i in range(0, len(ids), EXTRACT_NODE_BATCH):
                rows = await NodeRepo().get_many(workspace_id, ids[_i:_i + EXTRACT_NODE_BATCH])
                done += await _extract_over(ntype, agg, rows)
                seen += len(rows)
                rows = None                       # release before the next slice is loaded
                if reporter is not None:
                    try:
                        await reporter.step(3, f"Extract over {ntype} "
                                               f"({len(agg['extractors'])} extractors)",
                                            "running", seen)
                    except Exception:             # noqa: BLE001 — progress must never fail a run
                        pass
            return done

        for ntype, agg in node_ext.items():
            if not agg["extractors"]:
                continue
            ids = [uuid.UUID(i) for i in (created_by_type.get(ntype) or [])]
            if not ids:                           # not a walker type → chained over emitted nodes
                emitted_targets[ntype] = agg
                continue
            counts["node_attached_extracted"] += await _extract_batched(ntype, agg, ids)

        for ntype, agg in emitted_targets.items():   # phase 2 — chain over emitted nodes
            _producers = sorted(agg.get("producers") or [])
            if not _producers:
                log.warning("semistructured.stage_input_has_no_producer", node_type=str(ntype),
                        remedy="re-open the pipeline and re-tick this input so the builder "
                               "records which step produces it")
                continue
            _ids = await _made_by(workspace_id, ntype, _producers, collection_id)
            counts["node_attached_extracted"] += await _extract_batched(ntype, agg, _ids)

        counts.update(_breakdown_to_counts(_bd))

        if reporter is not None and node_ext:
            for _idx, _st in enumerate([_s for _s in stages if isinstance(_s, dict)
                                        and _stage_kind(_s.get("type")) not in (None, "chunk")],
                                       start=3):
                try:
                    await reporter.step(_idx, str(_st.get("type") or "extractor"), "completed",
                                        None)
                except Exception:                 # noqa: BLE001 — progress must never fail a run
                    pass

    from opencortyx.core.config import runtime as _rc
    if _rc.defer_extraction() and _dag_stages(stages):
        await _mark_ingested_ok(workspace_id, source_id)
        return {"status": "ok", "source_id": str(source_id), **walk, **counts,
                "extraction": "deferred", "pending_chunks": len(pairs)}
    dag = await _run_dag(workspace_id, source_id, pairs, _dag_stages(stages), collection_id,
                         reporter=reporter, base_index=2)
    await _mark_ingested_ok(workspace_id, source_id)
    return {"status": "ok", "source_id": str(source_id), **walk, **counts, **dag}
