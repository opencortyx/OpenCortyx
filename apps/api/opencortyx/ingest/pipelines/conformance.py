"""Does the graph match the mapping? — one answer, for all three paradigms.

A schema mapping is a CONTRACT: the node types and relationships it declares are what the ingestion
must produce, and nothing else should appear beside them. Until now nobody could check that. The
verify panel compared declared rules to produced types with a fuzzy alias matcher — any produced type
containing "chunk" satisfied a `TextChunk` rule — so it reported "every node and edge rule you declared
came out of this one" while the graph actually held `Chunk`, which is precisely the drift it existed to
catch. And it only ever looked one way: a graph full of types the mapping never mentioned passed.

So: exact names, both directions, and a third bucket for the edges the PLATFORM writes no matter what
a mapping says. Naming that third bucket is what makes the report honest — a `CREATED_ON` edge to a
Date node is not the mapping being disobeyed, and calling it a violation would train people to ignore
the report.

Pure functions except `asset_shape`, which reads one asset's subgraph.
"""

PLATFORM_EDGES = frozenset({
    "CREATED_ON", "MODIFIED_ON",            # temporal anchoring (repo.link_date)
    "HAS_RECORD", "CONTAINS", "NEXT",       # provenance + reading order
    "EXTRACTED_FROM", "REFERENCES",         # extraction lineage / cross-record id refs
})

#: Node types the platform writes regardless of the mapping.
PLATFORM_NODES = frozenset({"Source", "Date"})

STRICT_SUPPRESSED = frozenset({"NEXT", "REFERENCES"})


def platform_reading_types() -> dict:
    """The vocabulary the document READER writes whatever a mapping names — its default chunk shape
    and the layout reader's element kinds. A mapping that repeats these is describing how its file is
    read, not declaring new ontology; publishing them as reviewable candidates asked a person to
    approve `Chunk`, `NEXT` and `SectionHeader`."""
    from opencortyx.ingest.formats.unstructured.docling.schemas import layout_element_types
    from opencortyx.ontology.extractors import READING_SHAPES
    nodes = {n["type"] for shape in READING_SHAPES.values() for n in shape.get("nodes") or []}
    edges = {e["type"] for shape in READING_SHAPES.values() for e in shape.get("edges") or []}
    return {"nodes": PLATFORM_NODES | nodes | layout_element_types(),
            "edges": PLATFORM_EDGES | edges | {"CONTAINS_CHUNK", "HAS_SECTION"}}


def _clean(names) -> set:
    return {str(n).strip() for n in (names or []) if str(n or "").strip()}


# ── what a mapping DECLARES, per paradigm ───────────────────────────────────────────────────────
def declared_shape(mapping) -> dict:
    """`{"nodes": {...}, "edges": {...}}` — the vocabulary this mapping promises, by exact name.

    Read from the ENGINE's rule model (`payload.editor`), because that is the picture the author drew
    and the thing they compare the graph against. The translated payload is derived from it.
    """
    payload = (mapping or {}).get("payload") or {}
    editor = payload.get("editor") or {}
    paradigm = (mapping or {}).get("paradigm") or ""
    nodes: set = set()
    edges: set = set()

    if paradigm == "semistructured":
        for r in editor.get("keyRows") or []:
            if r.get("kind") == "node" and (r.get("decision") or r.get("becomes")) not in ("skip", "ignore"):
                nodes |= _clean([r.get("node_type")])
                edges |= _clean([r.get("edge_type")])
            elif r.get("kind") == "attribute" and r.get("becomes") == "node":
                nodes |= _clean([r.get("node_type")])
        for z in editor.get("zipCorrelations") or []:
            nodes |= _clean([z.get("nodeType") or z.get("node_type")])
        for e in editor.get("edgeRules") or []:
            edges |= _clean([e.get("edgeLabel")])
            nodes |= _clean([e.get("fromNode"), e.get("toNode")])

    elif paradigm == "structured":
        for c in editor.get("columns") or []:
            nodes |= _clean([c.get("targetNode")])
            if str(c.get("mappingRole") or "").lower() in ("fk", "node"):
                nodes |= _clean([c.get("fkTargetNode")])
                edges |= _clean([c.get("propertyNameOrEdge")])
        for pk in editor.get("primaryKeyRules") or []:
            nodes |= _clean([pk.get("nodeLabel")])
        for fk in editor.get("foreignKeyRules") or []:
            nodes |= _clean([fk.get("targetNode")])
            edges |= _clean([fk.get("edgeLabel")])
        for j in editor.get("junctionRules") or []:
            nodes |= _clean([j.get("nodeLabelA"), j.get("nodeLabelB")])
            edges |= _clean([j.get("edgeLabel")])

    elif paradigm == "unstructured":
        for n in editor.get("nodeRules") or []:
            nodes |= _clean([n.get("nodeLabel")])
        for e in editor.get("edgeRules") or []:
            edges |= _clean([e.get("edgeLabel")])
        for h in editor.get("hierarchyRules") or []:
            edges |= _clean([h.get("parentEdgeLabel")])

    if not nodes and not edges:
        for st in payload.get("structures") or []:
            for k in st.get("keys") or []:
                if k.get("kind") == "node":
                    nodes |= _clean([k.get("node_type")])
                    edges |= _clean([k.get("edge_type")])
            for er in st.get("edge_rules") or []:
                edges |= _clean([er.get("type")])
                nodes |= _clean([(er.get("from") or {}).get("type"), (er.get("to") or {}).get("type")])
    edges = {str(e).strip().upper() for e in edges if str(e).strip()}
    return {"nodes": nodes, "edges": edges}


