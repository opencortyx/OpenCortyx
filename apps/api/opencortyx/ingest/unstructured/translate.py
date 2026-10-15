"""Translate the Unstructured (document) ENGINE's rule model → the Step-1 reading rules that
`ingest_document` actually runs. Third sibling of `semistructured/translate.py` (JSON) and
`structured_map_translate.py` (SQL/CSV).

Why this exists: it was MISSING. `_prepare_mapping_payload` translated the semi-structured and
structured engines' rule models and passed the unstructured one through untouched, and the only thing
the document ingest ever read back out of an unstructured mapping was `editor.chunking`. So the node
rules, attribute rules and edge rules an author drew on that page — the picture the canvas shows them —
were pure UI state: every document came out as a `Chunk` with `text`/`ordinal` and nothing else, no
matter what the mapping declared. That is the document half of "the graph doesn't obey my mapping".

What a document pipeline CAN honour, and therefore what this translates:
  • the ROOT node's type           → `root_type`   (the asset is RETYPED to it, keeping `__base_type`)
  • the SECTION tier               → `section_type` + `section_edge` + `chunk_edge`
  • the CHUNK NODE's type          → `node_type`
  • the attributes attached to it  → `chunk_attrs` (verbatim / computed / regex / llm)
  • edges LEAVING the chunk        → `edges` (promote a chunk attribute to its own node + link)

The section tier is real, not invented: the `markdown_heading` strategy already discovers a `heading`
and `level` for every chunk (`parsing.chunk_document`), which is exactly one Section node per heading
with its chunks beneath it. It is emitted ONLY when the mapping declares a section node — a mapping
that declares none keeps the flat `Source -CONTAINS-> Chunk` shape it always had.

What it still does not translate: `TableEntity` / `ImageEntity` node rules. `parsing.parse_content`
returns raw text only — there are no table or image ELEMENTS to build them from — and declaring a node
type the pipeline cannot fill is the exact failure this module exists to end. The conformance report
names them as unfulfilled rather than pretending.

Pure functions; no DB.
"""

_CONTENT_ROLES = ("content", "chunk", "text")
_CONTENT_SOURCE_HINTS = ("chunk", "paragraph", "text", "passage")
_ROOT_ROLES = ("root entity", "root", "document", "asset")
_ROOT_SOURCE_HINTS = ("root", "file", "document")
_SECTION_ROLES = ("structural", "section", "layout")
_SECTION_SOURCE_HINTS = ("section", "heading", "layout")
_TABLE_ROLES = ("structured", "table")
_TABLE_SOURCE_HINTS = ("table", "grid")
_IMAGE_ROLES = ("media", "image", "vision")
_IMAGE_SOURCE_HINTS = ("image", "figure", "chart", "picture")

_VERBATIM = "verbatim"


def _tier(node_rules: list, roles: tuple, hints: tuple, fallback: str = "") -> str:
    """The node label the author gave one tier of the document, as declared on the canvas."""
    for r in node_rules or []:
        if str(r.get("nodeType") or "").strip().lower() in roles and r.get("nodeLabel"):
            return str(r["nodeLabel"]).strip()
    for r in node_rules or []:
        src = str(r.get("elementSource") or "").lower()
        if any(h in src for h in hints) and r.get("nodeLabel"):
            return str(r["nodeLabel"]).strip()
    return fallback


def _content_node(node_rules: list, fallback: str = "") -> str:
    return _tier(node_rules, _CONTENT_ROLES, _CONTENT_SOURCE_HINTS, fallback)


def _edge_between(edge_rules: list, src: str, dst: str) -> str:
    """The label the author gave the edge from one tier to another."""
    if not (src and dst):
        return ""
    for e in edge_rules or []:
        if (str(e.get("fromNode") or "").strip() == src
                and str(e.get("toNode") or "").strip() == dst
                and str(e.get("edgeLabel") or "").strip()):
            return str(e["edgeLabel"]).strip()
    return ""


