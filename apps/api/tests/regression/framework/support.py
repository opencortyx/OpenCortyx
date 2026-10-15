"""In-process harness: an in-memory graph store, the ingest entry points, and independent oracles."""
from __future__ import annotations

import uuid
from collections import defaultdict, deque
from types import SimpleNamespace

ORG = uuid.UUID(int=0xC0FFEE)


class MemRepo:
    """The NodeRepo calls the walkers make, held in memory, with the resulting graph inspectable."""

    def __init__(self):
        self.nodes, self.edges = {}, set()

    async def upsert_node(self, *, workspace_id, type, label="", properties=None, source_ref=None,
                          collection_id=None, node_id=None, **_):
        old = self.nodes.get(node_id) or {}
        props = {**(old.get("properties") or {}), **(properties or {})}
        self.nodes[node_id] = {"type": old.get("type") or type, "label": label, "properties": props,
                               "source_ref": source_ref if source_ref is not None else old.get("source_ref")}
        return SimpleNamespace(id=node_id, properties=props, type=type, label=label)

    async def add_edge(self, *, workspace_id, source_id, target_id, type, properties=None, **_):
        self.edges.add((source_id, target_id, type))
        return True

    async def upsert_many(self, workspace_id, specs, **_):
        """NodeRepo.upsert_many: the same writes, one record at a time."""
        return [await self.upsert_node(workspace_id=workspace_id, **s) for s in specs]

    async def add_edges(self, *, workspace_id, edges, **_):
        for e in edges:
            self.edges.add((e["source_id"], e["target_id"], e["type"]))
        return len(edges)

    async def get(self, workspace_id, node_id):
        n = self.nodes.get(node_id)
        return SimpleNamespace(id=node_id, properties=n["properties"], type=n["type"]) if n else None

    async def patch_properties(self, workspace_id, node_ids, values=None, **_):
        for i in node_ids:
            if i in self.nodes:
                self.nodes[i]["properties"].update(values or {})
        return len(node_ids)

    async def find(self, *a, **k):
        return []

    # ── inspection ──────────────────────────────────────────────────────────────────────────────
    def depths(self, root) -> dict:
        kids = defaultdict(list)
        for s, d, t in self.edges:
            if t.startswith("HAS_"):
                kids[s].append(d)
        seen, q = {root: 0}, deque([root])
        while q:
            n = q.popleft()
            for c in kids[n]:
                if c not in seen:
                    seen[c] = seen[n] + 1
                    q.append(c)
        return seen

    def text_of_all(self) -> str:
        parts = []
        for n in self.nodes.values():
            parts.append(str(n["label"]))
            for k, v in n["properties"].items():
                parts.append(str(k))
                parts.append(str(v).lower() if isinstance(v, bool) else str(v))
        return "\n".join(parts)


def graph_problems(repo: MemRepo, source_id) -> list[str]:
    """Provenance and connectivity: every node traces to the file and hangs under a parent."""
    out = []
    orphans = [i for i, n in repo.nodes.items() if i != source_id and n["source_ref"] != source_id]
    if orphans:
        out.append(f"{len(orphans)} node(s) without source_ref")
    children = {d for _, d, t in repo.edges if t.startswith("HAS_")}
    loose = [i for i in repo.nodes if i != source_id and i not in children]
    if loose:
        out.append(f"{len(loose)} node(s) with no parent")
    return out


def walked_data(parsed):
    """What the default walker walks, exactly as SemiStructuredWalker.ingest_file hands it over."""
    return [r.data for r in parsed.records] if parsed.records else parsed.data


def oracle_plain_walk(data, max_depth: int = 6) -> tuple[int, int]:
    """(nodes, edges) the PLAIN walker must emit, from its documented rules — not from its code:

      object → one node, scalar fields as attributes, each object/array field walked one level down
      array of scalars → a container node plus one node per element, chained by NEXT
      array with objects → a container node plus each element walked, chained by NEXT
      scalar under the root → one node
      every node gets one HAS_<key> edge to its parent; nothing is created beyond max_depth
    """
    nodes = edges = 0

    def elem(v, depth) -> bool:
        nonlocal nodes, edges
        if depth > max_depth:
            return False
        nodes += 1
        edges += 1                                       # HAS_ edge to the parent
        if isinstance(v, dict):
            for c in v.values():
                if isinstance(c, (dict, list)):
                    elem(c, depth + 1)
        elif isinstance(v, list):
            if all(not isinstance(x, (dict, list)) for x in v):
                nodes += len(v)
                edges += len(v) + max(0, len(v) - 1)
            else:
                made = [elem(x, depth + 1) for x in v]
                edges += sum(1 for a, b in zip(made, made[1:]) if a and b)
        return True

    if isinstance(data, dict):
        for v in data.values():
            elem(v, 1)
    elif isinstance(data, list):
        made = [elem(x, 1) for x in data]
        edges += sum(1 for a, b in zip(made, made[1:]) if a and b)
    return nodes, edges


async def ingest_default(monkeypatch, content: bytes, filename: str, repo: MemRepo | None = None, **options):
    """The production default path: SemiStructuredWalker.ingest_file into an in-memory graph."""
    from opencortyx.ingest.semistructured import ingest as ingest_module
    from opencortyx.ingest.walkers import SemiStructuredWalker
    repo = repo or MemRepo()
    monkeypatch.setattr(ingest_module, "_repo", repo)
    res = await SemiStructuredWalker().ingest_file(ORG, content, filename, options={"replace": True, **options})
    return repo, res


async def ingest_json_cfg(monkeypatch, data, label: str, repo: MemRepo | None = None, **kw):
    from opencortyx.ingest.semistructured import ingest as ingest_module
    repo = repo or MemRepo()
    monkeypatch.setattr(ingest_module, "_repo", repo)
    res = await ingest_module.ingest_json(ORG, data, source_label=label, replace=True, **kw)
    return repo, res
