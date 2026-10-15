"""Rehydrate an EDITOR rule model from a saved mapping payload.

Why this exists: the mapping store holds two representations of the same thing. `editor` is what a
human authored and what the builder UI reads back; `structures` / `table_mappings` / `step1` is the
compiled form ingestion actually runs. Everything the platform generated itself — auto-detected
mappings, anything created before the editor model existed, anything written by a script — has only
the compiled half. Opened in a builder those mappings look EMPTY, which reads as data loss and is the
single reason a migration to a new UI would otherwise strand them.

So: `editor_from_payload` inverts the three translators. It is the exact mirror of
`semistructured.translate._node_key` / `_attr_key`, of `structured_map_translate`, and of
`document_map_translate`, and it is deliberately lossless in the direction that matters — anything it
cannot express is left out of the editor rather than guessed at, because a guessed rule that then
gets SAVED would rewrite the compiled form and change what the mapping ingests.

Derive-on-read is the migration: nothing is rewritten until the author saves, so a mapping that is
never opened keeps running exactly as it does today.
"""
from __future__ import annotations


from opencortyx.ingest.semistructured.schema import canonical_path

# The editor's vocabulary for a row's fate, inverted from translate._BECOMES_* .
_DECISION_BECOMES = {"node": "node", "dict": "dict", "embed": "embed", "skip": "ignore"}
_ROLE_BECOMES = {"text": "text", "date": "date", "id": "id",
                 "metadata": "label", "skip": "ignore", "measure": "measure"}


def _node_row(k: dict) -> dict:
    """A compiled node key → the editor row that would have produced it."""
    decision = str(k.get("decision") or "node")
    if k.get("dict_mode"):
        decision = "dict"
    becomes = "container" if k.get("container") else _DECISION_BECOMES.get(decision, "node")
    row: dict = {
        "path": k.get("path", ""),
        "kind": "node",
        # carried verbatim: "document" is a declaration (prose field → its own Document node), not a
        # structural label, and flattening it here loses the promotion on the author's next save.
        "type": k.get("type") or ("item" if str(k.get("path", "")).endswith("[]") else "object"),
        "node_type": k.get("node_type") or "",
        "decision": "skip" if decision == "skip" else ("node" if decision == "dict" else decision),
        "becomes": becomes,
        "sample": k.get("sample") or "",
    }
    if k.get("dict_mode"):
        row["dict_mode"] = True
        if k.get("key_attr"):
            row["key_attr"] = k["key_attr"]
    if k.get("auto_type"):
        row["auto_type"] = True          # a SUGGESTED name stays a suggestion across the round trip
    if k.get("edge_type"):
        row["edge_type"] = k["edge_type"]
    if k.get("edge_dir"):
        row["edge_dir"] = k["edge_dir"]
    if k.get("parent"):
        row["attachToNode"] = k["parent"]
    return row


def _attr_row(k: dict) -> dict:
    """A compiled attribute key → its editor row."""
    role = str(k.get("role") or "metadata")
    row: dict = {
        "path": k.get("path", ""),
        "kind": "attribute",
        "type": k.get("type") or "string",
        "role": role,
        "becomes": _ROLE_BECOMES.get(role, "label"),
        "sample": k.get("sample") or "",
    }
    if k.get("pair_with"):
        row["pair_with"] = k["pair_with"]
    if k.get("emit") == "node":
        row["becomes"] = "node"
        row["emit"] = "node"
        if k.get("node_type"):
            row["node_type"] = k["node_type"]
        if k.get("attr_name"):
            row["attr_name"] = k["attr_name"]
    return row


