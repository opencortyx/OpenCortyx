"""Reading extracted entities: what marks a node as naming a thing.

Extraction only writes nodes (`templates`); a node that names a thing carries `__identity`. Which
nodes are the same thing is decided after the asset, by the resolution step (`opencortyx.resolution`).
"""


def is_identity(node) -> bool:
    """A node this resolver minted, known by its `__identity` stamp rather than by a type name: the
    node's type is whatever the extractor card declared."""
    return bool((node.properties or {}).get("__identity"))


ENTITY_FIELD_TO_COLUMN = {"name": "label", "type": "entity_type"}
ENTITY_COLUMN_TO_FIELD = {v: k for k, v in ENTITY_FIELD_TO_COLUMN.items()}