def _chunk_attrs(editor: dict, chunk_label: str) -> list:
    """Attribute + doc-key rules attached to the chunk node → `chunk_attrs` specs.

    `from` carries the SOURCE field when it differs from the property name (file_size → size); without
    it a renamed attribute would look for its value under the new name and always come back empty.
    """
    out: list = []
    seen: set = set()

    def _add(name, source):
        name = str(name or "").strip()
        if not name or name in seen:
            return
        seen.add(name)
        spec = {"name": name, "mechanism": _VERBATIM}
        source = str(source or "").strip()
        if source and source != name:
            spec["from"] = source
        out.append(spec)

    for a in editor.get("attributeRules") or []:
        if str(a.get("attachToNode") or "").strip() == chunk_label:
            _add(a.get("propertyName") or a.get("sourceField"), a.get("sourceField"))
    for k in editor.get("docKeys") or []:
        if str(k.get("attachToNode") or "").strip() == chunk_label:
            _add(k.get("propertyName") or k.get("keyPath"), k.get("keyPath"))
    return out


def _chunk_edges(editor: dict, chunk_label: str, attrs: list) -> list:
    """Edges the chunk itself draws. `_draw_chunk_edges` promotes ONE of the chunk's attributes to a
    node of the target type and links to it, so an edge is translatable only when the target has an
    attribute rule naming a field the chunk actually carries. Anything else (Document → Section,
    Section → Chunk) is parenthood the flat chunker does not build, and is left out rather than
    declared and silently unfulfilled."""
    have = {a["name"] for a in attrs}
    by_node: dict = {}
    for a in editor.get("attributeRules") or []:
        node = str(a.get("attachToNode") or "").strip()
        field = str(a.get("propertyName") or a.get("sourceField") or "").strip()
        if node and field:
            by_node.setdefault(node, field)
    out: list = []
    for e in editor.get("edgeRules") or []:
        if str(e.get("fromNode") or "").strip() != chunk_label:
            continue
        target = str(e.get("toNode") or "").strip()
        rel = str(e.get("edgeLabel") or "").strip()
        via = by_node.get(target)
        if target and rel and via and via in have:
            out.append({"rel": rel, "target": target, "from_attr": via})
    return out


_ELEMENT_ROLES = {"layout": "sections", "embedding": "chunks",
                  "table_extraction": "tables", "vision": "images"}


def _elements(editor: dict) -> dict:
    """`elements` → {capability: {enabled, model_id}}. The engine's switches for what to pull OUT of a
    document. Nothing read them before, so turning "Embedded Tables & Grids" off changed nothing —
    and turning it ON changed nothing either, because no table extraction existed. Both halves are
    real now (see ingest_docs._document_elements)."""
    out = {}
    for e in editor.get("elements") or []:
        if not isinstance(e, dict):
            continue
        cap = _ELEMENT_ROLES.get(str(e.get("role") or "").strip())
        if cap:
            out[cap] = {"enabled": bool(e.get("enabled")), "model_id": e.get("model_id") or None}
    return out


def _filters(editor: dict) -> dict:
    """`filterRules` → the chunk-level filters `ingest_document` applies while splitting."""
    out: dict = {}
    for f in editor.get("filterRules") or []:
        if not isinstance(f, dict):
            continue
        cond = str(f.get("condition") or "").lower()
        if "empty" in cond:
            out["skip_empty"] = True
        elif "header" in cond or "footer" in cond:
            out["skip_header_footer"] = True
        elif "pattern" in cond and str(f.get("patternValue") or "").strip():
            out.setdefault("skip_patterns", []).append(str(f["patternValue"]).strip())
    return out


def _identity(editor: dict, root: str) -> dict:
    """`compositeKeyRules` → how the ROOT document node is IDENTIFIED. `UPSERT` on (file_name,
    creation_date) means two uploads of the same file under different names are ONE document — the
    thing a filename-derived id can never express."""
    for c in editor.get("compositeKeyRules") or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("targetNodeLabel") or "").strip() not in (root, "Document", ""):
            continue
        fields = [str(x).strip() for x in (c.get("compositeKeyFields") or []) if str(x).strip()]
        if fields:
            return {"fields": fields, "strategy": str(c.get("mergeStrategy") or "UPSERT").upper()}
    return {}


