"""Routes: resolution — proposals awaiting review, per-type settings, the decision ledger, and its
quality: labels, the score against them, checking automatic merges, splitting and joining by hand."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select

from opencortyx.api.schemas.graph.resolution import (
    EvaluateIn, LabelImportIn, LabelIn, MergeIn, ResolutionDecideIn, ResolutionSpecIn, SplitIn,
)
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
    node's value or `use_new` to take the newer record's: the choice is recorded and the cluster's
    master is rebuilt with it (resolution/master.py) - no member is overwritten, so the other value is
    still there to change one's mind. For a possible duplicate, `use_new` means "the same thing" (the
    two join one cluster) and `keep` means "different" (they are never proposed again). Either answer
    to a possible duplicate is also a label for measuring resolution (resolution/quality.py)."""
    from opencortyx.resolution import master as _master, quality
    repo = NodeRepo()
    async with V2Session() as db:
        d = await db.get(ResolutionDecision, decision_id)
        if d is None or d.workspace_id != user.workspace_id or not d.needs_review:
            raise HTTPException(status_code=404, detail="No conflict awaiting a decision with that id.")
        d.needs_review = False
        d.evidence = {**(d.evidence or {}), "decision": body.choice, "decided_by": str(user.user_id),
                      "by_person": True}
        subject, target, attribute, relation = d.subject, d.target, d.attribute, d.relation
        value = (d.evidence or {}).get("value")
        await db.commit()
    if not target:
        return {"status": "decided", "choice": body.choice}
    if attribute:
        masters = await _master.masters_touching(repo, user.workspace_id, [subject, target])
        if masters:
            await _master.rebuild(repo, user.workspace_id, masters)
        elif body.choice == "use_new":
            # Not in a cluster (written before clusters existed): the old behaviour, on the target.
            await repo.patch_properties(user.workspace_id, [target], {attribute: value})
    elif relation == "SAME":
        if body.choice == "use_new":
            from opencortyx.resolution.engine import join
            await join(repo, user.workspace_id, [(subject, target)], by=f"person:{user.user_id}")
        a, b = await repo.get(user.workspace_id, subject), await repo.get(user.workspace_id, target)
        if a is not None and b is not None:
            await quality.label_nodes(user.workspace_id, a, b, body.choice == "use_new", origin="review",
                                      author=str(user.user_id))
    return {"status": "decided", "choice": body.choice}


# ── resolution quality: labels, the score, checking automatic merges ──────────────────────────────

@router.get("/resolution/quality")
async def resolution_quality(user: CurrentUser = Depends(require_auth)):
    """Every type with resolution decisions or labels: label counts, automatic merges and possible
    duplicates, so it is clear where measuring is still missing."""
    from sqlalchemy import Integer, cast, func
    from opencortyx.resolution import quality
    labelled = {e["node_type"]: e for e in await quality.summary(user.workspace_id)}
    async with V2Session() as db:
        rows = (await db.execute(select(
            ResolutionDecision.node_type, func.count(),
            func.sum(cast(ResolutionDecision.needs_review, Integer)))
            .where(ResolutionDecision.workspace_id == user.workspace_id,
                   ResolutionDecision.relation == "SAME", ResolutionDecision.attribute.is_(None))
            .group_by(ResolutionDecision.node_type))).all()
    types = {}
    for t, n, review in rows:
        types[t] = {"node_type": t, "same_decisions": int(n), "awaiting_review": int(review or 0),
                    "labels": 0, "same": 0, "different": 0, "by_origin": {}}
    for t, e in labelled.items():
        types.setdefault(t, {"node_type": t, "same_decisions": 0, "awaiting_review": 0})
        types[t].update(e)
    return {"types": sorted(types.values(), key=lambda e: (-(e.get("labels") or 0), e["node_type"]))}


@router.post("/resolution/quality/evaluate")
async def evaluate_resolution(body: EvaluateIn, user: CurrentUser = Depends(require_auth)):
    """Score the type's rule on its labels - and, with `overrides`, a proposed rule - before saving
    it: precision, recall, false merges (each one listed), and the pairs a person would review."""
    from opencortyx.resolution import quality
    try:
        return await quality.evaluate_type(user.workspace_id, body.node_type.strip(), body.overrides)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"those settings are not valid: {e}")


