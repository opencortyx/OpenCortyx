"""Register a semi-structured structure's node/edge TYPES into the ontology (type_registry).

This is the piece that dissolves the hard problem: an extractor binds to a SOURCE NODE TYPE +
attribute, but JSON node types are dynamic — unknown until a shape is defined. Defining a structure
(Mode A auto-detect / Mode B custom) IS the schema-introspection step for JSON, exactly as
introspecting Postgres yields Table/Attribute types. Here we publish that schema into the SAME
type_registry the ontology, the graph, and every extractor's source-node selector read from — so
once a structure exists, `Session` / `Turn` / `Turn.content` are first-class named types and an
extractor binds to them the ordinary way (`source.select = ["Turn"]`, input attribute `content`),
with zero JSON-special handling downstream.

Idempotent: node types MERGE their attribute fields (a re-detect enriches, never clobbers); a
user-customized or system type is never overwritten. Edge types are insert-only. Tagged
`source="json"` + `properties_schema.domain="JSON: <structure name>"` so they group cleanly in the
ontology view and can be told apart from hand-authored types.
"""
import re

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from opencortyx.domain.ontology import TYPE_KEY, TypeRegistry, find_type
from opencortyx.ingest.semistructured.schema import normalize_structure as ss_normalize_structure


def _singular_upper(key: str) -> str:
    w = (re.split(r"[_\s\-]+", key.strip()) or [key])[-1]
    if w.endswith("ies"):
        w = w[:-3] + "y"
    elif w.endswith(("ses", "xes", "zes", "ches", "shes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w.upper()


def _child_key(path: str) -> str:
    """Last path segment (before its trailing '[]') — 'sessions[].turns[]' → 'turns'."""
    return path.replace("[]", "").rsplit(".", 1)[-1]


def _node_type_of(key: str) -> str:
    """TitleCased singular node type from a name — mirror of ingest_json._type_of / schema.node_type_for,
    so a promoted node's ontology name matches the type the walker actually creates."""
    parts = [p for p in re.split(r"[_\s\-]+", str(key).strip()) if p]
    if not parts:
        return "Record"
    w = parts[-1]
    if w.endswith("ies"):
        w = w[:-3] + "y"
    elif w.endswith(("ses", "xes", "zes", "ches", "shes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    parts[-1] = w
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Record"


def _walker_edge(path: str, child_names, node_type: str = "") -> str:
    """The edge type the walker grows into this node (see ingest_json). Mirrors the graph exactly so
    the ontology and the real edges agree — Document nodes (long prose) use HAS_DOCUMENT; list-of-lists
    inner nodes use HAS_TURN/HAS_ITEM; everything else HAS_<singular child key>."""
    if node_type == "Document":
        return "HAS_DOCUMENT"
    if not path:
        return "HAS_RECORD"
    if path.endswith("[][]"):
        s = {str(c).lower() for c in (child_names or [])}
        return "HAS_TURN" if ("role" in s and s & {"content", "text", "message"}) else "HAS_ITEM"
    return f"HAS_{_singular_upper(_child_key(path))}"


def structure_types(structure: dict) -> list:
    """Flatten ONE structure into type_registry rows: node types (with attribute fields) + the
    structural edges that connect them. Pure — returns dicts, writes nothing."""
    structure = ss_normalize_structure(structure)
    name = structure.get("name") or "json records"
    domain = f"JSON: {name}"
    keys = structure.get("keys") or []
    # skip = dropped from the graph; embed = folded into the parent — neither is its own node type
    node_rows = [k for k in keys if k.get("kind") == "node"
                 and (k.get("decision") or "node") not in ("skip", "embed")]
    attr_by_path = {k["path"]: k for k in keys if k.get("kind") == "attribute"}

    # path → node_type, for parent lookup (deepest strict-prefix node = the parent)
    path_to_type = {k["path"]: (k.get("node_type") or "Record") for k in node_rows}

    def parent_of(path: str):
        best = None
        for p in path_to_type:
            if p != path and path.startswith(p) and (best is None or len(p) > len(best)):
                best = p
        return best

    rows: list = []
    seen_edges: set = set()
    for nr in node_rows:
        ntype = nr.get("node_type") or "Record"
        # attribute fields = this node's direct scalar children, with their type + role
        fields = []
        for child_name in (nr.get("attrs") or []):
            ap = f"{nr['path']}.{child_name}"
            row = attr_by_path.get(ap) or {}
            fields.append({"name": child_name, "type": row.get("type") or "string",
                           "dtype": row.get("dtype") or row.get("type") or "string",
                           "role": row.get("role") or "metadata"})
        rows.append({"name": ntype, "kind": "node",
                     "description": f"JSON node from '{name}' ({nr['path'] or 'each record'})",
                     "source": "json", "status": "active",
                     # carried through so the writer can tell a DERIVED name from a declared one
                     "auto_type": bool(nr.get("auto_type")),
                     "properties_schema": {"domain": domain, "json_path": nr["path"], "fields": fields}})
        par_path = parent_of(nr["path"])
        src_type = path_to_type.get(par_path, "Source")
        # a card-declared edge type (walk-spec override) wins over the walker's default name
        etype = nr.get("edge_type") or _walker_edge(nr["path"], nr.get("attrs") or [], ntype)
        ekey = (etype, src_type, ntype)
        if ekey not in seen_edges:
            seen_edges.add(ekey)
            rows.append({"name": etype, "kind": "edge",
                         "description": f"{src_type} → {ntype} (JSON structure '{name}')",
                         "source": "json", "status": "active",
                         "allowed_source_types": [src_type], "allowed_target_types": [ntype],
                         "properties_schema": {"domain": domain}})

    # PROMOTED attributes → their own node type + edge from the node that owns the attribute path
    # ('answer' promoted to an Answer node hanging off the record; 'answer_session_ids[]' → AnswerSession).
    def owner_type(attr_path: str) -> str:
        best_p = ""                                    # "" = the root record row, if any
        for p in path_to_type:
            if p and attr_path.startswith(p) and len(p) > len(best_p):
                best_p = p
        return path_to_type.get(best_p, "Record")
    for k in keys:
        if k.get("kind") != "attribute" or k.get("emit") != "node":
            continue
        ntype = _node_type_of(k.get("node_type") or _child_key(k["path"]))
        attr_name = k.get("attr_name") or _child_key(k["path"])
        rows.append({"name": ntype, "kind": "node",
                     "description": f"Promoted from '{name}' attribute {k['path']}",
                     "source": "json", "status": "active",
                     "properties_schema": {"domain": domain, "json_path": k["path"],
                                           "fields": [{"name": attr_name, "type": k.get("type") or "string",
                                                       "role": k.get("role") or "metadata"}]}})
        src_type = owner_type(k["path"])
        etype = k.get("edge_type") or f"HAS_{_singular_upper(ntype)}"
        ekey = (etype, src_type, ntype)
        if ekey not in seen_edges:
            seen_edges.add(ekey)
            rows.append({"name": etype, "kind": "edge",
                         "description": f"{src_type} → {ntype} (promoted attribute, structure '{name}')",
                         "source": "json", "status": "active",
                         "allowed_source_types": [src_type], "allowed_target_types": [ntype],
                         "properties_schema": {"domain": domain}})

    # DECLARED parallel-array zips (correlations) → their node type exists in the ontology too,
    # so extractors can bind to the zipped nodes the walker will create.
    for g in (structure.get("correlations") or []):
        ntype = (g.get("node_type") or "").strip()
        if not ntype:
            continue
        fields = [{"name": _child_key(str(p)).rstrip("s") or _child_key(str(p)), "type": "string",
                   "role": "metadata"} for p in (g.get("keys") or [])]
        rows.append({"name": ntype, "kind": "node",
                     "description": f"Zipped parallel arrays {g.get('keys')} (structure '{name}')",
                     "source": "json", "status": "active",
                     "properties_schema": {"domain": domain, "fields": fields}})
        first = str((g.get("keys") or [""])[0])
        src_type = owner_type(first.replace("[]", ""))
        etype = f"HAS_{_singular_upper(ntype)}"
        if (etype, src_type, ntype) not in seen_edges:
            seen_edges.add((etype, src_type, ntype))
            rows.append({"name": etype, "kind": "edge",
                         "description": f"{src_type} → {ntype} (zipped arrays, structure '{name}')",
                         "source": "json", "status": "active",
                         "allowed_source_types": [src_type], "allowed_target_types": [ntype],
                         "properties_schema": {"domain": domain}})
    return rows


async def register_structures(db, workspace_id, structures: list) -> dict:
    """Upsert every structure's types into this organization's ontology. MERGES node attribute fields
    on conflict (never clobbers a built-in or user-customized row); edges are insert-only, and a type
    the organization already sees (its own or built-in) is left alone. Caller owns the commit."""
    empty_or_json = sa.or_(TypeRegistry.source == "json",
                           TypeRegistry.properties_schema.is_(None),
                           sa.cast(TypeRegistry.properties_schema, sa.Text) == "{}")
    made = {"nodes": 0, "edges": 0}
    for structure in (structures or []):
        for r in structure_types(structure):
            _status = "candidate" if r.get("auto_type") else "active"
            if r["kind"] == "node":
                # fetch existing to union fields (keep user edits + prior JSON attrs)
                existing = await find_type(db, workspace_id, r["name"], "node")
                if existing is not None and existing.source not in ("json", None):
                    continue                       # a hand-authored / system type — don't touch
                ps = dict(r["properties_schema"])
                if existing is not None:
                    old = dict(existing.properties_schema or {})
                    by_name = {f.get("name"): f for f in (old.get("fields") or []) if isinstance(f, dict)}
                    for f in ps.get("fields") or []:
                        by_name.setdefault(f.get("name"), f)
                    ps["fields"] = list(by_name.values())
                stmt = pg_insert(TypeRegistry).values(
                    workspace_id=workspace_id, name=r["name"], kind="node", description=r["description"],
                    source="json", status=_status, properties_schema=ps
                ).on_conflict_do_update(index_elements=TYPE_KEY,
                                        set_={"properties_schema": ps},
                                        where=empty_or_json)
                await db.execute(stmt)
                made["nodes"] += 1
            else:
                if await find_type(db, workspace_id, r["name"], "edge") is not None:
                    continue
                stmt = pg_insert(TypeRegistry).values(
                    workspace_id=workspace_id, name=r["name"], kind="edge", description=r["description"],
                    source="json", status="active",
                    allowed_source_types=r.get("allowed_source_types"),
                    allowed_target_types=r.get("allowed_target_types"),
                    properties_schema=r.get("properties_schema") or {}
                ).on_conflict_do_nothing(index_elements=TYPE_KEY)
                await db.execute(stmt)
                made["edges"] += 1
    return made
