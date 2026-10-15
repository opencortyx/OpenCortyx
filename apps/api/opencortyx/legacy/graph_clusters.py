"""Community hierarchy for the dashboard graph: ~40 clusters per screen, whatever the graph's size.

A graph with a billion nodes cannot be drawn, and it does not need to be: a person reads ~40 things
on a screen. So the graph page never draws the raw graph. It draws one cluster's children — the top
level first — and each child opens into its own children, down to leaves of real nodes.

The hierarchy comes from Leiden (graspologic-native, the Rust core of graspologic), in two passes:

  down  `hierarchical_leiden` partitions the graph, then re-partitions every community bigger than
        FANOUT until each is at most FANOUT nodes. A community Leiden cannot split (a star: one hub,
        many leaves) stays a leaf, and the page pages through its members.
  up    The first partition of a large graph has thousands of communities, too many for one screen.
        So the communities become nodes of a smaller graph — edges between two communities summed
        into one weighted edge — and that graph is partitioned the same way. Repeat until the top
        level fits.

Leiden only groups what is connected. Nodes with no edges are grouped by type ("Unlinked · Person"),
and a level that is still too wide keeps its FANOUT-1 largest clusters and bundles the rest into one
"N smaller groups" cluster, so no screen shows more than FANOUT.

Time bookkeeping is left out: every node has a CREATED_ON edge to its calendar Date node, and those
few Date hubs would otherwise glue unrelated assets into one community.

Cluster ids are derived from the cluster's best-connected node and its depth, so re-clustering an
unchanged graph gives the same ids and the page can keep its place across a rebuild.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from opencortyx.core.observability import get_logger

log = get_logger(__name__)

FANOUT = 40                  # most clusters (or member nodes) one screen draws
SEED = 7                     # fixed, so the same graph gives the same clusters
MERGE_ROUNDS = 12            # bottom-up passes; each shrinks the top level by roughly FANOUT×
PREVIEW = 5                  # member labels kept per cluster for the side panel
BOOKKEEPING_EDGES = frozenset({"CREATED_ON", "MODIFIED_ON", "VALID_FROM", "VALID_TO", "INGESTED_ON"})
CALENDAR_TYPES = frozenset({"Date"})


@dataclass
class Cluster:
    key: str
    kind: str = "community"                       # community | bundle | unlinked
    children: list[str] = field(default_factory=list)
    members: list[str] = field(default_factory=list)
    parent: str = ""
    id: str = ""
    depth: int = 0
    size: int = 0
    rep: str | None = None
    label: str = ""
    types: Counter = field(default_factory=Counter)
    preview: list[tuple[int, str]] = field(default_factory=list)   # (degree, label), best first
    internal: int = 0
    external: int = 0


@dataclass
class Hierarchy:
    clusters: dict[str, Cluster]                  # by final id
    roots: list[str]                              # top-level ids
    member_of: dict[str, str]                     # node id → leaf cluster id
    links: dict[tuple[str, str], dict]            # (a id, b id) → parent, weight, edges, edge_types
    degree: dict[str, int]
    modularity: float | None
    nodes: int
    edges: int

    @property
    def depth(self) -> int:
        return 1 + max((c.depth for c in self.clusters.values()), default=-1)


def build_hierarchy(nodes: dict[str, tuple[str, str]], edges, *, fanout: int = FANOUT,
                    seed: int = SEED) -> Hierarchy:
    """Cluster a graph. `nodes` maps id → (label, type); `edges` yields (source, target, type, weight).

    Pure computation: no database, so it can be tested on any graph and timed on synthetic ones."""
    import graspologic_native as gn

    fanout = max(2, int(fanout))
    pair_w: dict[tuple[str, str], float] = defaultdict(float)
    pair_n: Counter = Counter()
    pair_types: dict[tuple[str, str], Counter] = defaultdict(Counter)
    degree: Counter = Counter()
    for a, b, etype, w in edges:
        if a == b or a not in nodes or b not in nodes:
            continue
        k = (a, b) if a < b else (b, a)
        pair_w[k] += float(w or 1.0)
        pair_n[k] += 1
        pair_types[k][etype] += 1
        degree[a] += 1
        degree[b] += 1

    cl: dict[str, Cluster] = {}
    roots: list[str] = []
    root_of: dict[str, str] = {}                  # node → its current top-level cluster key
    modularity = None

    # ── down: Leiden, then Leiden again inside every community bigger than FANOUT ─────────────
    if pair_w:
        # Sorted: Leiden's result depends on the order it first meets each node, and a seed alone
        # does not make the same graph give the same clusters when the store returns edges reordered.
        weighted = sorted((a, b, w) for (a, b), w in pair_w.items())
        level0: dict[str, int] = {}
        for e in gn.hierarchical_leiden(weighted, max_cluster_size=fanout, seed=seed):
            key = f"L{e.cluster}"
            c = cl.get(key) or cl.setdefault(key, Cluster(key))
            if e.parent_cluster is None:
                root_of[e.node] = key
                level0[e.node] = e.cluster
            else:
                parent = f"L{e.parent_cluster}"
                cl.setdefault(parent, Cluster(parent))
                c.parent = parent
            if e.is_final_cluster:
                c.members.append(e.node)
        for c in sorted(cl.values(), key=lambda c: c.key):
            if c.parent:
                cl[c.parent].children.append(c.key)
        roots = sorted({root_of[n] for n in root_of})
        try:
            modularity = float(gn.modularity(weighted, level0))
        except Exception:                         # noqa: BLE001 — a statistic, never worth failing for
            modularity = None

    # ── nodes with no edges: grouped by type, since there is nothing to cluster them by ─────────
    unlinked: dict[str, list[str]] = defaultdict(list)
    for nid, (_, ntype) in nodes.items():
        if nid not in degree:
            unlinked[ntype or "Node"].append(nid)
    unlinked_roots = []
    for ntype, members in sorted(unlinked.items()):
        key = f"U{ntype}"
        cl[key] = Cluster(key, kind="unlinked", members=members, label=f"Unlinked · {ntype}")
        unlinked_roots.append(key)

    # ── up: partition the graph OF communities until the top level fits on one screen ───────────
    # Merging stops once the communities that still touch each other fit in half a screen. Counting
    # all roots instead would keep merging whenever many small disconnected pieces fill the top
    # level — they never merge — until the connected graph had collapsed into one blob.
    for rnd in range(MERGE_ROUNDS):
        if len(roots) + len(unlinked_roots) <= fanout:
            break
        between: dict[tuple[str, str], float] = defaultdict(float)
        for (a, b), w in pair_w.items():
            ra, rb = root_of[a], root_of[b]
            if ra != rb:
                between[(ra, rb) if ra < rb else (rb, ra)] += w
        if len({r for k in between for r in k}) <= fanout // 2:
            break                                 # the rest is disconnected; bundling handles it
        groups: dict[int, list[str]] = defaultdict(list)
        for e in gn.hierarchical_leiden(sorted((a, b, w) for (a, b), w in between.items()),
                                        max_cluster_size=fanout, seed=seed):
            if e.is_final_cluster:
                groups[e.cluster].append(e.node)
        remap: dict[str, str] = {}
        for g, members in groups.items():
            if len(members) < 2:
                continue
            key = f"M{rnd}_{g}"
            cl[key] = Cluster(key, children=sorted(members))
            for m in members:
                cl[m].parent = key
                remap[m] = key
        if not remap:
            break
        roots = sorted({remap.get(r, r) for r in roots})
        root_of = {n: remap.get(r, r) for n, r in root_of.items()}

    roots = roots + unlinked_roots

    # ── tidy: a cluster with a single child is not a level worth a click ───────────────────────
    def _collapse(key: str) -> str:
        c = cl[key]
        while len(c.children) == 1 and not c.members:
            c = cl[c.children[0]]
        c.children = [_collapse(k) for k in c.children]
        return c.key

    roots = [_collapse(k) for k in roots]
    while len(roots) == 1 and cl[roots[0]].children:
        roots = cl[roots[0]].children

    # sizes bottom-up, then bundle any level still wider than FANOUT
    def _size(key: str) -> int:
        c = cl[key]
        c.size = len(c.members) + sum(_size(k) for k in c.children)
        return c.size

    for k in roots:
        _size(k)

    bundles = 0

    def _bundle(keys: list[str]) -> str:
        nonlocal bundles
        bundles += 1
        key = f"B{bundles}"
        cl[key] = Cluster(key, kind="bundle", children=keys, size=sum(cl[k].size for k in keys))
        return key

    def _pack(keys: list[str]) -> list[str]:
        """The largest half stays visible; the rest goes into bundles of up to FANOUT, and bundles
        of bundles, so the extra depth grows with log(count) rather than with the count."""
        keys = sorted(keys, key=lambda k: (-cl[k].size, k))
        if len(keys) <= fanout:
            return keys
        keep = keys[:fanout // 2]
        rest = keys[len(keep):]
        while len(rest) > fanout - len(keep):
            rest = [_bundle(rest[i:i + fanout]) for i in range(0, len(rest), fanout)]
        return keep + rest

    def _pack_all(keys: list[str]) -> list[str]:
        for k in keys:
            c = cl[k]
            if c.children and c.kind != "bundle":
                c.children = _pack_all(_pack(c.children))
            elif c.kind == "bundle":
                _pack_all(c.children)
        return keys

    roots = _pack_all(_pack(roots))

    # ── describe: representative, label, type mix, preview; then stable ids ───────────────────
    def _describe(key: str, parent: str, depth: int) -> None:
        c = cl[key]
        c.parent, c.depth = parent, depth
        for k in c.children:
            _describe(k, key, depth + 1)
        if c.members:
            ranked = sorted(c.members, key=lambda n: (-degree.get(n, 0), nodes[n][0] or "", n))
            c.types = Counter(nodes[n][1] or "Node" for n in c.members)
            c.preview = [(degree.get(n, 0), nodes[n][0] or nodes[n][1]) for n in ranked[:PREVIEW]]
            c.rep = ranked[0]
        else:
            for k in c.children:
                c.types.update(cl[k].types)
            c.preview = sorted((p for k in c.children for p in cl[k].preview),
                               key=lambda p: (-p[0], p[1]))[:PREVIEW]
            reps = [cl[k].rep for k in c.children if cl[k].rep]
            c.rep = max(reps, key=lambda n: (degree.get(n, 0), n)) if reps else None
        if c.kind == "community":
            c.label = (nodes[c.rep][0] or nodes[c.rep][1]) if c.rep else "Cluster"
        elif c.kind == "bundle":
            c.label = f"{len(c.children)} smaller groups"

    for k in roots:
        _describe(k, "", 0)

    final: dict[str, Cluster] = {}
    ids: dict[str, str] = {}
    order: list[str] = list(roots)
    i = 0
    while i < len(order):                        # breadth-first, so ids assign top-down
        c = cl[order[i]]
        order.extend(c.children)
        base = "c" + hashlib.sha1(f"{c.kind}:{c.rep}:{c.depth}:{c.label}".encode()).hexdigest()[:15]
        cid, n = base, 1
        while cid in final:
            n += 1
            cid = f"{base[:13]}{n:02d}"
        c.id = cid
        ids[c.key] = cid
        final[cid] = c
        i += 1
    for c in final.values():
        c.parent = ids.get(c.parent, "")
        c.children = [ids[k] for k in c.children]

    member_of = {n: c.id for c in final.values() for n in c.members}

    # ── links between siblings, and how inward- or outward-facing each cluster is ──────────────
    path_cache: dict[str, list[str]] = {}

    def _path(cid: str) -> list[str]:
        p = path_cache.get(cid)
        if p is None:
            c = final[cid]
            p = (_path(c.parent) if c.parent else []) + [cid]
            path_cache[cid] = p
        return p

    links: dict[tuple[str, str], dict] = {}
    for (a, b), w in pair_w.items():
        pa, pb = _path(member_of[a]), _path(member_of[b])
        n = pair_n[(a, b)]
        i = 0
        while i < len(pa) and i < len(pb) and pa[i] == pb[i]:
            final[pa[i]].internal += n
            i += 1
        if i == len(pa) and i == len(pb):
            continue                              # same leaf: drawn from the graph when it is opened
        for cid in pa[i:] + pb[i:]:
            final[cid].external += n
        x, y = (pa[i], pb[i]) if pa[i] < pb[i] else (pb[i], pa[i])
        link = links.get((x, y))
        if link is None:
            link = links[(x, y)] = {"parent": pa[i - 1] if i else "", "weight": 0.0, "edges": 0,
                                    "edge_types": Counter()}
        link["weight"] += w
        link["edges"] += n
        link["edge_types"].update(pair_types[(a, b)])

    return Hierarchy(clusters=final, roots=[ids[k] for k in roots], member_of=member_of, links=links,
                     degree=dict(degree), modularity=modularity, nodes=len(nodes),
                     edges=sum(pair_n.values()))


async def recluster(workspace_id) -> dict:
    """Rebuild one organization's cluster hierarchy from its graph and replace the stored one.

    Reads the whole graph, so the cost follows the graph's size, not the ingest's. The replace is one
    transaction: a reader sees the old hierarchy or the new one, never half of each."""
    from sqlalchemy import delete, insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from opencortyx.core.db import V2Session
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.domain.graph_clusters import (
        GraphCluster, GraphClusterLink, GraphClusterMember, GraphClusterRun)

    org = uuid.UUID(str(workspace_id))
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()

    async def _run_row(**values):
        values["updated_at"] = datetime.now(timezone.utc)
        async with V2Session() as s:
            stmt = pg_insert(GraphClusterRun).values(workspace_id=org, **values)
            await s.execute(stmt.on_conflict_do_update(index_elements=["workspace_id"], set_=values))
            await s.commit()

    repo = NodeRepo()
    if not await repo.count(org):
        # No graph (never ingested, or wiped, or a test tenant torn down): no cluster state either.
        async with V2Session() as s:
            for table in (GraphClusterLink, GraphClusterMember, GraphCluster, GraphClusterRun):
                await s.execute(delete(table).where(table.workspace_id == org))
            await s.commit()
        return {"nodes": 0, "edges": 0, "clusters": 0}

    await _run_row(status="running", started_at=started, error=None)
    try:
        nodes = {str(n.id): ((n.label or "").strip()[:300], (n.type or "")[:80])
                 for n in await repo.find(org) if n.type not in CALENDAR_TYPES}
        edges = [(str(e.source_id), str(e.target_id), e.type, e.weight)
                 for e in await repo.edges(org) if e.type not in BOOKKEEPING_EDGES]
        h = build_hierarchy(nodes, edges)

        async with V2Session() as s:
            for table in (GraphClusterLink, GraphClusterMember, GraphCluster):
                await s.execute(delete(table).where(table.workspace_id == org))
            await _insert_chunked(s, insert(GraphCluster), [
                {"workspace_id": org, "id": c.id, "parent_id": c.parent, "depth": c.depth, "kind": c.kind,
                 "label": c.label[:300], "size": c.size, "n_children": len(c.children),
                 "n_members": len(c.members), "internal_edges": c.internal,
                 "external_edges": c.external, "rep_node_id": uuid.UUID(c.rep) if c.rep else None,
                 "types": dict(c.types.most_common(12)), "preview": [p[1] for p in c.preview]}
                for c in h.clusters.values()])
            await _insert_chunked(s, insert(GraphClusterMember), [
                {"workspace_id": org, "node_id": uuid.UUID(n), "cluster_id": cid, "label": nodes[n][0],
                 "type": nodes[n][1], "degree": h.degree.get(n, 0)}
                for n, cid in h.member_of.items()])
            await _insert_chunked(s, insert(GraphClusterLink), [
                {"workspace_id": org, "a_id": a, "b_id": b, "parent_id": l["parent"], "weight": l["weight"],
                 "edges": l["edges"], "edge_types": dict(l["edge_types"].most_common(12))}
                for (a, b), l in h.links.items()])
            await s.commit()

        out = {"nodes": h.nodes, "edges": h.edges, "clusters": len(h.clusters), "depth": h.depth,
               "top_level": len(h.roots), "modularity": h.modularity,
               "duration_ms": int((time.monotonic() - t0) * 1000)}
        await _run_row(status="ok", nodes=out["nodes"], edges=out["edges"], clusters=out["clusters"],
                       depth=out["depth"], modularity=h.modularity, duration_ms=out["duration_ms"],
                       finished_at=datetime.now(timezone.utc), error=None)
        log.info("graph.clustered", workspace_id=str(org), **out)
        return out
    except Exception as e:
        await _run_row(status="error", error=str(e)[:2000], finished_at=datetime.now(timezone.utc),
                       duration_ms=int((time.monotonic() - t0) * 1000))
        raise


async def _insert_chunked(session, stmt, rows: list[dict], chunk: int = 5000) -> None:
    for i in range(0, len(rows), chunk):
        await session.execute(stmt, rows[i:i + chunk])
