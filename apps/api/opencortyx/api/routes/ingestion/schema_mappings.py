"""Routes: schema mappings and their conformance."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select

from opencortyx.api.deps import audit
from opencortyx.api.schemas.ingestion.schema_mappings import SchemaMappingPatch, SchemaMappingSpec
from opencortyx.api.services.graph.nodes import node_subgraph
from opencortyx.api.services.ingestion.pipelines import register_semistructured_types
from opencortyx.api.services.ingestion.schema_mappings import (
    MAPPING_PARADIGMS,
    mapping_refs,
    mapping_summary,
    mapping_view,
    prepare_mapping_payload,
    register_mapping_types,
    link_existing_later,
    save_mapping_references,
)
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.control import SchemaMapping

router = APIRouter()


@router.get("/schema-mappings")
async def list_schema_mappings(paradigm: Optional[str] = None, light: bool = False,
                               user: CurrentUser = Depends(require_auth)):
    """List saved schema mappings (optionally by paradigm) for the engines + hub + pipeline binding.

    `light=1` omits the payload. A full listing re-derives every mapping's compiled form AND its
    editor model on read — correct, but it made this endpoint 25 MB across ~180 mappings, which a
    browser then has to download and parse before anything renders. A hub only needs the name, the
    paradigm and the shape counts, so it asks for those; anything that genuinely reads rules still
    gets the whole payload by default, and nothing that exists today changes.
    """
    async with V2Session() as db:
        q = select(SchemaMapping).where(SchemaMapping.workspace_id == user.workspace_id)
        if paradigm:
            q = q.where(SchemaMapping.paradigm == paradigm)
        rows = (await db.execute(q.order_by(SchemaMapping.updated_at.desc()))).scalars().all()
    if not light:
        return {"items": [mapping_view(m) for m in rows]}
    items = []
    for m in rows:
        p = m.payload or {}
        ed = p.get("editor") or {}
        items.append({
            "id": str(m.id), "name": m.name, "paradigm": m.paradigm,
            "source_type": m.source_type or "", "summary": m.summary or {},
            "status": m.status,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
            # enough for the hub's badges without shipping the rules themselves
            "authored": bool(ed),
            "structures": len(p.get("structures") or []) or None,
        })
    return {"items": items}


@router.post("/schema-mappings")
async def create_schema_mapping(spec: SchemaMappingSpec, user: CurrentUser = Depends(require_write)):
    if not spec.name.strip():
        raise HTTPException(status_code=400, detail="name required")
    paradigm = spec.paradigm if spec.paradigm in MAPPING_PARADIGMS else "semistructured"
    raw = dict(spec.payload or {})
    if raw.get("knowledge"):
        # A person's declaration (Mode B): its assets named by id, version 1, never overwritten.
        from opencortyx.knowledge.semantic.store import resolve_assets
        raw["knowledge"] = await resolve_assets(user.workspace_id, raw["knowledge"])
        raw = _with_knowledge({}, raw)
    payload = prepare_mapping_payload(paradigm, spec.name.strip(), raw)
    summary = mapping_summary(paradigm, payload, spec.summary)
    async with V2Session() as db:
        m = SchemaMapping(workspace_id=user.workspace_id, name=spec.name.strip(), paradigm=paradigm,
                          source_type=spec.source_type or "", payload=payload,
                          summary=summary, status=spec.status or "active")
        db.add(m)
        await db.flush()                                # its id names the reference rules it declares
        # a mapping's node/edge types ARE its schema — publish so extractors can bind and the
        # ontology/lineage views show what was declared, for EVERY paradigm.
        if paradigm == "semistructured":
            await register_semistructured_types(
                db, user.workspace_id, [{"type": "json_structures", "config": payload}])
        await register_mapping_types(db, user.workspace_id, paradigm, spec.name.strip(), payload)
        linked = await save_mapping_references(db, user.workspace_id, m.id, payload)
        await db.commit()
        await db.refresh(m)
    link_existing_later(user.workspace_id, linked)
    return mapping_view(m)


def _with_knowledge(old: dict | None, new: dict) -> dict:
    """`new` keeps the stored knowledge and its history when it does not carry them (an editor that
    only edits rules); a changed knowledge section becomes a new version, marked as curated so the
    Detector never overwrites it."""
    from opencortyx.knowledge.semantic.store import with_version
    old, new = dict(old or {}), dict(new or {})
    new.setdefault("history", old.get("history") or [])
    if "knowledge" not in new:
        if old.get("knowledge"):
            new["knowledge"] = old["knowledge"]
        return new
    k = dict(new.pop("knowledge") or {})
    prior = old.get("knowledge") or {}
    if {**k, "version": 0, "curated": True} == {**prior, "version": 0, "curated": True}:
        new["knowledge"] = prior
        return new
    base = {**new, "knowledge": prior} if prior else new
    return with_version(base, {**k, "curated": True})


async def _mapping(user, mapping_id: str) -> SchemaMapping:
    try:
        mid = uuid.UUID(mapping_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="mapping not found") from None
    async with V2Session() as db:
        m = await db.get(SchemaMapping, mid)
    if not m or m.workspace_id != user.workspace_id:
        raise HTTPException(status_code=404, detail="mapping not found")
    return m


@router.post("/schema-mappings/detect")
async def detect_mappings(user: CurrentUser = Depends(require_write)):
    """Read the workspace's tables and records and propose what they mean: entity types with their
    keys, where each lives, and the links between them - as `auto` mappings when confident, `draft`
    when not. Curated mappings are left as they are."""
    from opencortyx.knowledge.semantic import detector, store
    layer = await detector.detect(user.workspace_id, apply=True)
    await audit(user.workspace_id, user.user_id, "schema_mapping/detect",
                meta={"types": len(layer.types), "links": len(layer.links)})
    return detector.summary(await store.load(user.workspace_id))


@router.post("/schema-mappings/{mapping_id}/detect")
async def redetect_mapping(mapping_id: str, user: CurrentUser = Depends(require_write)):
    """Re-detect ONE mapping: the Detector reads the workspace again (links reach other files) and
    this mapping takes what it now proposes for its own assets - a new version, the previous one kept
    in its history, the person's "publish to graph" kept. Hands the mapping back to the Detector:
    later passes update it too."""
    from sqlalchemy.orm.attributes import flag_modified

    from opencortyx.knowledge.semantic import detector, store
    layer = await detector.detect(user.workspace_id, apply=False)
    async with V2Session() as db:
        m = await db.get(SchemaMapping, uuid.UUID(mapping_id))
        if not m or m.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="mapping not found")
        knowledge = store.redetected(m, layer)
        if knowledge is None:
            raise HTTPException(status_code=409, detail="The data no longer shows an entity type for this mapping.")
        m.payload = store.with_version(m.payload or {}, knowledge)
        flag_modified(m, "payload")
        await db.commit()
        await db.refresh(m)
    store.invalidate_views(user.workspace_id)
    await audit(user.workspace_id, user.user_id, "schema_mapping/redetect",
                meta={"mapping": mapping_id, "types": [e["type"] for e in knowledge["entities"]]})
    return mapping_view(m)


@router.post("/schema-mappings/publish")
async def publish_knowledge(everything: bool = False, user: CurrentUser = Depends(require_write)):
    """Publish the chosen entity types to the graph: one node per entity (by its identifier), one
    edge per link. `everything`: every accepted entity type, not only those marked `materialize`."""
    from opencortyx.knowledge.semantic import publish
    res = await publish.publish(user.workspace_id, everything=everything)
    await audit(user.workspace_id, user.user_id, "schema_mapping/publish", meta=res)
    return res


@router.get("/schema-mappings/knowledge")
async def knowledge_layer(user: CurrentUser = Depends(require_auth)):
    """Every entity type and relationship the workspace's mappings declare, merged across them."""
    from opencortyx.knowledge.semantic import detector, store
    return detector.summary(await store.load(user.workspace_id))


