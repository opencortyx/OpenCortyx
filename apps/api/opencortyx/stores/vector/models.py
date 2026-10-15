"""Vector points and the search filter every adapter applies the same way.

A point is one searchable surface of a node: an attribute's text with its dense embedding, or the
node's whole document with a BM25 sparse vector for keyword search. Each point carries the node
fields searches filter on, so filtering happens inside the index rather than after it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional, Sequence

from opencortyx.stores.graph.models import Node, type_variants
from opencortyx.stores.graph.query import HIDDEN_STATUSES, as_text, node_status

_POINT_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/vector-point")

#: The point holding a node's whole document for keyword search.
DOCUMENT_ATTR = "__doc"

#: Properties that hold other names for a node.
NAME_KEYS = ("name", "aliases", "alias", "also_known_as", "aka", "synonyms", "other_names",
             "short_name", "nickname", "abbreviation")
_DOCUMENT_KEYS = ("text", "__context", "__text")
_MAX_PROP_CHARS = 512

#: Node properties that mark a node as never evidence, and the flags that record them on a point.
EVIDENCE_FLAGS = {"superseded": "superseded", "structural": "__structural", "internal": "__internal"}
CONVERSATION_FLAG = "__memory"


def point_id(node_id, attr: str) -> str:
    return str(uuid.uuid5(_POINT_NS, f"{node_id}|{attr}"))


def search_fields(node: Node) -> dict:
    """The node fields a vector search filters on, as stored on each of the node's points."""
    props = node.properties or {}
    fields = {
        "types": sorted({node.type, node.base_type}),
        "collection_id": str(node.collection_id) if node.collection_id else None,
        "source_ref": str(node.source_ref) if node.source_ref else None,
        "sensitivity": node.sensitivity or "normal",
        "confidence": float(node.confidence if node.confidence is not None else 1.0),
        "hidden": node_status(node) in HIDDEN_STATUSES,
        "memory": as_text(props.get(CONVERSATION_FLAG)) == "true",
        "source": as_text(props.get("source")),
    }
    for flag, key in EVIDENCE_FLAGS.items():
        fields[flag] = as_text(props.get(key)) == "true"
    return fields


def document_text(node: Node) -> str:
    """What keyword search ranks a node on: its text, generated context, stored rendering and label."""
    props = node.properties or {}
    parts = [props.get(k) for k in _DOCUMENT_KEYS] + [node.label]
    return " ".join(str(p) for p in parts if p not in (None, ""))


def document_fields(node: Node) -> dict:
    """Fields only the document point carries: names, scalar properties and the matchable text."""
    props = node.properties or {}
    names: set[str] = set()
    for value in [node.label] + [props.get(k) for k in NAME_KEYS]:
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and item.strip():
                names.add(item.strip().lower())
    scalars = {k: (v[:_MAX_PROP_CHARS] if isinstance(v, str) else v) for k, v in props.items()
               if isinstance(v, (str, int, float, bool)) and v != "" and not k.startswith("__")
               and k not in _DOCUMENT_KEYS}
    search = " ".join([document_text(node), *(str(v) for v in scalars.values()), *sorted(names)])
    return {"names": sorted(names), "names_text": " ; ".join(sorted(names)), "props": scalars,
            "search": search, "embedded": False}


def document_point(node: Node, sparse, *, embedded: bool = False) -> "VectorPoint":
    """The node's keyword document: BM25 vector, filter fields and document fields."""
    return VectorPoint(workspace_id=node.workspace_id, node_id=node.id, attr=DOCUMENT_ATTR,
                       text=document_text(node) or node.type,
                       fields={**search_fields(node), **document_fields(node), "embedded": embedded},
                       sparse=sparse)


def tokens(text: str) -> list[str]:
    import re
    return re.findall(r"\w+", (text or "").lower())


@dataclass
class SparseVector:
    indices: list[int]
    values: list[float]


