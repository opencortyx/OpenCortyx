"""Where a node came from: the asset(s) it was read out of, or an explicit origin when there is none.

Provenance is stored, but indirectly, and in two shapes:

  parent chain   A chunk, a row, a nested record points at its parent through `source_ref` (chunk →
                 document, row → table, record → the record above it). Walking up always ends at the
                 asset: a node whose base type is an asset type (`Source`, `Table`).
  containment    A walked JSON payload records nesting as edges, not `source_ref`: file -HAS_PROGRAMME->
                 programme -HAS_PROJECT-> project. When the parent chain finds no asset, the walk
                 climbs incoming containment edges (`HAS_*`, `CONTAINS`; any other edge only if none
                 of those exist) until it reaches assets — possibly several, when a record is
                 contained in more than one.
  lineage edges  An entity is SHARED — one "Acme Corp" node for every document that names it — so it
                 has no single parent. It points with EXTRACTED_FROM at each chunk it was read from,
                 and each of those chunks walks up to an asset. One entity, many assets, legitimately.

`provenance()` answers for a batch of nodes at once. When a node has several assets they are RANKED
FOR THE QUESTION, not listed in arrival order: each asset reaches the node through a specific chunk,
and the asset whose chunk best matches the question comes first — asked about Acme's revenue, the
answer cites the document where Acme appears next to revenue, not the first file that ever named it.
Every entry also carries that chunk (`via` and `snippet`), because "Acme" is not evidence; the
sentence that says it is.

A node with no asset is not left blank: `origin` says what it is instead (curation, template, memory,
system). So the rule a caller can rely on is exact: every node has an `asset_id` or an `origin`.

The evidence ledger (core/dataplane/evidence.py) is read FIRST: a node an extractor wrote inside an
evidence scope has pointers to the exact chunk, row or record it rests on, with the quote, and those
are its sources - ranked for the question by their quote. The graph walks above answer for nodes
written before the ledger existed, or by writers that keep no evidence.

Bounded for hub entities: a node mentioned in millions of chunks reads at most LINEAGE_CAP lineage
edges here and says so (`source_count_capped`); `sources_page()` pages the full list on demand.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Sequence

from opencortyx.domain.ontology import is_asset_node
from opencortyx.stores.graph.models import EdgeQuery, Node

LINEAGE_EDGE = "EXTRACTED_FROM"
#: Edges that never mean "is inside": date bookkeeping, extraction lineage, reading order.
NOT_CONTAINMENT = frozenset({"CREATED_ON", "MODIFIED_ON", "VALID_FROM", "VALID_TO", "INGESTED_ON",
                             LINEAGE_EDGE, "NEXT"})
CLIMB_NODES = 500          # nodes visited per containment climb, however wide the parents fan out
LINEAGE_CAP = 200          # lineage edges read per node while answering
MAX_DEPTH = 12             # parent-chain hops before giving up (deep JSON nests stay well under)
PER_NODE = 5               # sources returned per node; the rest is counted
SNIPPET = 200

_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset("the and for with that this from what which who whom whose when where why how are was "
                  "were been has have had does did its their there about into over under than then them "
                  "they you your our can could would should will shall may might must".split())


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def _text_of(node: Optional[Node]) -> str:
    if node is None:
        return ""
    p = node.properties or {}
    # `__text` is how a walked JSON record states its fields ("name: Payments Core Rewrite\nstatus:
    # amber …"); without it a record's evidence snippet was just its label.
    return str(p.get("text") or p.get("statement") or p.get("content") or p.get("__text") or node.label or "")


def origin_of(node: Node) -> str:
    """What a node with no asset is. Checked only after the asset lookups came up empty."""
    p = node.properties or {}
    if str(p.get("__memory") or p.get("__internal") or "").lower() == "true":
        return "memory"
    if p.get("__template") or p.get("__concept"):
        return "template"
    if p.get("__source") in ("curation", "user"):
        return "curation"
    if node.type == "Date":
        return "system"
    return "unknown"


def _ledger_entry(asset: Node, pointer: dict, score: float) -> dict[str, Any]:
    return {"asset_id": str(asset.id), "label": asset.label or asset.type, "type": asset.type,
            "via": pointer.get("unit_key"),
            "snippet": " ".join(str(pointer.get("quote") or "").split())[:SNIPPET],
            "score": round(score, 4),
            "unit": {"kind": pointer.get("unit_kind"), "key": pointer.get("unit_key"),
                     "version_ref": pointer.get("version_ref"), "property": pointer.get("property")}}


async def _ledger(workspace_id, ids: list[str]) -> dict[str, list[dict]]:
    try:
        from opencortyx.core.dataplane import evidence
        return await evidence.for_targets(workspace_id, ids)
    except Exception:                                         # noqa: BLE001 - the graph walk still answers
        return {}


def _asset_entry(asset: Node, via: Optional[Node], score: float) -> dict[str, Any]:
    return {"asset_id": str(asset.id), "label": asset.label or asset.type, "type": asset.type,
            "via": str(via.id) if via is not None else None,
            "snippet": " · ".join(s.strip() for s in _text_of(via).splitlines() if s.strip())[:SNIPPET]
                       if via is not None else "",
            "score": round(score, 4)}


class _Walker:
    """Batched, cached `source_ref` walks from any node up to its asset."""

    def __init__(self, store, workspace_id):
        self.store, self.org = store, workspace_id
        self.nodes: dict[str, Node] = {}
        self.asset_of: dict[str, Optional[str]] = {}

    async def load(self, ids) -> None:
        missing = [i for i in dict.fromkeys(str(x) for x in ids if x) if i not in self.nodes]
        if missing:
            for n in await self.store.get_nodes(self.org, missing):
                self.nodes[str(n.id)] = n

    async def assets(self, ids: Sequence[str]) -> dict[str, Optional[str]]:
        """node id → id of the asset at the top of its parent chain (itself, if it is one)."""
        todo = [str(i) for i in ids if str(i) not in self.asset_of]
        await self.load(todo)
        # chain[start] = the node reached so far; advance every unresolved chain one level per pass
        chain = {i: i for i in todo}
        seen: dict[str, set[str]] = {i: {i} for i in todo}
        for _ in range(MAX_DEPTH):
            if not chain:
                break
            parents = []
            for start, cur in list(chain.items()):
                node = self.nodes.get(cur)
                if node is None:
                    self.asset_of[start] = None
                    del chain[start]
                elif is_asset_node(node):
                    self.asset_of[start] = cur
                    del chain[start]
                elif cur in self.asset_of and cur != start:
                    self.asset_of[start] = self.asset_of[cur]
                    del chain[start]
                elif node.source_ref is None or str(node.source_ref) in seen[start]:
                    self.asset_of[start] = None
                    del chain[start]
                else:
                    parents.append(str(node.source_ref))
            await self.load(parents)
            for start, cur in list(chain.items()):
                nxt = str(self.nodes[cur].source_ref)
                seen[start].add(nxt)
                chain[start] = nxt
        for start in chain:
            self.asset_of[start] = None
        return {str(i): self.asset_of.get(str(i)) for i in ids}

    async def climb(self, ids: Sequence[str]) -> dict[str, list[str]]:
        """node id → assets reached by climbing incoming containment edges, nearest level first.

        All nodes climb together: one edge query per LEVEL for the whole batch, not one per node."""
        found: dict[str, list[str]] = {i: [] for i in ids}
        seen: dict[str, set[str]] = {i: {i} for i in ids}
        frontier: dict[str, list[str]] = {i: [i] for i in ids}
        for _ in range(MAX_DEPTH):
            active = [i for i in ids if frontier[i] and not found[i] and len(seen[i]) < CLIMB_NODES]
            if not active:
                break
            targets = list(dict.fromkeys(t for i in active for t in frontier[i]))
            edges = [e for e in await self.store.find_edges(
                self.org, EdgeQuery(target_ids=targets, limit=CLIMB_NODES * len(active)))
                if e.type not in NOT_CONTAINMENT]
            incoming: dict[str, list] = {}
            for e in edges:
                incoming.setdefault(str(e.target_id), []).append(e)
            parents_of: dict[str, list[str]] = {}
            for i in active:
                es = [e for t in frontier[i] for e in incoming.get(t, [])]
                contain = [e for e in es if e.type.startswith("HAS_") or e.type == "CONTAINS"]
                ps = [str(e.source_id) for e in (contain or es) if str(e.source_id) not in seen[i]]
                parents_of[i] = list(dict.fromkeys(ps))[:CLIMB_NODES - len(seen[i])]
            await self.load([p for ps in parents_of.values() for p in ps])
            for i in active:
                frontier[i] = []
                for pid in parents_of[i]:
                    seen[i].add(pid)
                    node = self.nodes.get(pid)
                    if node is None:
                        continue
                    if is_asset_node(node):
                        found[i].append(pid)
                    else:
                        frontier[i].append(pid)
        return found


async def assets_of(workspace_id, node_ids: Sequence, *, store=None) -> set[str]:
    """The assets these nodes belong to (by their parent chains), without the ones that have none."""
    if store is None:
        from opencortyx.core.substrate import NodeRepo
        store = NodeRepo().graph
    found = await _Walker(store, workspace_id).assets([str(i) for i in node_ids if i])
    return {a for a in found.values() if a}


async def _lineages(store, workspace_id, ids: list[str], cap: int) -> dict[str, list[str]]:
    """Chunks each shared node was read from (EXTRACTED_FROM node → chunk), at most `cap` per node.

    One query for the whole batch. Only when that query fills its limit — a hub entity is in the
    batch and may have crowded the others out — are nodes re-read one by one, each with its own cap.
    One after another, never gathered: the NebulaGraph client shares one connection per process, and
    35 concurrent reads on it broke it ("Socket read failed: Bad file descriptor") for later queries."""
    limit = cap * len(ids)
    edges = await store.find_edges(workspace_id, EdgeQuery(source_ids=ids, types=[LINEAGE_EDGE], limit=limit))
    out: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        out.setdefault(str(e.source_id), []).append(str(e.target_id))
    if len(edges) >= limit:
        for i in ids:
            out[i] = await _lineage(store, workspace_id, i, cap)
    return {i: list(dict.fromkeys(cs))[:cap] for i, cs in out.items()}


async def _lineage(store, workspace_id, node_id: str, cap: int) -> list[str]:
    """Chunks a shared node was read from (EXTRACTED_FROM node → chunk), at most `cap`."""
    edges = await store.find_edges(workspace_id, EdgeQuery(source_ids=[node_id], types=[LINEAGE_EDGE], limit=cap))
    return list(dict.fromkeys(str(e.target_id) for e in edges))


async def provenance(workspace_id, node_ids: Sequence, *, query: str = "", store=None,
                     per_node: int = PER_NODE, lineage_cap: int = LINEAGE_CAP) -> dict[str, dict[str, Any]]:
    """For each node: its main asset, its top sources ranked for `query`, and how many there are.

    Returns `{node_id: {"asset_id", "asset_label", "asset_type", "origin", "sources", "source_count",
    "source_count_capped"}}`. `asset_id` is None exactly when `origin` explains why."""
    if store is None:
        from opencortyx.core.substrate import NodeRepo
        store = NodeRepo().graph
    ids = list(dict.fromkeys(str(i) for i in node_ids if i))
    if not ids:
        return {}
    walker = _Walker(store, workspace_id)
    await walker.load(ids)
    q = _terms(query)
    ledger = await _ledger(workspace_id, ids)
    await walker.load([p["asset_id"] for ps in ledger.values() for p in ps])
    out: dict[str, dict[str, Any]] = {}
    for nid, pointers in ledger.items():
        best_p: dict[str, tuple[float, dict]] = {}
        for p in pointers:
            if p["asset_id"] not in walker.nodes:
                continue
            words = _terms(str(p.get("quote") or ""))
            score = (len(q & words) / len(q)) if q else 0.0
            prev = best_p.get(p["asset_id"])
            if prev is None or score > prev[0]:
                best_p[p["asset_id"]] = (score, p)
        if not best_p:
            continue
        ranked_p = sorted(best_p.items(), key=lambda kv: (-kv[1][0], walker.nodes[kv[0]].label or "", kv[0]))
        sources = [_ledger_entry(walker.nodes[a], p, sc) for a, (sc, p) in ranked_p[:per_node]]
        out[nid] = {"asset_id": sources[0]["asset_id"], "asset_label": sources[0]["label"],
                    "asset_type": sources[0]["type"], "origin": "asset", "sources": sources,
                    "source_count": len(best_p), "source_count_capped": False}
    ids_left = [i for i in ids if i not in out]
    if not ids_left:
        return out
    ids = ids_left
    lineage = await _lineages(store, workspace_id, ids, lineage_cap)
    lineages = [lineage[i] for i in ids]

    # every node that may lead to an asset: the node itself, and each chunk it was read from
    await walker.assets(ids + [c for cs in lineages for c in cs])
    # nodes the parent chain and lineage both left without an asset: climb containment edges
    # (not nodes whose origin is already known — a template or memory node is never inside a file)
    orphans = [i for i in ids if not walker.asset_of.get(i)
               and not any(walker.asset_of.get(c) for c in lineage[i])
               and i in walker.nodes and origin_of(walker.nodes[i]) == "unknown"]
    climbed = await walker.climb(orphans) if orphans else {}

    for nid in ids:
        node = walker.nodes.get(nid)
        # candidates: (asset id, the chunk that leads there — None when the node's own chain does)
        candidates: list[tuple[str, Optional[str]]] = []
        own = walker.asset_of.get(nid)
        if own:
            candidates.append((own, nid if own != nid else None))
        for chunk in lineage[nid]:
            a = walker.asset_of.get(chunk)
            if a:
                candidates.append((a, chunk))
        for a in climbed.get(nid, []):
            candidates.append((a, nid))
        best: dict[str, tuple[float, Optional[str]]] = {}
        for asset_id, via in candidates:
            text = _text_of(walker.nodes.get(via)) if via else ""
            words = _terms(text)
            score = (len(q & words) / len(q)) if q else 0.0
            prev = best.get(asset_id)
            # on a tie, a chunk the node was read from beats the node itself: it has the sentence
            better_via = via not in (None, nid) and (prev is not None and prev[1] in (None, nid))
            if prev is None or score > prev[0] or (score == prev[0] and better_via):
                best[asset_id] = (score, via)
        ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], walker.nodes[kv[0]].label or "", kv[0]))
        sources = [_asset_entry(walker.nodes[a], walker.nodes.get(v) if v else None, s)
                   for a, (s, v) in ranked[:per_node]]
        main = sources[0] if sources else None
        out[nid] = {
            "asset_id": main["asset_id"] if main else None,
            "asset_label": main["label"] if main else None,
            "asset_type": main["type"] if main else None,
            "origin": "asset" if main else (origin_of(node) if node is not None else "unknown"),
            "sources": sources,
            "source_count": len(best),
            "source_count_capped": len(lineage[nid]) >= lineage_cap,
        }
    return out


async def sources_page(workspace_id, node_id, *, query: str = "", offset: int = 0, limit: int = 25,
                       store=None, max_scan: int = 10_000) -> dict[str, Any]:
    """All assets of one node, ranked for `query`, a page at a time — the "+ 1,240 more" behind a citation.

    Reads at most `max_scan` lineage edges, so a hub entity costs a bounded amount per click."""
    if store is None:
        from opencortyx.core.substrate import NodeRepo
        store = NodeRepo().graph
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 100))
    full = await provenance(workspace_id, [node_id], query=query, store=store,
                            per_node=offset + limit, lineage_cap=max_scan)
    p = full.get(str(node_id)) or {"sources": [], "source_count": 0, "source_count_capped": False}
    return {"items": p["sources"][offset:offset + limit], "total": p["source_count"],
            "capped": p["source_count_capped"], "offset": offset, "limit": limit}


__all__ = ["LINEAGE_CAP", "origin_of", "provenance", "sources_page"]
