"""The browse tree the dashboard graph draws, one level at a time.

    root  (the workspace: its central node — a Person or an Organization, id = workspace_id)
      └─ source          where assets came from: Files, a database, a collection, Authored…
          └─ tag         what the assets are: tags given at upload or on the asset page; an untagged
              │          asset carries its file extension (`pdf`, `csv`) until someone tags it
              └─ asset   a Source or Table node
                  └─ what the asset produced (its provenance closure) and what it links to
                      └─ any node's relationships, and so on

Parts of the graph that no asset reaches — nodes authored by hand, imported without a file — have no
place under the root, so each such connected part is a root of its own, drawn beside the workspace.

Every read is one parent's children, a page (`PAGE`) at a time, so no screen depends on the size of
the graph. A parent with more than `GROUP_OVER` children is first split into groups — an asset's
parts by node type, a node's relationships by (edge type, direction, node type) — and each group
pages on its own. The top three levels are about assets, which can number millions, so they are read
from an index (`domain/graph_tree.py`) that `reindex` rebuilds after the graph changes; everything
below an asset is read from the graph store directly.

An asset's tags live on the asset node (`properties.tags`, a list of strings). The index copies them;
it never owns them.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from opencortyx.core.db import V2Session
from opencortyx.core.observability import get_logger
from opencortyx.core.substrate import NodeRepo
from opencortyx.domain import ASSET_TYPES, Collection, IngestRun, is_asset_node
from opencortyx.domain.graph_tree import GraphAsset, GraphAssetTag, GraphRoot, GraphTagRequest, GraphTreeRun
from opencortyx.stores.graph.query import HIDDEN_STATUSES, node_status

log = get_logger(__name__)

PAGE = 100                  # children per click
GROUP_OVER = 1000           # more children than this → grouped first, each group paged
ROOTS_SHOWN = 12            # unconnected parts drawn beside the workspace; the rest behind one item
TAG_MAX = 80
TAGS_PER_ASSET = 20
EDGE_CAP = 2000             # relationships returned with one page, for drawing links between items
PARTS_CACHE_S = 120         # an opened asset's parts are listed once, then paged from memory
LINKED_SCAN = 20000         # past this many parts, what they link to is not listed (too costly)
ASSET_PAGE = 200            # assets read in full at a time by the index pass

#: Time bookkeeping: every node has a CREATED_ON edge to its calendar Date node. Following those
#: would make one Date the neighbour of everything written that day.
BOOKKEEPING_EDGES = frozenset({"CREATED_ON", "MODIFIED_ON", "VALID_FROM", "VALID_TO", "INGESTED_ON"})
CALENDAR_TYPES = frozenset({"Date"})
AUTHORED_SOURCES = frozenset({"user", "curation", "custom_mapping"})


# ── naming ──────────────────────────────────────────────────────────────────────────────────────────

def asset_source(node, collections: dict[str, str]) -> str:
    """Where an asset came from, as the tree's second level names it.

    A collection is the user's own grouping, so it wins. Otherwise: a database (per host:database,
    so two databases are two sources), the starter templates, things authored in the app, or files —
    uploads and synced folders alike, which the graph does not tell apart."""
    if node.collection_id and str(node.collection_id) in collections:
        return collections[str(node.collection_id)][:200]
    p = node.properties or {}
    system = p.get("source_system")
    if system == "sql":
        key = str(p.get("source_key") or "").strip()
        return f"Database · {key}"[:200] if key else "Databases"
    if system == "domain_template":
        return "Templates"
    if p.get("__source") in AUTHORED_SOURCES:
        return "Authored"
    return "Files"


def clean_tags(values) -> list[str]:
    """Tags as stored: trimmed, lower case, single-spaced, unique, at most TAGS_PER_ASSET."""
    out: list[str] = []
    for v in values or []:
        t = " ".join(str(v).strip().lower().split())[:TAG_MAX].strip()
        if t and t not in out:
            out.append(t)
    return out[:TAGS_PER_ASSET]


def parse_tags(text: str | None) -> list[str]:
    """Tags from a form field: a JSON list, or comma-separated words."""
    if not text or not str(text).strip():
        return []
    try:
        value = json.loads(text)
        if isinstance(value, list):
            return clean_tags(value)
    except ValueError:
        pass
    return clean_tags(str(text).split(","))


def default_tag(node) -> str:
    """The tag an untagged asset carries: `db` for a database table, else its file extension (`pdf`,
    `csv`), else what it is (`template`, `table`). However the asset arrived — upload, watched folder,
    schedule — this is derived from the asset itself.

    A database table is checked first: `public.orders` is a schema-qualified name, not a file with
    an `orders` extension."""
    p = node.properties or {}
    system = p.get("source_system")
    if system == "sql":
        return "db"
    if system == "domain_template":
        return "template"
    name = str(p.get("filename") or p.get("source_file") or node.label or "")
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    if ext and ext.isalnum() and len(ext) <= 10:
        return ext
    return (node.type or "asset").lower()


def asset_tags(node) -> tuple[list[str], bool]:
    """(tags, auto): the asset's own tags, or its default tag when it has none."""
    tags = clean_tags((node.properties or {}).get("tags"))
    return (tags, False) if tags else ([default_tag(node)], True)


