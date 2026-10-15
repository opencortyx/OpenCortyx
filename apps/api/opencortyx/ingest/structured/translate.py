"""Translate the Structured ENGINE's rule model → the `table_mappings` payload consumed by
`apply_custom_mappings` (ingest_structured). Mirror of semistructured/translate.py for the SQL/CSV
paradigm: columns (pk/fk/attribute) + PK/FK rules → {nodes, edges} in the shape the structured ingest
already reads (nodes carry `table.column` attribute refs; edges reference node list indices, same-table).

Pure function; no DB. Kept standalone so importing it never pulls the heavy ingest_structured module.
"""


def _col_ref(table: str, col: str) -> str:
    return f"{table}.{col}" if table and col else (col or "")


def _node_from_table(table: str) -> str:
    w = (table or "records").rsplit(".", 1)[-1]
    if w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w[:1].upper() + w[1:] if w else "Record"


_ROLE_MAP = {"pk": "identity", "fk": "fk", "node": "fk", "attribute": "", "ignore": "ignore"}


def polymorphic_rules(editor: dict) -> list:
    """`polymorphicFKRules` → the runtime type switches a row's FK needs.

    One FK column pointing at DIFFERENT entity types depending on a discriminator column
    (`owner_id` → Customer when `owner_type='customer'`, Supplier when `'supplier'`) cannot be said with
    a per-column `target_type`, because the target is decided per ROW. Emitted as a spec the structured
    row reader evaluates against each row, exactly like the semi-structured walker's discriminators.
    """
    out = []
    for p in editor.get("polymorphicFKRules") or []:
        if not isinstance(p, dict):
            continue
        col, disc = str(p.get("fkColumn") or "").strip(), str(p.get("discriminatorColumn") or "").strip()
        target = str(p.get("targetNodeLabel") or "").strip()
        if col and disc and target:
            out.append({"column": col, "discriminator": disc,
                        "value": str(p.get("matchValue") or "").strip(), "target_type": target})
    return out


def is_key_row_editor(editor: dict) -> bool:
    """Is this the Mapper's rule model rather than the old structured builder's?

    `columns` wins when both are present: a legacy mapping opened in the Mapper keeps its authored
    columns alongside whatever empty `keyRows` the new editor seeded, and the authored half is the
    one that describes what the mapping actually ingests.
    """
    editor = editor or {}
    return bool(editor.get("keyRows")) and not editor.get("columns")


def _kr_table(path: str) -> str:
    """`orders[].order_id` / `orders[]` -> `orders`."""
    head = str(path or "").split(".", 1)[0].strip()
    return head[:-2] if head.endswith("[]") else head


def _kr_column(node_path: str, attr_path: str) -> str:
    """The column an attribute row names, or "" when it is not a DIRECT child of that node row.

    Direct-child only, for the same reason the canvas counts attributes that way: `orders[].line[].sku`
    belongs to the nested rule, and hoisting it onto `orders` would invent a column the table has not
    got — which reads at ingest as a missing column rather than as a mapping mistake.
    """
    pre = (node_path + ".") if node_path else ""
    if not pre or not attr_path.startswith(pre):
        return ""
    rest = attr_path[len(pre):]
    return "" if (not rest or "." in rest or "[]" in rest) else rest


_KR_ROLE = {"id": "identity", "ignore": "ignore", "measure": "measure", "text": "text"}


