"""Structure schemas for SEMI-STRUCTURED (JSON) pipelines.

A *structure* is the reusable description of one JSON shape — the "table definition" of the
semi-structured world (what Table/Attribute are for SQL and Chunk is for documents):

    {"name": "chat sessions", "fingerprint": "9f2c…", "status": "auto|declared|edited",
     "unit": "parent|record|value",
     "paths": ["sessions[]", "sessions[].date", "sessions[].turns[].content", …],
     "keys":  [{"path": "sessions[].turns[].content", "type": "str", "role": "text",
                "sample": "My dog Max…", "description": "…"}, …]}

Structures live INSIDE a pipeline (kind="semistructured") as the config of its `json_structures`
pseudo-stage — the pipeline is authored BEFORE any data exists, against this contract, exactly
like document pipelines are authored against Chunk. At ingest time each record is fingerprint-
matched to a structure; the structure's key ROLES say which values are text (→ chunks →
extractors), which are dates (→ valid_from), and which are ids/metadata.

Pure functions + one optional LLM scout; no DB access in this module.
"""
import hashlib
import re
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

# role vocabulary
ROLES = ("text", "date", "id", "metadata", "skip")

ROOT_PATH_ALIASES = ("payload_root", "payload_root[]", "$root", "__root__", "root_payload")


def canonical_path(path) -> str:
    """A structure key's path with the engines' root sentinel folded to the walker's own "" .

    The sentinel is folded as a PREFIX too, not only as a whole path. The editor samples a
    top-level array by prefixing every key with a root sentinel, so a card authored there reads
    `<sentinel>.conversation.session_1[]` while the walker — which iterates the array and walks
    each element on its own — produces `conversation.session_1[]`. Folding only the exact match
    left every one of those keys unmatchable.

    Measured before this: a card authored in the UI against a top-level-array payload produced
    ZERO nodes of its declared type, while auto-detection still wrote 93 nodes of its own, so the
    run looked successful and the mapping had simply been ignored.
    """
    p = str(path or "").strip()
    if p in ROOT_PATH_ALIASES:
        return ""
    for alias in ROOT_PATH_ALIASES:
        if p.startswith(alias + "."):
            return p[len(alias) + 1:]
    return p


def normalize_structure(structure: dict) -> dict:
    """A structure whose key paths (and `paths` list) use `canonical_path`. Non-mutating: returns a
    shallow copy so a caller's stored payload is never rewritten behind its back."""
    if not isinstance(structure, dict):
        return structure
    keys = structure.get("keys") or []
    if not any(canonical_path(k.get("path")) != (k.get("path") or "")
               for k in keys if isinstance(k, dict)):
        return structure                            # already canonical — no copy
    out = dict(structure)
    out["keys"] = [({**k, "path": canonical_path(k.get("path"))} if isinstance(k, dict) else k)
                   for k in keys]
    # the root row is not a DATA path: it must not sit in `paths` either, where it would drag down
    # the overlap score every record is matched by (schema.match_structure).
    out["paths"] = sorted({p for p in (canonical_path(p) for p in (structure.get("paths") or [])) if p})
    return out


def normalize_structures(structures) -> list:
    return [normalize_structure(s) for s in (structures or [])]

# richer attribute value types (P18) — layered ON TOP of the python type + role, purely descriptive:
# they inform the card/ontology (and future retrieval), they don't change walking.
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")
_URL_RE = re.compile(r"^https?://", re.I)
_CURRENCY_RE = re.compile(r"^[$€£₹¥]\s?\d[\d,]*(\.\d+)?$|^\d[\d,]*(\.\d+)?\s?(USD|EUR|GBP|INR|JPY)$", re.I)
_GEO_RE = re.compile(r"^-?\d{1,3}\.\d+\s*,\s*-?\d{1,3}\.\d+$")
_TS_RE = re.compile(r"\d{1,2}:\d{2}")