# ── keys: how the page names what to open ──────────────────────────────────────────────────────────
# `kind:[json args]` — JSON so a source or tag name may hold any character.

def key(kind: str, *args) -> str:
    return f"{kind}:{json.dumps(list(args), separators=(',', ':'), ensure_ascii=False)}"


def parse_key(value: str) -> tuple[str, list]:
    kind, _, rest = (value or "").partition(":")
    try:
        args = json.loads(rest) if rest else []
    except ValueError:
        raise ValueError(f"not a tree key: {value!r}")
    if not isinstance(args, list):
        raise ValueError(f"not a tree key: {value!r}")
    return kind, args


def _node_item(n, *, size: int | None = None) -> dict:
    asset = is_asset_node(n)
    return {"id": str(n.id), "key": key("asset" if asset else "node", str(n.id)),
            "kind": "asset" if asset else "node", "label": (n.label or n.type or "")[:300],
            "type": n.type or "", "count": n.degree, "size": size, "openable": asset or n.degree > 0}


def _page(offset: int, limit: int | None) -> tuple[int, int]:
    return max(0, int(offset or 0)), max(1, min(int(limit or PAGE), PAGE))


# ── the index pass ─────────────────────────────────────────────────────────────────────────────────

async def reindex(workspace_id) -> dict:
    """Rebuild an organization's tree index from its graph.

    Unconnected parts need the whole graph — nodes, edges and provenance pointers — joined with
    union-find, but only its outline: ids, types, labels and the ends of each edge. Reading whole
    nodes, text included, for every pass ran the graph store out of memory once a thousand files
    were in it. Only assets, which the index describes, are then read in full, a page at a time."""
    org = uuid.UUID(str(workspace_id))
    started, t0 = datetime.now(timezone.utc), time.monotonic()
    await _run_row(org, status="running", started_at=started, finished_at=None, error=None)
    try:
        await apply_tag_requests(org)
        repo = NodeRepo()
        # The central node IS the root. Left in, it would be drawn again as an unconnected part, and
        # anything hung from it would glue every part it touches into one.
        nodes = [n for n in await repo.outlines(org)
                 if n.type not in CALENDAR_TYPES and node_status(n) not in HIDDEN_STATUSES
                 and n.id != org]
        edges = await repo.edge_ends(org, exclude_types=sorted(BOOKKEEPING_EDGES))
        async with V2Session() as s:
            collections = {str(c.id): c.name for c in
                           (await s.execute(select(Collection).where(Collection.workspace_id == org))).scalars()}
        asset_ids = [n.id for n in nodes if is_asset_node(n)]
        assets = []
        for i in range(0, len(asset_ids), ASSET_PAGE):
            assets += await repo.get_many(org, asset_ids[i:i + ASSET_PAGE])
        roots = unconnected_parts(nodes, edges)
        await _replace_index(org, assets, collections, roots)
        result = {"nodes": len(nodes), "assets": len(assets), "roots": len(roots)}
        await _run_row(org, status="ok", finished_at=datetime.now(timezone.utc),
                       duration_ms=int((time.monotonic() - t0) * 1000), **result)
        return result
    except Exception as e:
        await _run_row(org, status="error", error=str(e)[:2000], finished_at=datetime.now(timezone.utc))
        raise