@dataclass
class VectorPoint:
    workspace_id: uuid.UUID
    node_id: uuid.UUID
    attr: str
    text: str
    fields: dict = field(default_factory=dict)
    dense: Optional[list[float]] = None
    sparse: Optional[SparseVector] = None

    @property
    def id(self) -> str:
        return point_id(self.node_id, self.attr)


@dataclass
class VectorFilter:
    """Which points a search may return. `None` means no constraint.

    `evidence` excludes nodes that are superseded, structural, internal, awaiting review or removed.
    `scope` is "knowledge", "conversations", "all" or "source:<value>". `include_unscoped` keeps
    nodes in no collection when filtering by collection. `text_all` needs every word of the text
    in the document's matchable text, `phrase` the words adjacent and in order, `names_phrase`
    the same within the node's names. `has_props` needs at least one of these properties.
    """
    workspace_id: uuid.UUID
    attrs: Optional[Sequence[str]] = None
    exclude_attrs: Optional[Sequence[str]] = None
    node_ids: Optional[Sequence] = None
    types: Optional[Sequence[str]] = None
    collection_ids: Optional[Sequence] = None
    source_ref: Optional[uuid.UUID] = None
    sensitivities: Optional[Sequence[str]] = None
    min_confidence: Optional[float] = None
    evidence: bool = False
    exclude_superseded: bool = False
    scope: Optional[str] = None
    include_unscoped: bool = False
    text_all: Optional[str] = None
    phrase: Optional[str] = None
    names: Optional[Sequence[str]] = None
    names_phrase: Optional[str] = None
    has_props: Optional[Sequence[str]] = None
    embedded: Optional[bool] = None

    def matches(self, payload: dict) -> bool:
        """The filter applied to a stored payload; the in-memory store uses it directly."""
        def within(key, values):
            return payload.get(key) in {str(v) for v in values}

        if payload.get("workspace_id") != str(self.workspace_id):
            return False
        if self.attrs is not None and payload.get("attr") not in set(self.attrs):
            return False
        if self.exclude_attrs and payload.get("attr") in set(self.exclude_attrs):
            return False
        if self.node_ids is not None and not within("node_id", self.node_ids):
            return False
        if self.types and not set(payload.get("types") or []) & set(type_variants(*self.types)):
            return False
        if self.collection_ids is not None and not within("collection_id", self.collection_ids) and not (
                self.include_unscoped and not payload.get("collection_id")):
            return False
        if self.source_ref is not None and payload.get("source_ref") != str(self.source_ref):
            return False
        if self.sensitivities is not None and payload.get("sensitivity") not in set(self.sensitivities):
            return False
        if self.min_confidence is not None and (payload.get("confidence") or 0.0) < self.min_confidence:
            return False
        if (self.evidence or self.exclude_superseded) and payload.get("superseded"):
            return False
        if self.evidence and (payload.get("structural") or payload.get("internal") or payload.get("hidden")):
            return False
        if self.scope == "conversations" and not payload.get("memory"):
            return False
        if self.scope == "knowledge" and payload.get("memory"):
            return False
        if self.scope and self.scope.startswith("source:") and payload.get("source") != self.scope[len("source:"):]:
            return False
        words = tokens(payload.get("search", ""))
        if self.text_all and not set(tokens(self.text_all)) <= set(words):
            return False
        if self.phrase and not _contains(words, tokens(self.phrase)):
            return False
        if self.names is not None and not set(payload.get("names") or []) & {n.lower() for n in self.names}:
            return False
        if self.names_phrase and not _contains(tokens(payload.get("names_text", "")), tokens(self.names_phrase)):
            return False
        if self.has_props and not any((payload.get("props") or {}).get(k) not in (None, "") for k in self.has_props):
            return False
        if self.embedded is not None and bool(payload.get("embedded")) != self.embedded:
            return False
        return True


def _contains(words: list[str], phrase: list[str]) -> bool:
    if not phrase:
        return True
    n = len(phrase)
    return any(words[i:i + n] == phrase for i in range(len(words) - n + 1))


@dataclass
class VectorHit:
    node_id: uuid.UUID
    attr: str
    text: str
    score: float