def _semistructured_editor(payload: dict) -> dict:
    """`structures` → the semi-structured editor model (key rows + canvas rules + reading rules)."""
    structures = [s for s in (payload.get("structures") or []) if isinstance(s, dict)]
    key_rows: list = []
    node_rules: list = []
    edge_rules: list = []
    attribute_rules: list = []
    zips: list = []
    seen_paths: set = set()

    for st in structures:
        for k in (st.get("keys") or []):
            if not isinstance(k, dict):
                continue
            path = canonical_path(k.get("path"))
            ident = (path, k.get("kind"))
            doc_ident = (path, k.get("kind"), "document")
            if k.get("type") == "document":
                if doc_ident in seen_paths:
                    continue
                seen_paths.add(doc_ident)
            else:
                if ident in seen_paths:
                    continue
                seen_paths.add(ident)
            if k.get("kind") == "node":
                row = _node_row({**k, "path": path})
                key_rows.append(row)
                if row["node_type"] and row.get("type") != "document":
                    node_rules.append({"id": f"n-{len(node_rules)}",
                                       "nodeLabel": row["node_type"],
                                       "sourcePath": path,
                                       "attachedAttributes": []})
            elif k.get("kind") == "attribute":
                row = _attr_row({**k, "path": path})
                key_rows.append(row)
                parent = path.rsplit(".", 1)[0] if "." in path else ""
                attribute_rules.append({"id": f"a-{len(attribute_rules)}",
                                        "sourcePath": path,
                                        "attachToNode": parent,
                                        "propertyName": path.rsplit(".", 1)[-1],
                                        "dataType": row["type"]})

        # containment edges the compiled form carries on the CHILD row
        for k in (st.get("keys") or []):
            if not isinstance(k, dict) or k.get("kind") != "node" or not k.get("edge_type"):
                continue
            child = k.get("node_type") or ""
            parent = k.get("parent") or ""
            if not child:
                continue
            edge_rules.append({"id": f"e-{len(edge_rules)}",
                               "fromNode": parent, "toNode": child,
                               "edgeLabel": k["edge_type"],
                               "cardinality": "1:N",
                               "direction": k.get("edge_dir") or "out"})

        # DECLARED JOIN EDGES — the new rule shape, carried back so the builder shows the column
        # each endpoint is matched on rather than silently degrading it to a containment edge.
        for er in (st.get("edge_rules") or []):
            if not isinstance(er, dict):
                continue
            frm, to = er.get("from") or {}, er.get("to") or {}
            edge_rules.append({
                "id": f"e-{len(edge_rules)}",
                "fromNode": frm.get("type") or "", "toNode": to.get("type") or "",
                "edgeLabel": er.get("type") or "",
                "fromKey": frm.get("field") or "", "fromOn": frm.get("on") or "",
                "toKey": to.get("field") or "", "toOn": to.get("on") or "",
                "readFrom": "", "cardinality": "1:N", "direction": "out",
                "props": [{"prop": p, "from_field": f}
                          for p, f in (er.get("properties") or {}).items()]})

        for c in (st.get("correlations") or []):
            keys = [str(x) for x in (c.get("keys") or [])]
            if len(keys) >= 2:
                zips.append({"id": f"z-{len(zips)}", "arrayNodeA": keys[0], "arrayNodeB": keys[1],
                             "zipEdgeLabel": c.get("node_type") or "ALIGNED_INDEX_ZIP"})

    reading = {}
    for src, dst in (("structure_only", "structure_only"), ("auto_accept", "auto_accept"),
                     ("strict", "strict"), ("auto_text_min_chars", "auto_text_min_chars"),
                     ("auto_embed_small", "auto_embed_small"), ("auto_skip_nodes", "auto_skip_nodes"),
                     ("auto_skip_attrs", "auto_skip_attrs"), ("id_patterns", "id_patterns")):
        if payload.get(src) is not None:
            reading[dst] = payload[src]

    status = next((st.get("status") for st in structures if st.get("status")), "auto")
    return {"status": status, "__structures": len(structures),
            "keyRows": key_rows, "nodeRules": node_rules, "edgeRules": edge_rules,
            "attributeRules": attribute_rules, "edgeAttributeRules": [],
            "compositeKeyRules": [], "discriminatorRules": _disc_rules(payload),
            "arrayFilterRules": _filter_rules(payload), "patternRules": _pattern_rules(payload),
            "zipCorrelations": zips, "reading": reading, "__derived": True}