def unconnected_parts(nodes, edges) -> list[dict]:
    """The connected parts of the graph that hold no asset, largest first, each named after its
    best-connected node: the most links within `edges`. A provenance pointer (`source_ref`) joins a
    node to its source like an edge does: a claim read from a chunk belongs to the chunk's asset even
    with no edge between them. `edges` are Edges or (source id, target id) pairs."""
    parent = {str(n.id): str(n.id) for n in nodes}
    ends = [(str(e[0]), str(e[1])) if isinstance(e, tuple) else (str(e.source_id), str(e.target_id))
            for e in edges]
    links: Counter = Counter()
    for a, b in ends:
        links[a] += 1
        links[b] += 1

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

    for a, b in ends:
        union(a, b)
    for n in nodes:
        if n.source_ref:
            union(str(n.id), str(n.source_ref))
    anchored = {find(str(n.id)) for n in nodes if is_asset_node(n)}
    parts: dict[str, list] = defaultdict(list)
    for n in nodes:
        r = find(str(n.id))
        if r not in anchored:
            parts[r].append(n)
    out = []
    for members in parts.values():
        rep = min(members, key=lambda n: (-links[str(n.id)], (n.label or "").lower(), str(n.id)))
        out.append({"node_id": rep.id, "label": (rep.label or rep.type or "")[:300], "type": (rep.type or "")[:80],
                    "size": len(members), "types": dict(Counter(n.type for n in members).most_common(12))})
    out.sort(key=lambda r: (-r["size"], r["label"].lower(), str(r["node_id"])))
    return out


def _asset_rows(org, node, collections) -> tuple[dict, list[dict]]:
    source = asset_source(node, collections)
    label = (node.label or node.type or "")[:300]
    tags, auto = asset_tags(node)
    row = {"workspace_id": org, "asset_id": node.id, "label": label, "type": (node.type or "")[:80], "source": source}
    return row, [{"workspace_id": org, "asset_id": node.id, "tag": t, "source": source, "label": label, "auto": auto}
                 for t in tags]


async def _replace_index(org, assets, collections, roots) -> None:
    asset_rows, tag_rows = [], []
    for n in assets:
        a, ts = _asset_rows(org, n, collections)
        asset_rows.append(a)
        tag_rows += ts
    root_rows = [{"workspace_id": org, **r} for r in roots]
    async with V2Session() as s:
        for table in (GraphAsset, GraphAssetTag, GraphRoot):
            await s.execute(delete(table).where(table.workspace_id == org))
        for table, rows in ((GraphAsset, asset_rows), (GraphAssetTag, tag_rows), (GraphRoot, root_rows)):
            for i in range(0, len(rows), 2000):
                await s.execute(insert(table), rows[i:i + 2000])
        await s.commit()


async def reindex_asset(workspace_id, asset_id) -> None:
    """Refresh one asset's rows now — after its tags were edited — without a whole pass."""
    org = uuid.UUID(str(workspace_id))
    node = await NodeRepo().get(org, asset_id)
    async with V2Session() as s:
        await s.execute(delete(GraphAssetTag).where(GraphAssetTag.workspace_id == org, GraphAssetTag.asset_id == asset_id))
        await s.execute(delete(GraphAsset).where(GraphAsset.workspace_id == org, GraphAsset.asset_id == asset_id))
        if node is not None and is_asset_node(node):
            collections = {}
            if node.collection_id:
                c = await s.get(Collection, node.collection_id)
                if c is not None:
                    collections[str(c.id)] = c.name
            a, ts = _asset_rows(org, node, collections)
            await s.execute(insert(GraphAsset), [a])
            await s.execute(insert(GraphAssetTag), ts)
        await s.commit()


