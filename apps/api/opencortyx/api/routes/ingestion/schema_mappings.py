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
    payload = prepare_mapping_payload(paradigm, spec.name.strip(), spec.payload or {})
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
        if patch.payload is not None:
            m.payload = prepare_mapping_payload(m.paradigm, m.name, patch.payload)
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