# ── what an ingested asset actually PRODUCED ────────────────────────────────────────────────────
def produced_shape(graph: dict) -> dict:
    """`{"nodes", "edges"}` from a `{nodes:[{type}], edges:[{label|type}]}` subgraph."""
    nodes = _clean(n.get("type") for n in (graph or {}).get("nodes") or [])
    edges = _clean((e.get("label") or e.get("type")) for e in (graph or {}).get("edges") or [])
    return {"nodes": nodes, "edges": edges}


def conformance(mapping, graph: dict) -> dict:
    """Compare, exactly, in both directions.

      missing   — declared and NOT produced. Either the data has no such content, or the rule is not
                  being honoured. This is what "strictly" is about.
      extra     — produced and NOT declared, excluding platform writes. A mapping that is authoritative
                  should have none.
      platform  — produced, not declared, and written by the platform itself. Explained, not a fault.
      conformant— nothing missing and nothing extra.

    An UNFULFILLABLE declaration (a node type the pipeline has no way to fill, e.g. `TableEntity` from
    a parser that returns raw text) shows up in `missing` — which is the honest place for it. It is not
    silently forgiven, because forgiving it is how a mapping quietly stops meaning anything.
    """
    dec = declared_shape(mapping)
    prod = produced_shape(graph)
    extra_nodes = prod["nodes"] - dec["nodes"]
    extra_edges = prod["edges"] - dec["edges"]
    return {
        "declared": {"nodes": sorted(dec["nodes"]), "edges": sorted(dec["edges"])},
        "produced": {"nodes": sorted(prod["nodes"]), "edges": sorted(prod["edges"])},
        "missing": {"nodes": sorted(dec["nodes"] - prod["nodes"]),
                    "edges": sorted(dec["edges"] - prod["edges"])},
        "extra": {"nodes": sorted(extra_nodes - PLATFORM_NODES),
                  "edges": sorted(extra_edges - PLATFORM_EDGES)},
        "platform": {"nodes": sorted(extra_nodes & PLATFORM_NODES),
                     "edges": sorted(extra_edges & PLATFORM_EDGES)},
        "conformant": not (dec["nodes"] - prod["nodes"]) and not (dec["edges"] - prod["edges"])
                      and not (extra_nodes - PLATFORM_NODES) and not (extra_edges - PLATFORM_EDGES),
    }


def strict_mode(mapping) -> bool:
    """Is this mapping AUTHORITATIVE — its declared vocabulary is the whole graph?

    Off by default: turning it on retroactively would silently drop `NEXT` edges that existing
    retrieval and UI already read. Authored on the mapping's reading rules, next to the other
    "how to read this source" knobs, because that is what it is."""
    payload = (mapping or {}).get("payload") or {}
    if payload.get("strict") is not None:
        return bool(payload["strict"])
    reading = (payload.get("editor") or {}).get("reading") or {}
    return bool(reading.get("strict"))


def strict_suppressed(strict: bool, declared_edges) -> frozenset:
    """Edge types a strict run must NOT write — the suppressible platform edges the mapping did not
    declare. A mapping that declares `NEXT` still gets it."""
    if not strict:
        return frozenset()
    return frozenset(STRICT_SUPPRESSED - _clean(declared_edges))