@router.get("/resolution/audit")
async def audit_merges(node_type: Optional[str] = None, n: int = 20, user: CurrentUser = Depends(require_write)):
    """Automatic merges nobody has checked, at random, to answer "same" or "different" (each answer
    is a label; "different" also splits the pair)."""
    from opencortyx.resolution import quality
    return {"items": await quality.audit_sample(user.workspace_id, node_type, max(1, min(int(n), 100)))}


def _uid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{what} is not a node id")


@router.post("/resolution/labels")
async def add_label(body: LabelIn, user: CurrentUser = Depends(require_write)):
    """Label two nodes the same thing or different. With `apply` (the default) the graph follows:
    "different" splits them if they are joined and keeps them apart from now on; "same" joins them."""
    from opencortyx.resolution import engine, master as _master, quality
    repo = NodeRepo()
    a_id, b_id = _uid(body.a_id, "a_id"), _uid(body.b_id, "b_id")
    if a_id == b_id:
        raise HTTPException(status_code=422, detail="a node is always the same as itself")
    a, b = await repo.get(user.workspace_id, a_id), await repo.get(user.workspace_id, b_id)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="node not found")
    if a.type != b.type:
        raise HTTPException(status_code=422, detail="only two nodes of the same type can be the same thing")
    out = await quality.label_nodes(user.workspace_id, a, b, body.same, origin="audit" if body.apply else "manual",
                                    author=str(user.user_id), note=body.note)
    if body.apply:
        if body.same:
            await engine.join(repo, user.workspace_id, [(a.id, b.id)], by=f"person:{user.user_id}")
        else:
            await _keep_apart(user, a, b)
            current = await _master.masters_of(repo, user.workspace_id, [a.id, b.id])
            if current.get(a.id) and current.get(a.id) == current.get(b.id):
                await _split(user, current[a.id], a.id)
    return {"status": "ok", **out}