async def _run_row(org, **values) -> None:
    async with V2Session() as s:
        stmt = insert(GraphTreeRun).values(workspace_id=org, **values)
        await s.execute(stmt.on_conflict_do_update(index_elements=["workspace_id"], set_=values))
        await s.commit()


# ── tags ───────────────────────────────────────────────────────────────────────────────────────────

async def set_tags(workspace_id, asset_id, tags) -> list[str]:
    """Replace an asset's tags. An empty list gives it back its default tag."""
    org = uuid.UUID(str(workspace_id))
    repo = NodeRepo()
    node = await repo.get(org, asset_id)
    if node is None or not is_asset_node(node):
        raise LookupError("asset not found")
    tags = clean_tags(tags)
    if tags:
        await repo.patch_properties(org, [node.id], {"tags": tags})
    else:
        await repo.patch_properties(org, [node.id], remove=["tags"])
    await reindex_asset(org, node.id)
    return tags


async def request_tags(workspace_id, run_id, tags) -> None:
    """Remember tags given with an upload until its ingest says which asset they belong to."""
    tags = clean_tags(tags)
    if not tags or not run_id:
        return
    async with V2Session() as s:
        stmt = insert(GraphTagRequest).values(workspace_id=uuid.UUID(str(workspace_id)), run_id=uuid.UUID(str(run_id)), tags=tags)
        await s.execute(stmt.on_conflict_do_update(index_elements=["workspace_id", "run_id"], set_={"tags": tags}))
        await s.commit()


async def apply_tag_requests(workspace_id) -> int:
    """Move upload tags onto the assets their finished runs produced. Returns assets tagged.

    A run that is still going keeps its request; one that failed, or is a week old, drops it."""
    org = uuid.UUID(str(workspace_id))
    async with V2Session() as s:
        reqs = (await s.execute(select(GraphTagRequest).where(GraphTagRequest.workspace_id == org))).scalars().all()
        if not reqs:
            return 0
        runs = {r.id: r for r in (await s.execute(
            select(IngestRun).where(IngestRun.workspace_id == org, IngestRun.id.in_([q.run_id for q in reqs])))).scalars()}
    repo, tagged, done = NodeRepo(), 0, []
    week_ago = time.time() - 7 * 24 * 3600
    for req in reqs:
        run = runs.get(req.run_id)
        if run is not None and run.status in ("queued", "running"):
            created = req.created_at.timestamp() if req.created_at else time.time()
            if created > week_ago:
                continue
        done.append(req.run_id)
        if run is None or run.status != "completed":
            continue
        ref = run.source_ref or (run.result or {}).get("source_id")
        if not ref:
            continue
        try:
            targets = [await repo.get(org, uuid.UUID(str(ref)))]
        except ValueError:                        # a structured run names its source by key, not id
            targets = await repo.find(org, types=list(ASSET_TYPES), source_key=str(ref))
        for node in targets:
            if node is None or not is_asset_node(node):
                continue
            merged = clean_tags(list((node.properties or {}).get("tags") or []) + list(req.tags or []))
            await repo.patch_properties(org, [node.id], {"tags": merged})
            tagged += 1
    if done:
        async with V2Session() as s:
            await s.execute(delete(GraphTagRequest).where(GraphTagRequest.workspace_id == org,
                                                          GraphTagRequest.run_id.in_(done)))
            await s.commit()
    return tagged


# ── reads ──────────────────────────────────────────────────────────────────────────────────────────

def _run(r: GraphTreeRun | None) -> dict | None:
    if r is None:
        return None
    return {"status": r.status, "nodes": r.nodes, "assets": r.assets, "roots": r.roots,
            "duration_ms": r.duration_ms, "error": r.error,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None}