def _key_row_tables(editor: dict) -> list:
    """`keyRows` + `edgeRules` -> one CARD PER DECLARED TABLE, in declaration order.

    The legacy shape could only say one table per mapping (`editor["table"]`); keyRows can name
    several, and `load_mapping_step1` already dedupes cards by table across mappings, so returning
    several here composes correctly instead of silently keeping only the first.
    """
    editor = editor or {}
    rows = editor.get("keyRows") or []
    node_rows = [r for r in rows if isinstance(r, dict) and r.get("kind") == "node" and r.get("node_type")]
    attr_rows = [r for r in rows if isinstance(r, dict) and r.get("kind") == "attribute"]

    fk_by_node: dict = {}
    for e in editor.get("edgeRules") or []:
        if not isinstance(e, dict):
            continue
        col = str(e.get("toKey") or e.get("fromKey") or "").strip()
        src = str(e.get("fromNode") or "").strip()
        if col and src:
            fk_by_node.setdefault(src, {})[col] = e

    cards, seen = [], set()
    for nr in node_rows:
        base = str(nr.get("path") or "")
        table = _kr_table(base)
        ntype = str(nr.get("node_type") or "").strip()
        if not table or not ntype or table in seen:
            continue
        seen.add(table)
        fks = fk_by_node.get(ntype, {})
        cols = []
        for a in attr_rows:
            col = _kr_column(base, str(a.get("path") or ""))
            if not col:
                continue
            # `sample` carries the SQL type for rows proposed from a live schema (see the Mapper's
            # proposeFromTables, which stores `c.type` there); for a hand-authored row it is a value.
            spec = {"name": col, "type": str(a.get("sample") or ""),
                    "attr_name": str(a.get("attr_name") or col)}
            fk = fks.get(col)
            if fk is not None:
                spec["role"] = "fk"
                spec["edge_type"] = str(fk.get("edgeLabel") or "").strip()
                spec["target_type"] = str(fk.get("toNode") or "").strip()
                if fk.get("direction") in ("in", "out"):
                    spec["edge_dir"] = fk["direction"]
            else:
                role = ""
                for cand in (a.get("becomes"), a.get("role")):
                    hit = _KR_ROLE.get(str(cand or "").strip().lower(), "")
                    if hit:
                        role = hit
                        break
                if role:
                    spec["role"] = role
            cols.append(spec)
        cards.append({"table": table, "node_type": ntype, "columns": cols})
    return cards


def _key_rows_to_table_mappings(editor: dict) -> dict:
    """`keyRows` + `edgeRules` -> the `{nodes, edges}` `apply_custom_mappings` consumes."""
    editor = editor or {}
    cards = _key_row_tables(editor)
    nodes: list = []
    index: dict = {}
    table_of: dict = {}

    def _ensure(ntype: str, attr=None) -> int:
        if ntype not in index:
            index[ntype] = len(nodes)
            nodes.append({"nodeType": ntype, "attributes": [attr] if attr else []})
        elif attr and not nodes[index[ntype]]["attributes"]:
            nodes[index[ntype]]["attributes"] = [attr]
        return index[ntype]

    for card in cards:
        t, ntype = card["table"], card["node_type"]
        attrs = [{"property": c.get("attr_name") or c["name"], "column": _col_ref(t, c["name"])}
                 for c in card["columns"] if c.get("role") != "ignore"]
        i = _ensure(ntype)
        # A node with no attributes has no table to read and `apply_custom_mappings` skips it — so a
        # table whose every column was ignored still gets one column back to be read by.
        nodes[i]["attributes"] = attrs or [
            {"property": "id",
             "column": _col_ref(t, card["columns"][0]["name"] if card["columns"] else "")}]
        table_of[ntype] = t

    edge_specs: list = []
    for e in editor.get("edgeRules") or []:
        if not isinstance(e, dict):
            continue
        src, tgt = str(e.get("fromNode") or "").strip(), str(e.get("toNode") or "").strip()
        if not (src and tgt) or src not in index:
            continue
        if tgt not in index:
            col = str(e.get("toKey") or e.get("fromKey") or "").strip()
            t = table_of.get(src, "")
            if not (col and t):
                continue
            _ensure(tgt, {"property": "id", "column": _col_ref(t, col)})
        spec = {"sourceRef": index[src], "targetRef": index[tgt],
                "edgeType": (str(e.get("edgeLabel") or "").strip() or "RELATES_TO")}
        jf = str(e.get("toKey") or e.get("fromKey") or "").strip()
        jt = str(e.get("toOn") or e.get("fromOn") or "").strip() or jf
        if jf and table_of.get(src) != table_of.get(tgt):
            spec["joinFrom"], spec["joinTo"] = jf, jt
        edge_specs.append(spec)

    return {"table_mappings": {"nodes": nodes, "edges": edge_specs},
            "table": cards[0]["table"] if cards else ""}


