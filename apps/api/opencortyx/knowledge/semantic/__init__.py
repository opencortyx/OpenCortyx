"""The semantic layer: what the workspace's data MEANS - object types, their keys and properties,
and the links between them - over records that stay in Delta.

    detector.py   reads the data (profiles, uniqueness, value overlap) and proposes the layer
    store.py      the layer where meaning already lives: types in the type registry, where they are
                  in the Mapper's mappings (their `knowledge` section)
    views.py      each object type as ONE logical table over every asset bound to it
    check.py      a mapping validated and previewed against the data

Modelled on what the large data platforms do (docs/knowledge-layer.md, "Semantic layer"):

  - an object type is bound to the datasets that hold it, with an identity key and a column ->
    property mapping (Palantir object types, Microsoft Fabric IQ bindings);
  - a link type comes from keys: the values of one field all found among another type's keys
    (inclusion dependencies, the basis of automatic foreign-key discovery);
  - records stay where they are; a question reads them through the type's logical table.

Nothing here knows any particular field name. Keys, links, value sets and dates are read from the
values; where the data leaves a choice open (which of two unique fields identifies a record, what to
call a type) the workspace's model picks among the candidates the data offers.
"""
from opencortyx.knowledge.semantic.store import Layer, LinkType, ObjectType, Property, load

__all__ = ["Layer", "LinkType", "ObjectType", "Property", "load"]
