"""Async execution engine for the OpenCortyx Graph Query primitive.

Executes composable graph operations (START, TRAVERSE, FILTER, MATCH, INTERSECT, UNION, PATH, LIMIT, RETURN)
over the graph store, strictly scoped to tenant `workspace_id`.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from opencortyx.core.substrate import NodeRepo
from opencortyx.stores.graph import Edge, Node

from opencortyx.stores.graph_query.ast import (
    FilterOp,
    FilterOpType,
    GraphPath,
    GraphQuerySpec,
    IntersectOp,
    LimitOp,
    MatchOp,
    PathOp,
    StartOp,
    TraverseOp,
    TraversedStep,
    UnionOp,
)
from opencortyx.stores.graph_query.parser import parse_graph_query

logger = logging.getLogger(__name__)


def _compare(actual: Any, op: FilterOpType, target: Any) -> bool:
    """Evaluate comparison predicate safely across types."""
    if actual is None:
        return op == FilterOpType.NEQ and target is not None

    if op == FilterOpType.EQ:
        return str(actual).strip().lower() == str(target).strip().lower()
    elif op == FilterOpType.NEQ:
        return str(actual).strip().lower() != str(target).strip().lower()
    elif op == FilterOpType.CONTAINS:
        return str(target).lower() in str(actual).lower()
    elif op == FilterOpType.IN:
        if isinstance(target, (list, tuple, set)):
            return actual in target or str(actual).lower() in [str(x).lower() for x in target]
        return str(actual).lower() in str(target).lower()

    # Numeric comparisons
    try:
        n_actual = float(actual)
        n_target = float(target)
        if op == FilterOpType.GT:
            return n_actual > n_target
        elif op == FilterOpType.GTE:
            return n_actual >= n_target
        elif op == FilterOpType.LT:
            return n_actual < n_target
        elif op == FilterOpType.LTE:
            return n_actual <= n_target
    except (ValueError, TypeError):
        pass

    # String order comparison fallback
    s_actual, s_target = str(actual), str(target)
    if op == FilterOpType.GT:
        return s_actual > s_target
    elif op == FilterOpType.GTE:
        return s_actual >= s_target
    elif op == FilterOpType.LT:
        return s_actual < s_target
    elif op == FilterOpType.LTE:
        return s_actual <= s_target

    return False


def _get_nested_field(d: dict, path: str) -> Any:
    """Extract nested property like 'properties.department' or 'label'."""
    parts = path.split(".")
    curr = d
    for p in parts:
        if isinstance(curr, dict):
            curr = curr.get(p)
        else:
            return None
    return curr


def _node_to_dict(node: Node) -> dict:
    return {
        "id": str(node.id),
        "label": (node.label or "").strip() or node.type,
        "type": node.type,
        "entity_type": node.entity_type,
        "level": node.level or 0,
        "degree": node.degree or 0,
        "properties": dict(node.properties or {}),
    }


def _type_matches(node: Node, wanted: Optional[str]) -> bool:
    if not wanted:
        return True
    want = wanted.strip().lower()
    return want in {(node.type or "").lower(), (node.base_type or "").lower(), (node.entity_type or "").lower()}


def _uuids(values) -> list[uuid.UUID]:
    out = []
    for v in values or []:
        try:
            out.append(uuid.UUID(str(v)))
        except ValueError:
            continue
    return out


def _extract_evidence_text(node: dict) -> str:
    """Extract human/LLM-readable text from a node."""
    props = node.get("properties") or {}
    text_cands = [
        props.get("text"),
        props.get("content"),
        props.get("summary"),
        props.get("definition"),
        props.get("claim"),
        props.get("fact"),
        props.get("description"),
    ]
    for c in text_cands:
        if c and isinstance(c, str) and c.strip():
            return c.strip()

    # Fallback to key-value pairs
    kv_pairs = []
    for k, v in props.items():
        if not str(k).startswith("__") and isinstance(v, (str, int, float, bool)) and str(v).strip():
            kv_pairs.append(f"{k}={v}")
    if kv_pairs:
        return "; ".join(kv_pairs)
    return node.get("label", "")


class GraphQueryContext:
    def __init__(self, workspace_id: uuid.UUID, collection_id: Optional[str] = None, repo: NodeRepo | None = None):
        self.repo = repo or NodeRepo()
        self.workspace_id = workspace_id
        self.collection_id = collection_id
        self.active_nodes: Dict[str, dict] = {}  # id -> node dict
        self.paths: List[GraphPath] = []
        self.node_labels: Dict[str, str] = {}  # id -> label
        self.traversed_edges: Dict[str, dict] = {}  # edge_id -> edge dict
        self.track_paths: bool = True
        self.seed_limit: Optional[int] = None   # the caller's limit, applied to START
        self.seed_truncated: bool = False       # set when START had more rows than it returned
        self._edge_types: Optional[Dict[str, str]] = None

    def register_node(self, n: dict):
        nid = n["id"]
        self.active_nodes[nid] = n
        self.node_labels[nid] = n.get("label") or n.get("type", "")

    def register_edge(self, edge: dict):
        eid = edge.get("id")
        if eid:
            self.traversed_edges[eid] = edge

    async def edge_types(self, names) -> Optional[List[str]]:
        """The stored edge types named by `names`, matched case-insensitively. None means any type."""
        wanted = [str(n).strip().lower() for n in names or [] if str(n).strip() and str(n).strip() != "*"]
        if not wanted:
            return None
        if self._edge_types is None:
            counts = await self.repo.count_edges_by_type(self.workspace_id)
            self._edge_types = {t.lower(): t for t in counts}
        return [self._edge_types[w] for w in wanted if w in self._edge_types]

    async def hops(self, node_ids, rel, direction: str, limit: int) -> list[tuple[Edge, Node, str]]:
        """One hop from `node_ids` as (edge, far node, direction relative to the near node)."""
        types = await self.edge_types(rel if isinstance(rel, list) else [rel] if rel else [])
        if types == []:
            return []
        near = {str(i) for i in node_ids}
        out = []
        for edge, node in await self.repo.neighbors(self.workspace_id, _uuids(node_ids), edge_types=types,
                                                    direction=direction, limit=limit):
            outgoing = str(edge.source_id) in near and str(edge.target_id) == str(node.id)
            out.append((edge, node, "out" if outgoing else "in"))
        return out


async def execute_start(ctx: GraphQueryContext, op: StartOp) -> None:
    """Execute START step to find initial nodes and seed paths."""
    repo, cap = ctx.repo, max(1, int(ctx.seed_limit or 100))
    coll = op.collection_id or ctx.collection_id
    collections = _uuids([coll]) if coll else None

    if op.node_id or op.node_ids:
        nodes = await repo.get_many(ctx.workspace_id, _uuids([op.node_id] if op.node_id else op.node_ids))
    elif op.label:
        nodes = await repo.find(ctx.workspace_id, label_contains=op.label, collection_ids=collections)
    elif op.query:
        hits = await repo.keyword_search(workspace_id=ctx.workspace_id, query=op.query, collection_ids=collections,
                                         k=(cap + 1) * 4)
        nodes = [n for n, _score in hits]
    elif op.type:
        nodes = await repo.find(ctx.workspace_id, collection_ids=collections)
    else:
        nodes = await repo.find(ctx.workspace_id, collection_ids=collections, order_by="-degree", limit=cap + 1)

    nodes = [n for n in nodes if _type_matches(n, op.type)
             and (not collections or str(n.collection_id) == str(collections[0]))]
    if not op.query:
        nodes.sort(key=lambda n: (n.degree or 0, n.created_at.timestamp() if n.created_at else 0), reverse=True)
    if len(nodes) > cap:
        ctx.seed_truncated = True
        nodes = nodes[:cap]

    ctx.active_nodes.clear()
    ctx.paths.clear()
    for node in nodes:
        n = _node_to_dict(node)
        ctx.register_node(n)
        ctx.paths.append(GraphPath(steps=[], seed_id=n["id"]))


async def execute_traverse(ctx: GraphQueryContext, op: TraverseOp) -> None:
    """Execute TRAVERSE step along edges in the knowledge graph."""
    if not ctx.active_nodes or not ctx.paths:
        return
    rels = op.rel if isinstance(op.rel, list) else ([op.rel] if op.rel else [])

    for _ in range(max(1, min(op.hops, 10))):
        frontier_ids = list(ctx.active_nodes.keys())
        if not frontier_ids:
            break

        edges_by_from: Dict[str, list] = {}
        new_nodes: Dict[str, dict] = {}
        for edge, node, direction in await ctx.hops(frontier_ids, rels, op.direction.value, 500):
            if not _type_matches(node, op.target_type):
                continue
            if op.min_confidence is not None and float(edge.confidence or 0) < op.min_confidence:
                continue
            if op.min_weight is not None and float(edge.weight or 0) < op.min_weight:
                continue
            edge_dict = {
                "id": str(edge.id),
                "from_id": str(edge.source_id if direction == "out" else edge.target_id),
                "to_id": str(node.id),
                "source_id": str(edge.source_id),
                "target_id": str(edge.target_id),
                "type": edge.type,
                "direction": direction,
                "weight": float(edge.weight or 1.0),
            }
            ctx.register_edge(edge_dict)
            node_dict = _node_to_dict(node)
            new_nodes[node_dict["id"]] = node_dict
            ctx.register_node(node_dict)
            edges_by_from.setdefault(edge_dict["from_id"], []).append(edge_dict)

        # Extend each existing path ending at from_id
        next_paths: List[GraphPath] = []
        for path in ctx.paths:
            term = path.terminal_id
            visited = {path.seed_id} | {st.to_id for st in path.steps}
            for e in edges_by_from.get(term, []):
                if e["to_id"] in visited:       # never revisit a node along one path
                    continue
                step = TraversedStep(from_id=term, edge_id=e["id"], edge_type=e["type"], to_id=e["to_id"],
                                     direction=e["direction"], weight=e["weight"])
                next_paths.append(GraphPath(steps=path.steps + [step], seed_id=path.seed_id))

        if not next_paths:
            break

        ctx.paths = next_paths
        terminal_ids = {p.terminal_id for p in next_paths if p.terminal_id}
        ctx.active_nodes = {nid: new_nodes[nid] for nid in terminal_ids if nid in new_nodes}


def execute_filter(ctx: GraphQueryContext, op: FilterOp) -> None:
    """Filter active nodes by predicate."""
    surviving_nodes: Dict[str, dict] = {}
    for nid, n in ctx.active_nodes.items():
        actual_val = _get_nested_field(n, op.field)
        if _compare(actual_val, op.operator, op.value):
            surviving_nodes[nid] = n

    ctx.active_nodes = surviving_nodes
    ctx.paths = [p for p in ctx.paths if p.terminal_id in surviving_nodes]


async def execute_match(ctx: GraphQueryContext, op: MatchOp) -> None:
    """Filter nodes that have a neighbor matching the pattern."""
    if not ctx.active_nodes:
        return
    frontier = set(ctx.active_nodes)
    label = (op.target_label or "").lower()
    matched_ids = set()
    for edge, node, direction in await ctx.hops(list(frontier), [op.rel] if op.rel else [],
                                                op.direction.value, 10_000):
        if _type_matches(node, op.target_type) and label in (node.label or "").lower():
            matched_ids.add(str(edge.source_id if direction == "out" else edge.target_id))
    ctx.active_nodes = {nid: n for nid, n in ctx.active_nodes.items() if nid in matched_ids}
    ctx.paths = [p for p in ctx.paths if p.terminal_id in matched_ids]


async def execute_intersect(ctx: GraphQueryContext, op: IntersectOp) -> None:
    """Intersect current active nodes with subquery results."""
    if not op.subquery:
        return

    sub_spec = parse_graph_query(query=op.subquery if isinstance(op.subquery, str) else None,
                                 steps=op.subquery if isinstance(op.subquery, list) else None)
    sub_ctx = GraphQueryContext(ctx.workspace_id, ctx.collection_id, repo=ctx.repo)
    await run_pipeline(sub_ctx, sub_spec)
    sub_node_ids = set(sub_ctx.active_nodes.keys())
    ctx.seed_truncated = ctx.seed_truncated or sub_ctx.seed_truncated

    ctx.active_nodes = {nid: n for nid, n in ctx.active_nodes.items() if nid in sub_node_ids}
    ctx.paths = [p for p in ctx.paths if p.terminal_id in sub_node_ids]


async def execute_union(ctx: GraphQueryContext, op: UnionOp) -> None:
    """Union current active nodes with subquery results."""
    if not op.subquery:
        return

    sub_spec = parse_graph_query(query=op.subquery if isinstance(op.subquery, str) else None,
                                 steps=op.subquery if isinstance(op.subquery, list) else None)
    sub_ctx = GraphQueryContext(ctx.workspace_id, ctx.collection_id, repo=ctx.repo)
    await run_pipeline(sub_ctx, sub_spec)
    ctx.seed_truncated = ctx.seed_truncated or sub_ctx.seed_truncated

    for n in sub_ctx.active_nodes.values():   # raw nodes, full ids - not the compact projection
        ctx.register_node(n)
    for eid, e in sub_ctx.traversed_edges.items():
        ctx.traversed_edges.setdefault(eid, e)
    for p in sub_ctx.paths:
        ctx.paths.append(p)


def execute_limit(ctx: GraphQueryContext, op: LimitOp) -> None:
    """Limit the number of active nodes and paths."""
    count = max(1, op.count)
    ctx.paths = ctx.paths[:count]
    retained_ids = {p.terminal_id for p in ctx.paths if p.terminal_id}
    ctx.active_nodes = {nid: n for nid, n in ctx.active_nodes.items() if nid in retained_ids}


async def run_pipeline(ctx: GraphQueryContext, spec: GraphQuerySpec) -> dict:
    """Run all steps of a GraphQuerySpec sequentially."""
    start_time = time.monotonic()
    ctx.seed_limit = spec.limit

    for step in spec.steps:
        if isinstance(step, StartOp):
            await execute_start(ctx, step)
        elif isinstance(step, TraverseOp):
            await execute_traverse(ctx, step)
        elif isinstance(step, FilterOp):
            execute_filter(ctx, step)
        elif isinstance(step, MatchOp):
            await execute_match(ctx, step)
        elif isinstance(step, IntersectOp):
            await execute_intersect(ctx, step)
        elif isinstance(step, UnionOp):
            await execute_union(ctx, step)
        elif isinstance(step, PathOp):
            ctx.track_paths = step.track
        elif isinstance(step, LimitOp):
            execute_limit(ctx, step)

    # Apply global limit
    if len(ctx.paths) > spec.limit:
        execute_limit(ctx, LimitOp(count=spec.limit))

    # Format result projections
    raw_nodes = list(ctx.active_nodes.values())
    paths_out = [p.to_dict(ctx.node_labels) for p in ctx.paths]
    raw_edges = list(ctx.traversed_edges.values())

    # Compile grounded evidence
    evidence_blocks = []
    seen_terminals = set()
    for p in ctx.paths:
        term_id = p.terminal_id
        term_node = ctx.active_nodes.get(term_id)
        if not term_node:
            continue
        if term_id in seen_terminals:
            continue
        seen_terminals.add(term_id)
        text_content = _extract_evidence_text(term_node)
        path_str = p.to_dict(ctx.node_labels)["readable_path"]
        evidence_blocks.append({
            "node_id": term_id,
            "label": term_node.get("label", ""),
            "type": term_node.get("type", ""),
            "path": path_str,
            "content": text_content,
        })

    evidence_summary = "\n".join([
        f"• [{e['type']}] {e['label']} (via {e['path']}): {e['content']}"
        for e in evidence_blocks
    ]) if evidence_blocks else "No evidence found for the specified graph traversal."

    execution_time_ms = round((time.monotonic() - start_time) * 1000, 2)

    requested = {r.strip().lower() for r in (spec.returns or [])}
    want = lambda *names: any(k in requested for k in names)

    result = {
        "status": "ok",
        "count": len(raw_nodes),
        "execution_time_ms": execution_time_ms,
    }
    if ctx.seed_truncated:
        result["truncated"] = True
        result["truncated_note"] = (
            "START matched more nodes than `limit`; `count` is a lower bound. "
            "Raise `limit` or narrow the seed before trusting it as a total."
        )

    if raw_nodes and not want("no_edges"):
        ids = {n["id"] for n in raw_nodes}
        counts: Dict[tuple, int] = {}
        for edge in await ctx.repo.edges(ctx.workspace_id, node_ids=_uuids(ids)):
            if str(edge.source_id) in ids:
                counts[(edge.type, "out")] = counts.get((edge.type, "out"), 0) + 1
            if str(edge.target_id) in ids:
                counts[(edge.type, "in")] = counts.get((edge.type, "in"), 0) + 1
        if counts:
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:40]
            result["available_edges"] = [{"rel": t, "direction": d, "n": c} for (t, d), c in top]

    if want("path", "paths"):
        result["paths"] = [
            {"nodes": p.get("nodes", []), "edges": p.get("edges", []),
             "readable_path": p.get("readable_path"), "length": p.get("length")}
            for p in paths_out
        ]

    if want("evidence", "fact", "facts"):
        result["evidence"] = evidence_summary
    if want("evidence_blocks"):                      # the structured form, never both by default
        result["evidence_blocks"] = evidence_blocks

    # Compact (id, label, type) - 12 tok/node, the handles an agent re-queries with.
    if want("node", "nodes", "id", "label") or not requested:
        result["nodes"] = [
            {"id": n.get("id", "")[:8], "label": n.get("label", ""), "type": n.get("type", "")}
            for n in raw_nodes
        ]

    if want("edge", "edges", "relation", "relations"):
        result["edges"] = [
            {"source": e.get("source_id", "")[:8], "rel": e.get("type", ""),
             "target": e.get("target_id", "")[:8]}
            for e in raw_edges
        ]

    return result


async def execute_graph_query(workspace_id: uuid.UUID, spec: GraphQuerySpec, collection_id: Optional[str] = None) -> dict:
    """High-level entry point to execute a GraphQuerySpec."""
    return await run_pipeline(GraphQueryContext(workspace_id, collection_id), spec)