def _disc_rules(payload: dict) -> list:
    out = []
    for i, d in enumerate(payload.get("discriminators") or []):
        if isinstance(d, dict):
            out.append({"id": f"d-{i}", "fieldPath": d.get("field") or "",
                        "operator": d.get("op") or "equals", "matchValue": d.get("value") or "",
                        "targetNodeLabel": d.get("node_type") or ""})
    return out


def _filter_rules(payload: dict) -> list:
    out = []
    for i, (path, f) in enumerate((payload.get("array_filters") or {}).items()):
        if isinstance(f, dict):
            out.append({"id": f"f-{i}", "arrayPath": path, "skipNulls": bool(f.get("skip_nulls")),
                        "skipEmpty": bool(f.get("skip_empty")), "flatten": bool(f.get("flatten"))})
    return out


def _pattern_rules(payload: dict) -> list:
    out = []
    for i, r in enumerate(payload.get("rules") or []):
        if not isinstance(r, dict):
            continue
        st = r.get("set") or {}
        becomes = "ignore" if st.get("skip") else (st.get("role") or ("node" if st.get("node_type") else ""))
        out.append({"id": f"p-{i}", "patternOperator": r.get("op") or "contains",
                    "patternValue": r.get("value") or "", "actionBecomes": becomes,
                    "targetNodeLabel": st.get("node_type") or ""})
    return out


def _structured_editor(payload: dict) -> dict:
    """`table_mappings` → the structured editor model. One node rule per table, one edge per FK."""
    tables = payload.get("table_mappings") or payload.get("tables") or []
    node_rules, edge_rules, pk_rules = [], [], []
    for i, t in enumerate(tables if isinstance(tables, list) else []):
        if not isinstance(t, dict):
            continue
        name = t.get("table") or t.get("name") or ""
        label = t.get("node_type") or t.get("label") or name
        cols = t.get("columns") or []
        ids = [c.get("name") for c in cols
               if isinstance(c, dict) and (c.get("role") == "identity" or c.get("primary_key"))]
        node_rules.append({"id": f"t-{i}", "table": name, "nodeLabel": label,
                           "idColumns": [c for c in ids if c],
                           "attributes": [c.get("name") for c in cols
                                          if isinstance(c, dict) and c.get("name") not in ids]})
        if ids:
            pk_rules.append({"id": f"pk-{i}", "targetNodeLabel": label, "compositeKeyFields": ids})
        for c in cols:
            if not isinstance(c, dict):
                continue
            if c.get("role") == "fk" or c.get("foreign_key") or c.get("references_table"):
                edge_rules.append({"id": f"fk-{i}-{c.get('name')}",
                                   "fromNode": label,
                                   "toNode": c.get("target_type") or c.get("references_table") or "",
                                   "edgeLabel": c.get("edge_type") or "REFERENCES",
                                   "fromKey": c.get("name") or "",
                                   "toOn": c.get("references_column") or "",
                                   "direction": c.get("edge_dir") or "out", "props": []})
    return {"tableRules": node_rules, "edgeRules": edge_rules, "compositeKeyRules": pk_rules,
            "junctionRules": [], "discriminatorRules": [], "reading": {}, "__derived": True}


def _unstructured_editor(payload: dict) -> dict:
    """`step1` → the document editor model (elements + chunking + node/edge rules)."""
    step1 = payload.get("step1") or {}
    chunking = step1.get("chunking") or payload.get("chunking") or {}
    elements = step1.get("elements") or payload.get("elements") or {}
    return {"chunking": {"strategy": chunking.get("strategy") or "markdown",
                         "mode": chunking.get("mode") or "save",
                         "size": chunking.get("size") or chunking.get("target_size") or 1200,
                         "overlap": chunking.get("overlap") or 150,
                         "node": chunking.get("node") or "Chunk"},
            "elements": elements,
            "nodeRules": step1.get("node_rules") or [],
            "edgeRules": step1.get("edge_rules") or [],
            "edgeAttributeRules": step1.get("edge_attributes") or [],
            "compositeKeyRules": [], "citationRules": step1.get("citations") or [],
            "reading": {}, "__derived": True}