def _doc_links(editor: dict) -> list:
    """`docLinkRules` → chunk-edge specs. A citation field on a chunk becomes a node of the target doc
    type plus the declared relationship — exactly the shape `_draw_chunk_edges` already promotes."""
    out = []
    for d in editor.get("docLinkRules") or []:
        if not isinstance(d, dict):
            continue
        src = str(d.get("citationSource") or "").strip()
        rel = str(d.get("relationshipLabel") or "").strip()
        tgt = str(d.get("targetDocType") or "").strip()
        if src and rel and tgt:
            out.append({"rel": rel, "target": tgt, "from_attr": src})
    return out


def _pattern_roles(editor: dict) -> list:
    """`patternRules` → {match, role}. "IF the key contains `debug` → skip" applied to chunk attrs."""
    out = []
    for p in editor.get("patternRules") or []:
        if not isinstance(p, dict):
            continue
        val = str(p.get("patternValue") or "").strip()
        role = str(p.get("actionRole") or "").strip().lower()
        if val and role:
            out.append({"match": val.lower(), "role": role})
    return out


def _doc_edge_attrs(editor: dict) -> dict:
    """`edgeAttributeRules` → {EDGE_TYPE: [{prop, from_field}]} — properties on the structural edge."""
    out: dict = {}
    for r in editor.get("edgeAttributeRules") or []:
        if not isinstance(r, dict):
            continue
        etype = str(r.get("attachToEdge") or "").strip()
        src = str(r.get("sourceField") or "").strip()
        if etype and src:
            out.setdefault(etype, []).append(
                {"prop": str(r.get("propertyName") or "").strip() or src, "from_field": src})
    return out


def _kr_leaf(node_path: str, attr_path: str) -> str:
    """The field an attribute row names, or "" when it is not a DIRECT child of that node row."""
    pre = (node_path + ".") if node_path else ""
    if not pre or not attr_path.startswith(pre):
        return ""
    rest = attr_path[len(pre):]
    return "" if (not rest or "." in rest or "[]" in rest) else rest