def _key_rows_declare_entities(editor: dict) -> bool:
    """A keyRow mapping declares entities as soon as it names a node type for a table's rows."""
    editor = editor or {}
    for r in editor.get("keyRows") or []:
        if not isinstance(r, dict):
            continue
        if r.get("kind") == "node" and str(r.get("node_type") or "").strip():
            return True
        if str(r.get("becomes") or r.get("role") or "").strip().lower() == "id":
            return True
    return bool(editor.get("edgeRules"))


def editor_to_table_cards(editor: dict) -> list:
    """Structured editor rule model → the per-table CARD format the base_schema step consumes
    (`[{table, node_type, columns:[{name, type, role, attr_name, edge_type, target_type}]}]`). Used at
    ingest to regenerate cards from a bound mapping (by-reference) — the mirror of the builder's JS
    onSelectMapping, kept in Python so the live mapping drives the graph."""
    editor = editor or {}
    if is_key_row_editor(editor):
        return _key_row_tables(editor)
    columns = editor.get("columns") or []
    pk_rules = editor.get("primaryKeyRules") or []
    table = editor.get("table") or (pk_rules[0].get("tableName") if pk_rules else "") or "records"
    node = (pk_rules[0].get("nodeLabel") if pk_rules else "") or _node_from_table(table)
    cols = []
    for c in columns:
        role = _ROLE_MAP.get(c.get("mappingRole"), "")
        col = {"name": c.get("columnName"), "type": c.get("sqlType") or "",
               "attr_name": c.get("propertyNameOrEdge") or c.get("columnName")}
        if role:                                   # "" → let _col_roles infer the reading role
            col["role"] = role
        if role == "fk":
            col["edge_type"] = c.get("propertyNameOrEdge") or ""
            col["target_type"] = c.get("fkTargetNode") or ""
            # 'in' flips the edge (value node → row); the ingest honours this per column.
            if c.get("edgeDirection") in ("in", "out"):
                col["edge_dir"] = c["edgeDirection"]
        cols.append(col)
    card = {"table": table, "node_type": node, "columns": cols}
    poly = polymorphic_rules(editor)
    if poly:
        card["polymorphic"] = poly
    fs = editor.get("file_settings") or {}
    if fs:
        card["file_settings"] = fs
    return [card]


_ENTITY_ROLES = ("pk", "fk", "node")


def mapping_declares_entities(editor: dict) -> bool:
    """Does this mapping describe ENTITIES (one node per row) rather than just a catalogue?

    This is the question `reading_mode` answers, and until now a bound mapping was never asked it: the
    cards were regenerated from the mapping while the mode came from the pipeline's Step-1 card, whose
    default is 'catalog'. So a mapping declaring `Customer -OWNS_ACCOUNT-> Account` produced Table and
    Property nodes and none of what it declared — the schema of the table, not the graph the author drew.

    An author who named a node label for the rows, or gave any column a pk/fk/node role, has declared
    entities. A mapping that did neither is a catalogue and stays one."""
    editor = editor or {}
    if is_key_row_editor(editor):
        return _key_rows_declare_entities(editor)
    for pk in editor.get("primaryKeyRules") or []:
        if str(pk.get("nodeLabel") or "").strip():
            return True
    for c in editor.get("columns") or []:
        if str(c.get("mappingRole") or "").strip().lower() in _ENTITY_ROLES:
            return True
    return bool(editor.get("foreignKeyRules"))