def _root_item(r: GraphRoot) -> dict:
    return {"id": str(r.node_id), "key": key("node", str(r.node_id)), "kind": "root_part", "label": r.label,
            "type": r.type, "count": r.size, "size": r.size, "types": r.types or {}, "openable": True}


async def top(workspace_id) -> dict:
    """The first screen: the workspace root and the largest unconnected parts beside it."""
    org = uuid.UUID(str(workspace_id))
    async with V2Session() as s:
        run = await s.get(GraphTreeRun, org)
        assets = (await s.execute(select(func.count()).select_from(GraphAsset)
                                  .where(GraphAsset.workspace_id == org))).scalar_one()
        sources = (await s.execute(select(func.count(func.distinct(GraphAsset.source)))
                                   .where(GraphAsset.workspace_id == org))).scalar_one()
        roots_total = (await s.execute(select(func.count()).select_from(GraphRoot)
                                       .where(GraphRoot.workspace_id == org))).scalar_one()
        roots = (await s.execute(select(GraphRoot).where(GraphRoot.workspace_id == org)
                                 .order_by(GraphRoot.size.desc(), GraphRoot.label, GraphRoot.node_id)
                                 .limit(ROOTS_SHOWN))).scalars().all()
    from opencortyx.knowledge.workspace import ensure_central_node, get_profile
    profile = await get_profile(org)
    try:
        await ensure_central_node(org, profile)    # a workspace made before it had one gets it here
    except Exception as e:                         # noqa: BLE001 — the tree still draws without it
        log.warning("graph_tree.central_node_failed", workspace_id=str(org), error=str(e))
    items = [{"id": "root", "key": "root:[]", "kind": "root", "label": profile.display_name,
              "type": profile.central_type, "node_id": str(org), "mode": profile.mode,
              "count": assets, "size": sources, "openable": assets > 0}]
    items += [_root_item(r) for r in roots]
    if roots_total > ROOTS_SHOWN:
        items.append({"id": "roots", "key": "roots:[]", "kind": "bundle",
                      "label": f"{roots_total - ROOTS_SHOWN:,} more unconnected parts", "type": "",
                      "count": roots_total - ROOTS_SHOWN, "size": None, "openable": True})
    return {"run": _run(run), "items": items, "assets": assets, "sources": sources, "roots_total": roots_total}


async def children(workspace_id, tree_key: str, offset: int = 0, limit: int | None = None) -> dict:
    """What `tree_key` opens into, one page: {items, edges, total, offset, limit, grouped}."""
    org = uuid.UUID(str(workspace_id))
    offset, limit = _page(offset, limit)
    kind, args = parse_key(tree_key)
    if kind == "root":
        return await _sources(org, offset, limit)
    if kind == "roots":
        return await _more_roots(org, offset, limit)
    if kind == "source" and len(args) == 1:
        return await _tags(org, str(args[0]), offset, limit)
    if kind == "tag" and len(args) == 2:
        return await _tagged_assets(org, str(args[0]), str(args[1]), offset, limit)
    if kind == "asset" and len(args) == 1:
        return await _asset_children(org, uuid.UUID(str(args[0])), None, offset, limit)
    if kind == "parts" and len(args) == 3:
        return await _asset_children(org, uuid.UUID(str(args[0])), (str(args[1]), str(args[2])), offset, limit)
    if kind == "node" and len(args) == 1:
        return await _node_children(org, uuid.UUID(str(args[0])), offset, limit)
    if kind == "rel" and len(args) == 4:
        return await _relation_page(org, uuid.UUID(str(args[0])), str(args[1]), str(args[2]), str(args[3]),
                                    offset, limit)
    raise ValueError(f"not a tree key: {tree_key!r}")


