"""A cluster's master is COMPUTED from its members, never patched.

Resolution used to write a master once and then add to it (`EXTENDS`), so splitting a member off
left that member's values on the master, and deleting a file left its values there too. Now the
master is a projection of its members, rebuilt whenever its membership changes:

    compose(members, winners) -> the master's properties, and for each one which members state it

  * order     members in the order they were first written (the canonical one first)
  * a value   for each attribute, the value a person chose (a settled conflict, `winners`), else
              the first member's that has one
  * __prov    {attribute: [member ids stating the winning value]} - what supports each value
  * scope     the master is as restricted as its most restricted member: the highest sensitivity
              of any member, and a collection only when every member is in the same one (else none,
              with `__collections` listing them). Someone who cannot see every member cannot see
              the merged record; they still see the members they can (core/security/scope.py).

`rebuild(ids)` re-reads the members of each master (`MEMBER_OF` in-edges), writes the composed
record over the old one, and deletes a master left with fewer than two members: a cluster of one
is no cluster.
"""
from __future__ import annotations

import uuid
from typing import Iterable, Optional

from opencortyx.core.observability import get_logger

log = get_logger(__name__)

MEMBER = "MEMBER_OF"
#: How many supporting member ids are kept per attribute (a cluster can be large).
PROV_CAP = 20
#: Keys a member carries that are its own bookkeeping, never the master's.
_OWN = {"created_date", "modified_date", "__version", "__generated", "__unit_key", "__stale",
        "__resolved", "__resolution", "__resolve_waiting", "__base_type"}


def _order(n) -> tuple:
    return (str(getattr(n, "created_at", "") or ""), str(n.id))


def _norm(v):
    from opencortyx.resolution.normalize import norm
    try:
        return norm(v)
    except Exception:                                         # noqa: BLE001
        return str(v)


def compose(members: list, winners: Optional[dict] = None, canonical=None) -> dict:
    """{"label", "type", "properties", "prov", "sensitivity", "collection_id", "collections"} of the
    master of `members` (at least one). `winners` = {attribute: member id} chosen by a person.
    `canonical`: the member the cluster was founded on, which comes first (else the oldest)."""
    from opencortyx.domain import SENSITIVITY_ORDER
    members = sorted(members, key=lambda m: (str(m.id) != str(canonical), *_order(m)))
    winners = {k: str(v) for k, v in (winners or {}).items()}
    by_id = {str(m.id): m for m in members}
    canonical = members[0]
    attrs: list = []
    for m in members:
        for k in (m.properties or {}):
            if k not in attrs and not str(k).startswith("__") and k not in _OWN:
                attrs.append(k)
    props, prov = {}, {}
    for attr in attrs:
        chosen = by_id.get(winners.get(attr, ""))
        value = (chosen.properties or {}).get(attr) if chosen is not None else None
        if value in (None, ""):
            value = next(((m.properties or {}).get(attr) for m in members
                          if (m.properties or {}).get(attr) not in (None, "")), None)
        if value in (None, ""):
            continue
        props[attr] = value
        target = _norm(value)
        prov[attr] = [str(m.id) for m in members if _norm((m.properties or {}).get(attr)) == target][:PROV_CAP]
    levels = [SENSITIVITY_ORDER.get(getattr(m, "sensitivity", None) or "normal", 1) for m in members]
    top = max(levels) if levels else 1
    sensitivity = next((k for k, v in SENSITIVITY_ORDER.items() if v == top), "normal")
    colls = sorted({str(m.collection_id) for m in members if getattr(m, "collection_id", None)})
    unscoped = any(not getattr(m, "collection_id", None) for m in members)
    collection = uuid.UUID(colls[0]) if len(colls) == 1 and not unscoped else None
    return {"label": canonical.label, "type": canonical.type, "properties": props, "prov": prov,
            "sensitivity": sensitivity, "collection_id": collection,
            "collections": colls if collection is None and colls else [],
            "identity": (canonical.properties or {}).get("__identity") or "record",
            "members": [str(m.id) for m in members]}


async def masters_of(repo, workspace_id, node_ids: Iterable) -> dict:
    """{member id: master id} for those of `node_ids` that are members of a cluster."""
    ids = [i for i in dict.fromkeys(node_ids) if i]
    if not ids:
        return {}
    edges = await repo.edges(workspace_id, source_ids=ids, types=[MEMBER])
    return {e.source_id: e.target_id for e in edges}


