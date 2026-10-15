"""L2 — Query-directed beam search over the graph, producing PATHS as evidence.

    seeds ─► expand over the highest-scoring relations ─► score paths ─► keep top B ─► repeat
                                                                                        │
                                                                              paths + terminals

WHY NOT BREADTH-FIRST

The previous engine took one hop from its top-24 candidates across a fixed edge set and kept the
endpoints. Two failures followed from that shape:

  - Blind expansion is a fan-out bomb. Widening it was measured: raising the anchor count to 240
    inflated the package from ~200 to 274 nodes and dropped recall from 89.04% to 84.49%, because
    breadth of weakly-related material crowded out the evidence.
  - Endpoints alone cannot answer a relationship question. "Which companies connect to John
    through Germany" is answered by the ROUTE, and the route was discarded.

Beam search fixes both: expansion follows relations the QUESTION implicates (scored, not listed),
and every surviving candidate carries the path that reached it.

SCORING A PATH

A path's score is the product of its edge relevances, decayed by length. Product rather than sum
because one irrelevant hop should invalidate a route — a chain is as strong as its weakest link,
and a sum lets two strong hops carry a nonsensical third. Decay because a shorter route to the
same evidence is better evidence.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, Iterable, Optional


from opencortyx.core.substrate import NodeRepo
from opencortyx.retrieval.contracts import (
    Candidate, Evidence, Path, PathStep,
)
from opencortyx.retrieval.l2_graph.edge_scoring import (
    EdgeRelevance, MIN_EDGE_RELEVANCE,
)
from opencortyx.retrieval.l2_graph.expansion import (
    NODE_FLOOR, ExpansionContext,
)
from opencortyx.core.substrate import document as _node_document

#: Per-hop penalty. A two-hop route to the same node is weaker evidence than a one-hop route.
LENGTH_DECAY = 0.82

#: Ceiling on neighbours pulled per node per hop, so one hub cannot dominate a frontier. Applied
#: per (node, relation) so a highly-connected node still contributes across relations.
FANOUT_PER_RELATION = 24


@dataclass
class BeamConfig:
    width: int = 32                 # paths carried between hops
    max_hops: int = 2               # from the L0 plan
    fanout: int = FANOUT_PER_RELATION
    min_edge: float = MIN_EDGE_RELEVANCE
    max_relations: int = 8          # relations considered per hop
    max_terminals: int = 400        # safety bound on total nodes discovered


def score_path(path: Path) -> float:
    """Product of edge relevances, modulated by the nodes landed on, with length decay.

    The node term is why this is not edge-only. Scoring a route by its relations alone made every
    hop over the same relation interchangeable, so the beam's pruning — the step that trades
    breadth for relevance — was choosing between routes it had no way to distinguish.

    A step whose `node_score` is `None` was never assessed, and contributes no node term at all.
    That is different from contributing a neutral one: a route through unassessed nodes is
    scored on exactly the evidence that exists for it, rather than on an assumed value.
    """
    if not path.steps:
        return 0.0
    s = 1.0
    for step in path.steps:
        s *= max(step.edge_score, 1e-6)
        if step.node_score is not None:
            # Bounded below: a node the question does not resemble is dampened, never deleted.
            # The answer to "what is she allergic to" may repeat none of the question's words.
            s *= NODE_FLOOR + (1.0 - NODE_FLOOR) * max(0.0, min(1.0, step.node_score))
    return s * (LENGTH_DECAY ** (len(path.steps) - 1))


async def _neighbours(workspace_id: uuid.UUID, node_ids: list[str], relations: list[str],
                      limit: int) -> list[tuple]:
    """One hop, both directions, over the relations this question implicates, skipping superseded nodes.

    Returns `(from_id, relation, to_id, direction, node_type, label, properties)`.
    """
    if not node_ids or not relations:
        return []
    rows = []
    for edge, node in await NodeRepo().neighbors(workspace_id, node_ids, edge_types=relations, direction="both",
                                                 limit=limit):
        if str((node.properties or {}).get("superseded", "false")).lower() == "true":
            continue
        outgoing = str(edge.target_id) == str(node.id)
        near = edge.source_id if outgoing else edge.target_id
        rows.append((str(near), edge.type, str(node.id), "out" if outgoing else "in",
                     node.type, node.label, node.properties or {}))
    return rows


class BeamSearch:
    """Query-guided traversal. Emits candidates whose evidence is the path that reached them."""

    surface = "graph"

    def __init__(self, config: Optional[BeamConfig] = None):
        self.cfg = config or BeamConfig()

    async def run(self, workspace_id: uuid.UUID, seeds: Iterable[uuid.UUID],
                  edge_rel: EdgeRelevance,
                  on_trace: Optional[Callable[[str, dict], None]] = None,
                  out_edges: Optional[list] = None,
                  ctx: Optional[ExpansionContext] = None,
                  ) -> list[Candidate]:
        """`out_edges`, when given, receives every DISTINCT step traversed.

        The steps already exist inside each candidate's paths, but only per candidate: nothing
        held the subgraph as a subgraph, and `CandidateGraph.edges` was declared and never
        populated. A relationship question is answered by the CONNECTION, so the set of edges
        actually walked has to survive as a first-class result rather than being reconstructable
        in principle from a hundred separate path objects.
        """
        ctx = ctx or ExpansionContext()
        seed_ids = [str(s) for s in seeds if s]
        if not seed_ids or not edge_rel.scores:
            return []

        relations = [r for r in edge_rel.walkable(self.cfg.min_edge)][: self.cfg.max_relations]
        if not relations:
            if on_trace:
                on_trace("beam", {"stop": "no relation cleared the relevance floor",
                                  "top": edge_rel.top(5)})
            return []

        # A frontier entry is (node_id, path_so_far).
        frontier: list[tuple[str, Path]] = [
            (sid, Path(seed_id=uuid.UUID(sid))) for sid in seed_ids
        ]
        visited: set[str] = set(seed_ids)
        found: dict[str, Candidate] = {}
        # Deduped on (from, relation, to, direction): the same edge is reached once per route
        # that uses it, and a subgraph must not list it once per route.
        seen_steps: set[tuple] = set()

        for hop in range(self.cfg.max_hops):
            if not frontier:
                break
            ids = [nid for nid, _ in frontier]
            limit = max(self.cfg.fanout * len(ids), self.cfg.fanout)
            rows = await _neighbours(workspace_id, ids, relations, limit)
            if not rows:
                if on_trace:
                    on_trace("beam", {"hop": hop, "stop": "no neighbours over these relations"})
                break

            path_by_id: dict[str, Path] = {nid: p for nid, p in frontier}
            per_source_relation: dict[tuple[str, str], int] = {}
            extended: list[tuple[str, Path]] = []

            rejected = 0
            for from_id, rel, to_id, direction, ntype, label, props in rows:
                if not ctx.accepts(props):
                    rejected += 1
                    continue

                key = (from_id, rel)
                seen = per_source_relation.get(key, 0)
                if seen >= self.cfg.fanout:
                    continue
                per_source_relation[key] = seen + 1

                base = path_by_id.get(from_id)
                if base is None:
                    continue
                step = PathStep(from_id=uuid.UUID(from_id), edge_type=rel,
                                to_id=uuid.UUID(to_id), direction=direction,
                                edge_score=edge_rel.score(rel),
                                node_score=(ctx.affinity(label, props) if ctx.active else None))
                new_path = Path(seed_id=base.seed_id, steps=list(base.steps) + [step])
                new_path.score = score_path(new_path)

                if out_edges is not None:
                    key = (str(step.from_id), step.edge_type, str(step.to_id), step.direction)
                    if key not in seen_steps:
                        seen_steps.add(key)
                        out_edges.append(step)

                cand = found.get(to_id)
                if cand is None:
                    cand = Candidate(
                        node_id=uuid.UUID(to_id), node_type=ntype or "", label=label or "",
                        text=_node_document.from_row(props, label, ntype), properties=props or {})
                    found[to_id] = cand
                cand.paths.append(new_path)

                if to_id not in visited and len(found) < self.cfg.max_terminals:
                    extended.append((to_id, new_path))

            # Prune: keep the best `width` routes into the next hop. This is where breadth is
            # traded for relevance — the step the previous engine had no equivalent of.
            extended.sort(key=lambda t: t[1].score, reverse=True)
            frontier = extended[: self.cfg.width]
            visited.update(nid for nid, _ in frontier)

            if rejected:
                ctx.rejected_temporal += rejected
            if on_trace:
                on_trace("beam", {"hop": hop, "expanded": len(rows), "kept": len(frontier),
                                  "discovered": len(found), "out_of_range": rejected,
                                  "relations": relations[:5]})

        # Rank each candidate by its BEST route, and record that route as evidence.
        out: list[Candidate] = []
        for nid, cand in found.items():
            if not cand.paths:
                continue
            best = max(cand.paths, key=lambda p: p.score)
            rank = 0  # assigned below after sorting
            cand.paths.sort(key=lambda p: p.score, reverse=True)
            cand.add(Evidence(
                surface=self.surface, rank=rank, raw_score=best.score, scale="path",
                detail="->".join(s.edge_type for s in best.steps),
                matched_text=str(best.terminal_id) if best.terminal_id else ""))
            out.append(cand)

        out.sort(key=lambda c: c.evidence[-1].raw_score, reverse=True)
        for i, cand in enumerate(out):
            cand.evidence[-1].rank = i
        return out