def _result(items, total, offset, limit, *, edges=None, grouped=False, consumed: int | None = None) -> dict:
    """`next` is where the following page starts. It counts what was paged over, which for a node is
    relationships, not the distinct neighbours they lead to (two edges to one node are one item)."""
    return {"items": items, "edges": edges or [], "total": total, "offset": offset, "limit": limit,
            "next": offset + (len(items) if consumed is None else consumed), "grouped": grouped}


async def _sources(org, offset, limit) -> dict:
    async with V2Session() as s:
        n = func.count().label("n")
        rows = (await s.execute(select(GraphAsset.source, n).where(GraphAsset.workspace_id == org)
                                .group_by(GraphAsset.source).order_by(n.desc(), GraphAsset.source)
                                .offset(offset).limit(limit))).all()
        total = (await s.execute(select(func.count(func.distinct(GraphAsset.source)))
                                 .where(GraphAsset.workspace_id == org))).scalar_one()
    return _result([{"id": key("source", src), "key": key("source", src), "kind": "source", "label": src,
                     "type": "Source", "count": c, "size": None, "openable": True} for src, c in rows],
                   total, offset, limit)


async def _more_roots(org, offset, limit) -> dict:
    async with V2Session() as s:
        total = (await s.execute(select(func.count()).select_from(GraphRoot)
                                 .where(GraphRoot.workspace_id == org))).scalar_one()
        rows = (await s.execute(select(GraphRoot).where(GraphRoot.workspace_id == org)
                                .order_by(GraphRoot.size.desc(), GraphRoot.label, GraphRoot.node_id)
                                .offset(ROOTS_SHOWN + offset).limit(limit))).scalars().all()
    return _result([_root_item(r) for r in rows], max(0, total - ROOTS_SHOWN), offset, limit)


async def _tags(org, source, offset, limit) -> dict:
    async with V2Session() as s:
        n = func.count().label("n")
        where = (GraphAssetTag.workspace_id == org, GraphAssetTag.source == source)
        rows = (await s.execute(select(GraphAssetTag.tag, n, func.bool_and(GraphAssetTag.auto)).where(*where)
                                .group_by(GraphAssetTag.tag).order_by(n.desc(), GraphAssetTag.tag)
                                .offset(offset).limit(limit))).all()
        total = (await s.execute(select(func.count(func.distinct(GraphAssetTag.tag))).where(*where))).scalar_one()
    return _result([{"id": key("tag", source, t), "key": key("tag", source, t), "kind": "tag", "label": t,
                     "type": "Tag", "count": c, "size": None, "auto": bool(auto), "openable": True}
                    for t, c, auto in rows], total, offset, limit)


async def _tagged_assets(org, source, tag, offset, limit) -> dict:
    async with V2Session() as s:
        where = (GraphAssetTag.workspace_id == org, GraphAssetTag.source == source, GraphAssetTag.tag == tag)
        total = (await s.execute(select(func.count()).select_from(GraphAssetTag).where(*where))).scalar_one()
        ids = [r[0] for r in (await s.execute(select(GraphAssetTag.asset_id).where(*where)
                                              .order_by(GraphAssetTag.label, GraphAssetTag.asset_id)
                                              .offset(offset).limit(limit))).all()]
    nodes = await NodeRepo().get_many(org, ids)
    return _result([_node_item(n) for n in nodes], total, offset, limit)


# An opened asset: its parts (the provenance closure — what exists because of it) and what they link
# to, listed once and paged from memory for a couple of minutes. Ordered part-first, then by type.
_parts_cache: dict[tuple[str, str], tuple[float, list[tuple[str, str, str, str]]]] = {}


