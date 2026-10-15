"""Structured-data domain for the v2 context layer — the business-catalogue ontology, shared
write helpers, LLM derived-node generators, and the catalogue retriever.

The business node types are graph nodes that differ only in `type` and properties, and each
relationship is an edge whose endpoints must exist. A relationship between tables is a `JOINS_TO`
*edge*, never a node.

This module is provider-agnostic about HOW catalogue nodes get created — native ingestion lives
in `ingest_structured.py`, which reuses the helpers + generators here. (The legacy v1 mirror was
removed at the v2 cutover.)
"""
import json
import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert

from opencortyx.core.db import V2Session
from opencortyx.domain.ontology import PLATFORM_ORG, TYPE_KEY, TypeRegistry
from opencortyx.core.substrate import NodeRepo

_repo = NodeRepo()


STRUCTURED_NODE_TYPES = [
    ("Table", "A database table or tabular file: one business concept (Customer, Order, Product)."),
    ("Attribute", "A queryable column/field of a table with its semantic type, unit and query capabilities."),
    ("Segment", "A business cohort/classification over a table defined by a condition (e.g. Cold Customer)."),
    ("Computation", "A derived metric/KPI formula over attributes/tables (e.g. Gross Margin)."),
    ("Synonym", "A business term that resolves to a Table/Attribute/Segment/Computation for NL understanding."),
    ("TimeIntelligence", "A natural-language time phrase and its SQL resolution (e.g. 'last 30 days')."),
    ("EntityInstance", "A notable concrete instance of a table (e.g. the customer 'Accenture')."),
    ("Comment", "A human/system note or warning attached to a table or attribute."),
    ("DataQuality", "A known data-quality issue (nulls, duplicates) affecting a Table/Attribute, plus a workaround."),
    ("Lineage", "Where a Table/Attribute's data originates and the pipeline that loads it."),
    ("Report", "A report or dashboard built on tables + metrics, and how users refer to it informally."),
    ("UserProfile", "A user persona with defaults (time window, currency) + vocabulary for personalised NL→SQL."),
    ("InferenceRule", "A rule that derives a new classification/fact from data conditions."),
    ("Fact", "An atomic subject-predicate-object fact about an entity instance."),
    ("QueryPattern", "A reusable NL→SQL template (e.g. 'top [entity] by [metric] in [time]')."),
    ("ContextRule", "A business rule / domain-knowledge note that constrains how data must be queried."),
    ("Term", "A glossary term and its definition, added by a user."),
    ("BusinessContext", "Org/collection-level business context (mission, domain, standing assumptions); always-on items frame every answer."),
    ("Absence", "A marker that a relationship is missing (e.g. no foreign keys, no lineage) — shown so gaps are visible, not silent."),
    # NOTE: 'Summary' (table/column summaries) reuses the structural 'Summary' node type so it
    # participates in the existing Hierarchy retriever — it is not re-declared here.
]

# name, description, allowed_source_types, allowed_target_types
STRUCTURED_EDGE_TYPES = [
    ("HAS_ATTRIBUTE", "a table owns a property/column", ["Table"], ["Attribute"]),
    ("HAS_SEGMENT", "a table has a business segment", ["Table"], ["Segment"]),
    ("HAS_METRIC", "a Table/Attribute feeds a computation", ["Table", "Attribute"], ["Computation"]),
    ("JOINS_TO", "join / foreign-key path between two tables (join meta in properties)", ["Table"], ["Table"]),
    ("REFERENCES", "a property is a foreign key to another property", ["Attribute"], ["Attribute"]),
    ("DEPENDS_ON", "a segment/computation depends on a property", ["Segment", "Computation"], ["Attribute"]),
    ("OVERLAPS_WITH", "two segments of the same Table overlap", ["Segment"], ["Segment"]),
    ("HAS_SYNONYM", "a node is referred to by a synonym term", ["Table", "Attribute", "Segment", "Computation", "entity"], ["Synonym"]),
    ("DEFINES", "a glossary term defines/explains a node", ["Term"], ["Table", "Attribute", "entity", "Concept"]),
    ("HAS_INSTANCE", "a table has a notable concrete instance", ["Table"], ["EntityInstance"]),
    ("HAS_SUMMARY", "a Table/Attribute is described by a catalogue summary", ["Table", "Attribute"], ["Summary"]),
    ("HAS_COMMENT", "a note/warning is attached to a node", ["Table", "Attribute"], ["Comment"]),
    ("HAS_QUALITY_ISSUE", "a data-quality issue affects a node", ["Table", "Attribute"], ["DataQuality"]),
    ("HAS_LINEAGE", "lineage for a Table/Attribute", ["Table", "Attribute"], ["Lineage"]),
    ("HAS_TIME_INTELLIGENCE", "an org/Table exposes a time phrase", ["Organization", "Table"], ["TimeIntelligence"]),
    ("HAS_USER", "an org has a user persona", ["Organization"], ["UserProfile"]),
    ("HAS_REPORT", "a table appears in a report/dashboard", ["Table"], ["Report"]),
    ("USES_METRIC", "a report uses a computation", ["Report"], ["Computation"]),
    ("HAS_INFERENCE_RULE", "an inference rule applies to a table", ["Table"], ["InferenceRule"]),
    ("PRODUCES_SEGMENT", "an inference rule classifies a table into a segment", ["InferenceRule"], ["Segment"]),
    ("HAS_FACT", "a fact about an entity instance", ["EntityInstance"], ["Fact"]),
    ("ANSWERS_PATTERN", "a query pattern applies to a table", ["QueryPattern"], ["Table"]),
    ("APPLIES_TO", "a context rule constrains a Table/Attribute/Org", ["ContextRule"], ["Table", "Attribute", "Organization"]),
    # ── full relationship vocabulary (R1–R30): registered so all are valid, labelled & manually addable ──
    ("HAS_MANY", "parent Table has many child records (one-to-many FK)", ["Table"], ["Table"]),
    ("BELONGS_TO", "child Table belongs to one parent (reverse of HAS_MANY)", ["Table"], ["Table"]),
    ("HAS_ONE", "each record has exactly one related record", ["Table"], ["Table"]),
    ("MANY_TO_MANY", "two tables related through a bridge table", ["Table"], ["Table"]),
    ("IS_TYPE_OF", "a sub-entity is a filtered subset of a parent Table (inheritance)", ["Table"], ["Table"]),
    ("DERIVED_FROM", "a computed property/metric is calculated from source properties", ["Attribute", "Computation"], ["Attribute"]),
    ("USES_TIME_NODE", "a temporal property uses a time-intelligence pattern", ["Attribute"], ["TimeIntelligence"]),
    ("HAS_CONTEXT", "a business-context rule applies when querying this node", ["Table", "Computation", "Attribute"], ["ContextRule"]),
    ("SUPERSEDES", "a newer context overrides an older one", ["ContextRule"], ["ContextRule"]),
    ("QUERIES", "a query pattern accesses this entity", ["QueryPattern"], ["Table"]),
    ("SIMILAR_TO", "two query patterns are semantically similar", ["QueryPattern"], ["QueryPattern"]),
    ("FREQUENTLY_USES", "a user frequently uses this query pattern", ["UserProfile"], ["QueryPattern"]),
    ("FEATURED_IN", "a table is featured in a report", ["Table"], ["Report"]),
    ("SOURCED_FROM", "a property's data comes from a lineage/pipeline", ["Attribute"], ["Lineage"]),
    ("HAS_PERMISSION", "a user can access this Table at a permission level", ["UserProfile"], ["Table"]),
    ("HAS_ABSENCE", "marks a missing relationship (no FK / no lineage) so the gap is visible", ["Table", "Attribute"], ["Absence"]),
]