@router.post("/schema-mappings/{mapping_id}/validate")
async def validate_mapping(mapping_id: str, user: CurrentUser = Depends(require_auth)):
    from opencortyx.knowledge.semantic import check
    return await check.validate(user.workspace_id, await _mapping(user, mapping_id))


@router.get("/schema-mappings/{mapping_id}/preview")
async def preview_mapping(mapping_id: str, limit: int = 10, user: CurrentUser = Depends(require_auth)):
    from opencortyx.knowledge.semantic import check
    return await check.preview(user.workspace_id, await _mapping(user, mapping_id), limit=max(1, min(limit, 50)))


@router.get("/schema-mappings/{mapping_id}/history")
async def mapping_history(mapping_id: str, user: CurrentUser = Depends(require_auth)):
    m = await _mapping(user, mapping_id)
    p = m.payload or {}
    cur = p.get("knowledge") or {}
    now = [{"version": cur.get("version"), "at": m.updated_at.isoformat() if m.updated_at else None,
            "current": True, "knowledge": cur}] if cur else []
    return {"current": cur.get("version"),
            "versions": now + [{**h, "current": False} for h in p.get("history") or []]}


@router.get("/schema-mappings/{mapping_id}")
async def get_schema_mapping(mapping_id: str, user: CurrentUser = Depends(require_auth)):
    async with V2Session() as db:
        m = await db.get(SchemaMapping, uuid.UUID(mapping_id))
    if not m or m.workspace_id != user.workspace_id:
        raise HTTPException(status_code=404, detail="mapping not found")
    return mapping_view(m)


