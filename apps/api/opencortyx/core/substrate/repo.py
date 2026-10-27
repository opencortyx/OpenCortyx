"""NodeRepo: the one way the application reads and writes the graph.

Nodes and edges live in the graph store (NebulaGraph) and their search surfaces in the vector store
(Qdrant). NodeRepo keeps the two consistent and applies the rules every write follows:

* an observation is merged into what a node already holds, never replacing facts it omits;
* every write stamps audit dates and increments the node's version;
* every node is linked to its calendar Date node (CREATED_ON, then MODIFIED_ON);
* an edge whose endpoint does not exist yet is quarantined for replay instead of dropped;
* every write is counted, so a lost write shows up as a number (see core.substrate.accounting).
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Sequence

from opencortyx.core.dataplane import evidence as _evidence
from opencortyx.core.substrate import accounting as _acct
from opencortyx.stores.graph import (
    Edge, EdgeQuery, EdgeWrite, Node, NodeQuery, get_graph_store,
)
from opencortyx.stores.graph.query import (
    HIDDEN_STATUSES, as_text, as_uuid, node_status, require_uuid,
)
from opencortyx.stores.vector import (
    DOCUMENT_ATTR, VectorFilter, VectorPoint, get_vector_store, search_fields,
)
from opencortyx.stores.vector.models import document_fields, document_point, document_text
from opencortyx.core.inference.embedding import sparse_embed

logger = logging.getLogger("opencortyx.substrate")

#: Who hears that an organization's graph changed — see `on_graph_change`.
_change_listeners: list = []
_last_notice: dict = {}
_NOTICE_EVERY_S = 5.0


def on_graph_change(fn) -> None:
    """Register `fn(workspace_id: str)` to hear that an organization's graph changed.

    Anything derived from the whole graph — the dashboard's cluster hierarchy — has to be rebuilt
    after ANY write, not only after an ingest: curation edits, imports, template rebuilds, merges and
    deletes all go through this class. The layer that owns the follow-up registers itself here; this
    layer only reports that a change happened, at most once per organization every few seconds, so a
    bulk write costs one notice rather than one per call."""
    if fn not in _change_listeners:
        _change_listeners.append(fn)


def _changed(workspace_id) -> None:
    if not _change_listeners:
        return
    key = str(workspace_id)
    now = time.monotonic()
    if now - _last_notice.get(key, float("-inf")) < _NOTICE_EVERY_S:
        return
    _last_notice[key] = now
    for fn in _change_listeners:
        try:
            fn(key)
        except Exception as e:                    # noqa: BLE001 — a listener never fails a write
            logger.warning("graph change listener failed: %s", e)

_DATE_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/date-node")
_NUL = chr(0)

#: `properties.__source` of nodes the app writes as its own memory of conversations.
APP_MEMORY_SOURCE = "user"
#: Stamped by conversation capture; the "conversations" scope selects these nodes.
CONVERSATION_FLAG = "__memory"
#: The app's own bookkeeping (eval cases and the like), never evidence in any scope.
NON_EVIDENCE_FLAG = "__internal"
#: Incremented on every write of a node.
AUDIT_VERSION = "__audit_version"
#: The date stamps `_stamp` writes on every node: bookkeeping, not anything the node asserts.
AUDIT_DATE_KEYS = ("created_date", "modified_date")
DELETED = "deleted"


def _strip_nuls(value):
    """Remove NUL characters anywhere in a value, including dict keys and nested lists."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.replace(_NUL, "") if _NUL in value else value
    if isinstance(value, dict):
        return {_strip_nuls(k): _strip_nuls(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        cleaned = [_strip_nuls(v) for v in value]
        return tuple(cleaned) if isinstance(value, tuple) else cleaned
    return value


async def _q_capture(workspace_id, kind, payload, *, reason, retryable=True, asset=None, error=None, funnel=False):
    from opencortyx.core.substrate.quarantine import capture
    return await capture(workspace_id, kind, payload, reason=reason, retryable=retryable,
                         asset=asset, error=error, funnel=funnel)


def _merge_props(old: dict | None, new: dict | None) -> dict:
    """Merge an observation into a node's properties without losing a fact it already holds.

    A key the observation carries wins; a key it omits, or gives as None or "", is kept. For the
    searchable renderings `__text` and `text`, the longer one survives. Callers stating the whole
    record (editors, where deleting an attribute must delete it) pass `replace_properties=True`.
    """
    if not old:
        return dict(new or {})
    if not new:
        return dict(old)
    merged = dict(old)
    for k, v in new.items():
        if v is None or v == "":
            continue
        merged[k] = v
    for k in ("__text", "text"):
        a, b = old.get(k), new.get(k)
        if isinstance(a, str) and isinstance(b, str) and len(a) > len(b):
            merged[k] = a
    return merged


def _version(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _stamp(props: dict, old: dict | None) -> dict:
    """Audit stamps: creation date kept from the first write, modification date and version bumped."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    props = dict(props)
    if old is None:
        props.setdefault("created_date", today)
        props[AUDIT_VERSION] = _version(props.get(AUDIT_VERSION)) or 1
    else:
        props["created_date"] = old.get("created_date") or props.get("created_date") or today
        props[AUDIT_VERSION] = _version(old.get(AUDIT_VERSION) or props.get(AUDIT_VERSION)) + 1
    props["modified_date"] = today
    return props


def window_open(properties: dict | None, as_of) -> bool:
    """Whether a statement's [valid_from, valid_until) window contains `as_of`.

    A missing bound is open: an undated statement is neither known to start later nor to have ended.
    ISO-8601 text compares in time order, so mixed precision compares correctly.
    """
    if as_of is None:
        return True
    stamp = as_of.isoformat() if hasattr(as_of, "isoformat") else str(as_of)
    if not stamp.strip():
        return True
    props = properties or {}
    start, end = as_text(props.get("valid_from")) or "", as_text(props.get("valid_until")) or ""
    return (not start or start <= stamp) and (not end or end > stamp)


def valid_at(node: Node, as_of) -> bool:
    return window_open(node.properties, as_of)


def sensitivities_up_to(max_sensitivity: str) -> list[str]:
    from opencortyx.domain import SENSITIVITY_ORDER
    cap = SENSITIVITY_ORDER.get(max_sensitivity, 1)
    return [name for name, level in SENSITIVITY_ORDER.items() if level <= cap]


def in_scope(node: Node, scope: str | None) -> bool:
    """`scope` is "knowledge", "conversations", "all" or "source:<value>"."""
    if not scope or scope == "all":
        return True
    props = node.properties or {}
    memory = as_text(props.get(CONVERSATION_FLAG)) == "true"
    if scope == "conversations":
        return memory
    if scope == "knowledge":
        return not memory
    return as_text(props.get("source")) == scope.replace("source:", "", 1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class NodeRepo:
    """Stateless; the stores are resolved per process."""

    def __init__(self, graph=None, vectors=None):
        self._graph = graph
        self._vectors = vectors

    @property
    def graph(self):
        return self._graph or get_graph_store()

    @property
    def vectors(self):
        return self._vectors or get_vector_store()

    # ── reads ────────────────────────────────────────────────────────────

    async def get(self, workspace_id, node_id) -> Optional[Node]:
        if node_id is None:
            return None
        found = await self.graph.get_nodes(as_uuid(workspace_id), [as_uuid(node_id)])
        return found[0] if found else None

    async def get_many(self, workspace_id, node_ids: Sequence) -> list[Node]:
        """Nodes in the order of `node_ids`, skipping ids that do not exist."""
        ids = [str(i) for i in node_ids if i is not None]
        by_id = {str(n.id): n for n in await self.graph.get_nodes(as_uuid(workspace_id), ids)}
        return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]

    async def find(self, workspace_id, query: NodeQuery | None = None, **filters) -> list[Node]:
        return await self.graph.find_nodes(as_uuid(workspace_id), query or NodeQuery(**filters))

    async def outlines(self, workspace_id) -> list[Node]:
        """Every node, without text or properties beyond its base type and status: for passes over
        the whole graph (`GraphStore.node_outlines`)."""
        return await self.graph.node_outlines(as_uuid(workspace_id))

    async def incident_edge_counts(self, workspace_id, node_ids: Sequence) -> dict[str, int]:
        return await self.graph.incident_edge_counts(as_uuid(workspace_id), node_ids)

    async def edge_ends(self, workspace_id, exclude_types: Sequence[str] = ()) -> list[tuple[str, str]]:
        return await self.graph.edge_ends(as_uuid(workspace_id), exclude_types)

    async def count(self, workspace_id, query: NodeQuery | None = None, **filters) -> int:
        return await self.graph.count_nodes(as_uuid(workspace_id), query or NodeQuery(**filters))

    async def count_by_type(self, workspace_id) -> dict[str, int]:
        return await self.graph.count_nodes_by_type(as_uuid(workspace_id))

    async def edges(self, workspace_id, query: EdgeQuery | None = None, **filters) -> list[Edge]:
        return await self.graph.find_edges(as_uuid(workspace_id), query or EdgeQuery(**filters))

    async def count_edges(self, workspace_id) -> int:
        return await self.graph.count_edges(as_uuid(workspace_id))

    async def count_edges_by_type(self, workspace_id) -> dict[str, int]:
        return await self.graph.count_edges_by_type(as_uuid(workspace_id))

    async def descendants(self, workspace_id, root_ids: Sequence, *, max_depth: int = 12,
                          query: NodeQuery | None = None) -> list[Node]:
        """Nodes derived from the roots through `source_ref`, at any depth up to `max_depth`.

        `query` narrows what is returned, not what is walked."""
        seen = {str(r) for r in root_ids if r is not None}
        frontier, out = list(seen), []
        for _ in range(max_depth):
            if not frontier:
                break
            children = await self.find(workspace_id, NodeQuery(source_refs=frontier))
            frontier = []
            for child in children:
                if str(child.id) not in seen:
                    seen.add(str(child.id))
                    frontier.append(str(child.id))
                    out.append(child)
        if query is None:
            return out
        from opencortyx.stores.graph.query import select_nodes
        return select_nodes(out, query)

    async def neighbors(self, workspace_id, node_ids: Sequence, *, edge_types: Sequence[str] | None = None,
                        direction: str = "out", limit: int = 200, scope: str | None = None) -> list[tuple[Edge, Node]]:
        """One hop from `node_ids` as (edge, far node), skipping anything awaiting review or removed.

        `scope` filters the far node like searches filter their hits, so a knowledge search does
        not walk back into conversation memory through a shared Date node."""
        if not node_ids:
            return []
        hops = await self.graph.neighbors(as_uuid(workspace_id), list(node_ids), edge_types=edge_types,
                                          direction=direction, limit=limit)
        out = []
        for hop in hops:
            if (hop.edge.properties or {}).get("__status") in HIDDEN_STATUSES or node_status(hop.node) in HIDDEN_STATUSES:
                continue
            if in_scope(hop.node, scope):
                out.append((hop.edge, hop.node))
        return out

    async def neighbor_groups(self, workspace_id, node_id, *, exclude_edge_types: Sequence[str] = ()):
        """One node's relationships counted by (edge type, direction, far node type), by the store.

        The counts include neighbours awaiting review; the pages below leave them out, so a group can
        show a few fewer rows than it says. That is the price of never loading a hub to count it."""
        return await self.graph.neighbor_groups(as_uuid(workspace_id), node_id,
                                                exclude_edge_types=list(exclude_edge_types))

    async def neighbor_page(self, workspace_id, node_id, *, edge_type: str, direction: str, node_type: str,
                            offset: int = 0, limit: int = 100) -> list[tuple[Edge, Node]]:
        """One group of `neighbor_groups`, a page at a time, as (edge, far node)."""
        hops = await self.graph.neighbor_page(as_uuid(workspace_id), node_id, edge_type=edge_type,
                                              direction=direction, node_type=node_type,
                                              offset=offset, limit=limit)
        return [(h.edge, h.node) for h in hops
                if (h.edge.properties or {}).get("__status") not in HIDDEN_STATUSES
                and node_status(h.node) not in HIDDEN_STATUSES]

    async def shortest_paths(self, workspace_id, source_id, target_id, *, max_depth: int = 3, limit: int = 10):
        return await self.graph.shortest_paths(as_uuid(workspace_id), source_id, target_id,
                                               max_depth=max_depth, limit=limit)

    async def get_text(self, workspace_id, node_id) -> str:
        node = await self.get(workspace_id, node_id)
        if node is None:
            return ""
        return (node.properties or {}).get("text") or node.label or ""

    # ── node writes ──────────────────────────────────────────────────────

    async def upsert_node(self, *, workspace_id, type, label="", entity_type=None, properties=None,
                          confidence=1.0, level=0, source_ref=None, collection_id=None, node_id=None,
                          replace_properties: bool = False, date_link: bool = True,
                          sensitivity: str | None = None) -> Node:
        """Create a node or merge into the existing one with `node_id`. An existing node keeps its type."""
        org = as_uuid(workspace_id)
        _acct.note(org, _acct.ATTEMPTED)
        outcome = None
        try:
            node, created = await self._write_node(
                org, type=type, label=label, entity_type=entity_type, properties=properties,
                confidence=confidence, level=level, source_ref=source_ref, collection_id=collection_id,
                node_id=node_id, replace_properties=replace_properties, sensitivity=sensitivity)
            outcome = _acct.WRITTEN
            _acct.note(org, _acct.CREATED if created else _acct.UPDATED)
        except BaseException:
            outcome = _acct.ESCAPED
            raise
        finally:
            _acct.note(org, outcome or _acct.UNACCOUNTED)
        if _evidence.current() is not None:
            await _evidence.note(org, node.id, node_type=node.type)
        if date_link and type != "Date":
            await self.link_date(workspace_id=org, node_id=node.id, created=created)
        return node

    async def _write_node(self, org, *, type, label, entity_type, properties, confidence, level,
                          source_ref, collection_id, node_id, replace_properties, sensitivity):
        if label is not None and not isinstance(label, str):
            label = str(label)
        if entity_type is not None and not isinstance(entity_type, str):
            entity_type = str(entity_type)
        nid = as_uuid(node_id) or uuid.uuid4()
        old = await self.get(org, nid) if node_id else None
        node = self._build_node(org, nid, old, type=type, label=label, entity_type=entity_type,
                                properties=properties, confidence=confidence, level=level, source_ref=source_ref,
                                collection_id=collection_id, replace_properties=replace_properties,
                                sensitivity=sensitivity)
        await self.graph.upsert_nodes([node])
        _changed(org)
        await self._index([(old, node)])
        return node, old is None

    @staticmethod
    def _build_node(org, nid, old, *, type, label=None, entity_type=None, properties=None, confidence=1.0,
                    level=0, source_ref=None, collection_id=None, replace_properties=False, sensitivity=None) -> Node:
        """The record a write produces: a new node, or `old` merged with the write. Shared by the single
        and the bulk write so both follow exactly the same rules."""
        if label is not None and not isinstance(label, str):
            label = str(label)
        if entity_type is not None and not isinstance(entity_type, str):
            entity_type = str(entity_type)
        label, entity_type = _strip_nuls(label), _strip_nuls(entity_type)
        properties = _strip_nuls(properties)
        now = _now()
        if old is None:
            node = Node(id=nid, workspace_id=org, type=type, label=label or None, entity_type=entity_type or None,
                        properties=_stamp(properties or {}, None), confidence=confidence, level=level,
                        source_ref=as_uuid(source_ref), collection_id=as_uuid(collection_id),
                        sensitivity=sensitivity or "normal", created_at=now, updated_at=now)
        else:
            merged = dict(properties or old.properties or {}) if replace_properties \
                else _merge_props(old.properties, properties)
            node = Node(id=nid, workspace_id=org, type=old.type, label=label or old.label,
                        entity_type=entity_type or old.entity_type, properties=_stamp(merged, old.properties),
                        confidence=confidence, level=level,
                        source_ref=as_uuid(source_ref) if source_ref is not None else old.source_ref,
                        collection_id=as_uuid(collection_id) if collection_id is not None else old.collection_id,
                        sensitivity=sensitivity or old.sensitivity,
                        degree=old.degree, created_at=old.created_at or now, updated_at=now)
        return node

    async def upsert_many(self, workspace_id, specs: Sequence[dict], *, date_link: bool = True) -> list[Node]:
        """`upsert_node` for many records of one organization: one read, one graph write, one index
        write and one edge batch for the day's date links, instead of four round trips per record.

        Each spec takes `upsert_node`'s keyword arguments. A record repeated within the batch merges
        like a second write, and accounting is noted per spec, exactly as the single write does."""
        org = as_uuid(workspace_id)
        specs = [dict(s) for s in specs if s]
        if not specs:
            return []
        ids = [as_uuid(s.pop("node_id", None)) or uuid.uuid4() for s in specs]
        stored = {n.id: n for n in await self.graph.get_nodes(org, list(dict.fromkeys(ids)))}
        built: dict = {}
        for nid, spec in zip(ids, specs):
            _acct.note(org, _acct.ATTEMPTED)
            prior = built.get(nid) or stored.get(nid)
            built[nid] = self._build_node(org, nid, prior, **spec)
            _acct.note(org, _acct.WRITTEN)
            _acct.note(org, _acct.UPDATED if prior is not None else _acct.CREATED)
        nodes = list(built.values())
        await self.graph.upsert_nodes(nodes)
        _changed(org)
        await self._index([(stored.get(n.id), n) for n in nodes])
        if _evidence.current() is not None:
            for n in nodes:
                await _evidence.note(org, n.id, node_type=n.type)
        if date_link:
            did = await self.ensure_date_node(workspace_id=org)
            await self.add_edges(workspace_id=org, edges=[
                {"source_id": n.id, "target_id": did, "type": "MODIFIED_ON" if n.id in stored else "CREATED_ON"}
                for n in nodes if n.type != "Date"], owned=[n.id for n in nodes if n.id not in stored])
        return nodes

    async def _index(self, changes: Sequence[tuple[Node | None, Node]]) -> None:
        """Keep search surfaces in step with records, given (old, new) pairs.

        A new node, or one whose words changed, gets its keyword document rewritten now and is
        marked for re-embedding; a change to filter fields only is copied onto every point."""
        rewrite, refield = [], []
        for old, new in changes:
            if old is None or (document_text(old), document_fields(old)) != (document_text(new), document_fields(new)):
                rewrite.append(new)
            if old is not None and search_fields(new) != search_fields(old):
                refield.append(new)
        if rewrite:
            sparse = await sparse_embed([document_text(n) or n.type for n in rewrite])
            await self.vectors.upsert([document_point(n, vec) for n, vec in zip(rewrite, sparse)])
        for new in refield:
            await self.vectors.update_fields(new.workspace_id, new.id, search_fields(new))

    async def save(self, node: Node) -> Node:
        """Write a whole record the caller loaded and changed: type, label and properties as given."""
        return (await self.save_many(node.workspace_id, [node]))[0]

    async def save_many(self, workspace_id, nodes: Sequence[Node]) -> list[Node]:
        """`save` for many records of one organization: one read, one graph write, one index write."""
        org = as_uuid(workspace_id)
        nodes = [n for n in nodes if str(n.workspace_id) == str(org)]
        if not nodes:
            return []
        old = {n.id: n for n in await self.graph.get_nodes(org, [n.id for n in nodes])}
        now = _now()
        for node in nodes:
            before = old.get(node.id)
            node.properties = _stamp(_strip_nuls(node.properties or {}), before.properties if before else None)
            node.updated_at = now
            node.created_at = (before.created_at if before else None) or node.created_at or now
        await self.graph.upsert_nodes(nodes)
        _changed(org)
        for node in nodes:
            _acct.note(org, _acct.ATTEMPTED)
            _acct.note(org, _acct.WRITTEN)
            _acct.note(org, _acct.UPDATED if node.id in old else _acct.CREATED)
        await self._index([(old.get(n.id), n) for n in nodes])
        return nodes

    async def patch_properties(self, workspace_id, node_ids: Sequence, values: dict | None = None,
                               remove: Sequence[str] = (), bump_version: bool = True) -> int:
        """Set and remove property keys on nodes. Returns how many nodes were changed."""
        nodes = await self.get_many(workspace_id, node_ids)
        changed = []
        for old in nodes:
            props = dict(old.properties or {})
            props.update(_strip_nuls(values or {}))
            for key in remove:
                props.pop(key, None)
            if props == (old.properties or {}):
                continue
            new = Node(**{**old.__dict__, "properties": _stamp(props, old.properties) if bump_version else props,
                          "updated_at": _now() if bump_version else old.updated_at})
            changed.append((old, new))
        if changed:
            await self.graph.upsert_nodes([new for _, new in changed])
            _changed(workspace_id)
            await self._index(changed)
        return len(changed)

    async def delete_nodes(self, workspace_id, node_ids: Sequence) -> int:
        """Delete nodes with their edges and search surfaces."""
        ids = [as_uuid(i) for i in node_ids if i is not None]
        if not ids:
            return 0
        org = as_uuid(workspace_id)
        await self.vectors.delete(VectorFilter(workspace_id=require_uuid(org, "workspace_id"), node_ids=ids))
        deleted = await self.graph.delete_nodes(org, ids)
        _changed(org)
        _acct.note(org, DELETED, deleted)
        return deleted

    async def note_access(self, *, workspace_id, node_ids) -> int:
        """Record that nodes were used in an answer: a ranking signal, never a filter."""
        stamp = _now().replace(microsecond=0).isoformat()
        count = 0
        for node in await self.get_many(workspace_id, node_ids or []):
            props = dict(node.properties or {})
            props["__access_count"] = _version(props.get("__access_count")) + 1
            props["__last_accessed"] = stamp
            node.properties = props
            await self.graph.upsert_nodes([node])
            count += 1
        return count

    async def ensure_date_node(self, *, workspace_id, dt: Optional[datetime] = None) -> uuid.UUID:
        """The calendar Date node for `dt` (default now), created once per organization and day."""
        dt = dt or _now()
        day = dt.strftime("%Y-%m-%d")
        org = as_uuid(workspace_id)
        did = uuid.uuid5(_DATE_NS, f"{org}:{day}")
        if (org, did) in _known_dates or await self.get(org, did) is not None:
            _known_dates.add((org, did))
            return did
        await self.upsert_node(workspace_id=org, type="Date", label=day, node_id=did, date_link=False,
                               properties={"date": day, "year": dt.year, "month": dt.month, "day": dt.day,
                                           "weekday": dt.strftime("%A"), "__source": "system"})
        _known_dates.add((org, did))
        return did

    async def link_date(self, *, workspace_id, node_id, created: bool) -> None:
        """CREATED_ON today's Date node for a new node, MODIFIED_ON otherwise (one edge per day)."""
        did = await self.ensure_date_node(workspace_id=workspace_id)
        await self.add_edge(workspace_id=workspace_id, source_id=node_id, target_id=did,
                            type="CREATED_ON" if created else "MODIFIED_ON")

    # ── edge writes ──────────────────────────────────────────────────────

    async def add_edge(self, *, workspace_id, source_id, target_id, type, weight=1.0, properties=None,
                       confidence=1.0, asset=None, _quarantine=True, dedupe=False, by=None) -> bool:
        """Write an edge if both endpoints exist; otherwise quarantine it for replay.

        One relationship of a type between two nodes is one edge, so a repeat updates it. Returns True
        when the edge is new. `dedupe` is accepted for callers written before edges were idempotent.
        """
        result = await self.add_edges(workspace_id=workspace_id, edges=[{
            "source_id": source_id, "target_id": target_id, "type": type, "weight": weight,
            "properties": properties, "confidence": confidence}], asset=asset, _quarantine=_quarantine, by=by)
        return bool(result.created)

    async def add_edges(self, *, workspace_id, edges: Sequence[dict], asset=None, _quarantine=True, by=None,
                        owned=None) -> EdgeWrite:
        """Write many edges with the same rules as `add_edge`. `owned`: ids of nodes this write alone
        is touching (see GraphStore.upsert_edges), whose degrees can be written in bulk."""
        org = as_uuid(workspace_id)
        result = EdgeWrite()
        valid: list[Edge] = []
        now = _now()
        for spec in edges:
            _acct.note(org, _acct.ATTEMPTED)
            src, dst, etype = spec.get("source_id"), spec.get("target_id"), spec.get("type")
            reason = None
            if not src or not dst:
                reason = "missing_endpoint_id"
            elif str(src) == str(dst):
                reason = "self_edge"
            if reason:
                if _quarantine:
                    await _q_capture(org, "edge", {"source_id": src, "target_id": dst, "type": etype},
                                     reason=reason, retryable=False, asset=asset, funnel=True)
                _acct.note(org, _acct.QUARANTINED)
                continue
            props = dict(_strip_nuls(spec.get("properties")) or {})
            if by and "__by" not in props:
                props["__by"] = str(by)
            valid.append(Edge(workspace_id=require_uuid(org, "workspace_id"), source_id=require_uuid(src, "source_id"),
                              target_id=require_uuid(dst, "target_id"), type=str(etype),
                              weight=spec.get("weight", 1.0) if spec.get("weight") is not None else 1.0,
                              properties=props, created_at=now,
                              confidence=spec.get("confidence", 1.0) if spec.get("confidence") is not None else 1.0))
        if not valid:
            return result
        try:
            written = await self.graph.upsert_edges(valid, owned=owned)
            _changed(org)
        except BaseException:
            _acct.note(org, _acct.ESCAPED, len(valid))
            raise
        for edge in written.refused:
            if _quarantine:
                await _q_capture(org, "edge", {
                    "source_id": edge.source_id, "target_id": edge.target_id, "type": edge.type,
                    "weight": edge.weight, "properties": edge.properties, "confidence": edge.confidence},
                    reason="endpoint_not_yet_created", asset=asset, funnel=True)
            _acct.note(org, _acct.QUARANTINED)
        _acct.note(org, _acct.WRITTEN, len(written.created) + len(written.updated))
        _acct.note(org, _acct.CREATED, len(written.created))
        _acct.note(org, _acct.UPDATED, len(written.updated))
        if _evidence.current() is not None:
            for e in [*written.created, *written.updated]:
                if e.type not in ("FROM_ASSET", "CREATED_ON", "MODIFIED_ON"):
                    await _evidence.note(org, _evidence.edge_id(e.source_id, e.type, e.target_id),
                                         target_kind="edge")
        return written

    async def delete_edges(self, workspace_id, edges: Sequence[Edge] | None = None, **filters) -> int:
        """Delete the given edges, or the edges matching `filters` (an EdgeQuery)."""
        org = as_uuid(workspace_id)
        if edges is None:
            edges = await self.edges(org, EdgeQuery(**filters))
        deleted = await self.graph.delete_edges(org, list(edges))
        _changed(org)
        _acct.note(org, DELETED, deleted)
        return deleted

    async def index_embeddings(self, workspace_id, nodes: Sequence[Node], embed_batch, *, batch_size: int = 64,
                               progress=None) -> dict[str, int]:
        """Embed every indexable attribute of `nodes`, one point per attribute, replacing old ones.

        Each attribute is its own surface, so a node is found by the one field that matches rather
        than by an average of all of them. Returns how many values were embedded per attribute."""
        from opencortyx.core.config import runtime as _rc
        from opencortyx.core.substrate.document import (
            ATTR_MAX_CHARS, ATTR_MIN_CHARS, TEXT_MAX_WINDOWS, attr_search_text, discover_attributes,
            text_windows,
        )
        org = as_uuid(workspace_id)
        counts: dict[str, int] = {}
        done = 0
        batch = list(nodes)
        for start in range(0, len(batch), max(1, batch_size)):
            chunk = batch[start:start + batch_size]
            surfaces = []
            for node in chunk:
                attrs = discover_attributes(node.properties)
                long_text = (node.properties or {}).get("text")
                windowed = isinstance(long_text, str) and len(long_text.strip()) > _rc.chunk_window_chars()
                if windowed:
                    attrs.pop("text", None)          # embedded window by window below instead
                label = (node.label or "").strip()
                if len(label) >= ATTR_MIN_CHARS:
                    attrs.setdefault("label", label[:ATTR_MAX_CHARS])
                for attr, value in attrs.items():
                    surfaces.append((node, attr, attr_search_text(attr, value), 0))
                    counts[attr] = counts.get(attr, 0) + 1
                if windowed:
                    # Too long to be one surface: a passage is embedded window by window instead.
                    for part, window in enumerate(text_windows(
                            str(long_text), _rc.chunk_window_chars(), _rc.chunk_overlap_chars())[:TEXT_MAX_WINDOWS]):
                        surfaces.append((node, "text", attr_search_text("text", window), part))
                    counts["text"] = counts.get("text", 0) + 1
            vectors = await embed_batch([text for _, _, text, _ in surfaces]) if surfaces else []
            sparse = await sparse_embed([document_text(n) or n.type for n in chunk])
            points = [VectorPoint(workspace_id=require_uuid(org, "workspace_id"), node_id=node.id, attr=attr,
                                  text=text[:max(ATTR_MAX_CHARS, _rc.chunk_window_chars()) + 64], part=part,
                                  fields=search_fields(node), dense=list(vec))
                      for (node, attr, text, part), vec in zip(surfaces, vectors) if vec]
            points += [document_point(node, vec, embedded=True) for node, vec in zip(chunk, sparse)]
            await self.vectors.delete(VectorFilter(workspace_id=require_uuid(org, "workspace_id"),
                                                   node_ids=[n.id for n in chunk],
                                                   exclude_attrs=[DOCUMENT_ATTR]))
            await self.vectors.upsert(points)
            done += len(chunk)
            if progress:
                progress(done, counts)
        return counts

    async def unembedded(self, workspace_id, nodes: Sequence[Node]) -> list[Node]:
        """The nodes among `nodes` whose attribute embeddings are missing or out of date."""
        current = await self.vectors.embedded_nodes(as_uuid(workspace_id), [n.id for n in nodes])
        return [n for n in nodes if str(n.id) not in current]

    async def keyword_only_ids(self, workspace_id, *, limit: int) -> list[str]:
        """Nodes whose keyword point is written but whose vectors are not, from the vector store alone.

        Every node gets its keyword point when it is written and is marked embedded when its vectors
        land, so these are exactly what embedding still owes, with no read of the graph."""
        rows = await self.vectors.scroll(VectorFilter(workspace_id=require_uuid(workspace_id, "workspace_id"), attrs=[DOCUMENT_ATTR],
                                                      embedded=False), limit=limit, fields=["node_id"])
        return [str(r["node_id"]) for r in rows if r.get("node_id")]

    async def unembedded_ids(self, workspace_id, *, created_after=None, created_before=None) -> list[str]:
        """Ids of the nodes created in the window that have no embeddings, found from ids alone.

        Ids first, nodes only for what is missing: after a thousand-file upload the window holds
        hundreds of thousands of nodes, and loading every one (text and all) to learn that nearly all
        are embedded ran the graph store out of memory."""
        ids = await self.graph.find_node_ids(as_uuid(workspace_id), NodeQuery(
            created_after=created_after, created_before=created_before))
        current = await self.vectors.embedded_nodes(as_uuid(workspace_id), ids)
        return [i for i in ids if i not in current]

    async def clear_org(self, workspace_id) -> int:
        org = as_uuid(workspace_id)
        await self.vectors.clear_org(org)
        out = await self.graph.clear_org(org)
        _changed(org)
        return out

    # ── search ───────────────────────────────────────────────────────────

    def _filter(self, workspace_id, *, types, collection_ids, min_conf, max_sensitivity, conversation_id,
                scope, attrs=None, exclude_attrs=None) -> VectorFilter:
        return VectorFilter(workspace_id=require_uuid(workspace_id, "workspace_id"), attrs=attrs, exclude_attrs=exclude_attrs, types=types or None,
                            collection_ids=collection_ids or None, source_ref=as_uuid(conversation_id),
                            sensitivities=sensitivities_up_to(max_sensitivity),
                            min_confidence=min_conf or None, evidence=True, scope=scope)

    async def _nodes_for_hits(self, workspace_id, hits, k: int, as_of) -> list[tuple[Node, float]]:
        best: dict[str, float] = {}
        for hit in hits:
            key = str(hit.node_id)
            if key not in best or hit.score > best[key]:
                best[key] = hit.score
        nodes = await self.get_many(workspace_id, list(best))
        ranked = [(n, best[str(n.id)]) for n in nodes if valid_at(n, as_of)]
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        return ranked[:k]

    async def vector_search(self, *, workspace_id, query_vec, types=None, collection_ids=None, k=12, min_conf=0.0,
                            max_sensitivity="normal", conversation_id=None, scope=None, as_of=None,
                            attrs=None) -> list[tuple[Node, float]]:
        """Nearest nodes by any of their attribute embeddings, as (node, cosine similarity).

        Superseded, structural, internal, proposed and removed nodes are never returned; a statement whose
        validity window has closed still is, since questions about the past need it. `as_of` keeps
        only statements valid at that instant."""
        if not query_vec:
            return []
        flt = self._filter(workspace_id, types=types, collection_ids=collection_ids, min_conf=min_conf,
                           max_sensitivity=max_sensitivity, conversation_id=conversation_id, scope=scope,
                           attrs=attrs, exclude_attrs=[DOCUMENT_ATTR])
        hits = await self.vectors.search_dense(list(query_vec), flt, k * (8 if as_of else 4))
        return await self._nodes_for_hits(workspace_id, hits, k, as_of)

    async def keyword_search(self, *, workspace_id, query, types=None, collection_ids=None, k=12,
                             max_sensitivity="normal", conversation_id=None, scope=None,
                             as_of=None) -> list[tuple[Node, float]]:
        """BM25-ranked nodes for the words in `query`, as (node, score)."""
        from opencortyx.core.inference.embedding import sparse_query
        vector = await sparse_query(query or "")
        if not vector.indices:
            return []
        flt = self._filter(workspace_id, types=types, collection_ids=collection_ids, min_conf=None,
                           max_sensitivity=max_sensitivity, conversation_id=conversation_id, scope=scope,
                           attrs=[DOCUMENT_ATTR])
        hits = await self.vectors.search_sparse(vector, flt, k * (4 if as_of else 1))
        return await self._nodes_for_hits(workspace_id, hits, k, as_of)


_known_dates: set = set()
