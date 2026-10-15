"""Routes: resolution — proposals awaiting review, per-type settings, the decision ledger."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select

from opencortyx.api.schemas.graph.resolution import ResolutionDecideIn, ResolutionSpecIn
from opencortyx.core.db import V2Session
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.substrate import NodeRepo
from opencortyx.domain.resolution import ResolutionDecision
from opencortyx.resolution.spec import ResolutionSpec, get_spec, save_spec

router = APIRouter()

from opencortyx.capabilities.modules.contradiction import RESERVED as _R

#: What a proposal writes on the statement it would retire — the engine's own reserved names plus the
#: versioning module's two, so a rejected proposal leaves nothing behind (a drifted copy of this list
#: left `resolution_priority` on every rejected statement).
_RESOLUTION_KEYS = (*_R.PROPOSAL_KEYS, "resolution_kind", "version_distance")


@router.get("/resolution/proposals")
async def resolution_proposals(user: CurrentUser = Depends(require_auth)):
    """Proposed supersessions awaiting approval: the loser (to be retired), the winner that replaces
    it, and what they are about. Only present when review mode created them."""
    repo = NodeRepo()
    losers = await repo.find(user.workspace_id, status="supersede_proposed")
    winners = {str(w.id): w for w in await repo.get_many(
        user.workspace_id, [(n.properties or {}).get("superseded_by") for n in losers
                      if (n.properties or {}).get("superseded_by")])}
    out = []
    for n in losers:
        p = n.properties or {}
        w = winners.get(str(p.get("superseded_by")))
        out.append({"id": str(n.id), "type": n.type, "about": p.get("resolution_about"),
                    "kind": p.get("resolution_kind") or "claim",
                    "distance": p.get("version_distance"),
                    "loser_text": p.get("text") or p.get("statement") or n.label,
                    "priority": p.get("resolution_priority") or "review",
                    "winner": {"id": str(w.id), "text": (w.properties or {}).get("text") or w.label} if w else None})
    out.sort(key=lambda r: (r["priority"] != "human", r["loser_text"] or ""))
    return {"proposals": out}


@router.get("/resolution/history/{node_id}")
async def resolution_history(node_id: uuid.UUID, user: CurrentUser = Depends(require_auth)):
    """How this value changed over time, newest first.

    Answers the two questions a validity window alone cannot tell apart: "where are they NOW?" is the
    head of the list, "where were they BEFORE?" is the next entry. Each `evolve` wrote an
    EVOLVED_FROM edge, so this walks a real chain in the graph rather than ranking guesses — which is
    the reason a superseded statement is kept rather than deleted."""
    from opencortyx.capabilities.modules.contradiction import history_of
    versions = await history_of(user.workspace_id, node_id)
    return {"node": str(node_id), "versions": versions,
            "current": next((v for v in versions if v.get("current")), None)}


@router.post("/resolution/proposals/{loser_id}/approve")
async def approve_proposal(loser_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    """Apply a proposed supersession: draw SUPERSEDES and retire the loser."""
    repo = NodeRepo()
    loser = await repo.get(user.workspace_id, loser_id)
    if loser is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    props = loser.properties or {}
    winner = props.get("superseded_by")
    if props.get("resolution_kind") == "version" and winner:
        from opencortyx.capabilities.modules.versioning import apply_version_supersede
        await apply_version_supersede(user.workspace_id, uuid.UUID(str(winner)), loser.id)
        return {"status": "approved", "kind": "version"}
    if winner:
        await repo.add_edge(workspace_id=user.workspace_id, source_id=uuid.UUID(str(winner)), target_id=loser.id,
                            type="SUPERSEDES")
    loser.properties = {**props, "superseded": "true"}
    loser.confidence = 0.0
    await repo.save(loser)
    return {"status": "approved", "kind": props.get("resolution_kind") or "claim"}


@router.post("/resolution/proposals/{loser_id}/reject")
async def reject_proposal(loser_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    """Dismiss a proposed supersession, so the statement stays fully active."""
    repo = NodeRepo()
    if await repo.get(user.workspace_id, loser_id) is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    await repo.patch_properties(user.workspace_id, [loser_id], remove=_RESOLUTION_KEYS)
    return {"status": "rejected"}


# ── the resolution engine: per-type settings, decisions, re-resolving an asset ─────────────────────
#: How many nodes of a type are read to list its attributes, and to find the assets to re-resolve.
_SAMPLE, _RERUN_CAP = 50, 5000
#: Bookkeeping every node carries; it says nothing about what the node is.
_NOT_IDENTITY = {"text", "created_date", "modified_date", "valid_from", "valid_to", "Label"}


@router.get("/resolution/types/{name}")
async def get_type_resolution(name: str, user: CurrentUser = Depends(require_auth)):
    """The type's settings, and the attributes its nodes actually carry: the choices for
    `identify_by`, `compare` and `ignore`, whichever mapping or extractor wrote them."""
    async with V2Session() as db:
        spec = await get_spec(db, user.workspace_id, name)
    nodes = await NodeRepo().find(user.workspace_id, exact_types=[name], limit=_SAMPLE)
    seen: dict[str, int] = {}
    for n in nodes:
        props = n.properties or {}
        generated = set(props.get("__generated") or [])
        for k in props:
            if not str(k).startswith("__") and k not in _NOT_IDENTITY and k not in generated:
                seen[k] = seen.get(k, 0) + 1
    return {"type": name, "resolution": spec.to_dict() if spec else None,
            "attributes": sorted(seen, key=lambda k: (-seen[k], k)), "sampled": len(nodes)}


@router.put("/resolution/types/{name}")
async def set_type_resolution(name: str, body: ResolutionSpecIn, user: CurrentUser = Depends(require_write)):
    """How nodes of `name` are resolved in this organization. The Extractor sets the same thing when
    it saves; this is for types no extractor emits (a mapping's records, for example)."""
    spec = ResolutionSpec.parse(body.model_dump())
    async with V2Session() as db:
        await save_spec(db, user.workspace_id, name.strip()[:80], spec)
        await db.commit()
    return {"type": name, "resolution": spec.to_dict()}


@router.post("/resolution/types/{name}/run")
async def resolve_type_again(name: str, user: CurrentUser = Depends(require_write)):
    """Resolve again every asset that holds nodes of this type, so settings saved now also apply to
    data that is already in, not only to what is ingested next. Queued, one task per asset."""
    from opencortyx.core.substrate.provenance import assets_of
    from opencortyx.tasks.resolution import v2_resolve_asset
    nodes = await NodeRepo().find(user.workspace_id, exact_types=[name], limit=_RERUN_CAP)
    assets = sorted(await assets_of(user.workspace_id, [n.id for n in nodes]))
    for asset_id in assets:
        v2_resolve_asset.delay(str(user.workspace_id), asset_id)
    return {"status": "queued", "assets": len(assets), "capped": len(nodes) >= _RERUN_CAP}


@router.get("/resolution/decisions")
async def list_decisions(node_id: Optional[uuid.UUID] = None, needs_review: Optional[bool] = None,
                         limit: int = 100, user: CurrentUser = Depends(require_auth)):
    """What the engine decided, newest first, with the evidence and score of each: for one node (as
    subject or target), those applied automatically (`needs_review=false`), or those awaiting a
    decision (`needs_review=true`). Node and asset names are included so the list reads as text."""
    q = select(ResolutionDecision).where(ResolutionDecision.workspace_id == user.workspace_id)
    if node_id is not None:
        q = q.where(or_(ResolutionDecision.subject == node_id, ResolutionDecision.target == node_id))
    if needs_review is not None:
        q = q.where(ResolutionDecision.needs_review == needs_review)
    q = q.order_by(ResolutionDecision.created_at.desc()).limit(max(1, min(int(limit), 1000)))
    async with V2Session() as db:
        rows = (await db.execute(q)).scalars().all()
    ids = {i for d in rows for i in (d.subject, d.target, d.source_id) if i}
    names = {n.id: n.label for n in await NodeRepo().get_many(user.workspace_id, list(ids))} if ids else {}
    return {"items": [{"id": str(d.id), "relation": d.relation, "node_type": d.node_type,
                       "subject": str(d.subject), "subject_label": names.get(d.subject),
                       "target": str(d.target) if d.target else None, "target_label": names.get(d.target),
                       "attribute": d.attribute, "evidence": d.evidence or {}, "needs_review": d.needs_review,
                       "confidence": d.confidence, "method": d.method,
                       "source_id": str(d.source_id) if d.source_id else None,
                       "source_label": names.get(d.source_id),
                       "created_at": d.created_at.isoformat() if d.created_at else None} for d in rows]}


@router.post("/resolution/decisions/{decision_id}/decide")
async def decide_conflict(decision_id: uuid.UUID, body: ResolutionDecideIn,
                          user: CurrentUser = Depends(require_write)):
    """Settle what resolution left for a person. For an attribute conflict, `keep` the existing
    node's value or `use_new` to take the newer record's (both stay in the ledger). For a possible
    duplicate (a SAME below the automatic threshold), `use_new` means "the same thing" and links the
    two nodes `SAME_AS`; `keep` means they are different."""
    async with V2Session() as db:
        d = await db.get(ResolutionDecision, decision_id)
        if d is None or d.workspace_id != user.workspace_id or not d.needs_review:
            raise HTTPException(status_code=404, detail="No conflict awaiting a decision with that id.")
        if body.choice == "use_new" and d.target:
            if d.attribute:
                await NodeRepo().patch_properties(user.workspace_id, [d.target], {d.attribute: (d.evidence or {}).get("value")})
            elif d.relation == "SAME":
                await NodeRepo().add_edges(workspace_id=user.workspace_id, edges=[{
                    "source_id": d.subject, "target_id": d.target, "type": "SAME_AS",
                    "properties": {"__by": "resolution", "decided_by": str(user.user_id)}}])
        d.needs_review = False
        d.evidence = {**(d.evidence or {}), "decision": body.choice, "decided_by": str(user.user_id)}
        await db.commit()
    return {"status": "decided", "choice": body.choice}


@router.post("/resolution/assets/{source_id}")
async def resolve_asset_again(source_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    """Resolve one asset again, for example after changing a type's settings. Queued; idempotent."""
    if await NodeRepo().get(user.workspace_id, source_id) is None:
        raise HTTPException(status_code=404, detail="Asset not found.")
    from opencortyx.tasks.resolution import v2_resolve_asset
    job = v2_resolve_asset.delay(str(user.workspace_id), str(source_id))
    return {"status": "queued", "task_id": job.id}