def with_key_rows(editor: dict) -> dict:
    """The Mapper's `keyRows` expressed in the vocabulary this module already reads.

    The Mapper (one builder for all three paradigms) writes `keyRows`; every reader below wants
    `nodeRules` (`{nodeType, nodeLabel, elementSource}`) and `attributeRules`
    (`{attachToNode, propertyName, sourceField}`). Nothing bridged the two, so a document mapping
    authored in the Mapper reached `step1` carrying only its chunking settings — the node types and
    attributes its author drew were dropped in silence.

    The declared TYPE NAME is put in all three slots on purpose. `_tier` matches a role word exactly
    and then falls back to substring hints on `elementSource`, so a type called `Document` resolves
    as the root by role, `TextChunk` as the content tier by hint, and a name matching neither simply
    does not claim a tier — the same "left out rather than declared and silently unfulfilled" rule
    `_chunk_edges` already follows.

    Non-destructive: an editor that already carries authored `nodeRules` is returned untouched, so
    a mapping from the retired document builder keeps compiling exactly as it does today.
    """
    editor = editor or {}
    rows = editor.get("keyRows") or []
    if not rows or editor.get("nodeRules") or editor.get("attributeRules"):
        return editor

    node_rows = [r for r in rows if isinstance(r, dict) and r.get("kind") == "node" and r.get("node_type")]
    attr_rows = [r for r in rows if isinstance(r, dict) and r.get("kind") == "attribute"]
    if not node_rows:
        return editor

    node_rules, attribute_rules, seen = [], [], set()
    for nr in node_rows:
        label = str(nr.get("node_type") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        node_rules.append({"nodeType": label.lower(), "nodeLabel": label, "elementSource": label})
        base = str(nr.get("path") or "")
        for a in attr_rows:
            field = _kr_leaf(base, str(a.get("path") or ""))
            if not field:
                continue
            if str(a.get("becomes") or a.get("role") or "").strip().lower() == "ignore":
                continue
            attribute_rules.append({"attachToNode": label,
                                    "propertyName": str(a.get("attr_name") or field),
                                    "sourceField": field})

    out = dict(editor)
    out["nodeRules"] = node_rules
    out["attributeRules"] = attribute_rules
    return out


def translate_document_editor(editor: dict, name: str = "") -> dict:
    """Editor rule model → `{"step1": {...}}` — merged into the Step-1 config at ingest by
    `ingest_docs.resolve_step1_chunk_config`.

    `editor.chunking.node_type` is the reading card's own "Chunk node" field. It wins when the author
    actually named something there; while it still holds the default the canvas's content node wins,
    so the two places that name one node stop contradicting each other (the card said `Chunk`, the
    canvas said `TextChunk`, and the graph got `Chunk`)."""
    editor = with_key_rows(editor or {})
    nodes = editor.get("nodeRules") or []
    edge_rules = editor.get("edgeRules") or []
    chunking = editor.get("chunking") or {}

    declared = str(chunking.get("node_type") or chunking.get("node") or "").strip()
    canvas = _content_node(nodes)
    node_type = declared if declared and declared != "Chunk" else (canvas or declared or "Chunk")
    chunk_label = canvas or node_type

    root = _tier(nodes, _ROOT_ROLES, _ROOT_SOURCE_HINTS)
    section = _tier(nodes, _SECTION_ROLES, _SECTION_SOURCE_HINTS)

    hier_section_edge = hier_chunk_edge = ""
    for h in editor.get("hierarchyRules") or []:
        if not isinstance(h, dict):
            continue
        label = str(h.get("nodeLabel") or "").strip()
        level = str(h.get("sectionLevel") or "").lower()
        edge = str(h.get("parentEdgeLabel") or "").strip()
        if not label:
            continue
        if any(w in level for w in ("heading", "section", "h1", "h2", "h3")):
            section, hier_section_edge = label, edge or hier_section_edge
        elif any(w in level for w in ("paragraph", "chunk", "text", "passage")):
            chunk_label = label
            hier_chunk_edge = edge or hier_chunk_edge
            if not declared or declared == "Chunk":
                node_type = label

    if section == chunk_label or section == root:
        section = ""                                # one tier declared twice is not a hierarchy

    step1: dict = {"node_type": node_type}
    if root:
        step1["root_type"] = root
    if section:
        # A section tier is only reachable when the chunker actually reports headings; the ingest
        # falls back to a flat document when it doesn't, so the declared labels travel either way.
        step1["section_type"] = section
        step1["section_edge"] = (hier_section_edge
                                 or _edge_between(edge_rules, root, section) or "HAS_SECTION")
        step1["chunk_edge"] = (hier_chunk_edge
                               or _edge_between(edge_rules, section, chunk_label) or "HAS_CHUNK")
    else:
        direct = _edge_between(edge_rules, root, chunk_label)
        if direct:
            step1["chunk_edge"] = direct

    # PATTERN RULES prune the attribute list before anything else reads it: a key the author marked
    # skip/ignore must never reach the chunk, and dropping it here means one place decides.
    pattern_roles = _pattern_roles(editor)
    attrs = _chunk_attrs(editor, chunk_label)
    if pattern_roles:
        def _dropped(nm: str) -> bool:
            low = nm.lower()
            return any(r["role"] in ("skip", "ignore") and r["match"] in low for r in pattern_roles)
        attrs = [a for a in attrs if not _dropped(a["name"])]
        step1["pattern_roles"] = pattern_roles

    edges = _chunk_edges(editor, chunk_label, attrs) if attrs else []
    edges += _doc_links(editor)              # citation fields → links to other documents
    if attrs:
        step1["chunk_attrs"] = attrs
    if edges:
        step1["edges"] = edges

    for key, tier, edge_key in (("table_type", _tier(nodes, _TABLE_ROLES, _TABLE_SOURCE_HINTS), "table_edge"),
                                ("image_type", _tier(nodes, _IMAGE_ROLES, _IMAGE_SOURCE_HINTS), "image_edge")):
        if tier:
            step1[key] = tier
            declared = _edge_between(edge_rules, root, tier)
            if declared:
                step1[edge_key] = declared

    for key, val in (("elements", _elements(editor)),
                     ("chunk_filters", _filters(editor)),
                     ("identity", _identity(editor, root or "Document")),
                     ("edge_attrs", _doc_edge_attrs(editor))):
        if val:
            step1[key] = val
    return {"step1": step1}