def translate_structured_editor(editor: dict, name: str = "") -> dict:
    """Structured editor rule model → {"table_mappings": {"nodes": [...], "edges": [...]}, "table": t}.

    Nodes are index-aligned so edges can reference them by position (sourceRef/targetRef), the format
    apply_custom_mappings uses. FK target nodes are built from the FK column of the SAME table, so the
    per-row same-table pairing the backend requires holds."""
    editor = editor or {}
    if is_key_row_editor(editor):
        return _key_rows_to_table_mappings(editor)
    columns = editor.get("columns") or []
    pk_rules = editor.get("primaryKeyRules") or []
    fk_rules = editor.get("foreignKeyRules") or []
    table = editor.get("table") or (pk_rules[0].get("tableName") if pk_rules else "") or ""

    main_node = ((pk_rules[0].get("nodeLabel") if pk_rules else "")
                 or next((c.get("targetNode") for c in columns if c.get("mappingRole") == "pk"), "")
                 or next((c.get("targetNode") for c in columns if c.get("targetNode")), "")
                 or "Record")

    node_specs: list = []
    node_index: dict = {}

    def _ensure_node(ntype: str, first_attr: dict | None = None) -> int:
        if ntype not in node_index:
            node_index[ntype] = len(node_specs)
            node_specs.append({"nodeType": ntype, "attributes": [first_attr] if first_attr else []})
        return node_index[ntype]

    # main node = pk + attribute columns that target it
    main_attrs = []
    for c in columns:
        if c.get("mappingRole") in ("pk", "attribute") and (c.get("targetNode") or main_node) == main_node:
            main_attrs.append({"property": c.get("propertyNameOrEdge") or c.get("columnName"),
                               "column": _col_ref(table, c.get("columnName"))})
    _ensure_node(main_node)
    node_specs[node_index[main_node]]["attributes"] = main_attrs or [
        {"property": "id", "column": _col_ref(table, (pk_rules[0].get("pkColumns") or [""])[0] if pk_rules else "")}]

    edge_specs: list = []

    def _add_edge(tgt: str, edge: str, col: str):
        if not (tgt and col):
            return
        ti = _ensure_node(tgt, {"property": "id", "column": _col_ref(table, col)})
        edge_specs.append({"sourceRef": node_index[main_node], "targetRef": ti,
                           "edgeType": (edge or "RELATES_TO").strip() or "RELATES_TO"})

    # Node-producing columns → their own node + an edge from the row. `node` and `fk` are the same
    # write (value → node, deduped, linked); they differ only in what the author meant by it.
    for c in columns:
        if c.get("mappingRole") in ("fk", "node"):
            _add_edge(c.get("fkTargetNode") or "Related", c.get("propertyNameOrEdge") or "RELATES_TO",
                      c.get("columnName"))
    # explicit FK rules (may name columns not in the column list)
    for fk in fk_rules:
        _add_edge(fk.get("targetNode"), fk.get("edgeLabel"), fk.get("sourceColumn"))

    for j in editor.get("junctionRules") or []:
        a_label, b_label = (j.get("nodeLabelA") or "").strip(), (j.get("nodeLabelB") or "").strip()
        a_col, b_col = (j.get("fkColumnA") or "").strip(), (j.get("fkColumnB") or "").strip()
        if not (a_label and b_label and a_col and b_col):
            continue
        jt = (j.get("junctionTableName") or table or "").strip()
        ai = _ensure_node(a_label, {"property": "id", "column": _col_ref(jt, a_col)})
        bi = _ensure_node(b_label, {"property": "id", "column": _col_ref(jt, b_col)})
        edge = {"sourceRef": ai, "targetRef": bi,
                "edgeType": (j.get("relationshipLabel") or "RELATES_TO").strip() or "RELATES_TO"}
        # Columns of the junction row that describe the RELATIONSHIP (quantity, unit_price) belong on
        # the edge, not on either endpoint — that is the whole reason the junction row exists.
        attrs = [c for c in (j.get("edgeAttributeColumns") or []) if str(c).strip()]
        if attrs:
            edge["attributes"] = [{"property": c, "column": _col_ref(jt, c)} for c in attrs]
        edge_specs.append(edge)

    return {"table_mappings": {"nodes": node_specs, "edges": edge_specs}, "table": table}
