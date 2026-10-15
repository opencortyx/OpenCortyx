"""Legacy routes: the cluster hierarchy the dashboard graph used to draw (see `legacy/__init__.py`).

Not included in the API router.

Every read is one parent's children: at most FANOUT clusters and the links between them, or — for a
leaf — its best-connected member nodes and the edges among them. The size of a response never depends
on the size of the graph.
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, or_, select, text

from opencortyx.core.db import V2Session
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.substrate import NodeRepo
from opencortyx.domain.graph_clusters import (
    GraphCluster, GraphClusterLink, GraphClusterMember, GraphClusterRun)
from opencortyx.legacy.graph_clusters import BOOKKEEPING_EDGES, FANOUT

router = APIRouter()

#: The clusters under one cluster, at any depth — how an internal cluster's members are reached.
_SUBTREE = text("""
    WITH RECURSIVE sub AS (
        SELECT id FROM opencortyx_v2.graph_cluster WHERE workspace_id = :org AND id = :cid
        UNION ALL
        SELECT c.id FROM opencortyx_v2.graph_cluster c JOIN sub ON c.parent_id = sub.id
        WHERE c.workspace_id = :org
    ) SELECT id FROM sub""")


def _cluster(c: GraphCluster) -> dict:
    return {"id": c.id, "parent_id": c.parent_id or None, "label": c.label, "kind": c.kind,
            "depth": c.depth, "size": c.size, "n_children": c.n_children, "n_members": c.n_members,
            "internal_edges": c.internal_edges, "external_edges": c.external_edges,
            "rep_node_id": str(c.rep_node_id) if c.rep_node_id else None,
            "types": c.types or {}, "preview": c.preview or []}


def _member(m: GraphClusterMember) -> dict:
    return {"id": str(m.node_id), "label": m.label, "type": m.type, "degree": m.degree,
            "cluster_id": m.cluster_id}


def _run(r: Optional[GraphClusterRun]) -> Optional[dict]:
    if r is None:
        return None
    return {"status": r.status, "nodes": r.nodes, "edges": r.edges, "clusters": r.clusters,
            "depth": r.depth, "modularity": r.modularity, "duration_ms": r.duration_ms,
            "error": r.error, "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None}


async def _path(s, org, cid: str) -> list[dict]:
    """Top-down ancestors of a cluster, itself included — the page's breadcrumb."""
    out = []
    seen = set()
    while cid and cid not in seen:
        seen.add(cid)
        c = await s.get(GraphCluster, (org, cid))
        if c is None:
            break
        out.append({"id": c.id, "label": c.label, "kind": c.kind})
        cid = c.parent_id
    return out[::-1]


def _queue_rebuild(org) -> Optional[str]:
    from opencortyx.legacy.cluster_tasks import v2_cluster_graph
    try:
        return v2_cluster_graph.delay(str(org)).id
    except Exception:                             # noqa: BLE001 — no broker: the page says "not built"
        return None


@router.get("/graph/clusters")
async def graph_clusters(parent: str = "", user: CurrentUser = Depends(require_auth)):
    """One level of the hierarchy: the children of `parent` (the top level when empty).

    For a leaf cluster the children are nodes: its FANOUT best-connected members, the edges among
    them, and `member_total` so the page can say how many more there are."""
    org = user.workspace_id
    async with V2Session() as s:
        run = await s.get(GraphClusterRun, org)
        if run is None:
            # First view of a graph clustered before this feature existed: build it now.
            if await NodeRepo().count(org):
                _queue_rebuild(org)
                return {"run": {"status": "queued"}, "parent": None, "path": [], "clusters": [],
                        "links": [], "members": [], "member_total": 0, "member_edges": []}
        node = None
        if parent:
            node = await s.get(GraphCluster, (org, parent))
            if node is None:
                raise HTTPException(404, "cluster not found — the hierarchy was rebuilt; start from the top")
        children = (await s.execute(
            select(GraphCluster).where(GraphCluster.workspace_id == org, GraphCluster.parent_id == (parent or ""))
            .order_by(GraphCluster.size.desc(), GraphCluster.id))).scalars().all()
        links = (await s.execute(
            select(GraphClusterLink).where(GraphClusterLink.workspace_id == org,
                                           GraphClusterLink.parent_id == (parent or ""))
            .order_by(GraphClusterLink.weight.desc()).limit(FANOUT * 8))).scalars().all()
        members, total = [], 0
        if node is not None and node.n_members:
            total = node.n_members
            members = (await s.execute(
                select(GraphClusterMember)
                .where(GraphClusterMember.workspace_id == org, GraphClusterMember.cluster_id == parent)
                .order_by(GraphClusterMember.degree.desc(), GraphClusterMember.label)
                .limit(FANOUT))).scalars().all()
        path = await _path(s, org, parent) if parent else []

    member_edges = []
    if members:
        ids = [m.node_id for m in members]
        shown = {str(i) for i in ids}
        seen = set()
        for e in await NodeRepo().edges(org, node_ids=ids):
            a, b = str(e.source_id), str(e.target_id)
            if a == b or a not in shown or b not in shown or e.type in BOOKKEEPING_EDGES:
                continue
            key = (a, b, e.type)
            if key in seen:
                continue
            seen.add(key)
            member_edges.append({"source": a, "target": b, "type": e.type,
                                 "relation": (e.properties or {}).get("relation")})

    return {"run": _run(run), "parent": _cluster(node) if node else None, "path": path,
            "clusters": [_cluster(c) for c in children],
            "links": [{"a": l.a_id, "b": l.b_id, "weight": l.weight, "edges": l.edges,
                       "edge_types": l.edge_types or {}} for l in links],
            "members": [_member(m) for m in members], "member_total": total,
            "member_edges": member_edges}


