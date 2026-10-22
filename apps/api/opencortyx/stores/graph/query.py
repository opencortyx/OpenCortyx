"""Query semantics shared by every adapter, so NebulaGraph and the in-memory store agree exactly."""
from __future__ import annotations
from opencortyx.stores.graph.exceptions import GraphStoreValidationError

import re
import uuid
from datetime import datetime, timezone
from typing import Iterable

from opencortyx.stores.graph.models import Edge, EdgeQuery, Node, NodeQuery, type_variants

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def label_key(label) -> str | None:
    """The comparable form of a label: trimmed, lower case."""
    if label is None:
        return None
    key = str(label).strip().lower()
    return key or None


_LEGAL_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co",
                   "company", "plc", "gmbh", "sa", "ag", "srl", "bv", "nv", "pte", "llp", "group"}
_NAME_STOP = {"my", "your", "our", "their", "his", "her", "its", "the", "a", "an"}


def normalize_name(name) -> str:
    """The key two surface forms of one entity share: lower case, no punctuation, legal suffixes,
    possessives or articles. "Acme, Inc." and "ACME Inc" → "acme"; "My dog" and "dog" → "dog"."""
    s = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower())
    return " ".join(t for t in s.split() if t not in _LEGAL_SUFFIXES and t not in _NAME_STOP)


def as_text(value) -> str | None:
    """A property value compared the way JSON text compares: `True` is "true", 3 is "3"."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value, sort_keys=True)
    return str(value)


def _ids(values: Iterable) -> set[str]:
    return {str(v) for v in values if v is not None}


def _prop_matches(props: dict, key: str, want) -> bool:
    have = as_text((props or {}).get(key))
    if want is None:
        return have in (None, "")
    if isinstance(want, (list, tuple, set, frozenset)):
        return have in {as_text(w) for w in want}
    return have == as_text(want)


def node_matches(node: Node, q: NodeQuery) -> bool:
    if q.ids is not None and str(node.id) not in _ids(q.ids):
        return False
    if q.types:
        wanted = set(type_variants(*q.types))
        if node.type not in wanted and node.base_type not in wanted:
            return False
    if q.exact_types and node.type not in set(q.exact_types):
        return False
    if q.exclude_types and node.type in set(q.exclude_types):
        return False
    if q.entity_type is not None and node.entity_type != q.entity_type:
        return False
    if q.source_refs is not None and str(node.source_ref) not in _ids(q.source_refs):
        return False
    if q.collection_ids is not None and str(node.collection_id) not in _ids(q.collection_ids):
        return False
    if q.source_key is not None and as_text((node.properties or {}).get("source_key")) != str(q.source_key):
        return False
    if q.status is not None and node_status(node) != q.status:
        return False
    if q.label is not None and label_key(node.label) != label_key(q.label):
        return False
    if q.name_key is not None and normalize_name(node.label) != q.name_key:
        return False
    if q.label_contains and q.label_contains.lower() not in (node.label or "").lower():
        return False
    for key, want in (q.props or {}).items():
        if not _prop_matches(node.properties, key, want):
            return False
    for key, want in (q.props_not or {}).items():
        if _prop_matches(node.properties, key, want):
            return False
    if q.created_after is not None and (node.created_at is None or node.created_at <= q.created_after):
        return False
    if q.created_before is not None and (node.created_at is None or node.created_at > q.created_before):
        return False
    return True


def node_status(node: Node) -> str:
    """A node's lifecycle state: an explicit `__status` (proposed, removed), else "superseded" when a
    newer version replaced it, "supersede_proposed" while that replacement awaits review, else "active"."""
    props = node.properties or {}
    if props.get("__status"):
        return str(props["__status"])
    superseded = as_text(props.get("superseded"))
    if superseded == "true":
        return "superseded"
    return "supersede_proposed" if superseded == "proposed" else "active"


#: Lifecycle states hidden from retrieval: awaiting review, or removed from their source.
HIDDEN_STATUSES = ("proposed", "removed")


def order_nodes(nodes: list[Node], order_by: str | None) -> list[Node]:
    if not order_by:
        return nodes
    desc = order_by.startswith("-")
    key = order_by.lstrip("-")
    if key in ("created_at", "updated_at"):
        return sorted(nodes, key=lambda n: getattr(n, key) or _EPOCH, reverse=desc)
    if key in ("degree", "level", "confidence"):
        return sorted(nodes, key=lambda n: getattr(n, key) or 0, reverse=desc)
    raise GraphStoreValidationError(f"unsupported order: {order_by}")


def select_nodes(nodes: Iterable[Node], q: NodeQuery) -> list[Node]:
    out = order_nodes([n for n in nodes if node_matches(n, q)], q.order_by)
    return out[:q.limit] if q.limit is not None else out


def edge_matches(edge: Edge, q: EdgeQuery) -> bool:
    if q.ids is not None and str(edge.id) not in _ids(q.ids):
        return False
    if q.source_ids is not None and str(edge.source_id) not in _ids(q.source_ids):
        return False
    if q.target_ids is not None and str(edge.target_id) not in _ids(q.target_ids):
        return False
    if q.node_ids is not None:
        ends = _ids(q.node_ids)
        if str(edge.source_id) not in ends and str(edge.target_id) not in ends:
            return False
    if q.types and edge.type not in set(q.types):
        return False
    if q.exclude_types and edge.type in set(q.exclude_types):
        return False
    for key, want in (q.props or {}).items():
        if not _prop_matches(edge.properties, key, want):
            return False
    return True


def require_uuid(value, what: str = "id") -> uuid.UUID:
    """`as_uuid`, for the places where a missing value is a bug rather than a blank field.

    The write path takes org ids and edge endpoints that have already been checked; typing them as
    optional the whole way down means every call site re-proves it, or — what actually happened —
    none of them do and the Optional just travels.
    """
    parsed = as_uuid(value)
    if parsed is None:
        raise ValueError(f"{what} is required, got {value!r}")
    return parsed


def as_uuid(value) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
