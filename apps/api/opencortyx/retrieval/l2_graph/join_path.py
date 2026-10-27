"""Join paths: how the tables a question needs connect, over the data layer's JOINS_TO.

The shortest weighted path between two tables, declared joins (a foreign key, a mapping's reference
rule) preferred over detected ones (matching `*_id` names). Paths cross storage freely: a managed CSV
joins a customer's Delta table or a live database table the same way, because all of them are views
on one DuckDB connection.
"""
from __future__ import annotations

import heapq
from typing import Optional

from opencortyx.retrieval.l2_data.schema import Join

#: What a hop costs. A detected join is allowed, but a declared one is taken when both exist.
COST = {"declared": 1.0, "detected": 2.5}


def path(schema, start: str, goal: str, max_hops: int = 4) -> Optional[list]:
    """[Join, ...] from view `start` to view `goal` (each oriented left -> right), or None."""
    if start == goal:
        return []
    adj: dict = {}
    for j in schema.joins:
        adj.setdefault(j.left, []).append((j, j.left, j.left_col, j.right, j.right_col))
        adj.setdefault(j.right, []).append((j, j.right, j.right_col, j.left, j.left_col))
    heap = [(0.0, 0, start, [])]
    seen: dict = {}
    n = 0
    while heap:
        cost, _, node, steps = heapq.heappop(heap)
        if node == goal:
            return steps
        if seen.get(node, 1e9) <= cost or len(steps) >= max_hops:
            continue
        seen[node] = cost
        for j, a, acol, b, bcol in adj.get(node, []):
            n += 1
            step = Join(a, acol, b, bcol, j.source)
            heapq.heappush(heap, (cost + COST.get(j.source, 3.0), n, b, steps + [step]))
    return None


def connect(schema, views: list[str]) -> Optional[list]:
    """Joins that connect every view in `views` to the first one (a star from the anchor table)."""
    if not views:
        return []
    out, have = [], {views[0]}
    for v in views[1:]:
        if v in have:
            continue
        best = None
        for h in have:
            p = path(schema, h, v)
            if p is not None and (best is None or len(p) < len(best)):
                best = p
        if best is None:
            return None
        for step in best:
            if step.right not in have:
                out.append(step)
                have.add(step.right)
    return out