@router.get("/resolution/labels")
async def list_labels(node_type: Optional[str] = None, limit: int = 200, user: CurrentUser = Depends(require_auth)):
    from opencortyx.resolution import quality
    rows = await quality.load_labels(user.workspace_id, node_type, limit=max(1, min(int(limit), 5000)))
    return {"items": [{"id": str(r.id), "node_type": r.node_type, "a": r.a, "b": r.b, "same": r.same,
                       "origin": r.origin, "note": r.note,
                       "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]}


@router.post("/resolution/labels/import")
async def import_labels(body: LabelImportIn, user: CurrentUser = Depends(require_write)):
    """Import labelled pairs (a benchmark such as DBLP-ACM or Abt-Buy, or your own). Each record is
    given as what it says - a label, fields, optionally its asset - so no node has to exist."""
    from opencortyx.resolution import quality
    done = 0
    for it in body.items:
        snaps = []
        for side in (it.a, it.b):
            props = dict(side.properties or {})
            if body.named:
                props["__identity"] = "entity"
            snaps.append({"label": side.label, "type": it.node_type, "properties": props, "asset_id": side.asset_id})
        if quality.signature(snaps[0]) == quality.signature(snaps[1]):
            continue
        await quality.add_label(user.workspace_id, it.node_type.strip(), snaps[0], snaps[1], it.same,
                                origin="import", author=str(user.user_id), note=it.note)
        done += 1
    return {"status": "ok", "imported": done, "skipped": len(body.items) - done}


@router.delete("/resolution/labels/{label_id}")
async def delete_label(label_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    from sqlalchemy import delete
    from opencortyx.domain.resolution import ResolutionLabel
    async with V2Session() as db:
        res = await db.execute(delete(ResolutionLabel).where(ResolutionLabel.id == label_id,
                                                             ResolutionLabel.workspace_id == user.workspace_id))
        await db.commit()
    if not res.rowcount:
        raise HTTPException(status_code=404, detail="label not found")
    return {"status": "deleted"}


# ── clusters: what a master is made of, splitting and joining by hand ─────────────────────────────

@router.get("/resolution/masters/{master_id}")
async def master_detail(master_id: uuid.UUID, user: CurrentUser = Depends(require_auth)):
    """A merged record: its members, and for each value which members state it."""
    from opencortyx.resolution import master as _master
    repo = NodeRepo()
    m = await repo.get(user.workspace_id, master_id)
    if m is None or not (m.properties or {}).get("__master"):
        raise HTTPException(status_code=404, detail="no merged record with that id")
    members = (await _master.members_of(repo, user.workspace_id, [master_id])).get(master_id) or []
    props = m.properties or {}
    return {"id": str(m.id), "type": m.type, "label": m.label,
            "properties": {k: v for k, v in props.items() if not str(k).startswith("__")},
            "prov": props.get("__prov") or {}, "collections": props.get("__collections") or [],
            "members": [{"id": str(n.id), "label": n.label, "source_ref": str(n.source_ref) if n.source_ref else None,
                         "properties": {k: v for k, v in (n.properties or {}).items() if not str(k).startswith("__")}}
                        for n in sorted(members, key=lambda n: (str(n.created_at or ""), str(n.id)))]}


async def _keep_apart(user, a, b) -> None:
    """A person's "different": a DISTINCT decision, which resolution never re-joins (engine.Store.apart)."""
    from sqlalchemy.dialects.postgresql import insert
    from opencortyx.resolution import records
    row = records._decision(user.workspace_id, None, a.type, records.DISTINCT, a.id, b.id,
                            evidence={"by_person": True, "decided_by": str(user.user_id)})
    async with V2Session() as db:
        await db.execute(insert(ResolutionDecision).values(**row).on_conflict_do_update(
            index_elements=[ResolutionDecision.id], set_={"evidence": row["evidence"], "needs_review": False}))
        await db.commit()


async def _split(user, master_id, member_id) -> dict:
    from opencortyx.resolution import master as _master
    repo = NodeRepo()
    await repo.delete_edges(user.workspace_id, source_ids=[member_id], target_ids=[master_id], types=[_master.MEMBER])
    return await _master.rebuild(repo, user.workspace_id, [master_id])


@router.post("/resolution/masters/{master_id}/split")
async def split_member(master_id: uuid.UUID, body: SplitIn, user: CurrentUser = Depends(require_write)):
    """Take one member out of a merged record: it stands alone again, the master is rebuilt from the
    rest (or removed when one is left), and the member is kept apart from every remaining member -
    resolving again will not put it back. Each pair is labelled "different"."""
    from opencortyx.resolution import master as _master, quality
    repo = NodeRepo()
    member_id = _uid(body.member_id, "member_id")
    members = (await _master.members_of(repo, user.workspace_id, [master_id])).get(master_id) or []
    member = next((n for n in members if n.id == member_id), None)
    if member is None:
        raise HTTPException(status_code=404, detail="that node is not a member of this merged record")
    for other in members:
        if other.id != member_id:
            await _keep_apart(user, member, other)
            await quality.label_nodes(user.workspace_id, member, other, False, origin="split",
                                      author=str(user.user_id))
    out = await _split(user, master_id, member_id)
    return {"status": "split", **out}


@router.post("/resolution/merge")
async def merge_nodes(body: MergeIn, user: CurrentUser = Depends(require_write)):
    """Join two nodes of one type as the same thing (a person's decision, so two clusters may be
    folded into one). Labelled "same"."""
    from opencortyx.resolution import engine, quality
    repo = NodeRepo()
    a_id, b_id = _uid(body.a_id, "a_id"), _uid(body.b_id, "b_id")
    a, b = await repo.get(user.workspace_id, a_id), await repo.get(user.workspace_id, b_id)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="node not found")
    if a.id == b.id:
        raise HTTPException(status_code=422, detail="a node is always the same as itself")
    if a.type != b.type:
        raise HTTPException(status_code=422, detail="only two nodes of the same type can be the same thing")
    if (a.properties or {}).get("__master") or (b.properties or {}).get("__master"):
        raise HTTPException(status_code=422, detail="join members, not merged records")
    masters = await engine.join(repo, user.workspace_id, [(a.id, b.id)], by=f"person:{user.user_id}")
    await quality.label_nodes(user.workspace_id, a, b, True, origin="merge", author=str(user.user_id), note=body.note)
    return {"status": "merged", "master_id": str(next(iter(masters.values()))) if masters else None}


@router.post("/resolution/assets/{source_id}")
async def resolve_asset_again(source_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    """Resolve one asset again, for example after changing a type's settings. Queued; idempotent."""
    if await NodeRepo().get(user.workspace_id, source_id) is None:
        raise HTTPException(status_code=404, detail="Asset not found.")
    from opencortyx.tasks.resolution import v2_resolve_asset
    job = v2_resolve_asset.delay(str(user.workspace_id), str(source_id))
    return {"status": "queued", "task_id": job.id}
