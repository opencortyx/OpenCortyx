"""Deterministic promotion of categorical attribute VALUES to their own nodes.

Why this exists rather than an LLM extractor:

A structured record is already an entity — the walker made it a node. Running an entity extractor
over it asks a model to re-derive structure from structure, and what comes back is the SCHEMA:
handed `"Vendor Northwind Systems. vendor_id=VEN-2; name=Northwind Systems; country=Ireland"`, an
extractor returns `vendor_id` and `VEN-2` as entities, which is a correct reading of a bad input.
Measured on this graph: 43 extracted entities, of which the majority were `owner_id`, `severity`,
`title`, `1290000`, `at_risk` — schema tokens competing for retrieval slots with the records they
came from.

What the records genuinely LACK is an anchor for their attribute VALUES. "Which project is red?"
cannot be answered by similarity, because `red` is one low-signal token inside a long record and
loses to name-like tokens every time. Promote `red` to a node with an edge from the project and the
question becomes a one-hop traversal.

So: values become nodes, deterministically. No model, so nothing can be hallucinated, and the same
input always produces the same graph.

WHICH attributes get promoted is decided from the SHAPE OF THE VALUE, never from a list of
interesting key names — a list is a thing to maintain, gets stale on the next dataset, and is the
hardcoding this module exists to avoid. A category is a short repeatable label (`red`, `Ireland`,
`high`, `at_risk`, `Chief Operating Officer`). An identifier, a measurement, a date, or a sentence
is not, and promoting one creates a node per record that anchors nothing.
"""
from __future__ import annotations

import re

#: Values longer than this are prose or titles, not category labels.
MAX_VALUE_CHARS = 40

#: A category with more words than this is a description.
MAX_VALUE_WORDS = 5

#: `PRJ-11`, `EMP-3`, `MS-1`, `C001`, `VEN-2` — an identifier's shape, independent of its key name.
_ID_SHAPED = re.compile(r"^[A-Za-z]{1,6}[-_]?\d+$")

#: ISO-ish dates and any purely numeric/decimal value.
_DATE_SHAPED = re.compile(r"^\d{4}-\d{2}-\d{2}")
_NUMERIC = re.compile(r"^-?[\d,]+(\.\d+)?$")

#: Written by the platform, never source data.
_INTERNAL_PREFIX = "__"

_BOOKKEEPING = frozenset({"created_date", "modified_date", "text", "key", "aliases",
                          "superseded", "valid_from", "valid_to"})


def is_identifier_key(key: str) -> bool:
    """`*_id` / `id` — the walker already turns these into REFERENCES edges, so promoting them
    would duplicate a relationship the graph has already drawn, as a node with no meaning."""
    k = (key or "").lower()
    return k == "id" or k.endswith("_id") or k.endswith("_ids")


def is_categorical_value(value) -> bool:
    """True when a value looks like a category label rather than an identifier, number, date or
    sentence. Shape only — the key's name is never consulted."""
    if not isinstance(value, str):
        return False                       # ints/floats/bools/lists are measurements or structure
    v = value.strip()
    if not v or len(v) > MAX_VALUE_CHARS:
        return False
    if len(v.split()) > MAX_VALUE_WORDS:
        return False
    if _NUMERIC.match(v) or _DATE_SHAPED.match(v) or _ID_SHAPED.match(v):
        return False
    if "@" in v or "://" in v:             # emails and URLs identify, they do not categorise
        return False
    return True


def categorical_attributes(properties: dict, *, label: str | None = None) -> dict:
    """The {attr: value} pairs of one node worth promoting.

    Excludes internals, bookkeeping, identifier keys, and the node's own label — promoting the label
    would create a duplicate of the node beside itself.
    """
    out = {}
    lab = (label or "").strip().lower()
    for k, v in (properties or {}).items():
        if not k or k.startswith(_INTERNAL_PREFIX) or k in _BOOKKEEPING:
            continue
        if is_identifier_key(k):
            continue
        if not is_categorical_value(v):
            continue
        if str(v).strip().lower() == lab:
            continue                       # the node already IS this value
        out[k] = str(v).strip()
    return out


def value_node_type(attr: str) -> str:
    """The node type a promoted value gets — derived from its attribute name, so `country` →
    `Country` and `based_in` → `BasedIn`. Derived rather than mapped: a mapping table would need an
    entry for every attribute any future dataset might carry."""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", attr or "") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Category"


def value_edge_type(attr: str) -> str:
    """`country` → `HAS_COUNTRY`. Same reasoning as `value_node_type`."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", attr or "").strip("_").upper()
    return f"HAS_{slug}" if slug else "HAS_CATEGORY"


def plan_promotions(nodes) -> list:
    """(node, attr, value, node_type, edge_type) for every promotion implied by `nodes`.

    Pure: takes node-like objects with `.properties` and `.label`, returns a plan. Nothing is
    written here, so the decision is testable without a database and reusable by any ingest path.
    """
    plan = []
    for n in nodes:
        props = getattr(n, "properties", None) or {}
        for attr, value in categorical_attributes(props, label=getattr(n, "label", None)).items():
            plan.append((n, attr, value, value_node_type(attr), value_edge_type(attr)))
    return plan
