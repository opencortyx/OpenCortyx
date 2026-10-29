"""Routes: node and relationship types."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from opencortyx.api.schemas.graph.ontology import TypeSpec, TypeStatusSpec
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.ontology import PLATFORM_ORG, TypeRegistry, own_type, visible_types

router = APIRouter()


@router.get("/node-types/attributes")
async def observed_node_attributes(user: CurrentUser = Depends(require_auth)):
    """What each node type ACTUALLY carries in this org's graph, sampled from the nodes themselves.

    The type registry holds a DECLARED ontology, and a mapping is free to disagree with it — the
    seeded `Turn` has `role/ordinal/ts/query` while a LoCoMo card's `Turn` has `speaker/dia_id/
    valid_from`. Offering the declared list in a picker would therefore suggest attributes that do
    not exist on the user's nodes.

    It also answers the one case nothing else can: an extractor that writes an OPEN vocabulary
    (`set: "$llm.attrs"`, where the model invents the field name) has no declared schema at all, so
    the only way to know it produced `allergy` and `nickname_used` is to look at what landed.

    Sampled, not exhaustive: a bounded number of nodes per type, because this answers "what is on
    this type" for a picker, not "audit every row".
    """
    from collections import Counter

    from opencortyx.core.substrate import NodeRepo
    repo = NodeRepo()

    def json_type(value) -> str:
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        return {dict: "object", list: "array", str: "string"}.get(type(value), "null")

    out: dict = {}
    for ntype in sorted(await repo.count_by_type(user.workspace_id)):
        sample = await repo.find(user.workspace_id, exact_types=[ntype], order_by="-created_at", limit=200)
        attrs = Counter((k, json_type(v)) for n in sample for k, v in (n.properties or {}).items()
                        if not k.startswith("__"))
        subtypes = Counter(n.entity_type for n in sample if n.entity_type)
        out[ntype] = {
            "node_type": ntype,
            "attributes": [{"name": k, "value_type": vt, "seen": c} for (k, vt), c in attrs.most_common()],
            "subtypes": [{"name": name, "seen": c} for name, c in subtypes.most_common()],
        }
    return {"items": sorted(out.values(), key=lambda x: x["node_type"])}


@router.get("/ontology")
async def list_ontology(user: CurrentUser = Depends(require_auth)):
    from opencortyx.ontology import ORG_DOMAINS
    from opencortyx.ontology import PERSON_DOMAINS
    async with V2Session() as db:
        rows = await visible_types(db, user.workspace_id)
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.resolution.spec import KEY as RESOLUTION, ResolutionSpec
    # What this workspace's data has: a node type with nodes, or a type detection or a person added.
    # The rest is the built-in catalogue (Person / Company templates) - offered, not in use.
    counts = await NodeRepo().count_by_type(user.workspace_id)
    types = []
    for r in rows:
        ps = r.properties_schema or {}
        spec = ResolutionSpec.parse(ps.get(RESOLUTION))
        types.append({"name": r.name, "kind": r.kind, "description": r.description,
                      "status": r.status, "source": r.source,
                      "built_in": r.workspace_id == PLATFORM_ORG,               # shared, read-only until edited
                      "domain": ps.get("domain"),                       # Person-template grouping
                      "attributes": ps.get("fields") or [],             # data points / attributes
                      "allowed_source_types": r.allowed_source_types,   # edges: from
                      "allowed_target_types": r.allowed_target_types,   # edges: to
                      "resolved": bool(spec and spec.active),           # same-thing matching is set
                      "count": counts.get(r.name, 0) if r.kind != "edge" else None,
                      "in_data": r.source in ("discovered", "user") or bool(counts.get(r.name))})
    from opencortyx.knowledge.workspace import get_profile
    return {"types": types, "mode": (await get_profile(user.workspace_id)).mode,
            "person_domains": PERSON_DOMAINS, "org_domains": ORG_DOMAINS}


@router.post("/ontology/types")
async def register_type(spec: TypeSpec, user: CurrentUser = Depends(require_write)):
    """Register OR update a custom node/edge/entity type — no migration. `fields` are the attributes
    (data points the extractor pulls into node.properties); `domain` groups it in the schema view.
    Updating an existing type MERGES into its properties_schema so the domain grouping is preserved."""
    async with V2Session() as db:
        existing = await own_type(db, user.workspace_id, spec.name, spec.kind)
        if existing:
            ps = dict(existing.properties_schema or {})
            if spec.fields is not None:
                ps["fields"] = spec.fields
            if spec.domain is not None:
                ps["domain"] = spec.domain
            existing.properties_schema = ps
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(existing, "properties_schema")
            if spec.description:
                existing.description = spec.description
            if spec.allowed_source_types is not None:
                existing.allowed_source_types = spec.allowed_source_types
            if spec.allowed_target_types is not None:
                existing.allowed_target_types = spec.allowed_target_types
            await db.commit()
            return {"status": "updated", "name": spec.name}
        schema = {}
        if spec.fields:
            schema["fields"] = spec.fields
        if spec.domain:
            schema["domain"] = spec.domain
        db.add(TypeRegistry(
            workspace_id=user.workspace_id, name=spec.name, kind=spec.kind, description=spec.description,
            allowed_source_types=spec.allowed_source_types,
            allowed_target_types=spec.allowed_target_types,
            properties_schema=schema, source="user", status="active"))
        await db.commit()
    return {"status": "registered", "name": spec.name, "kind": spec.kind}


@router.post("/ontology/materialize")
async def materialize_ontology(template: str, user: CurrentUser = Depends(require_write)):
    """Instantiate a built-in ontology template (person | organization) as REAL graph nodes + edges so
    its hierarchy + relationships render in the Lineage Graph and asset graphs. Idempotent."""
    from opencortyx.ontology import build_rows, materialize_template
    if template == "person":
        from opencortyx.ontology import (
            PERSON_EDGES,
            PERSON_HUB,
            PERSON_NODES,
        )
        rows = build_rows(PERSON_HUB, PERSON_NODES, PERSON_EDGES, source="person")
    elif template == "organization":
        from opencortyx.ontology import ORG_EDGES, ORG_HUB, ORG_NODES
        rows = build_rows(ORG_HUB, ORG_NODES, ORG_EDGES, source="organization")
    else:
        raise HTTPException(status_code=400, detail="template must be 'person' or 'organization'")
    out = await materialize_template(user.workspace_id, rows, template)
    # Someone is waiting to see it on the graph: re-index now, not after the debounce.
    from opencortyx.tasks.graph_tree import schedule_tree_index
    out["graph"] = "rebuilding" if schedule_tree_index(str(user.workspace_id), now=True) else "scheduled"
    return out


@router.get("/ontology/candidates")
async def candidates(user: CurrentUser = Depends(require_auth)):
    from opencortyx.ingest.pipelines.conformance import platform_reading_types
    own = platform_reading_types()
    async with V2Session() as db:
        rows = (await db.execute(select(TypeRegistry).where(
            TypeRegistry.workspace_id == user.workspace_id, TypeRegistry.status == "candidate"))).scalars().all()
    # rows registered before the reader's own vocabulary was filtered out are not shown either
    rows = [r for r in rows if r.name not in own["nodes" if r.kind == "node" else "edges"]]
    return {"candidates": [{"name": r.name, "kind": r.kind, "description": r.description} for r in rows]}


@router.post("/ontology/promote")
async def promote(name: str, kind: str, user: CurrentUser = Depends(require_write)):
    async with V2Session() as db:
        row = await db.get(TypeRegistry, (user.workspace_id, name, kind))   # candidates are always the org's own
        if not row:
            return {"status": "not_found"}
        row.status = "active"
        await db.commit()
    return {"status": "promoted", "name": name}


@router.patch("/ontology/types")
async def edit_type(name: str, kind: str, description: str, user: CurrentUser = Depends(require_write)):
    async with V2Session() as db:
        row = await own_type(db, user.workspace_id, name, kind)   # a built-in type is copied, never changed
        if not row:
            return {"status": "not_found"}
        row.description = description     # the description drives extraction
        await db.commit()
    return {"status": "updated", "name": name}


@router.delete("/ontology/types")
async def delete_type(name: str, kind: str, user: CurrentUser = Depends(require_write)):
    async with V2Session() as db:
        # Only the organization's own row: deleting its copy of a built-in type reverts to the
        # built-in one, and the shared built-in rows themselves are never deleted.
        row = await db.get(TypeRegistry, (user.workspace_id, name, kind))
        if not row:
            return {"status": "not_found_or_system"}
        await db.delete(row)
        await db.commit()
    return {"status": "deleted", "name": name}


@router.put("/types/{name}/status")
async def set_type_status(name: str, spec: TypeStatusSpec, user: CurrentUser = Depends(require_write)):
    """Toggle a type's lifecycle status (active/candidate/disabled) — drives whether the extractor
    runs it. Matches the web client's `updateTypeStatus(name, kind, status)`."""
    async with V2Session() as db:
        row = await own_type(db, user.workspace_id, name, spec.kind)
        if not row:
            return {"status": "not_found"}
        row.status = spec.status
        await db.commit()
    from opencortyx.knowledge import governance
    governance.bust_gate_cache(user.workspace_id)   # the extraction gates read status; apply the toggle now
    return {"status": "ok", "name": name, "new_status": spec.status}
