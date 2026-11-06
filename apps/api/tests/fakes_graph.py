"""An in-memory stand-in for NodeRepo with the calls resolution, deletion and access use.

Nodes are plain objects with the fields the engine reads (id, type, label, properties, created_at,
confidence, level, collection_id, sensitivity, source_ref); edges are dicts keyed by
(source, type, target). It keeps write semantics close to NodeRepo's: `upsert_many` merges
properties unless `replace_properties`, an update keeps the old collection when given none, and
deleting a node deletes its edges.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def node(label, type_="Person", props=None, *, named=True, collection_id=None, sensitivity="normal",
         source_ref=None, created_at=None, id_=None):
    p = dict(props or {})
    if named:
        p.setdefault("__identity", "entity")
    return SimpleNamespace(id=id_ or uuid.uuid4(), type=type_, label=label, properties=p,
                           created_at=created_at or T0, confidence=0.9, level=0,
                           collection_id=collection_id, sensitivity=sensitivity, source_ref=source_ref,
                           workspace_id=None, entity_type=None, degree=0, updated_at=None)


class FakeRepo:
    def __init__(self, nodes=()):
        self.nodes = {n.id: n for n in nodes}
        self.edge_map: dict = {}
        self.deleted: list = []

    # reads
    async def get(self, ws, i):
        return self.nodes.get(i)

    async def get_many(self, ws, ids):
        return [self.nodes[i] for i in ids if i in self.nodes]

    async def find(self, ws, query=None, **filters):
        return list(self.nodes.values())

    async def edges(self, ws, query=None, **f):
        out = []
        for e in self.edge_map.values():
            if f.get("source_ids") is not None and e["source_id"] not in set(f["source_ids"]):
                continue
            if f.get("target_ids") is not None and e["target_id"] not in set(f["target_ids"]):
                continue
            if f.get("types") is not None and e["type"] not in set(f["types"]):
                continue
            out.append(SimpleNamespace(**e))
        return out

    # writes
    async def upsert_many(self, ws, specs, date_link=True):
        out = []
        for s in specs:
            s = dict(s)
            nid = s.pop("node_id")
            old = self.nodes.get(nid)
            props = dict(s.get("properties") or {})
            if old is not None and not s.get("replace_properties"):
                props = {**(old.properties or {}), **props}
            n = SimpleNamespace(
                id=nid, type=(old.type if old else s.get("type")), label=s.get("label") or (old.label if old else None),
                properties=props, created_at=(old.created_at if old else T0), confidence=s.get("confidence", 1.0),
                level=s.get("level", 0),
                collection_id=s.get("collection_id") if s.get("collection_id") is not None else (old.collection_id if old else None),
                sensitivity=s.get("sensitivity") or (old.sensitivity if old else "normal"),
                source_ref=(old.source_ref if old else s.get("source_ref")), workspace_id=ws, entity_type=None,
                degree=0, updated_at=None)
            self.nodes[nid] = n
            out.append(n)
        return out

    async def save(self, n):
        self.nodes[n.id] = n
        return n

    async def add_edges(self, *, workspace_id, edges, **kw):
        for e in edges:
            key = (e["source_id"], e["type"], e["target_id"])
            self.edge_map[key] = {"source_id": e["source_id"], "target_id": e["target_id"], "type": e["type"],
                               "properties": dict(e.get("properties") or {})}

    async def delete_edges(self, ws, edges=None, **f):
        gone = [k for k, e in self.edge_map.items()
                if (f.get("source_ids") is None or e["source_id"] in set(f["source_ids"]))
                and (f.get("target_ids") is None or e["target_id"] in set(f["target_ids"]))
                and (f.get("types") is None or e["type"] in set(f["types"]))]
        for k in gone:
            del self.edge_map[k]
        return len(gone)

    async def delete_nodes(self, ws, ids):
        ids = set(ids)
        for i in ids:
            self.nodes.pop(i, None)
        self.deleted += list(ids)
        for k in [k for k, e in self.edge_map.items() if e["source_id"] in ids or e["target_id"] in ids]:
            del self.edge_map[k]
        return len(ids)

    async def patch_properties(self, ws, ids, values=None, remove=(), bump_version=True):
        n = 0
        for i in ids:
            node_ = self.nodes.get(i)
            if node_ is None:
                continue
            props = dict(node_.properties or {})
            props.update(values or {})
            for k in remove:
                props.pop(k, None)
            node_.properties = props
            n += 1
        return n