async def seed_structured_ontology():
    """Register the structured node/edge types (idempotent). Drives ontology-guided UI + curation."""
    rows = []
    for name, desc in STRUCTURED_NODE_TYPES:
        rows.append(dict(workspace_id=PLATFORM_ORG, name=name, kind="node", description=desc, source="system", status="active"))
    for name, desc, _src, _tgt in STRUCTURED_EDGE_TYPES:
        rows.append(dict(workspace_id=PLATFORM_ORG, name=name, kind="edge", description=desc, source="system", status="active"))
    async with V2Session() as db:
        for r in rows:
            stmt = pg_insert(TypeRegistry).values(**r).on_conflict_do_nothing(index_elements=TYPE_KEY)
            await db.execute(stmt)
        await db.commit()


def _det_id(workspace_id, kind: str, key: str) -> uuid.UUID:
    """Deterministic node id for GENERATED nodes (no v1 row) → idempotent re-indexing."""
    return uuid.uuid5(uuid.NAMESPACE_OID, f"v2struct:{workspace_id}:{kind}:{key}")


def _as_uuid(v):
    if isinstance(v, uuid.UUID):
        return v
    try:
        return uuid.UUID(str(v))
    except Exception:
        return None


def _clean(s):
    return (s or "").strip()


def _parse_json(raw: str):
    s = _clean(raw)
    if s.startswith("```"):
        s = s.split("```", 2)[1] if "```" in s[3:] else s[3:]
        if s[:4] == "json":
            s = s[4:]
        s = s.rsplit("```", 1)[0]
    try:
        return json.loads(s)
    except Exception:
        a, b = s.find("["), s.rfind("]")
        c, d = s.find("{"), s.rfind("}")
        for lo, hi in ((a, b), (c, d)):
            if lo >= 0 and hi > lo:
                try:
                    return json.loads(s[lo:hi + 1])
                except Exception:
                    continue
        return None


def _s(v) -> str:
    """Coerce an LLM-derived value to a string (small models sometimes return dicts/lists/numbers
    where we expect text — slicing those would raise 'unhashable type: slice')."""
    return v if isinstance(v, str) else ("" if v is None else str(v))


async def _uedge(*, workspace_id, source_id, target_id, type, properties=None, weight=1.0) -> bool:
    """Write an edge between two different nodes; True when it is new."""
    if not source_id or not target_id or str(source_id) == str(target_id):
        return False
    return await _repo.add_edge(workspace_id=workspace_id, source_id=source_id, target_id=target_id,
                                type=type, properties=properties or {}, weight=weight)


async def _put(*, node_id, workspace_id, type, label, properties, source_ref=None,
               collection_id=None, confidence=1.0, level=0, sensitivity=None):
    """Upsert one structured node. Its embeddings are written by the embedding pass."""
    label = _s(label) or type
    return await _repo.upsert_node(node_id=node_id, workspace_id=workspace_id, type=type, label=label[:300],
                                   properties=properties, source_ref=source_ref, sensitivity=sensitivity,
                                   collection_id=collection_id, confidence=confidence, level=level)


_STRUCTURED_TYPES = [
    "Table", "Attribute", "Segment", "Computation", "Synonym", "TimeIntelligence",
    "EntityInstance", "Comment", "DataQuality", "Lineage", "Report", "UserProfile",
    "InferenceRule", "Fact", "QueryPattern", "ContextRule",
]