@router.get("/graph/clusters/{cluster_id}/members")
async def graph_cluster_members(cluster_id: str, q: str = "", offset: int = 0, limit: int = 50,
                                user: CurrentUser = Depends(require_auth)):
    """Member nodes of a cluster at any depth, best-connected first, optionally filtered by label."""
    org = user.workspace_id
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    async with V2Session() as s:
        if await s.get(GraphCluster, (org, cluster_id)) is None:
            raise HTTPException(404, "cluster not found")
        ids = [r[0] for r in (await s.execute(_SUBTREE, {"org": org, "cid": cluster_id})).all()]
        where = [GraphClusterMember.workspace_id == org, GraphClusterMember.cluster_id.in_(ids)]
        if q.strip():
            where.append(GraphClusterMember.label.ilike(f"%{q.strip()}%"))
        total = (await s.execute(select(func.count()).select_from(GraphClusterMember)
                                 .where(and_(*where)))).scalar_one()
        rows = (await s.execute(select(GraphClusterMember).where(and_(*where))
                                .order_by(GraphClusterMember.degree.desc(), GraphClusterMember.label)
                                .offset(offset).limit(limit))).scalars().all()
    return {"items": [_member(m) for m in rows], "total": total, "offset": offset, "limit": limit}


@router.get("/graph/clusters/{cluster_id}/connections")
async def graph_cluster_connections(cluster_id: str, user: CurrentUser = Depends(require_auth)):
    """The sibling clusters this one is linked to, strongest first, with the relationship types."""
    org = user.workspace_id
    async with V2Session() as s:
        rows = (await s.execute(
            select(GraphClusterLink).where(GraphClusterLink.workspace_id == org,
                                           or_(GraphClusterLink.a_id == cluster_id,
                                               GraphClusterLink.b_id == cluster_id))
            .order_by(GraphClusterLink.weight.desc()).limit(100))).scalars().all()
        other_ids = [l.b_id if l.a_id == cluster_id else l.a_id for l in rows]
        others = {c.id: c for c in (await s.execute(
            select(GraphCluster).where(GraphCluster.workspace_id == org,
                                       GraphCluster.id.in_(other_ids)))).scalars().all()} if other_ids else {}
    return {"items": [{"cluster": _cluster(others[o]) if o in others else {"id": o}, "weight": l.weight,
                       "edges": l.edges, "edge_types": l.edge_types or {}}
                      for l, o in zip(rows, other_ids)]}


@router.get("/graph/clusters/search")
async def graph_cluster_search(q: str, limit: int = 20, user: CurrentUser = Depends(require_auth)):
    """Nodes and clusters whose label matches, each with its path from the top — everything the page
    needs to open the clusters around a hit and highlight it in place."""
    org = user.workspace_id
    term = (q or "").strip()
    if not term:
        return {"nodes": [], "clusters": [], "total": 0}
    limit = max(1, min(int(limit), 50))
    like = f"%{term}%"
    async with V2Session() as s:
        where = [GraphClusterMember.workspace_id == org, GraphClusterMember.label.ilike(like)]
        total = (await s.execute(select(func.count()).select_from(GraphClusterMember)
                                 .where(and_(*where)))).scalar_one()
        members = (await s.execute(select(GraphClusterMember).where(and_(*where))
                                   .order_by(GraphClusterMember.degree.desc(), GraphClusterMember.label)
                                   .limit(limit))).scalars().all()
        clusters = (await s.execute(
            select(GraphCluster).where(GraphCluster.workspace_id == org, GraphCluster.kind == "community",
                                       GraphCluster.label.ilike(like))
            .order_by(GraphCluster.size.desc()).limit(10))).scalars().all()
        paths: dict[str, list[dict]] = {}
        for cid in {m.cluster_id for m in members} | {c.id for c in clusters}:
            paths[cid] = await _path(s, org, cid)
    return {
        "nodes": [{"node": _member(m), "path": [p["id"] for p in paths.get(m.cluster_id, [])]} for m in members],
        "clusters": [{"cluster": _cluster(c), "path": [p["id"] for p in paths.get(c.id, [])]} for c in clusters],
        "total": total,
    }


@router.get("/graph/clusters/locate/{node_id}")
async def graph_cluster_locate(node_id: uuid.UUID, user: CurrentUser = Depends(require_auth)):
    """Where a node sits in the hierarchy: its leaf cluster and that cluster's ancestors."""
    org = user.workspace_id
    async with V2Session() as s:
        m = await s.get(GraphClusterMember, (org, node_id))
        if m is None:
            raise HTTPException(404, "node is not in the cluster hierarchy yet")
        return {"node": _member(m), "path": await _path(s, org, m.cluster_id)}


@router.post("/graph/clusters/rebuild")
async def graph_clusters_rebuild(user: CurrentUser = Depends(require_write)):
    """Rebuild the hierarchy now instead of waiting for the next ingest."""
    task_id = _queue_rebuild(user.workspace_id)
    if task_id is None:
        raise HTTPException(503, "the task queue is not reachable")
    return {"status": "queued", "task_id": task_id}