async def asset_parts(org, asset_id) -> list[tuple[str, str, str, str]]:
    """[(rel, type, label, id)] — rel is "part" (made from the asset) or "linked" (linked to it or
    to one of its parts, and not made from it: a shared entity, a neighbouring table)."""
    ck = (str(org), str(asset_id))
    hit = _parts_cache.get(ck)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    repo = NodeRepo()
    parts = [n for n in await repo.descendants(org, [asset_id], max_depth=24)
             if n.type not in CALENDAR_TYPES and node_status(n) not in HIDDEN_STATUSES]
    own = {str(n.id) for n in parts} | {str(asset_id)}
    linked: dict[str, Any] = {}
    scan = [str(asset_id)] + ([str(n.id) for n in parts] if len(parts) <= LINKED_SCAN else [])
    for i in range(0, len(scan), 500):
        for e, n in await repo.neighbors(org, scan[i:i + 500], direction="both", limit=LINKED_SCAN):
            if (e.type in BOOKKEEPING_EDGES or n.type in CALENDAR_TYPES or str(n.id) in own
                    or is_asset_node(n)):
                continue
            linked[str(n.id)] = n
    rows = sorted([("part", n.type or "", (n.label or "")[:300], str(n.id)) for n in parts]
                  + [("linked", n.type or "", (n.label or "")[:300], str(n.id)) for n in linked.values()],
                  key=lambda r: (r[0] != "part", r[1], r[2].lower(), r[3]))
    if len(_parts_cache) > 64:
        _parts_cache.pop(next(iter(_parts_cache)))
    _parts_cache[ck] = (time.monotonic() + PARTS_CACHE_S, rows)
    return rows


async def _asset_children(org, asset_id, group: tuple[str, str] | None, offset, limit) -> dict:
    rows = await asset_parts(org, asset_id)
    if group is None and len(rows) > GROUP_OVER:
        counts = Counter((r[0], r[1]) for r in rows)
        groups = sorted(counts.items(), key=lambda kv: (kv[0][0] != "part", -kv[1], kv[0][1]))
        page = groups[offset:offset + limit]
        return _result([{"id": key("parts", str(asset_id), t, rel), "key": key("parts", str(asset_id), t, rel),
                         "kind": "group", "label": t if rel == "part" else f"{t} (linked)", "type": t,
                         "count": c, "size": None, "rel": rel, "openable": True}
                        for (rel, t), c in page], len(groups), offset, limit, grouped=True)
    if group is not None:
        want_type, want_rel = group
        rows = [r for r in rows if r[1] == want_type and r[0] == want_rel]
    page = rows[offset:offset + limit]
    nodes = await NodeRepo().get_many(org, [r[3] for r in page])
    rel_of = {r[3]: r[0] for r in page}
    items = [{**_node_item(n), "rel": rel_of.get(str(n.id))} for n in nodes]
    return _result(items, len(rows), offset, limit, edges=await _edges_of(org, [str(n.id) for n in nodes]))


async def _node_children(org, node_id, offset, limit) -> dict:
    repo = NodeRepo()
    groups = sorted(await repo.neighbor_groups(org, node_id, exclude_edge_types=list(BOOKKEEPING_EDGES)),
                    key=lambda g: (-g.count, g.edge_type, g.direction, g.node_type))
    groups = [g for g in groups if g.node_type not in CALENDAR_TYPES]
    total = sum(g.count for g in groups)
    if total > GROUP_OVER:
        page = groups[offset:offset + limit]
        return _result([{"id": key("rel", str(node_id), g.edge_type, g.direction, g.node_type),
                         "key": key("rel", str(node_id), g.edge_type, g.direction, g.node_type),
                         "kind": "group", "label": f"{g.edge_type} {'→' if g.direction == 'out' else '←'} {g.node_type}",
                         "type": g.node_type, "count": g.count, "size": None, "edge_type": g.edge_type,
                         "direction": g.direction, "openable": True} for g in page],
                       len(groups), offset, limit, grouped=True)
    hops: list = []
    skip = offset
    for g in groups:                               # one page, walked across the groups in order
        if skip >= g.count:
            skip -= g.count
            continue
        take = min(limit - len(hops), g.count - skip)
        hops += await repo.neighbor_page(org, node_id, edge_type=g.edge_type, direction=g.direction,
                                         node_type=g.node_type, offset=skip, limit=take)
        skip = 0
        if len(hops) >= limit:
            break
    return await _hop_result(org, hops, total, offset, limit)