def _kr_node(path: str, node_type: str) -> dict:
    return {"path": path, "kind": "node", "type": "item", "node_type": node_type,
            "decision": "node", "becomes": "node"}


def _key_rows_from_table_mappings(payload: dict) -> dict:
    """Compiled `table_mappings` → the Mapper's `keyRows` + `edgeRules`.

    Derived from the COMPILED half rather than from the retired builder's `columns`, because the
    compiled half is what the mapping actually ingests: a junction rule, for instance, exists in the
    old editor as a rule and in `table_mappings` as the two endpoint nodes plus the edge between
    them — and the second is the truthful picture of what the graph will contain.
    """
    tm = (payload or {}).get("table_mappings") or {}
    nodes = tm.get("nodes") or []
    if not isinstance(nodes, list) or not nodes:
        return {}
    key_rows, seen_type = [], {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        ntype = str(n.get("nodeType") or "").strip()
        attrs = [a for a in (n.get("attributes") or []) if isinstance(a, dict)]
        table = ""
        for a in attrs:
            ref = str(a.get("column") or "")
            if "." in ref:
                table = ref.split(".", 1)[0]
                break
        if not ntype or not table:
            continue
        base = f"{table}[]"
        if ntype in seen_type:          # the same type read from two tables: first wins, as at ingest
            continue
        seen_type[ntype] = base
        key_rows.append(_kr_node(base, ntype))
        for a in attrs:
            ref = str(a.get("column") or "")
            col = ref.split(".", 1)[1] if "." in ref else ref
            if not col:
                continue
            prop = str(a.get("property") or col)
            key_rows.append({"path": f"{base}.{col}", "kind": "attribute", "type": "string",
                             "role": "id" if prop == "id" else "metadata",
                             "becomes": "id" if prop == "id" else "label",
                             "attr_name": prop, "sample": ""})
    if not key_rows:
        return {}
    edge_rules = []
    for i, e in enumerate(tm.get("edges") or []):
        if not isinstance(e, dict):
            continue
        try:
            src = str(nodes[int(e.get("sourceRef", -1))].get("nodeType") or "").strip()
            dst = str(nodes[int(e.get("targetRef", -1))].get("nodeType") or "").strip()
        except (IndexError, ValueError, TypeError, AttributeError):
            continue
        if not (src and dst):
            continue
        edge_rules.append({"id": f"tm-{i}", "fromNode": src, "toNode": dst,
                           "edgeLabel": str(e.get("edgeType") or "RELATES_TO"),
                           "direction": "out", "props": []})
    return {"keyRows": key_rows, "edgeRules": edge_rules}


def _key_rows_from_step1(payload: dict) -> dict:
    """Compiled `step1` → the Mapper's `keyRows` + `edgeRules` for a document mapping."""
    step1 = (payload or {}).get("step1") or {}
    if not isinstance(step1, dict):
        return {}
    root = str(step1.get("root_type") or "").strip()
    section = str(step1.get("section_type") or "").strip()
    chunk = str(step1.get("node_type") or "").strip()
    tiers = [(root, "document"), (section, "section"), (chunk, "chunk")]
    key_rows, present = [], []
    for label, path in tiers:
        if not label or label in [p[0] for p in present]:
            continue
        present.append((label, path))
        key_rows.append(_kr_node(path, label))
    if not key_rows:
        return {}
    chunk_path = next((p for lbl, p in present if lbl == chunk), "")
    for a in step1.get("chunk_attrs") or []:
        name = str((a or {}).get("name") or "").strip() if isinstance(a, dict) else str(a or "").strip()
        if not name or not chunk_path:
            continue
        key_rows.append({"path": f"{chunk_path}.{name}", "kind": "attribute", "type": "string",
                         "role": "metadata", "becomes": "label", "attr_name": name, "sample": ""})
    edge_rules = []
    if root and section and step1.get("section_edge"):
        edge_rules.append({"id": "s1-section", "fromNode": root, "toNode": section,
                           "edgeLabel": str(step1["section_edge"]), "direction": "out", "props": []})
    if chunk and step1.get("chunk_edge"):
        edge_rules.append({"id": "s1-chunk", "fromNode": section or root, "toNode": chunk,
                           "edgeLabel": str(step1["chunk_edge"]), "direction": "out", "props": []})
    return {"keyRows": key_rows, "edgeRules": edge_rules}


def key_rows_view(paradigm: str, payload: dict) -> dict:
    """`keyRows`/`edgeRules` for a mapping whose editor predates the Mapper. `{}` when none apply."""
    try:
        if paradigm == "structured":
            return _key_rows_from_table_mappings(payload)
        if paradigm == "unstructured":
            return _key_rows_from_step1(payload)
    except Exception:                                # noqa: BLE001 — a view must never break a read
        return {}
    return {}


def editor_from_payload(paradigm: str, payload: dict) -> dict:
    """The editor model for a mapping that never had one. Empty dict when nothing can be derived."""
    payload = payload or {}
    try:
        if paradigm == "semistructured":
            ed = _semistructured_editor(payload)
            return ed if ed.get("keyRows") else {}
        if paradigm == "structured":
            ed = _structured_editor(payload)
            ed.update(key_rows_view("structured", payload))
            return ed if (ed.get("tableRules") or ed.get("keyRows")) else {}
        if paradigm == "unstructured":
            ed = _unstructured_editor(payload)
            ed.update(key_rows_view("unstructured", payload))
            return ed
    except Exception as e:                          # noqa: BLE001 — a bad payload must not 500 a list
        return {"__derive_error": str(e)}
    return {}


def with_editor(paradigm: str, payload: dict) -> dict:
    """`payload` with an `editor` the Mapper can read. Never overwrites an authored rule.

    Two cases, not one. A mapping with NO editor gets the full derivation below. A mapping whose
    editor predates the Mapper — the retired structured builder's `columns`, the document builder's
    `docKeys`/`elementSource` — has an editor that is truthy but written in a dialect the Mapper does
    not read, so it opened with an empty canvas and "0 node rules" while its rules sat in the payload
    untouched. That one gets a `keyRows` VIEW added alongside its own keys: nothing is removed, and
    `structured_map_translate.is_key_row_editor` keeps preferring the authored `columns`, so what the
    mapping ingests does not move until its author actually edits it in the Mapper.
    """
    payload = dict(payload or {})
    ed = payload.get("editor")
    if ed:
        if isinstance(ed, dict) and not ed.get("keyRows"):
            view = key_rows_view(paradigm, payload)
            if view:
                merged = dict(ed)
                merged.update(view)
                merged["__derived_view"] = True
                payload["editor"] = merged
        return payload
    derived = editor_from_payload(paradigm, payload)
    if derived:
        payload["editor"] = derived
    return payload


_SUPERSEDED = {
    "structured": ("table", "columns", "primaryKeyRules", "foreignKeyRules",
                   "junctionRules", "polymorphicFKRules"),
    "unstructured": ("nodeRules", "attributeRules", "docKeys", "hierarchyRules",
                     "docLinkRules", "patternRules", "edgeAttributeRules", "compositeKeyRules"),
}


def promote_key_rows(paradigm: str, editor: dict) -> dict:
    """Make the Mapper's `keyRows` authoritative on a mapping that used to speak another dialect.

    Both halves can sit in one editor: the legacy `columns` the author wrote in the retired builder,
    and the `keyRows` VIEW added on read so the Mapper can show them. While nobody edits it the
    legacy half wins and the mapping ingests exactly as before. The moment someone edits it in the
    Mapper, keeping the legacy half would mean the rules they just changed are the ones NOT compiled
    — the mapping would silently keep ingesting the old shape. So at that point the superseded half
    is dropped and what the author edited is what runs.
    """
    editor = dict(editor or {})
    if not (editor.get("__derived_view") and editor.get("__touched") and editor.get("keyRows")):
        return editor
    for k in _SUPERSEDED.get(paradigm, ()):
        editor.pop(k, None)
    editor.pop("__derived_view", None)
    return editor
