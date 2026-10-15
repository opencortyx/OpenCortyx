"""The built-in ontology: the Person / Organization templates.

These are product DEFINITIONS — which node and edge types exist out of the box and what they mean —
together with the idempotent upserts that install them. They lived under `retrieve/` for historical
reasons, which read as though the ontology were a retrieval concern; it is not. Ingestion,
extraction, the ontology editor and retrieval all read the same type registry.

Depends downward on `core` (sessions, NodeRepo) and `domain` (TypeRegistry). Nothing in `core`
imports this package: bootstrap is composed in `opencortyx.bootstrap`, above both.
"""
from opencortyx.ontology.organization import (
    ORG_DOMAINS, ORG_EDGES, ORG_HUB, ORG_NODES, seed_org_ontology,
)
from opencortyx.ontology.person import (
    PERSON_DOMAINS, PERSON_EDGES, PERSON_HUB, PERSON_NODES, seed_person_ontology,
)
from opencortyx.ontology.templates import (
    apply_template_rows, build_rows, materialize_template,
)

__all__ = [
    "ORG_DOMAINS", "ORG_EDGES", "ORG_HUB", "ORG_NODES", "seed_org_ontology",
    "PERSON_DOMAINS", "PERSON_EDGES", "PERSON_HUB", "PERSON_NODES", "seed_person_ontology",
    "apply_template_rows", "build_rows", "materialize_template",
]
