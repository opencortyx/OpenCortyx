"""Translate the Semi-Structured ENGINE's rule model → the walker's `structures` payload.

The engine page authors with a rich rule model (keyRows, zipCorrelations, patternRules,
compositeKeyRules…). The ingest walker consumes `structures` cards (schema.build_structure /
compile_walk_spec). This module converts the former into the latter so a mapping authored in the
engine actually DRIVES ingestion — the missing sync between the two.

Happily, the engine's `keyRows` already use the walker's own field names (node_type, edge_type,
edge_dir, decision, dict_mode, role, attr_name), so the core is a faithful pass-through; the work is
mapping the higher-level rules (zips → correlations, composite keys → id roles, pattern rules →
stage rules). Pure functions; no DB.
"""
import hashlib
import re

from opencortyx.ingest.semistructured.schema import ROLES, canonical_path

_BECOMES_DECISION = {"node": "node", "container": "node", "dict": "dict",
                     "embed": "embed", "skip": "skip", "ignore": "skip"}
_BECOMES_ROLE = {"text": "text", "date": "date", "id": "id",
                 "label": "metadata", "metadata": "metadata",
                 "ignore": "skip", "skip": "skip"}


def _fingerprint(paths: list) -> str:
    return hashlib.sha1("|".join(sorted(set(paths))).encode("utf-8")).hexdigest()[:16]