@router.put("/schema-mappings/{mapping_id}")
async def update_schema_mapping(mapping_id: str, patch: SchemaMappingPatch,
                                user: CurrentUser = Depends(require_write)):
    async with V2Session() as db:
        m = await db.get(SchemaMapping, uuid.UUID(mapping_id))
        if not m or m.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="mapping not found")
        if patch.name is not None:
            m.name = patch.name.strip() or m.name
        if patch.paradigm in MAPPING_PARADIGMS:
            m.paradigm = patch.paradigm
        if patch.source_type is not None:
            m.source_type = patch.source_type
        if patch.status is not None:
            m.status = patch.status
        linked: list[str] = []
        before = dict((m.payload or {}).get("knowledge") or {})
        if patch.payload is not None:
            new = dict(patch.payload)
            if new.get("knowledge"):
                from opencortyx.knowledge.semantic.store import resolve_assets
                new["knowledge"] = await resolve_assets(m.workspace_id, new["knowledge"])
            m.payload = prepare_mapping_payload(m.paradigm, m.name, _with_knowledge(m.payload, new))
            linked = await save_mapping_references(db, m.workspace_id, m.id, m.payload)
            m.summary = mapping_summary(m.paradigm, m.payload, patch.summary or m.summary)
            if m.paradigm == "semistructured":
                await register_semistructured_types(
                    db, m.workspace_id, [{"type": "json_structures", "config": m.payload}])
            await register_mapping_types(db, m.workspace_id, m.paradigm, m.name, m.payload)
        elif patch.summary is not None:
            m.summary = patch.summary
        await db.commit()
        await db.refresh(m)
    link_existing_later(user.workspace_id, linked)
    if patch.payload is not None and (m.payload or {}).get("knowledge"):
        from opencortyx.knowledge.semantic.store import invalidate_views, publish_changed
        invalidate_views(user.workspace_id)                  # questions read the edited types from now on
        if publish_changed(before, (m.payload or {}).get("knowledge") or {}):
            from opencortyx.tasks.maintenance import v2_apply_knowledge_choices
            v2_apply_knowledge_choices.delay(str(user.workspace_id), publish_first=True)
    return mapping_view(m)


@router.get("/schema-mappings/{mapping_id}/usage")
async def schema_mapping_usage(mapping_id: str, user: CurrentUser = Depends(require_auth)):
    """Which pipelines bind this mapping — so the UI can warn before deleting it. A pipeline whose
    mapping disappears doesn't crash; it just silently stops applying that mapping's rules, which is
    exactly the kind of quiet breakage worth showing up-front."""
    from opencortyx.domain.control import Pipeline, SchemaMapping
    mid = str(uuid.UUID(mapping_id))
    async with V2Session() as db:
        m = await db.get(SchemaMapping, uuid.UUID(mapping_id))
        if not m or m.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="Mapping not found")
        pls = (await db.execute(select(Pipeline).where(Pipeline.workspace_id == user.workspace_id))).scalars().all()
    used_by = [{"id": str(p.id), "name": p.name, "kind": p.kind}
               for p in pls if mapping_refs(p.stages, mid)]
    return {"id": mid, "name": m.name, "paradigm": m.paradigm, "pipelines": used_by}


@router.get("/schema-mappings/{mapping_id}/conformance")
async def schema_mapping_conformance(mapping_id: str, asset_id: uuid.UUID,
                                     user: CurrentUser = Depends(require_auth)):
    """Does the graph this asset produced MATCH the mapping? Exact names, both directions.

    `missing` = declared but not produced (the rule isn't being honoured, or the data has no such
    content). `extra` = produced but never declared. `platform` = written by the platform regardless of
    any mapping (Date links, provenance, reading order) — explained, not counted as a violation.

    The verify panel used to answer this in the browser with an ALIAS matcher: a produced `Chunk`
    satisfied a declared `TextChunk` because both contain "chunk", and extras were never looked for at
    all. So it reported conformance over a graph that did not conform. One exact answer, server-side,
    is what makes a mapping enforceable."""
    from opencortyx.ingest.pipelines.conformance import conformance, strict_mode
    async with V2Session() as db:
        m = await db.get(SchemaMapping, uuid.UUID(mapping_id))
        if not m or m.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="Mapping not found")
    graph = await node_subgraph(user.workspace_id, asset_id, hops=3, focus=True)
    if graph.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Asset not in the graph")
    mapping = {"paradigm": m.paradigm, "payload": m.payload or {}}
    out = conformance(mapping, graph.get("graph") or {})
    out.update({"id": str(m.id), "name": m.name, "paradigm": m.paradigm,
                "asset_id": str(asset_id), "strict": strict_mode(mapping)})
    return out


@router.delete("/schema-mappings/{mapping_id}")
async def delete_schema_mapping(mapping_id: str, user: CurrentUser = Depends(require_write)):
    """Delete a saved schema mapping. The graph it already produced is untouched — this removes the
    RULES, not the data (use the asset delete for that)."""
    async with V2Session() as db:
        m = await db.get(SchemaMapping, uuid.UUID(mapping_id))
        if not m or m.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="Mapping not found")
        name = m.name
        await save_mapping_references(db, user.workspace_id, m.id, {})    # its reference rules go with it
        res = await db.execute(delete(SchemaMapping).where(
            SchemaMapping.id == uuid.UUID(mapping_id), SchemaMapping.workspace_id == user.workspace_id))
        await db.commit()
    await audit(user.workspace_id, user.user_id, "schema-mapping/delete", meta={"name": name})
    return {"status": "ok", "deleted": res.rowcount or 0, "name": name}