async def members_of(repo, workspace_id, master_ids: Iterable) -> dict:
    """{master id: [member node, ...]}."""
    ids = [i for i in dict.fromkeys(master_ids) if i]
    if not ids:
        return {}
    edges = await repo.edges(workspace_id, target_ids=ids, types=[MEMBER])
    nodes = {n.id: n for n in await repo.get_many(workspace_id, list({e.source_id for e in edges}))}
    out: dict = {i: [] for i in ids}
    for e in edges:
        if e.source_id in nodes:
            out.setdefault(e.target_id, []).append(nodes[e.source_id])
    return out


async def winners_of(workspace_id, members: list) -> dict:
    """{attribute: member id} a person chose in Review ("use the new value") among these members."""
    ids = [m.id for m in members]
    if len(ids) < 2:
        return {}
    try:
        from sqlalchemy import select
        from opencortyx.core.db import V2Session
        from opencortyx.domain.resolution import ResolutionDecision as D
        async with V2Session() as db:
            rows = (await db.execute(select(D).where(
                D.workspace_id == workspace_id, D.relation == "CONTRADICTS", D.needs_review.is_(False),
                D.subject.in_(ids), D.target.in_(ids)).order_by(D.created_at))).scalars().all()
    except Exception:                                         # noqa: BLE001 - no ledger: first wins
        return {}
    out = {}
    for d in rows:
        choice = (d.evidence or {}).get("decision")
        if d.attribute and choice in ("use_new", "keep"):
            out[d.attribute] = d.subject if choice == "use_new" else d.target
    return out


async def rebuild(repo, workspace_id, master_ids: Iterable, *, winners=winners_of) -> dict:
    """Recompute each master from its members. Returns {"rebuilt": n, "removed": n}."""
    workspace_id = uuid.UUID(str(workspace_id))
    ids = [uuid.UUID(str(i)) for i in dict.fromkeys(master_ids) if i]
    if not ids:
        return {"rebuilt": 0, "removed": 0}
    from opencortyx.core.security.scope import unscoped
    with unscoped():
        # Composed from EVERY member, whoever's request triggered it: a master rebuilt from only the
        # members one caller may see would lose the others' values for everyone.
        groups = await members_of(repo, workspace_id, ids)
        old = {n.id: n for n in await repo.get_many(workspace_id, ids)}
    gone, specs = [], []
    for mid in ids:
        members = groups.get(mid) or []
        if len(members) < 2:
            gone.append(mid)
            continue
        prior = old.get(mid)
        canonical = (prior.properties or {}).get("__canonical") if prior is not None else None
        c = compose(members, await winners(workspace_id, members), canonical)
        props = {**c["properties"], "__master": True, "__identity": c["identity"], "__prov": c["prov"],
                 "__members": len(members), "__canonical": canonical or c["members"][0]}
        if c["collections"]:
            props["__collections"] = c["collections"]
        if prior is not None:
            # what the master held of its own that is not a value (its search rendering, its stamps)
            for k in ("__text", "text", "__embedding_text"):
                if k in (prior.properties or {}) and k not in props:
                    props[k] = prior.properties[k]
        specs.append({"node_id": mid, "type": c["type"], "label": c["label"], "properties": props,
                      "replace_properties": True, "sensitivity": c["sensitivity"],
                      "collection_id": c["collection_id"],
                      "confidence": max(float(getattr(m, "confidence", 1.0) or 1.0) for m in members),
                      "level": 0})
    if specs:
        await repo.upsert_many(workspace_id, specs, date_link=False)
        # A collection the master no longer has (its members moved apart) is cleared explicitly:
        # an upsert keeps the old collection when given none.
        for s in specs:
            prior = old.get(s["node_id"])
            if prior is not None and s["collection_id"] is None and prior.collection_id is not None:
                with unscoped():
                    fresh = await repo.get(workspace_id, s["node_id"])
                if fresh is not None:
                    fresh.collection_id = None
                    await repo.save(fresh)
        try:
            from opencortyx.tasks.graph import embed_later
            embed_later(workspace_id, [s["node_id"] for s in specs])
        except Exception:                                     # noqa: BLE001 - re-embedding is best effort
            pass
    if gone:
        present = [g for g in gone if g in old]
        if present:
            await repo.delete_nodes(workspace_id, present)
    return {"rebuilt": len(specs), "removed": len(gone)}


async def masters_touching(repo, workspace_id, node_ids: Iterable) -> list:
    """The masters any of `node_ids` belong to - to rebuild after those nodes change or go."""
    return list(dict.fromkeys((await masters_of(repo, workspace_id, node_ids)).values()))


__all__ = ["MEMBER", "compose", "masters_of", "masters_touching", "members_of", "rebuild", "winners_of"]