async def _relation_page(org, node_id, edge_type, direction, node_type, offset, limit) -> dict:
    repo = NodeRepo()
    groups = await repo.neighbor_groups(org, node_id, exclude_edge_types=list(BOOKKEEPING_EDGES))
    total = next((g.count for g in groups if (g.edge_type, g.direction, g.node_type)
                  == (edge_type, direction, node_type)), 0)
    hops = await repo.neighbor_page(org, node_id, edge_type=edge_type, direction=direction,
                                    node_type=node_type, offset=offset, limit=limit)
    return await _hop_result(org, hops, total, offset, limit)


async def _hop_result(org, hops, total, offset, limit) -> dict:
    items, seen = [], set()
    for _, n in hops:
        if str(n.id) not in seen:
            seen.add(str(n.id))
            items.append(_node_item(n))
    return _result(items, total, offset, limit, edges=await _edges_of(org, list(seen)), consumed=len(hops))


async def _edges_of(org, ids: list[str]) -> list[dict]:
    """Relationships of the nodes on one page, to anything — the page draws those whose other end
    is on screen. Capped: a page holding a hub must not return the hub's million edges."""
    if not ids:
        return []
    out, seen = [], set()
    for e, _ in await NodeRepo().neighbors(org, ids, direction="both", limit=EDGE_CAP):
        if e.type in BOOKKEEPING_EDGES or str(e.id) in seen:
            continue
        seen.add(str(e.id))
        out.append({"source": str(e.source_id), "target": str(e.target_id), "type": e.type,
                    "relation": (e.properties or {}).get("relation")})
    return out


async def locate(workspace_id, node_id) -> dict | None:
    """The keys to open, top down, for a node to be on screen: root → source → tag → its asset,
    and the asset itself when the node is one of its parts. None when no asset reaches it.

    A shared entity has several assets; the one `provenance` ranks first is the one opened."""
    from opencortyx.core.substrate.provenance import provenance
    org = uuid.UUID(str(workspace_id))
    repo = NodeRepo()
    node = await repo.get(org, node_id)
    if node is None:
        return None
    asset_id = (await provenance(org, [node.id])).get(str(node.id), {}).get("asset_id")
    asset = node if is_asset_node(node) else (await repo.get(org, asset_id) if asset_id else None)
    if asset is None:
        return None
    async with V2Session() as s:
        row = (await s.execute(select(GraphAssetTag).where(GraphAssetTag.workspace_id == org,
                                                           GraphAssetTag.asset_id == asset.id)
                               .order_by(GraphAssetTag.auto, GraphAssetTag.tag).limit(1))).scalars().first()
    if row is None:
        return None
    path = ["root:[]", key("source", row.source), key("tag", row.source, row.tag)]
    if asset.id != node.id:
        path.append(key("asset", str(asset.id)))
    return {"path": path, "asset": _node_item(asset), "node": _node_item(node)}


async def search(workspace_id, q: str, limit: int = 20) -> dict:
    """Assets whose name matches, then nodes whose text matches (BM25), each with where it sits."""
    org = uuid.UUID(str(workspace_id))
    term = (q or "").strip()
    if not term:
        return {"items": []}
    async with V2Session() as s:
        ids = [r[0] for r in (await s.execute(select(GraphAsset.asset_id)
                                              .where(GraphAsset.workspace_id == org, GraphAsset.label.ilike(f"%{term}%"))
                                              .order_by(GraphAsset.label).limit(limit))).all()]
    repo = NodeRepo()
    found = await repo.get_many(org, ids)
    try:
        found += [n for n, _ in await repo.keyword_search(workspace_id=org, query=term, k=limit)
                  if str(n.id) not in {str(x.id) for x in found}]
    except Exception as e:                        # noqa: BLE001 — no search index: names only
        log.warning("graph_tree.search_text_failed", error=str(e))
    return {"items": [_node_item(n) for n in found[:limit] if n.type not in CALENDAR_TYPES]}
