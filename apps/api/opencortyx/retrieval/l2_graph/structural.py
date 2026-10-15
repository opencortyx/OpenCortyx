"""L2 — Structural expansion: multi-hop ego-net expansion from the best candidates, by topology alone.

No node type, edge type or text pattern is consulted. A neighbour's score is its parent's score,
damped by how many edges of that relation the parent has (hubs dampen smoothly, specific links
keep their weight) and decayed per hop.
"""
from __future__ import annotations

import math
import uuid
from typing import Optional

from opencortyx.core.substrate import NodeRepo
from ..contracts import Candidate, CandidateGraph, Evidence, QueryPlan

#: Maximum candidate seeds expanded from per hop.
MAX_SEEDS = 20
#: Maximum neighbours pulled per seed.
FANOUT_LIMIT = 350
#: Decay factor applied to successive hops away from initial seeds.
HOP_DECAY = 0.60
#: Relations with at most this many edges from a node keep full weight.
REFERENCE_FANOUT = 20.0


async def topological_expansion(workspace_id: uuid.UUID, graph: CandidateGraph,
                                plan: Optional[QueryPlan] = None,
                                seed_limit: int = MAX_SEEDS,
                                fanout_limit: int = FANOUT_LIMIT,
                                hops: int = 2,
                                collection_ids: Optional[list] = None) -> int:
    """Expand along every incident edge from resolved entities and the top fused candidates."""
    if not graph.candidates:
        return 0
    seed_scores: dict[str, float] = {}
    for e in (getattr(plan, "entities", None) or []):
        nid = str(getattr(e, "node_id", None) or "")
        if nid:
            seed_scores[nid] = 1.0
    for c in graph.ranked()[:seed_limit]:
        nid = str(c.node_id)
        score = float(c.fusion_score or 0.0)
        seed_scores[nid] = max(seed_scores.get(nid, 0.0), score if score > 0.0 else 0.5)
    if not seed_scores:
        return 0

    store = NodeRepo().graph
    allowed = {str(c) for c in collection_ids} if collection_ids else None
    seen = set(seed_scores)
    frontier = dict(seed_scores)
    added = 0
    rank = 0
    for hop in range(max(1, hops)):
        if not frontier:
            break
        next_frontier: dict[str, float] = {}
        for seed_id, parent in sorted(frontier.items(), key=lambda kv: -kv[1])[:seed_limit]:
            hops_out = await store.neighbors(workspace_id, [seed_id], direction="both", limit=fanout_limit)
            per_relation: dict[str, int] = {}
            for n in hops_out:
                per_relation[n.edge.type] = per_relation.get(n.edge.type, 0) + 1
            for n in hops_out:
                node = n.node
                if allowed is not None and str(node.collection_id) not in allowed:
                    continue
                rank += 1
                damping = min(1.0, math.sqrt(REFERENCE_FANOUT / max(1, per_relation[n.edge.type])))
                score = parent * damping * (HOP_DECAY if hop > 0 else 1.0)
                text = (node.properties or {}).get("text") or ""
                cand = Candidate(node_id=node.id, node_type=node.type or "", label=node.label or "",
                                 text=text, properties=node.properties or {})
                cand.add(Evidence(surface="topological_expansion", rank=rank, raw_score=float(score),
                                  scale="derived", detail=f"hop{hop + 1}:{n.edge.type} via {seed_id[:8]}",
                                  matched_text=text[:160]))
                graph.upsert(cand)
                target = str(node.id)
                if target not in seen:
                    seen.add(target)
                    next_frontier[target] = max(next_frontier.get(target, 0.0), score)
                    added += 1
        frontier = next_frontier
    return added