def detect_dtype(type_name: str, samples: list) -> str:
    """Best-effort VALUE type of an attribute from its samples — integer/float/boolean/date/
    timestamp/currency/geo/email/url/enum/text/string. Descriptive metadata for the card + ontology."""
    if type_name == "bool":
        return "boolean"
    if type_name == "int":
        return "integer"
    if type_name == "float":
        return "float"
    vals = [str(s) for s in samples[:5] if s not in (None, "")]
    if not vals:
        return type_name or "string"
    if all(_DATE_RE.search(v) for v in vals):
        return "timestamp" if any(_TS_RE.search(v) for v in vals) else "date"
    if all(_EMAIL_RE.match(v) for v in vals):
        return "email"
    if all(_URL_RE.match(v) for v in vals):
        return "url"
    if all(_CURRENCY_RE.match(v.strip()) for v in vals):
        return "currency"
    if all(_GEO_RE.match(v.strip()) for v in vals):
        return "geo"
    # enum: several samples drawn from a small set of short token-ish values
    if len(vals) >= 3 and len(set(vals)) <= max(2, len(vals) // 2) and all(len(v) <= 24 and " " not in v for v in vals):
        return "enum"
    if any(len(v) >= 40 and " " in v for v in vals):
        return "text"
    return "string"

_TEXT_KEY_HINTS = {"content", "text", "body", "message", "description", "comment", "comments",
                   "notes", "note", "transcript", "answer", "question", "summary", "review",
                   "title", "subject", "details", "reason", "feedback"}
_DATE_KEY_HINTS = ("date", "time", "timestamp", "created", "updated", "modified", "_at", "_on")
_ID_KEY_HINTS = ("id", "uuid", "key", "guid", "hash")
_DATE_RE = re.compile(r"\b(19|20)\d\d[/. -]?\d\d?[/. -]?\d\d?")

_MAX_DEPTH = 8
_TEXT_LEN_HINT = 40          # avg string length that suggests real prose


# ── path flattening ──────────────────────────────────────────────────────────
def _is_long_text_s(v, min_chars: int = _TEXT_LEN_HINT) -> bool:
    """Prose detection (mirror of ingest_json._is_long_text): a string that is long AND has whitespace.
    Such a scalar FIELD becomes its own Document node, matching the walker."""
    return isinstance(v, str) and " " in v.strip() and len(v.strip()) >= min_chars


def _flatten(value, prefix: str, out: dict, depth: int = 0, branches: dict | None = None) -> None:
    """Collect {schema_path: [sample values]} for SCALARS — dict keys append '.key', lists '[]'.
    `branches` collects the NODE-producing paths so the card models EXACTLY what the walker builds:
    objects/arrays-of-objects → nodes; a scalar-STRING array → one node per value ('item'); a long
    prose scalar → its own Document node ('document')."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, dict):
        if branches is not None and prefix:
            branches.setdefault(prefix, "object")
        for k, v in value.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            _flatten(v, p, out, depth + 1, branches)
    elif isinstance(value, list):
        head = value[:5]
        is_obj_array = any(isinstance(v, dict) for v in head)
        # array OF ARRAYS (e.g. haystack_sessions: [[turn, …], …]) — container + inner Turn/Item.
        is_nested_array = bool(head) and all(isinstance(v, list) for v in head)
        is_str_array = bool(head) and all(isinstance(v, str) for v in head)
        if branches is not None and prefix:
            if is_obj_array or is_nested_array:
                branches.setdefault(f"{prefix}[]", "array")
            elif is_str_array:
                branches.setdefault(f"{prefix}[]", "item")     # array of strings → one node per value
        for v in head:                            # a few elements are enough for shape + samples
            _flatten(v, f"{prefix}[]", out, depth + 1, branches)
        if not value:
            out.setdefault(f"{prefix}[]", [])
    else:
        if branches is not None and prefix and _is_long_text_s(value):
            branches.setdefault(prefix, "document")            # long prose scalar → its own Document node
        out.setdefault(prefix, []).append(value)


def node_type_for(path: str) -> str:
    """The node type the walker derives for a branch path — mirror of ingest_json._type_of:
    'sessions[].turns[]' → 'Turn' (last key, singularized, TitleCased)."""
    key = path.replace("[]", "").rsplit(".", 1)[-1]
    parts = [t for t in re.split(r"[_\s\-]+", key) if t]
    w = parts[-1] if parts else key
    if w.endswith("ies"):
        w = w[:-3] + "y"
    elif w.endswith(("ses", "xes", "zes", "ches", "shes")):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    parts[-1:] = [w]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Record"


def _looks_turn(child_names) -> bool:
    """Mirror of ingest_json._looks_like_turn on a node's direct child keys — a {role, content|text|
    message} shape. Anonymous inner-array elements the walker names 'Turn' vs 'Item' by this test."""
    s = {str(c).lower() for c in (child_names or [])}
    return "role" in s and bool(s & {"content", "text", "message"})


def walker_node_type(path: str, child_names=None) -> str:
    """The node type the walker actually creates for a branch. Anonymous inner-array elements
    ('x[][]', from a list-of-lists) become Turn/Item (walker._walk_inner) — NOT the outer key —
    so the card matches the graph. Every other branch uses the key-derived type (node_type_for)."""
    if path.endswith("[][]"):
        return "Turn" if _looks_turn(child_names) else "Item"
    return node_type_for(path)


def key_paths(record) -> list:
    """Sorted unique schema paths of a record — its shape."""
    out: dict = {}
    _flatten(record, "", out)
    return sorted(out.keys())


def fingerprint(record) -> str:
    """The shape 'barcode': stable hash of the record's key paths. Optional fields make raw
    fingerprints unstable across records, so MATCHING uses path overlap (match_structure);
    the fingerprint is the identity of the structure as DECLARED/first seen."""
    return hashlib.sha1("|".join(key_paths(record)).encode("utf-8")).hexdigest()[:16]


# ── role guessing (deterministic heuristics — the free 90%) ──────────────────
def _last_key(path: str) -> str:
    return path.replace("[]", "").rsplit(".", 1)[-1].lower()


def guess_role(path: str, type_name: str, samples: list, min_chars: int = _TEXT_LEN_HINT) -> str:
    """`min_chars` is the auto-mode text threshold: a string field whose average sample length
    reaches it is classified text even without a name hint ('we can set based on character')."""
    key = _last_key(path)
    if any(h in key for h in _DATE_KEY_HINTS) or (
            type_name == "str" and samples and all(_DATE_RE.search(str(s) or "") for s in samples[:3] if s)):
        return "date"
    if type_name == "str":
        if key in _TEXT_KEY_HINTS:
            return "text"
        lens = [len(str(s)) for s in samples[:5] if s is not None]
        if lens and sum(lens) / len(lens) >= max(1, int(min_chars)):
            return "text"
    return "metadata"


def build_structure(record, name: str = "", source: str = "auto",
                    text_min_chars: int | None = None, root_type: str = "Record",
                    embed_small: bool = False) -> dict:
    """Derive a full structure from ONE sample record — the mini graph MODEL of this JSON shape:

      • NODE rows (kind="node") — the object / array-of-object branches the walker turns into
        graph nodes, with the node type it will use ('sessions[].turns[]' → Turn). Users can
        attach extractors to a NODE ("run these on every Turn") and pick which scalar child keys
        (`input_attrs`) feed that extraction; `decision: node|skip` marks whether it participates.
      • ATTRIBUTE rows (kind="attribute") — scalar keys with their role (text/date/id/metadata/
        skip); text keys may carry their own `extractors` attachment as before.

    Keys are ordered parent-first so the UI renders the natural tree."""
    mc = int(text_min_chars) if text_min_chars else _TEXT_LEN_HINT
    flat: dict = {}
    branches: dict = {}
    _flatten(record, "", flat, 0, branches)
    top_scalars = [p for p in flat if "." not in p and "[]" not in p]
    if top_scalars:
        branches[""] = "object"
    keys = []
    for path in sorted(set(flat.keys()) | set(branches.keys())):
        if path in branches:
            btype = branches[path]
            samp = (str(flat[path][0])[:120] if flat.get(path) else "")
            if btype == "document":                # long prose scalar FIELD → its own Document node
                keys.append({"path": path, "kind": "node", "type": "document", "node_type": "Document",
                             "decision": "node", "attrs": ["text"], "input_attrs": [], "sample": samp})
            elif btype == "item":                  # scalar-string array → one node per value
                keys.append({"path": path, "kind": "node", "type": "item", "node_type": node_type_for(path),
                             "auto_type": True,
                             "decision": "node", "attrs": ["name"], "input_attrs": [], "sample": samp})
            else:                                  # object / array-of-objects → node (children = its scalars,
                # excluding any child promoted to its own document/item node)
                children = [p.rsplit(".", 1)[-1] for p in flat
                            if p.startswith(path + ".") and "." not in p[len(path) + 1:]
                            and "[]" not in p[len(path) + 1:] and p not in branches]
                has_nested = any(b.startswith(path + ".") or b.startswith(path + "[]")
                                 for b in branches if b != path)
                id_like = any(c.lower() in _ID_KEY_HINTS or c.lower().endswith("_id") for c in children)
                decision = ("embed" if (embed_small and path and btype == "object"
                                        and not path.endswith("[]") and not has_nested
                                        and not id_like and len(children) <= 4)
                            else "node")
                keys.append({"path": path, "kind": "node", "type": btype,
                             "node_type": walker_node_type(path, children), "auto_type": True,
                             "decision": decision,
                             "attrs": children, "input_attrs": [], "sample": ""})
        else:
            samples = flat[path]
            type_name = type(samples[0]).__name__ if samples else "unknown"
            role = guess_role(path, type_name, samples, mc)
            if role == "text":
                role = "metadata"
            keys.append({"path": path, "kind": "attribute", "type": type_name, "role": role,
                         "dtype": detect_dtype(type_name, samples),
                         "sample": (str(samples[0])[:120] if samples else "")})
    return {"name": name or _suggest_name(list(flat.keys())), "fingerprint": fingerprint(record),
            "status": source, "unit": "parent", "text_min_chars": mc,
            "paths": sorted(flat.keys()), "keys": keys}


def _suggest_name(paths: list) -> str:
    """Human-ish default name from the outermost array keys ('sessions / turns')."""
    tops = []
    for p in paths:
        head = p.split(".", 1)[0].replace("[]", "")
        if head and head not in tops:
            tops.append(head)
    return " / ".join(tops[:3]) or "json records"


# ── matching a record to a known structure ──────────────────────────────────
def _is_ancestor_path(anc: str, path: str) -> bool:
    """Is the node at `anc` an ancestor of `path`? Segment-aware, deliberately.

    Both callers below used a plain string prefix, `path.startswith(anc)`, which says `conversation` is
    the ancestor of `conversation_meta` - two SIBLING keys that merely share letters. In
    `compile_walk_spec` that silently DROPPED a declared parent: the arrow was judged to be "already
    the natural parent" and nothing was emitted, so the walker fell back to JSON nesting and the node
    landed on the root. In `owning_node_type` it handed a top-level field to a sibling node.

    Measured before the fix: `conversation_meta` declared under Conversation compiled to parents {},
    while `meta` declared under Conversation compiled to {"meta": "Conversation"}. The only difference
    between the two inputs was the name.

    The one legitimate non-dotted ancestor is a container and its own elements: `sessions` holds
    `sessions[]`. Strict - a path is not its own ancestor; `owning_node_type` adds its self-match on
    top, as it always had. The web canvas resolves parents with this exact rule
    (components/mapper/declaredShape.ts, `isAncestorPath`), so the picture and ingestion cannot
    disagree about where a node hangs. Pinned by tests/test_semistructured_parent_paths.py.
    """
    if anc == path:
        return False
    if anc == "":
        return path != ""
    return path.startswith(anc + ".") or path == anc + "[]" or path.startswith(anc + "[].")


def owning_node_type(structure: dict, key_path: str):
    """The node type whose subtree this key lives in — the LONGEST node-row path that is a prefix
    of `key_path`. 'car.car_desc' → 'Car'; 'sessions[].turns[].content' → 'Turn'. A TOP-LEVEL scalar
    (no '.'/'[]') belongs to the ROOT record node when the structure has one — so targeting that root
    node routes its text per-node (and suppresses the chunk), instead of it being an un-owned orphan."""
    best_p, best_t, root_t = None, None, None
    for k in (structure.get("keys") or []):
        if k.get("kind") != "node" or (k.get("decision") or "node") == "embed":
            continue                               # embedded objects fold into their parent
        p = canonical_path(k.get("path"))
        if p == "":
            root_t = k.get("node_type") or "Record"
        elif (key_path == p or _is_ancestor_path(p, key_path)) and (best_p is None or len(p) > len(best_p)):
            best_p, best_t = p, k.get("node_type") or "Record"
    if best_t is not None:
        return best_t
    if root_t is not None and "." not in key_path and "[]" not in key_path:
        return root_t                              # a direct child of the root record node
    return None


def node_text_attrs(structure: dict, node_path: str) -> list:
    """Names of the TEXT-role scalar attributes that are DIRECT children of the node at `node_path`."""
    prefix = node_path + "."
    out = []
    for k in (structure.get("keys") or []):
        if k.get("kind") == "attribute" and k.get("role") == "text":
            p = k.get("path") or ""
            rest = p[len(prefix):] if p.startswith(prefix) else None
            if rest and "." not in rest and "[]" not in rest:
                out.append(rest)
    return out


def apply_rules(structure: dict, rules: list) -> dict:
    """PATTERN RULES (P23) — 'IF key contains manager → edge MANAGES', applied to a structure's rows
    (the declarative complement of the walker's runtime pattern fallback for shapes with no card).
    Each rule: {op: contains|equals|regex, value: "<key pattern>", set: {node_type?|edge_type?|
    role?|skip?}}. Node-row sets touch node_type/edge_type/decision; attribute-row sets touch role.
    Mutates + returns the structure. Explicit user edits are NOT overridden (rules fill defaults)."""
    def _match(leaf: str, r: dict) -> bool:
        op, val = (r.get("op") or "contains"), str(r.get("value") or "").lower()
        if not val:
            return False
        if op == "equals":
            return leaf == val
        if op == "regex":
            try:
                return bool(re.search(val, leaf))
            except re.error:
                return False
        return val in leaf
    for k in structure.get("keys") or []:
        leaf = (k.get("path") or "").replace("[]", "").rsplit(".", 1)[-1].lower()
        for r in (rules or []):
            st = (r or {}).get("set") or {}
            if not st or not _match(leaf, r):
                continue
            if k.get("kind") == "node":
                if st.get("skip"):
                    k["decision"] = "skip"
                if st.get("node_type") and not k.get("node_type_edited"):
                    k["node_type"] = st["node_type"]
                if st.get("edge_type") and not k.get("edge_type"):
                    k["edge_type"] = st["edge_type"]
            elif k.get("kind") == "attribute":
                if st.get("skip"):
                    k["role"] = "skip"
                elif st.get("role") in ROLES:
                    k["role"] = st["role"]
    return structure


def compile_walk_spec(structures: list) -> dict:
    """Compile declared structure cards into the WALK SPEC the JSON walker enforces at EVERY depth —
    the piece that makes a declared structure AUTHORITATIVE over the graph shape (not just over
    reading/extraction). Union across cards (paths are namespaced by shape, so merging is safe):

      node_types  {path: "Vehicle"}         — node-row renames ('' = the root record row)
      parents     {path: "Order"}           — RE-PARENT: this branch hangs off the record's `Order`
                                              node rather than its natural JSON parent, so a mapping
                                              can draw `Order -HAVE_ITEM-> Item` for an `items[]`
                                              array that sits BESIDE `order` in the payload. Emitted
                                              only when the declared parent differs from the natural
                                              one, so untouched mappings walk exactly as before.
      edge_types  {path: "TAUGHT_BY"}       — the edge INTO that branch from its parent (node rows'
                                              optional `edge_type`; default stays HAS_<KEY>)
      edge_dirs   {path: "in"}              — flip the edge (child → parent), node rows' `edge_dir`
      skip_paths  [path, …]                 — decision=skip node rows + role=skip attribute rows:
                                              the branch/field is DROPPED from the graph entirely
      promotions  {path: {node_type, attr_name, edge_type}} — attribute rows promoted to their own
                                              node (any depth, not just top level) + prose Document
                                              rows (kind=node, type=document → a Document node)
      zips        [{parent, keys, node_type, attrs}] — user-DECLARED parallel-array groups
                                              (correlations): zip position-by-position into nodes
      attachments [{parent, key, target}]   — a value list attached positionally as an attribute
                                              onto the elements of the array whose node type is
                                              `target` (dates → each Session)
      embed_paths [path, …]                 — decision=embed object branches: their scalars FOLD
                                              into the parent node as prefixed attributes (P15)
      dict_paths  {path: {node_type, key_attr, edge_type}} — dictionary-as-instances (P6):
                                              the object's KEYS are data → one node per key,
                                              typed node_type, the key stored under key_attr

    Runtime-only additions the runner merges in from the stage config (not per-card): `rules`
    (pattern rules, P23) and `id_patterns` (extra id-like key names, P3).

    Returns {} when nothing is declared (auto mode keeps the walker's default behaviour)."""
    spec = {"node_types": {}, "node_semantics": {}, "edge_types": {}, "edge_dirs": {}, "skip_paths": [],
            "promotions": {}, "zips": [], "attachments": [], "embed_paths": [],
            "dict_paths": {}, "parents": {},
            "sibling_pairs": [], "edge_rules": []}
    declared = False
    # path → declared parent NODE TYPE, before we know whether it differs from the natural parent.
    wanted_parent: dict = {}
    for s in (structures or []):
        for k in (s.get("keys") or []):
            path = canonical_path(k.get("path"))    # the engines' root sentinel → the walker's ""
            if k.get("kind") == "node":
                if (k.get("decision") or "node") == "skip":
                    declared = True
                    spec["skip_paths"].append(path)
                    continue
                if (k.get("decision") or "node") == "embed":
                    declared = True
                    spec["embed_paths"].append(path)
                    continue
                if k.get("dict_mode"):
                    declared = True
                    spec["dict_paths"][path] = {
                        "node_type": k.get("node_type") or node_type_for(path),
                        "key_attr": k.get("key_attr") or "key",
                        "edge_type": k.get("edge_type") or None}
                    if k.get("node_type"):
                        spec["node_types"][path] = k["node_type"]
                    continue
                if k.get("type") == "document":
                    # prose scalar FIELD modelled as its own Document node — a promotion of `text`
                    declared = True
                    spec["promotions"][path] = {"node_type": k.get("node_type") or "Document",
                                                "attr_name": "text",
                                                "edge_type": k.get("edge_type") or "HAS_DOCUMENT"}
                    continue
                if k.get("node_type"):
                    if k.get("auto_type"):
                        spec.setdefault("node_semantics", {})[path] = k["node_type"]
                    else:
                        spec["node_types"][path] = k["node_type"]
                        declared = True
                if k.get("edge_type"):
                    spec["edge_types"][path] = k["edge_type"]
                    declared = True
                if k.get("edge_dir") in ("in", "out"):
                    spec["edge_dirs"][path] = k["edge_dir"]
                    declared = True
                if k.get("parent"):
                    wanted_parent[path] = str(k["parent"])
            elif k.get("kind") == "attribute":
                if k.get("role") == "skip":
                    declared = True
                    spec["skip_paths"].append(path)
                elif k.get("emit") == "node":
                    declared = True
                    leaf = path[:-2] if path.endswith("[]") else path
                    spec["promotions"][leaf] = {
                        "node_type": k.get("node_type") or node_type_for(path),
                        "attr_name": k.get("attr_name") or leaf.rsplit(".", 1)[-1],
                        "edge_type": k.get("edge_type") or None}
        for g in (s.get("correlations") or []):     # declared parallel-array zips (never inferred)
            keys = [str(p)[:-2] if str(p).endswith("[]") else str(p) for p in (g.get("keys") or [])]
            if len(keys) >= 2 and g.get("node_type"):
                declared = True
                parent = keys[0].rsplit(".", 1)[0] if "." in keys[0] else ""
                spec["zips"].append({"parent": parent, "keys": [p.rsplit(".", 1)[-1] for p in keys],
                                     "node_type": g["node_type"]})
                # pin the synthetic array's node type so the declared name is used EXACTLY
                syn = re.sub(r"(?<!^)(?=[A-Z])", "_", g["node_type"].strip()).lower() or "item"
                spec["node_types"][(f"{parent}." if parent else "") + f"{syn}s[]"] = g["node_type"]
        for er in (s.get("edge_rules") or []):
            if not isinstance(er, dict) or not er.get("type") or not er.get("from") or not er.get("to"):
                continue
            declared = True
            spec["edge_rules"].append({
                "type": str(er["type"]).strip().upper(),
                "path": canonical_path(er.get("path")),
                "from": dict(er["from"]),
                "to": dict(er["to"]),
                "properties": dict(er.get("properties") or {})})
        for a in (s.get("attachments") or []):      # declared positional attach onto a node type
            key = str(a.get("key") or "")
            key = key[:-2] if key.endswith("[]") else key
            if key and a.get("target"):
                declared = True
                parent = key.rsplit(".", 1)[0] if "." in key else ""
                spec["attachments"].append({"parent": parent, "key": key.rsplit(".", 1)[-1],
                                            "target": a["target"]})

    for s in structures:
        for k in (s.get("keys") or []):
            if k.get("kind") != "attribute" or not k.get("pair_with"):
                continue
            src = canonical_path(k.get("path"))
            tgt = canonical_path(k.get("pair_with"))
            parent = src.rsplit(".", 1)[0] if "." in src else ""
            if not src or not tgt or "*" not in src or "*" not in tgt:
                continue                            # a pairing without a capture cannot be matched
            declared = True
            spec["sibling_pairs"].append({
                "parent": parent,
                "source": src.rsplit(".", 1)[-1] if "." in src else src,
                "target": tgt.rsplit(".", 1)[-1] if "." in tgt else tgt,
                "role": k.get("role") or "date",
            })

    node_paths = {p: t for p, t in spec["node_types"].items()}
    for path, ptype in wanted_parent.items():
        natural = None
        for p in node_paths:
            if _is_ancestor_path(p, path) and (natural is None or len(p) > len(natural)):
                natural = p
        if natural is None and path:
            natural = "" if "" in node_paths else None
        if natural is not None and node_paths.get(natural) == ptype:
            continue                                   # the drawn parent IS the natural one
        if ptype in node_paths.values():
            declared = True
            spec["parents"][path] = ptype
    return spec if declared else {}


def apply_auto_rules(structure: dict, *, skip_nodes=None, skip_attrs=None) -> dict:
    """AUTO-mode exclusion rules (custom structures are untouched — the user controls those directly).
    Mutates + returns the structure:
      • skip_nodes — node TYPE names to exclude from extraction: the node row is marked decision=skip
        and its descendant text attributes are demoted (not read).
      • skip_attrs — attribute names to exclude: demoted from text so they're never extracted/read.
    (The text char-length threshold is applied earlier, in build_structure via text_min_chars.)"""
    sn = {str(n).strip().lower() for n in (skip_nodes or []) if str(n).strip()}
    sa = {str(a).strip().lower() for a in (skip_attrs or []) if str(a).strip()}
    skipped_node_paths = set()
    for k in structure.get("keys") or []:
        if k.get("kind") == "node" and (k.get("node_type") or "").strip().lower() in sn:
            k["decision"] = "skip"
            skipped_node_paths.add(k.get("path") or "")
    for k in structure.get("keys") or []:
        if k.get("kind") != "attribute":
            continue
        name = (k.get("path") or "").replace("[]", "").rsplit(".", 1)[-1].lower()
        under_skipped = any((k.get("path") or "").startswith(p + ".") for p in skipped_node_paths if p)
        if (name in sa or under_skipped) and k.get("role") == "text":
            k["role"] = "metadata"                # demote → excluded from reading + extraction
    return structure


def text_paths(structure: dict) -> list:
    return [k["path"] for k in (structure.get("keys") or []) if k.get("role") == "text"]


def document_paths(structure: dict) -> list:
    """Paths of prose scalar fields modelled as Document NODES (kind=node, node_type=Document)."""
    return [k["path"] for k in (structure.get("keys") or [])
            if k.get("kind") == "node" and k.get("node_type") == "Document"]


def readable_paths(structure: dict) -> list:
    """Every path that carries prose the pipeline can read — text-role attributes AND Document nodes.
    Used by matching so 'can this structure read the record?' still holds after prose became Documents."""
    return text_paths(structure) + document_paths(structure)


def date_paths(structure: dict) -> list:
    return [k["path"] for k in (structure.get("keys") or []) if k.get("role") == "date"]


def _glob_to_re(pattern: str):
    out, i, n = [], 0, len(pattern)
    while i < n:
        if pattern[i] == "*":
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^.\\[\\]]*")
                i += 1
            continue
        out.append(re.escape(pattern[i]))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _split_patterns(paths):
    """(literal set, compiled patterns). Patterns compile once, and only for paths that use one, so
    a card without wildcards costs exactly what it did before."""
    literal, pats = set(), []
    for p in paths or ():
        if not p:
            continue
        if isinstance(p, str) and "*" in p:
            try:
                pats.append(_glob_to_re(p))
            except re.error:
                continue
        else:
            literal.add(p)
    return literal, pats


def path_matches(path, literal, patterns) -> bool:
    """True when `path` is declared literally, or matched by one of the declared patterns."""
    return path in literal or any(rx.match(str(path)) for rx in patterns)


def match_structure(record, structures: list, min_score: float = 0.45):
    """Best structure for this record: highest Jaccard overlap of key paths, and at least one of the
    structure's READABLE paths (text attrs OR Document nodes) must actually exist in the record (a match
    that can't read is not a match). Returns (structure, score) or (None, 0.0). Tolerant of optional/
    missing fields — this is why matching is overlap-based, not exact-fingerprint."""
    rec_paths = set(key_paths(record))
    best, best_score = None, 0.0
    for s in structures or []:
        skipped = {k.get("path") for k in (s.get("keys") or [])
                   if k.get("decision") == "skip" or k.get("role") == "skip"}
        s_paths = {p for p in (s.get("paths") or []) if p not in skipped}
        if not s_paths:
            continue
        literal, patterns = _split_patterns(s_paths)

        sk_literal, sk_patterns = _split_patterns(skipped)
        rec_paths_scored = {p for p in rec_paths
                            if not path_matches(p, sk_literal, sk_patterns)
                            and not any(str(p).startswith(str(d) + ".") for d in skipped if d)}
        covered = {p for p in rec_paths_scored if path_matches(p, literal, patterns)}
        declared_missing = {p for p in literal if p not in rec_paths}
        for p in s_paths:
            if isinstance(p, str) and "*" in p:
                rx = _glob_to_re(p)
                if not any(rx.match(q) for q in rec_paths_scored):
                    declared_missing.add(p)          # a pattern matching nothing is still a miss
        union = len(rec_paths_scored | declared_missing) or 1
        score = len(covered) / union

        # "can this structure read the record" has to honour patterns too, or a card whose only
        # text row is `session_*[].text` reads as unreadable and is rejected after scoring fine.
        rp = readable_paths(s)
        r_literal, r_patterns = _split_patterns(rp)
        can_read = (not rp) or any(path_matches(q, r_literal, r_patterns) for q in rec_paths)

        if score >= min_score and score > best_score and can_read:
            best, best_score = s, score
    return best, best_score


# ── LLM scout (optional refinement for auto-accepted structures) ─────────────
async def scout_structure(records: list, structure: dict, model_spec: dict | None = None) -> dict:
    """One LLM call on the workspace's model (`model_spec`): refine guessed roles + write key
    descriptions so a human or agent can understand every key. Optional — the measured roles stand
    on their own, so a workspace with no model connected skips this rather than borrowing one. Only
    called when a pipeline auto-accepts a NEW structure (never per record)."""
    if not model_spec:
        return structure
    import json as _json
    from opencortyx.core.inference.llm import complete
    keys = structure.get("keys") or []
    # only ATTRIBUTE rows carry a role — node rows are structural (kind="node", no "role"), so classify
    # attributes only (a node row would otherwise KeyError on k["role"] and fail the whole ingestion).
    attr_keys = [k for k in keys if k.get("kind", "attribute") == "attribute"]
    listing = "\n".join(
        f'- {k.get("path")} ({k.get("type", "?")}) sample: {str(k.get("sample", ""))[:80]!r} '
        f'guess: {k.get("role", "metadata")}' for k in attr_keys[:60])
    sample = _json.dumps(records[0], default=str)[:2500] if records else "{}"
    prompt = (
        "You are classifying the keys of a JSON structure for a knowledge-extraction pipeline.\n"
        "Roles: text (prose worth reading/extracting), date (temporal anchor), id, metadata, skip.\n"
        f"KEYS:\n{listing}\n\nSAMPLE RECORD:\n{sample}\n\n"
        'Return ONLY JSON: {"name": "<short name>", "keys": [{"path": "...", "role": "...", '
        '"description": "<one plain-English sentence>"}]}')
    try:
        raw = await complete(prompt, system="You classify JSON keys. JSON only.",
                             max_tokens=1200, json_mode=True, temperature=0.1, model_spec=model_spec)
        parsed = _json.loads(raw)
        by_path = {k.get("path"): k for k in (parsed.get("keys") or []) if isinstance(k, dict)}
        for k in attr_keys:
            hit = by_path.get(k.get("path"))
            if hit:
                if hit.get("role") in ROLES:
                    k["role"] = hit["role"]
                if hit.get("description"):
                    k["description"] = str(hit["description"])[:300]
        if parsed.get("name"):
            structure["name"] = str(parsed["name"])[:120]
    except Exception as e:                        # noqa: BLE001 — heuristics already filled roles
        log.warning("semistructured.scout_failed", error=str(e),
                    action="keeping heuristic roles")
    return structure