def _titlecase(name: str) -> str:
    parts = [p for p in re.split(r"[_\s\-]+", str(name).strip()) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "ZipNode"


def _node_key(r: dict) -> dict:
    """A node keyRow → a structure node key (the shape compile_walk_spec reads)."""
    becomes = r.get("becomes")
    decision = _BECOMES_DECISION.get(becomes) or r.get("decision") or "node"
    path = canonical_path(r.get("path", ""))        # the editor's root sentinel → the walker's ""
    _t = r.get("type")
    key = {"path": path, "kind": "node",
           "type": _t if _t == "document" else ("item" if str(path).endswith("[]") else "object"),
           "node_type": r.get("node_type") or "", "decision": "node",
           "attrs": [], "input_attrs": []}
    # A name the DETECTOR suggested is not a declaration. Dropping this flag made every suggested
    # name authoritative on the next save, which is how auto-discovery reintroduces a type per key.
    if r.get("auto_type"):
        key["auto_type"] = True
    if becomes == "container":
        key["container"] = True
    if r.get("dict_mode") or decision == "dict":
        key["dict_mode"] = True
        if r.get("key_attr"):
            key["key_attr"] = r["key_attr"]
    elif decision in ("skip", "embed"):
        key["decision"] = decision
    if r.get("edge_type"):
        key["edge_type"] = r["edge_type"]
    if r.get("edge_dir") in ("in", "out"):
        key["edge_dir"] = r["edge_dir"]
    if r.get("attachToNode"):
        # "Attach → X" in the table is the same statement the canvas makes with an arrow: this branch
        # hangs off the X node, whatever the JSON nesting says.
        key["parent"] = r["attachToNode"]
    return key


def _attr_key(r: dict) -> dict:
    """An attribute keyRow → a structure attribute key. `becomes: node` promotes it to its own node."""
    becomes = r.get("becomes")
    role = _BECOMES_ROLE.get(becomes) or r.get("role") or becomes or "metadata"
    role = _BECOMES_ROLE.get(role, role)
    if role not in ROLES:
        role = "metadata"
    key = {"path": canonical_path(r.get("path", "")), "kind": "attribute",
           "type": r.get("type") or "string", "role": role}
    # "belongs to" in the editor: this sibling supplies a value for that array's elements.
    if r.get("pair_with"):
        key["pair_with"] = r["pair_with"]
    if becomes == "node" or r.get("decision") == "node" or r.get("emit") == "node":
        key["emit"] = "node"
        if r.get("node_type"):
            key["node_type"] = r["node_type"]
        if r.get("attr_name"):
            key["attr_name"] = r["attr_name"]
    return key


def _apply_node_rules(keys: list, node_rules: list) -> None:
    """Fold the canvas's NODE rules into the translated keys — the same one-way-sync problem as
    `_apply_edge_rules`. A node renamed on the canvas carries `sourcePath`, which is exactly the key it
    belongs to, so the rename is a direct lookup. Mutates `keys`."""
    by_path = {}
    for k in keys:
        if k.get("kind") == "node":
            by_path.setdefault(canonical_path(k.get("path")), k)
    for n in node_rules or []:
        if not isinstance(n, dict):
            continue
        key = by_path.get(canonical_path(n.get("sourcePath")))
        label = str(n.get("nodeLabel") or "").strip()
        if key is not None and label:
            key["node_type"] = label


def _id_field_of(keys: list, node_type: str):
    """The attribute a node type declares as its identity — what the other side of a join matches on
    when the author did not name a column explicitly."""
    npath = next((k.get("path") for k in keys
                  if k.get("kind") == "node" and k.get("node_type") == node_type), None)
    if npath is None:
        return None
    prefix = (npath + ".") if npath else ""
    for k in keys:
        if k.get("kind") != "attribute" or k.get("role") != "id":
            continue
        pth = str(k.get("path") or "")
        rest = pth[len(prefix):] if pth.startswith(prefix) else None
        if npath == "" and ("." in pth or "[]" in pth):
            continue                                    # root: direct scalars only
        if rest is not None and "." not in rest and "[]" not in rest:
            return rest
    return None


def _apply_edge_rules(keys: list, edge_rules: list) -> list:
    """Fold the CANVAS's edge rules into the translated node keys.

    The engine page holds the same graph twice: `keyRows` (the table) and `nodeRules`/`edgeRules` (the
    canvas — the "Declared Graph (your rules)" picture). `syncKeyRowsToRules` pushes the table into the
    canvas, but nothing came back, and this translator only ever read the table. So an edge renamed or
    re-parented ON THE CANVAS was saved, drawn, and never run: the picture said
    `Order -HAVE_ITEM-> Item` while ingestion walked `Record -CONTAINS_ITEMS-> Item`. The mapping wasn't
    "not saving" — half of it was saved into a model nothing consumed.

    An edge rule names its endpoints by NODE TYPE, which is how the author sees them; the keys are
    addressed by JSON path. Matching on the target's type is what joins the two. `direction: "in"`
    means the edge points at the parent, so from/to swap.

    Mutates `keys`.
    """
    by_type = {}
    for k in keys:
        if k.get("kind") == "node" and k.get("node_type"):
            by_type.setdefault(k["node_type"], k)
    declared: list = []
    for e in edge_rules or []:
        if not isinstance(e, dict):
            continue
        frm, to = str(e.get("fromNode") or ""), str(e.get("toNode") or "")
        label = str(e.get("edgeLabel") or "").strip().upper()
        from_key, to_key = str(e.get("fromKey") or ""), str(e.get("toKey") or "")

        if (from_key or to_key) and frm and to and label:
            anchor_type = str(e.get("readFrom") or "") or (to if to_key and not from_key else frm)
            anchor = by_type.get(anchor_type)
            if anchor is None:
                continue                           # anchored on a type no rule produces
            def _side(node_type, col, on_key):
                if not col:
                    return {"kind": "self", "type": node_type}
                return {"kind": "join", "type": node_type, "field": col,
                        "on": str(on_key or _id_field_of(keys, node_type) or col)}
            declared.append({
                "type": label,
                "path": anchor.get("path") or "",
                "from": _side(frm, from_key, e.get("fromOn")),
                "to": _side(to, to_key, e.get("toOn")),
                "properties": {str(pr.get("prop")): str(pr.get("from_field"))
                               for pr in (e.get("props") or [])
                               if isinstance(pr, dict) and pr.get("prop") and pr.get("from_field")}})
            continue

        # ── CONTAINMENT EDGE (unchanged) ─────────────────────────────────────────────────────────
        if (e.get("direction") or "out") == "in":
            frm, to = to, frm
        key = by_type.get(to)
        if key is None or not frm:
            continue                               # an edge to a type no key produces — nothing to bind
        if label:
            key["edge_type"] = label
        # The PARENT the author drew. `compile_walk_spec` only emits it when it disagrees with the
        # branch's natural JSON parent, so the common case stays exactly as it was.
        key["parent"] = frm
    return declared


def _apply_attribute_rules(keys: list, attribute_rules: list) -> None:
    """The canvas's ATTRIBUTE rules: which node a scalar hangs off, and what it is called there.

    `keyRows` already carries `attr_name`/`attachToNode`, but the canvas is a second place to edit the
    same thing and (like nodeRules/edgeRules) it never came back. Addressed by `sourcePath`, which is
    the key's own path. Mutates `keys`."""
    by_path = {}
    for k in keys:
        if k.get("kind") == "attribute":
            by_path.setdefault(canonical_path(k.get("path")), k)
    for a in attribute_rules or []:
        if not isinstance(a, dict):
            continue
        key = by_path.get(canonical_path(a.get("sourcePath")))
        if key is None:
            continue
        if a.get("propertyName"):
            key["attr_name"] = str(a["propertyName"])
        if a.get("attachToNode"):
            key["parent"] = str(a["attachToNode"])


def _discriminators(rules: list) -> list:
    """DISCRIMINATOR rules → runtime type switches. One array holding mixed things ("type":"cat" /
    "dog") is a single path, so a per-path node type cannot express it: the type comes from a FIELD of
    each element. Emitted as a spec the walker evaluates per record."""
    out = []
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        field, val, target = (str(r.get("fieldPath") or "").strip(),
                              str(r.get("matchValue") or "").strip(),
                              str(r.get("targetNodeLabel") or "").strip())
        if field and target and (val or r.get("operator") == "regex"):
            out.append({"field": field.replace("[]", "").rsplit(".", 1)[-1],
                        "op": r.get("operator") or "equals", "value": val, "node_type": target})
    return out


def _array_filters(rules: list) -> dict:
    """ARRAY FILTER rules → {array path: {skip_nulls, skip_empty, flatten}} for the walker."""
    out = {}
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        p = canonical_path(r.get("arrayPath"))
        if not p:
            continue
        out[p if p.endswith("[]") else p + "[]"] = {
            "skip_nulls": bool(r.get("skipNulls")),
            "skip_empty": bool(r.get("skipEmpty")),
            "flatten": bool(r.get("flattenMultiDimensional")),
        }
    return out


def _edge_attributes(rules: list) -> dict:
    """EDGE ATTRIBUTE rules → {EDGE_TYPE: [{prop, from_field}]}. A property ON the relationship
    (`HAS_CHUNK.page_num`), which the walker can only write while it is drawing that edge."""
    out: dict = {}
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        etype = str(r.get("attachToEdge") or "").strip()
        src = str(r.get("sourcePath") or "").strip()
        if not (etype and src):
            continue
        out.setdefault(etype, []).append({
            "prop": str(r.get("propertyName") or "").strip() or src.rsplit(".", 1)[-1],
            "from_field": src.replace("[]", "").rsplit(".", 1)[-1]})
    return out


def translate_editor(editor: dict, name: str = "") -> dict:
    """Editor rule model → `{structures:[...], auto_accept, rules, id_patterns}` — the SAME payload the
    pipeline's json_structures stage carries, so the walker/compile_walk_spec runs it unchanged."""
    editor = editor or {}
    key_rows = editor.get("keyRows") or []
    keys: list = []
    paths: list = []
    for r in key_rows:
        p = r.get("path")
        if p is None:
            continue                               # no path at all → not a rule
        p = canonical_path(p)
        if p:
            paths.append(p)
        keys.append(_node_key(r) if r.get("kind") == "node" else _attr_key(r))

    # The canvas is the picture the author edits last, so it wins where the two disagree. Nodes first:
    # edge rules name their endpoints by node TYPE, so the types have to be settled before they bind.
    _apply_node_rules(keys, editor.get("nodeRules") or [])
    _declared_edges = _apply_edge_rules(keys, editor.get("edgeRules") or [])
    _apply_attribute_rules(keys, editor.get("attributeRules") or [])

    # node label → its source path (zips + composite keys reference labels; the walk spec needs paths)
    label_to_path = {r.get("node_type"): canonical_path(r.get("path"))
                     for r in key_rows if r.get("kind") == "node" and r.get("node_type")}

    # composite keys → mark id-role attributes under the target node (deterministic identity → merge)
    for ck in editor.get("compositeKeyRules") or []:
        npath = label_to_path.get(ck.get("targetNodeLabel"))
        if npath is None:
            continue
        for f in ck.get("compositeKeyFields") or []:
            fpath = f"{npath}.{f}" if npath else str(f)
            hit = next((k for k in keys if k.get("path") == fpath and k.get("kind") == "attribute"), None)
            if hit is not None:
                hit["role"] = "id"
            else:
                keys.append({"path": fpath, "kind": "attribute", "type": "string", "role": "id"})
                paths.append(fpath)

    correlations: list = []
    for z in editor.get("zipCorrelations") or []:
        a, b = z.get("arrayNodeA") or "", z.get("arrayNodeB") or ""
        pa, pb = label_to_path.get(a), label_to_path.get(b)
        zkeys = [x for x in (pa, pb) if x]
        if len(zkeys) >= 2:
            correlations.append({"keys": zkeys, "node_type": _titlecase(f"{a}_{b}")})

    # pattern rules → stage-level rules (P23): IF key <op> value → set node_type/role/skip
    rules: list = []
    for pr in editor.get("patternRules") or []:
        val = pr.get("patternValue") or ""
        becomes = pr.get("actionBecomes")
        setd: dict = {}
        if becomes in ("skip", "ignore"):
            setd["skip"] = True
        elif becomes in _BECOMES_ROLE:
            setd["role"] = _BECOMES_ROLE[becomes]
        if pr.get("targetNodeLabel"):
            setd["node_type"] = pr["targetNodeLabel"]
        if val and setd:
            rules.append({"op": pr.get("operator") or "contains", "value": val, "set": setd})

    structure = {"name": name or "mapping", "fingerprint": _fingerprint(paths),
                 "status": (editor.get("status") or "edited"), "unit": "parent",
                 "paths": sorted(set(paths)), "keys": keys}
    if correlations:
        structure["correlations"] = correlations
    if _declared_edges:
        structure["edge_rules"] = _declared_edges

    payload = {"structures": [structure]}
    if rules:
        payload["rules"] = rules
    # Runtime rule sets the walker evaluates PER RECORD rather than per declared path — they cannot be
    # folded into the structure's keys because what they decide depends on the data, not the shape.
    _disc = _discriminators(editor.get("discriminatorRules"))
    if _disc:
        payload["discriminators"] = _disc
    _afilt = _array_filters(editor.get("arrayFilterRules"))
    if _afilt:
        payload["array_filters"] = _afilt
    _eattrs = _edge_attributes(editor.get("edgeAttributeRules"))
    if _eattrs:
        payload["edge_attributes"] = _eattrs

    reading = editor.get("reading") or {}
    if reading.get("structure_only") is not None:
        payload["structure_only"] = bool(reading["structure_only"])
    if reading.get("auto_accept") is not None:
        payload["auto_accept"] = bool(reading["auto_accept"])
    if reading.get("strict") is not None:
        payload["strict"] = bool(reading["strict"])
    if reading.get("auto_text_min_chars"):
        payload["auto_text_min_chars"] = int(reading["auto_text_min_chars"])
    if reading.get("auto_embed_small") is not None:
        payload["auto_embed_small"] = bool(reading["auto_embed_small"])
    for key in ("auto_skip_nodes", "auto_skip_attrs", "id_patterns"):
        vals = [str(v).strip() for v in (reading.get(key) or []) if str(v).strip()]
        if vals:
            payload[key] = vals
    return payload
