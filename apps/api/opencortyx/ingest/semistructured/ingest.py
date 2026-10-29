"""JSON → graph ingest.

With nothing declared (no structure cards, mapping, id field or root type) the payload is walked by
the plain `SemiStructuredWalker` in `ingest/walkers/semistructured.py`: objects, lists and values
become nodes with HAS_<KEY> and NEXT edges, nothing inferred. `_Walker` below is the DECLARED walker.

`_Walker` turns ANY JSON (an uploaded file or an API payload) into a hierarchical node/edge graph through
NodeRepo — no per-schema code. The mapping rules:

  object {}            → a node (type = its key, singularized + TitleCased)
  scalar field         → an attribute on that node
  nested object        → a child node + HAS_<KEY> edge
  array of objects     → one child node per element + HAS_<KEY> edges + a NEXT chain (preserves order)
  array of scalars     → a list attribute on the parent
  array of arrays      → a container node per element, recurse the inner array
  parallel arrays      → sibling scalar arrays of equal length ZIP onto the primary array's children
                         as attributes (e.g. *_dates → date, *_session_ids → session_id); a date-ish
                         satellite also sets `valid_from` (temporal anchor)
  {role, content,…}    → recognised as a Turn (chat/message shape), with a NEXT chain
  id-like field        → the node's DETERMINISTIC id (idempotent re-ingest = update, not duplicate)

Links between records are declared, never guessed: a mapping's join edges ("`customer_id` names a
Customer's `id`") are drawn while the file is walked when both ends are in it, and saved as reference
rules the resolution engine applies across files. Bounded by `max_depth` / `max_array`. Each node stores a `text` summary (so lexical retrieval works); embeddings
are optional (`embed_nodes`) since a big payload would otherwise make one embed call per node.
"""
import os
import re
import uuid
from typing import Any, Optional

from opencortyx.core.substrate import NodeRepo
from opencortyx.ingest.semistructured import dataplane as _sdp
from opencortyx.ingest.walkers.semistructured import SemiStructuredWalker

_repo = NodeRepo()
_JNS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/json-walker")   # namespace for deterministic ids

_KEYNODE = "KeyNode"

_SCALAR = (str, int, float, bool)
_DATE_RE = re.compile(r"\b(19|20)\d\d[/-]\d\d?[/-]\d\d?")


def _ks(key) -> str:
    """Mapping keys are not guaranteed to be strings.

    JSON keys always are, but the walker is shared with YAML and TOML where they need not be: YAML
    1.1 resolves bare `on`/`yes`/`no` to booleans and permits int and null keys outright. A bool key
    reached `k.upper()` and raised `'bool' object has no attribute 'upper'`, failing the whole file.
    The YAML parser now preserves key text, and this keeps the walker safe for any other source.
    """
    return key if isinstance(key, str) else str(key)


def _is_scalar(v) -> bool:
    return v is None or isinstance(v, _SCALAR)


_NOISE_RE = re.compile(
    r'^(https?://|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{4}-\d{2}-\d{2}T|\d{4}-\d{2}-\d{2}$)',
    re.I
)
_PRIORITY_PROSE_KEYS = {"content", "text", "message", "description", "question", "answer", "summary", "prompt", "comment", "note"}


def extract_prose_context(attrs: dict) -> str:
    """Extract prose-dense scalar attributes, filtering out noise (IPs, UUIDs, dates) and formatting as key: value lines."""
    if not attrs or not isinstance(attrs, dict):
        return ""
    candidates = []
    for k, v in attrs.items():
        if v in (None, "") or not isinstance(v, _SCALAR):
            continue
        sv = str(v).strip()
        if not sv or _NOISE_RE.match(sv):
            continue

        k_lower = str(k).lower()
        is_priority = any(p in k_lower for p in _PRIORITY_PROSE_KEYS)
        if not is_priority and len(sv) < 3 and not sv.isalpha():
            continue

        score = len(sv) + (1000 if is_priority else 0)
        candidates.append((score, str(k), sv))

    if not candidates:
        return ""

    candidates.sort(key=lambda x: x[0], reverse=True)
    return "\n".join([f"{k}: {v}" for _, k, v in candidates])


def evaluate_key_filters(
    node_type: str,
    attrs: dict,
    include_keys: Any = None,
    exclude_keys: Any = None,
    min_words: Optional[int] = None,
    max_words: Optional[int] = None,
    min_confidence: Optional[float] = None,
    confidence_score: Optional[float] = None,
) -> tuple[bool, dict]:
    """Evaluates Include/Exclude key filters, Word Count limits, and Confidence Thresholds with Node vs Attribute disambiguation."""
    inc = {str(k).strip() for k in (include_keys if isinstance(include_keys, list) else str(include_keys or "").split(",")) if str(k).strip()}
    exc = {str(k).strip() for k in (exclude_keys if isinstance(exclude_keys, list) else str(exclude_keys or "").split(",")) if str(k).strip()}

    # 0. CONFIDENCE THRESHOLD
    if min_confidence is not None and confidence_score is not None and confidence_score < min_confidence:
        return False, {}

    # 1. NODE-LEVEL FILTERING
    if any(k in (node_type, f"node:{node_type}", f"entity:{node_type}") for k in exc):
        return False, {}

    node_includes = {k.replace("node:", "").replace("entity:", "") for k in inc if k.startswith(("node:", "entity:"))}
    if node_includes and node_type not in node_includes:
        return False, {}

    # 2. ATTRIBUTE-LEVEL & WORD COUNT FILTERING
    filtered_attrs = {}
    attr_includes = {k.replace("attr:", "").replace("prop:", "") for k in inc if not k.startswith(("node:", "entity:"))}

    for attr_name, attr_val in attrs.items():
        if any(k in (attr_name, f"attr:{attr_name}", f"prop:{attr_name}") for k in exc):
            continue

        if attr_includes and attr_name not in attr_includes and not any(k in (attr_name, f"attr:{attr_name}") for k in inc):
            continue

        # Word count check on text attributes
        if isinstance(attr_val, str) and attr_val.strip():
            word_count = len(attr_val.split())
            if min_words is not None and min_words > 0 and word_count < min_words:
                continue
            if max_words is not None and max_words > 0 and word_count > max_words:
                continue

        filtered_attrs[attr_name] = attr_val

    return (len(filtered_attrs) > 0 or not attrs), filtered_attrs



def _tokens(key) -> list:
    key = _ks(key)
    return [t for t in re.split(r"[_\s\-]+", str(key).strip()) if t]


def _singular(word: str) -> str:
    w = (_tokens(word) or [word])[-1]
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith(("ses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _type_of(key: Optional[str], default: str = "Record") -> str:
    if not key:
        return default
    parts = _tokens(key)
    if not parts:
        return default
    parts[-1] = _singular(parts[-1])
    return "".join(p[:1].upper() + p[1:] for p in parts) or default


def _id_key(k: str) -> bool:
    kl = str(k).lower()
    return kl in ("id", "uuid", "key", "@id") or kl.endswith("_id") or kl.endswith("_ids")


def _looks_like_turn(o) -> bool:
    return isinstance(o, dict) and "role" in o and any(k in o for k in ("content", "text", "message"))


_TEXT_MIN_CHARS = 40


def _is_long_text(v, min_chars: int = _TEXT_MIN_CHARS) -> bool:
    """Character-based prose detection: a string that is long AND has whitespace (a sentence, not an id,
    code, url or slug). These become their own Document nodes instead of node attributes."""
    return isinstance(v, str) and " " in v.strip() and len(v.strip()) >= min_chars


def _shares_prefix(a: str, b: str) -> bool:
    """True if two keys share their first token — the signal that a same-length scalar array is a
    PARALLEL satellite of an object array (haystack_dates ↔ haystack_sessions), not a coincidental
    length match (prerequisites ↔ modules). Prevents unrelated arrays being zipped in as attributes."""
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb and ta[0].lower() == tb[0].lower())


def _snake(name: str) -> str:
    """'CarSpec' → 'car_spec' — synthetic field name for a declared zip group's node type."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", str(name).strip()).lower() or "item"


def _embed_flat(v: dict, prefix: str, out: dict, depth: int = 0) -> None:
    """Fold an EMBEDDED object's scalars into prefixed attribute names for the parent node —
    {'address': {'city': 'NY', 'geo': {'lat': 1}}} → {'address_city': 'NY', 'address_geo_lat': 1}."""
    if depth > 3 or not isinstance(v, dict):
        return
    for k, val in v.items():
        name = f"{prefix}_{k}"
        if _is_scalar(val):
            out[name] = val
        elif isinstance(val, dict):
            _embed_flat(val, name, out, depth + 1)
        elif isinstance(val, list) and all(_is_scalar(x) for x in val):
            out[name] = val


def normalize_parallel_arrays(data):
    """
    Recursively scans a JSON structure, detects parallel arrays 
    (same length), and zips them into an array of objects.
    """
    if isinstance(data, list):
        return [normalize_parallel_arrays(x) for x in data]
    if not isinstance(data, dict):
        return data

    obj = {k: normalize_parallel_arrays(v) for k, v in data.items()}
    arrays = {k: v for k, v in obj.items()
              if isinstance(v, list) and not _id_key(k)}
    if len(arrays) < 2:
        return obj

    from collections import defaultdict
    groups = defaultdict(list)
    for key, arr in arrays.items():
        if len(arr) > 0:
            groups[len(arr)].append(key)

    zipped_count = 0
    for length, keys in groups.items():
        keys = [k for k in keys if any(k2 != k and _shares_prefix(k, k2) for k2 in keys)]
        if len(keys) > 1:
            zipped_array = []
            for i in range(length):
                item = {}
                for k in keys:
                    item[k] = obj[k][i]
                zipped_array.append(item)
            for k in keys:
                del obj[k]
            
            array_name = f"zipped_items_{zipped_count}" if zipped_count > 0 else "zipped_items"
            obj[array_name] = zipped_array
            zipped_count += 1
    return obj


_WILDCARD = "*"


def _glob_to_re(pattern: str):
    """Compile a path glob. `[]` is literal here (it is array syntax, not a character class), so
    everything except the wildcards is escaped."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        if pattern[i] == "*":
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append(r"[^.\[\]]*")
                i += 1
            continue
        out.append(re.escape(pattern[i]))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _looks_date(v) -> bool:
    return isinstance(v, str) and bool(_DATE_RE.search(v))


def _date_key(k: str) -> bool:
    return any(t in str(k).lower() for t in ("date", "time", "timestamp", "created", "_at", "_on"))


def _promotable(v) -> bool:
    """A field that can be promoted to its OWN node: a scalar, or an array of scalars (one node per
    element). Objects / arrays-of-objects already become nodes by the default rules."""
    return _is_scalar(v) or (isinstance(v, list) and all(_is_scalar(x) for x in v))


def _label_of(key: str) -> str:
    """Human label for a field — 'confidential_memo' → 'Confidential Memo', 'overview' → 'Overview'."""
    return " ".join(p[:1].upper() + p[1:] for p in _tokens(key)) or "Document"


def _sat_name(sat_key: str, primary_key: str) -> str:
    """Name a satellite array's per-element attribute by stripping the prefix it shares with the
    primary array key, then singularising: (sessions, *_session_ids) → 'session_id'; (*, *_dates) → 'date'."""
    p, s = _tokens(primary_key), _tokens(sat_key)
    i = 0
    while i < len(p) - 0 and i < len(s) - 1 and p[i].lower() == s[i].lower():
        i += 1
    rem = s[i:] or s[-1:]
    rem = rem[:-1] + [_singular(rem[-1])]
    return "_".join(rem)


def _jp(parent: str, key) -> str:
    """Join a schema path — the same path grammar the structure cards use ('sessions[].turns[]')."""
    return f"{parent}.{key}" if parent else str(key)


def apply_declared_zips(data, zips: list):
    """Apply user-DECLARED parallel-array groups (structure-card correlations) to one record:
    at each group's parent path, the named value lists are zipped position-by-position into ONE
    synthetic array of objects — item 1 of each list becomes one node, item 2 the next… The
    synthetic key is `<snake(node_type)>s`, so the walker derives exactly the declared node type.
    Only the declared keys are touched; nothing is inferred."""
    if not zips or not isinstance(data, dict):
        return data

    def _descend(node, segs, fn):
        if not isinstance(node, dict):
            return
        if not segs:
            fn(node)
            return
        seg = segs[0]
        if seg.endswith("[]"):
            v = node.get(seg[:-2])
            if isinstance(v, list):
                for el in v:
                    _descend(el, segs[1:], fn)
        else:
            _descend(node.get(seg), segs[1:], fn)

    for g in zips:
        keys, ntype = g.get("keys") or [], g.get("node_type") or "Item"
        if len(keys) < 2:
            continue

        def _zip_here(d, keys=keys, ntype=ntype):
            lists = [d.get(k) for k in keys]
            if not all(isinstance(x, list) for x in lists):
                return
            n = min(len(x) for x in lists)
            attr_names = [_singular(k) for k in keys]
            items = [{attr_names[j]: lists[j][i] for j in range(len(keys))} for i in range(n)]
            for k in keys:
                d.pop(k, None)
            d[f"{_snake(ntype)}s"] = items

        parent = g.get("parent") or ""
        _descend(data, [s for s in parent.split(".") if s], _zip_here)
    return data


class _PathIndex:
    """Exact-first, pattern-second lookup over one walk-spec mapping.

    Patterns are compiled once per ingest, and only for keys that actually contain a wildcard, so
    the common case (no wildcards declared) costs one `dict.get` and nothing else. Where several
    patterns match, the MOST SPECIFIC wins - longest literal prefix, then fewest wildcards - so
    `conversation.session_*[]` beats `**` rather than losing to whichever was declared first.
    """

    __slots__ = ("exact", "patterns")

    def __init__(self, mapping):
        self.exact = dict(mapping or {})
        pats = []
        for key, value in self.exact.items():
            if isinstance(key, str) and _WILDCARD in key:
                try:
                    pats.append((_specificity(key), _glob_to_re(key), value))
                except re.error:
                    continue
        pats.sort(key=lambda t: t[0], reverse=True)
        self.patterns = [(rx, val) for _, rx, val in pats]

    def get(self, path, default=None):
        hit = self.exact.get(path)
        if hit is not None:
            return hit
        if self.patterns:
            for rx, val in self.patterns:
                if rx.match(str(path)):
                    return val
        return default

    def __contains__(self, path):
        return self.get(path) is not None

    def __getitem__(self, path):
        hit = self.get(path)
        if hit is None:
            raise KeyError(path)
        return hit

    def __bool__(self):
        return bool(self.exact)


class _PathSet:
    """The same exact-first, pattern-second rule for a spec entry that is a LIST of paths."""

    __slots__ = ("exact", "patterns")

    def __init__(self, paths):
        self.exact = set()
        pats = []
        for p in (paths or ()):
            if isinstance(p, str) and _WILDCARD in p:
                try:
                    pats.append(_glob_to_re(p))
                except re.error:
                    continue
            else:
                self.exact.add(p)
        self.patterns = pats

    def __contains__(self, path):
        if path in self.exact:
            return True
        return any(rx.match(str(path)) for rx in self.patterns)

    def __bool__(self):
        return bool(self.exact or self.patterns)


def _specificity(pattern: str):
    """Rank a pattern: longer literal prefix first, then fewer wildcards. Deterministic, so two
    cards declaring overlapping patterns resolve the same way on every run."""
    head = pattern.split("*", 1)[0]
    return (len(head), -pattern.count("*"))


def _capture(pattern: str, key: str):
    """What the single `*` in `pattern` matched inside `key`, or None.

    Used to pair sibling keys by the part of their name that varies: `session_1_date_time` and
    `session_1[]` both capture "1", which is the only thing that says they belong together.
    """
    if pattern.count("*") != 1:
        return None
    head, tail = pattern.split("*", 1)
    if not key.startswith(head) or not key.endswith(tail):
        return None
    mid = key[len(head):len(key) - len(tail)] if tail else key[len(head):]
    return mid or None


class _SiblingPairs:
    """Declared pairings between two sibling keys, matched on their shared wildcard capture.

    WHY THIS EXISTS. JSON that keys a collection by number often stores each member's metadata as
    a SEPARATE sibling rather than inside it:

        "session_1_date_time": "7:31 pm on 21 January, 2022",
        "session_1":           [ {...turns...} ],

    The date belongs to that session's turns and to no others, but nothing in the tree says so —
    the two are related only by the `1` in both names. Declared as an ordinary attribute the date
    lands on the PARENT, so all 29 dates pile onto one node and not a single turn gets one. Worse,
    the parent's `valid_from` propagates down as a date hint, so every turn in the file would
    inherit the same wrong date: uniformly incorrect ordering, which is harder to notice than none.

    The existing `attachments` primitive pairs equal-length parallel ARRAYS. This pairs named
    SIBLINGS, which is a different shape and the one numbered-key JSON actually uses.
    """

    __slots__ = ("rules",)

    def __init__(self, rules):
        self.rules = [r for r in (rules or []) if isinstance(r, dict)
                      and r.get("source") and r.get("target")]

    def hints_for(self, path: str, obj: dict) -> dict:
        """`{array_key: value}` for the sibling values that belong to arrays in THIS object."""
        if not self.rules or not isinstance(obj, dict):
            return {}
        out = {}
        for rule in self.rules:
            if (rule.get("parent") or "") != (path or ""):
                continue
            src_pat, tgt_pat = rule["source"], rule["target"]
            tgt_pat = tgt_pat[:-2] if tgt_pat.endswith("[]") else tgt_pat
            for key, val in obj.items():
                if isinstance(val, (dict, list)):
                    continue                      # the SOURCE is a scalar; the target is the array
                cap = _capture(src_pat, key)
                if cap is None:
                    continue
                mate = tgt_pat.replace("*", cap)
                if isinstance(obj.get(mate), list):
                    out[mate] = val
        return out

    def __bool__(self):
        return bool(self.rules)


class _Walker:
    def __init__(self, workspace_id, collection_id, *, max_depth, max_array, embed_nodes, spec=None):
        self.org = workspace_id
        self.coll = collection_id
        self.max_depth = max_depth
        self.max_array = max_array
        self.embed_nodes = embed_nodes
        # WALK SPEC (schema.compile_walk_spec) — declared structure cards made authoritative: per-PATH
        # node-type renames, edge-type/direction overrides, skips, promotions. Empty = pure auto mode.
        self.spec = spec or {}
        # Exact-first, wildcard-second. Built once; a spec with no `*` behaves identically.
        self._skips = _PathSet(self.spec.get("skip_paths") or ())
        self._ix_node_types = _PathIndex(self.spec.get("node_types") or {})
        self._ix_edge_types = _PathIndex(self.spec.get("edge_types") or {})
        self._ix_edge_dirs = _PathIndex(self.spec.get("edge_dirs") or {})
        self._ix_promotions = _PathIndex(self.spec.get("promotions") or {})
        self._ix_dict_paths = _PathIndex(self.spec.get("dict_paths") or {})
        self._pairs = _SiblingPairs(self.spec.get("sibling_pairs") or [])
        self._ix_embed = _PathSet(self.spec.get("embed_paths") or ())
        # PATTERN RULES (P23) — runtime fallback for paths no card covers: match on the leaf key.
        self._prules = []
        for r in (self.spec.get("rules") or []):
            st, val = (r or {}).get("set") or {}, str((r or {}).get("value") or "").lower()
            if val and st:
                self._prules.append(((r or {}).get("op") or "contains", val, st))
        # CONFIGURABLE ID DETECTOR (P3) — extra id-like key names beyond id/uuid/*_id.
        self._idpats = tuple(str(p).strip().lower() for p in (self.spec.get("id_patterns") or ())
                             if str(p).strip())
        # STRICT: edge types this run must not write, because the mapping is authoritative and did not
        # declare them (conformance.strict_suppressed). Empty unless the mapping asked for it.
        self._suppressed = frozenset(self.spec.get("suppress_edges") or ())
        self._strict = bool(self.spec.get("strict"))
        self._parents = self.spec.get("parents") or {}
        self._ix_parents = _PathIndex(self._parents)
        self._record_nodes: dict = {}     # node type → id, for THIS record only; reset per record
        self._disc = [d for d in (self.spec.get("discriminators") or []) if isinstance(d, dict)]
        # ARRAY FILTERS: drop nulls/empties out of an array before it becomes nodes.
        self._afilters = self.spec.get("array_filters") or {}
        # EDGE ATTRIBUTES: properties that belong on the RELATIONSHIP, readable only while it is drawn.
        self._eattrs = self.spec.get("edge_attributes") or {}
        self._edge_rules = [r for r in (self.spec.get("edge_rules") or [])
                            if isinstance(r, dict) and r.get("type") and r.get("from") and r.get("to")]
        #: (node_type, field, value) → node_id. Bounded on purpose: only the (type, field) pairs some
        #: rule actually joins ON are indexed, so this cannot grow into a second copy of the graph.
        self.typed_index: dict = {}
        self._join_targets: set = set()
        for _r in self._edge_rules:
            for _side in ("from", "to"):
                _ref = _r.get(_side) or {}
                if (_ref.get("kind") or "self") == "join" and _ref.get("type"):
                    self._join_targets.add((str(_ref["type"]),
                                            str(_ref.get("on") or _ref.get("field") or "id")))
        self.pending_edges: list = []     # declared edges, resolved once every node exists
        self.unresolved_joins: list = []  # declared joins that matched nothing — reported, never dropped
        self.counts = {"nodes": 0, "edges": 0, "declared_edges": 0}
        self.created: dict = {}           # type → [node ids]; filled only when collect_ids is on

    def _discriminated(self, obj: dict, default: str) -> str:
        """The node type a mapping's discriminator rules give THIS object, else `default`."""
        if not self._disc or not isinstance(obj, dict):
            return default
        for d in self._disc:
            raw = obj.get(d.get("field"))
            if raw is None:
                continue
            val, want, op = str(raw), str(d.get("value") or ""), (d.get("op") or "equals")
            hit = (val == want if op == "equals"
                   else want.lower() in val.lower() if op == "contains"
                   else bool(re.search(want, val)) if op == "regex" else False)
            if hit and d.get("node_type"):
                return d["node_type"]
        return default

    def _filter_array(self, path: str, values: list) -> list:
        """Apply the mapping's array filters to one array before it is walked."""
        f = self._afilters.get(path if path.endswith("[]") else path + "[]")
        if not f or not isinstance(values, list):
            return values
        out = values
        if f.get("flatten"):
            flat = []
            for v in out:
                flat.extend(v) if isinstance(v, list) else flat.append(v)
            out = flat
        if f.get("skip_nulls"):
            out = [v for v in out if v is not None]
        if f.get("skip_empty"):
            out = [v for v in out if v not in ("", [], {})]
        return out

    def _edge_props(self, etype: str, rec) -> dict:
        """Properties the mapping puts ON this edge, read from the record it is being drawn for."""
        specs = self._eattrs.get(etype)
        if not specs or not isinstance(rec, dict):
            return {}
        out = {}
        for s in specs:
            v = rec.get(s.get("from_field"))
            if v not in (None, ""):
                out[s.get("prop")] = v
        return out

    # ── spec lookups (path = the structure-card schema path of the branch/field) ──
    def _rule(self, path) -> dict:
        """First pattern rule whose key pattern matches this path's LEAF key — {} when none."""
        if not self._prules:
            return {}
        leaf = str(path).replace("[]", "").rsplit(".", 1)[-1].lower()
        for op, val, st in self._prules:
            try:
                if (op == "equals" and leaf == val) or (op == "regex" and re.search(val, leaf)) \
                        or (op == "contains" and val in leaf):
                    return st
            except re.error:
                continue
        return {}

    def _nt(self, path, default):
        """The node's TYPE. Only DECLARED sources may override the structural default — a card whose
        `node_type` was merely derived from the key lands in `node_semantics` (see `_sem`), never
        here, so auto-discovery cannot reintroduce a type per key through the back door."""
        return self._ix_node_types.get(path) or self._rule(path).get("node_type") or default

    def _sem(self, path, default):
        """What the node MEANS: a declared type if there is one, else the card's derived suggestion,
        else the key-derived name. Used for the label, the `key` attribute, the embedded text head and
        edge names — everything that has to stay readable when the type is structural."""
        return (self._ix_node_types.get(path)
                or (self.spec.get("node_semantics") or {}).get(path)
                or self._rule(path).get("node_type") or default)

    def _et(self, path, default):
        return self._ix_edge_types.get(path) or self._rule(path).get("edge_type") or default

    def _skip_p(self, path) -> bool:
        return path in self._skips or (path + "[]") in self._skips or bool(self._rule(path).get("skip"))

    def _undeclared(self, path) -> bool:
        """STRICT only: this branch is not part of the mapping's vocabulary, so do not walk it.

        "Declared" is read the same way the walk itself reads it — a node type, a re-parenting, a
        promotion, a dictionary-as-instances rule or a pattern rule at this path, matched through the
        same `_PathIndex` so a wildcard row covers the family it was written for. Anything a container
        row declares also keeps its element path (`sessions` keeps `sessions[]`), because the two are
        one declaration in the author's head and splitting them would silently drop the elements.

        Always False when the mapping is not strict: the default reading rules exist precisely to
        describe a source nobody has described yet.
        """
        if not self._strict:
            return False
        p = str(path or "")
        if not p:                                   # the record root is always the mapping's own
            return False
        base = p[:-2] if p.endswith("[]") else p
        for cand in (p, base, base + "[]"):
            if (self._ix_node_types.get(cand)
                    or (self.spec.get("node_semantics") or {}).get(cand)
                    or self._ix_parents.get(cand)
                    or self._ix_promotions.get(cand)
                    or self._ix_dict_paths.get(cand)
                    or self._ix_edge_types.get(cand)
                    or self._rule(cand).get("node_type")):
                return False
        return True

    def _is_idkey(self, k) -> bool:
        """Id-like key: builtin detector + configured id patterns + pattern rules that set role=id."""
        kl = str(k).lower()
        return _id_key(k) or any(p in kl for p in self._idpats) or self._rule(kl).get("role") == "id"

    def _parent_for(self, path, natural):
        """Who owns this branch: the declared parent node TYPE if the mapping re-parented it and that
        node exists in this record, else the natural JSON parent. Falling back rather than dropping the
        edge matters — a record that happens not to contain the declared parent still belongs to its
        payload, and a silently missing edge is the failure this whole area keeps producing."""
        want = self._parents.get(path)
        if not want:
            return natural
        return self._record_nodes.get(want) or natural

    async def _link(self, parent, child, path, default_etype, rec=None):
        """Create the structural edge for a branch, honouring the card's edge-type override, its
        declared parent, and direction ('in' flips it: child → parent, e.g. Module -REPORTS_TO→ Record)."""
        parent = self._parent_for(path, parent)
        et = self._et(path, default_etype)
        props = self._edge_props(et, rec)      # mapping-declared properties ON the relationship
        if self._ix_edge_dirs.get(path) == "in":
            await self._edge(child, parent, et, props)
        else:
            await self._edge(parent, child, et, props)

    async def _node(self, *, type, label, attrs, det_key, source_ref, valid_from=None, text=None,
                    semantic=None):
        nid = uuid.uuid5(_JNS, f"{self.org}:{type}:{det_key}")
        props = {k: v for k, v in (attrs or {}).items() if v not in (None, "")}
        pairs = "; ".join(f"{k}={v}" for k, v in props.items() if _is_scalar(v))
        _head_name = semantic if (type == _KEYNODE and semantic) else type
        head = (f"{_head_name} {label}".strip() if label and str(label) != str(_head_name)
                else str(_head_name or ""))
        summary = text or ". ".join(p for p in (head, pairs) if p)

        _summary = (summary or label)[:4000]
        generated = []                          # fields this reader writes, not the source: never compared
        if props.get("text") in (None, ""):
            if type != _KEYNODE:
                props["text"] = _summary
                generated.append("text")
            else:
                props["__text"] = _summary
        else:
            props["__text"] = _summary

        props["__source"] = "json"

        if semantic and type == _KEYNODE and "Label" not in props:
            props["Label"] = semantic
            generated.append("Label")
        if valid_from and "valid_from" not in props:
            props["valid_from"] = valid_from
            generated.append("valid_from")
        if generated:
            props["__generated"] = generated
        # Vectors are written by the deferred embedding pass over what this walk created.
        _has_content = bool(pairs) or bool(text) or (summary or label)[:4000].strip() != head.strip()
        if not _has_content:
            props["__structural"] = True          # why it has no vector, visible rather than implied
        with self._evidence_unit(det_key, _summary, source_ref):
            await _repo.upsert_node(node_id=nid, workspace_id=self.org, type=type,
                                    label=str(label)[:200], properties=props, source_ref=source_ref,
                                    collection_id=self.coll, confidence=0.9)
        self.counts["nodes"] += 1
        # First node of each type in this record — what a declared re-parent resolves against.
        self._record_nodes.setdefault(type, nid)
        if self.created is not None and isinstance(self.created, dict) and len(self.created.get(type, [])) < 50000:
            self.created.setdefault(type, []).append(str(nid))
        if self._join_targets:
            for k, v in (attrs or {}).items():
                if (type, k) in self._join_targets and isinstance(v, (str, int, float)):
                    self.typed_index.setdefault((type, k, str(v)), nid)
        return nid

    def _evidence_unit(self, det_key, quote, asset=None):
        """The record a node or edge comes from, as its evidence (core/dataplane/evidence.py). The
        record's deterministic key is its id in the asset's Delta table too, so a pointer opens it."""
        import contextlib

        from opencortyx.core.dataplane import evidence
        if det_key is not None:
            self._last_unit = (asset, getattr(self, "_record_key", None) or det_key, quote)
        asset, key, quote = getattr(self, "_last_unit", None) or (None, None, None)
        if evidence.current() is not None or not asset or key is None:
            return contextlib.nullcontext()
        return evidence.unit(asset, "record", key, quote=str(quote or "")[:300] or None,
                             extractor="semistructured walker")

    async def _edge(self, src, dst, etype, props=None):
        if etype in self._suppressed:
            return
        with self._evidence_unit(None, None):
            created = bool(src and dst and await _repo.add_edge(
                workspace_id=self.org, source_id=src, target_id=dst, type=etype,
                properties=props or {}, dedupe=True))
        if created:
            self.counts["edges"] += 1

    async def walk_object(self, obj: dict, *, key, parent_id, edge_type, depth, det_prefix,
                          date_hint=None, field_map=None, type_override=None,
                          path=""):
        if depth <= 1:
            self._record_nodes = {}
            self._record_key = det_prefix         # the record a nested node's evidence opens
        fm = dict(field_map or {})
        if self.spec:
            sp = self._ix_promotions
            for k, v in obj.items():
                p = _jp(path, k)
                if k in fm:
                    continue
                if self._skip_p(p):
                    fm[k] = "skip"
                elif p in sp and _promotable(v):
                    fm[k] = {"as": "node", **sp[p]}
        # EMBEDDED objects (P15): decision=embed folds the object's scalars into THIS node as
        # prefixed attributes (address → address_city, address_street) instead of a child node.
        emb = self._ix_embed
        if emb and any(_jp(path, k) in emb and isinstance(v, dict) for k, v in obj.items()):
            obj = dict(obj)                        # never mutate the caller's record
            folded = {}
            for k in [k for k in obj if _jp(path, k) in emb and isinstance(obj[k], dict)]:
                _embed_flat(obj.pop(k), k, folded)
            obj.update(folded)

        forced_attrs = {}
        promotions = {}
        if fm:
            import json as _json
            kept = {}
            for k, v in obj.items():
                raw = fm.get(k)
                dec = raw.get("as") if isinstance(raw, dict) else raw
                if dec == "skip":
                    continue
                if dec == "node" and isinstance(raw, dict) and _promotable(v):
                    promotions[k] = (raw, v)              # scalar / scalar-array → its OWN node(s)
                    continue
                if dec == "attribute" and not _is_scalar(v):
                    forced_attrs[k] = _json.dumps(v, default=str)[:2000]
                    continue
                kept[k] = v
            obj = kept

        scalars = {k: v for k, v in obj.items() if _is_scalar(v)}
        scalars.update(forced_attrs)
        # PRODUCTION PARSING: For semi-structured JSON, we keep all scalar values (even long text)
        # as node attributes rather than splitting them into separate Document chunk nodes.
        text_scalars = {}
        attrs = scalars
        arrays = {k: v for k, v in obj.items() if isinstance(v, list)}
        objects = {k: v for k, v in obj.items() if isinstance(v, dict)}

        _base = type_override or ("Turn" if _looks_like_turn(obj) else _type_of(key))
        sem = self._sem(path, _base)
        _dflt = _base if (type_override or _looks_like_turn(obj)) else _KEYNODE
        type_ = self._discriminated(obj, self._nt(path, _dflt))

        inc_k = self.spec.get("include_keys") or self.spec.get("include_fields")
        exc_k = self.spec.get("exclude_keys") or self.spec.get("exclude_fields")
        keep_node, attrs = evaluate_key_filters(type_, attrs, inc_k, exc_k)
        if not keep_node:
            return None

        # IDENTITY: a node's id is generated from where it sits — its file, then its path and position
        # in it — never from a value in the data. An `id`/`*_id` field is an attribute like any other.
        # Taking identity from those values merged the same id across files into one node, so the file
        # read last silently overwrote what the first said; whether two records are the same thing is
        # the Resolution Engine's decision (compared, scored, reviewed, conflicts kept), not ingest's.
        det = det_prefix
        vf = next((v for k, v in attrs.items() if _date_key(k) and _looks_date(v)), None) or date_hint
        label = next((attrs[k] for k in ("name", "title", "label", "id") if attrs.get(k)),
                     next((v for v in attrs.values() if isinstance(v, str) and v.strip()), sem))

        nid = await self._node(type=type_, label=label, attrs=attrs, det_key=det,
                               source_ref=parent_id, valid_from=vf, semantic=sem)
        if parent_id and edge_type:
            await self._link(parent_id, nid, path, edge_type, rec=obj)
        self._queue_declared_edges(nid, type_, path, attrs)
        for k, v in text_scalars.items():          # long prose → its own Document node (HAS_DOCUMENT)
            if self._undeclared(_jp(path, k)):
                continue
            await self._document(nid, k, v, det)
        for k, (raw, v) in promotions.items():     # scalar/array fields the user promoted to nodes
            await self._promote(nid, k, v, raw, det, path=_jp(path, k))

        if depth < self.max_depth:
            dp = self._ix_dict_paths
            for k, v in objects.items():
                cpath = _jp(path, k)
                if self._undeclared(cpath):        # strict: not the mapping's vocabulary → not walked
                    continue
                if cpath in dp:                    # DICTIONARY-AS-INSTANCES (P6): keys are data —
                    dm = dp[cpath]                 # one node per key, the key stored as an attribute
                    etype = dm.get("edge_type") or f"HAS_{_singular(k).upper()}"
                    for kk, vv in v.items():
                        if isinstance(vv, dict):
                            await self.walk_object({**vv, (dm.get("key_attr") or "key"): kk},
                                                   key=_singular(k), parent_id=nid, edge_type=etype,
                                                   depth=depth + 1, det_prefix=f"{det}:{k}:{kk}",
                                                   path=cpath)
                    continue
                await self.walk_object(v, key=k, parent_id=nid, edge_type=f"HAS_{_ks(k).upper()}",
                                       depth=depth + 1, det_prefix=f"{det}:{k}", path=cpath)
            pair_hints = self._pairs.hints_for(path, obj) if self._pairs else {}
            await self._walk_arrays(arrays, parent_id=nid, depth=depth, det_prefix=det, date_hint=vf,
                                    path=path, pair_hints=pair_hints)
        return nid

    async def _walk_arrays(self, arrays: dict, *, parent_id, depth, det_prefix, date_hint=None,
                           path="", pair_hints=None):
        # Mapping-declared array filters run FIRST: a skipped element must never become a node.
        arrays = {k: self._filter_array(_jp(path, k), v) for k, v in (arrays or {}).items()}
        nonscalar = {k: v for k, v in arrays.items() if v and not _is_scalar(v[0])}
        scalararr = {k: v for k, v in arrays.items() if not v or _is_scalar(v[0])}
        # declared attachments at THIS path: a value list attached positionally onto the elements
        # of the primary array whose node type matches `target` (dates → each Session).
        att = [a for a in (self.spec.get("attachments") or []) if (a.get("parent") or "") == path]
        consumed = set()
        for pk, pv in nonscalar.items():            # primary array(s) of objects/arrays
            kpath = _jp(path, pk) + "[]"
            if self._undeclared(kpath):             # strict: undeclared array → not walked
                continue
            psem = _type_of(pk)
            ptype = self._nt(kpath, _KEYNODE)
            sats = {k: v for k, v in scalararr.items()
                    if k != pk and len(v) == len(pv)
                    and (_shares_prefix(k, pk)
                         or any(a.get("key") == k and a.get("target") in (ptype, psem) for a in att))}
            # A paired sibling BEATS the parent's date: it was declared for this array by name,
            # while the parent's is a fallback that applies to everything under it.
            await self._walk_primary(pk, pv, sats, parent_id=parent_id, depth=depth,
                                     det_prefix=det_prefix,
                                     date_hint=(pair_hints or {}).get(pk) or date_hint, path=path)
            consumed |= set(sats.keys())
        leftover = {k: v for k, v in scalararr.items() if k not in consumed}
        list_attrs = {}
        for k, v in leftover.items():
            if self._is_idkey(k) and v and all(isinstance(x, str) for x in v):
                list_attrs[k] = v                    # ids stay values; a mapping's join rule links them
            elif v and all(isinstance(x, str) for x in v):
                kpath = _jp(path, k) + "[]"
                isem = _type_of(k)
                itype = self._nt(kpath, _KEYNODE)
                etype = f"HAS_{_singular(k).upper()}"      # edge stays semantic (derived from the KEY)
                for i, val in enumerate(v[: self.max_array]):
                    if val in (None, ""):
                        continue
                    cid = await self._node(type=itype, label=str(val)[:120], attrs={"name": val},
                                           det_key=f"{det_prefix}:{k}:{i}", source_ref=parent_id,
                                           semantic=isem)
                    await self._link(parent_id, cid, kpath, etype)
            else:
                list_attrs[k] = v
        if list_attrs:                              # numbers/bools → list attributes on the parent
            await _repo.patch_properties(self.org, [parent_id], list_attrs, bump_version=False)

    async def _walk_primary(self, key, arr, satellites, *, parent_id, depth, det_prefix, date_hint=None,
                            path=""):
        kpath = _jp(path, key) + "[]"
        etype = f"HAS_{_singular(key).upper()}"

        apath = _jp(path, key)
        ctype_decl = self._ix_node_types.get(apath)
        holder = parent_id
        if ctype_decl and ctype_decl != self._ix_node_types.get(kpath):
            csem = _type_of(key)
            cdet = f"{det_prefix}:{key}"
            cvf = date_hint
            holder = await self._node(type=ctype_decl, label=f"{csem}", attrs={"count": len(arr)},
                                      det_key=cdet, source_ref=parent_id, valid_from=cvf,
                                      semantic=csem)
            await self._link(parent_id, holder, apath,
                             self._ix_edge_types.get(apath) or f"HAS_{_singular(key).upper()}")
            parent_id = holder
            det_prefix = cdet

        prev = None
        for i, elem in enumerate(arr[: self.max_array]):
            sat = {_sat_name(sk, key): sv[i] for sk, sv in satellites.items()}
            vf = next((v for k, v in sat.items() if _date_key(k) and _looks_date(v)), None) or date_hint
            if isinstance(elem, dict):
                cid = await self.walk_object(elem, key=key, parent_id=parent_id, edge_type=etype,
                                             depth=depth + 1, det_prefix=f"{det_prefix}:{key}:{i}",
                                             date_hint=vf, path=kpath)
                if sat:
                    await self._merge(cid, sat)
            elif isinstance(elem, list):            # array-of-arrays → a container node, recurse inner
                csem = _type_of(key)
                ctype = self._nt(kpath, _KEYNODE)
                det = f"{det_prefix}:{key}:{i}"
                cid = await self._node(type=ctype, label=f"{csem} {i}", attrs={**sat, "index": i},
                                      det_key=det, source_ref=parent_id, valid_from=vf, semantic=csem)
                await self._link(parent_id, cid, kpath, etype)
                if depth < self.max_depth:
                    await self._walk_inner(elem, container_id=cid, depth=depth + 1, det_prefix=det,
                                           valid_from=vf, path=kpath + "[]")
            else:                                   # MIXED array (P5): a scalar among objects → a
                if elem in (None, ""):              # wrapper node {value, index}, so nothing is dropped
                    continue
                wsem = _type_of(key)
                wtype = self._nt(kpath, _KEYNODE)
                cid = await self._node(type=wtype, label=str(elem)[:120],
                                       attrs={**sat, "value": elem, "index": i},
                                       det_key=f"{det_prefix}:{key}:{i}", source_ref=parent_id,
                                       valid_from=vf, semantic=wsem)
                await self._link(parent_id, cid, kpath, etype)
            if prev:
                await self._edge(prev, cid, "NEXT")
            prev = cid

    async def _walk_inner(self, arr, *, container_id, depth, det_prefix, valid_from=None, path=""):
        """Inner (anonymous) array — e.g. a session's turns. Each element becomes a child of the container."""
        prev = None
        for i, elem in enumerate(arr[: self.max_array]):
            if not isinstance(elem, dict):
                continue
            turn = _looks_like_turn(elem)
            type_ = self._nt(path, "Turn" if turn else "Item")
            etype = "HAS_TURN" if turn else "HAS_ITEM"
            scal = {k: v for k, v in elem.items() if _is_scalar(v)}
            explicit_text = scal.get("content") or scal.get("text") or scal.get("message")
            text = explicit_text if explicit_text else extract_prose_context(scal)
            label = scal.get("role") or scal.get("name") or f"{type_} {i}"
            cid = await self._node(type=type_, label=label, attrs={**scal, "seq": i},
                                  det_key=f"{det_prefix}:{i}", source_ref=container_id, text=text, valid_from=valid_from)
            await self._link(container_id, cid, path, etype)
            if prev:
                await self._edge(prev, cid, "NEXT")
            prev = cid

    async def _document(self, parent_id, key, value, det_prefix):
        """A long prose value → its own Document node {type, text} linked HAS_DOCUMENT from its parent —
        auto parsing surfaces readable text as a first-class node (so extractors/keywords bind to it and
        retrieval finds it) instead of burying it in an attribute. `type` labels it from the key; a
        confidential/internal key also stamps a classification."""
        dtype = _label_of(key)
        attrs = {"type": dtype, "text": value}
        if any(w in key.lower() for w in ("confidential", "secret", "private", "internal")):
            attrs["classification"] = "Confidential"
        cid = await self._node(type="Document", label=dtype, attrs=attrs,
                               det_key=f"{det_prefix}:{key}", source_ref=parent_id, text=value)
        await self._edge(parent_id, cid, "HAS_DOCUMENT")

    async def _promote(self, parent_id, key, value, dec, det_prefix, path=""):
        """Promote a scalar (or scalar array) field into its OWN node(s), connected to the parent by
        HAS_<TYPE> (or the card's declared `edge_type`). `dec` = {node_type, attr_name, edge_type}:
        the value is stored under the user-named `attr_name`, so `answer` → an Answer node
        {answer: "…"} and `answer_session_ids[]` → one AnswerSession node {session_id: "…"} per
        element (NEXT-chained, order preserved). An `attr_name` of "text" is the prose-Document
        promotion — the node reads as a document (text = the value, label = the field name).
        Promoted id values still index for REFERENCES, so a promoted *_id can wire back."""
        declared = str(dec.get("node_type") or "").strip()
        nsem = declared or _type_of(key)           # a declared type is used EXACTLY as written
        ntype = declared or _KEYNODE
        attr = dec.get("attr_name") or key
        # Edge from the SEMANTIC name, never the structural type — `HAS_KEYNODE` would throw away the
        # only thing that makes the relationship readable.
        etype = dec.get("edge_type") or f"HAS_{_singular(nsem).upper()}"
        is_doc = attr == "text"
        is_list = isinstance(value, list)
        prev = None
        for i, v in enumerate(value if is_list else [value]):
            if v in (None, ""):
                continue
            attrs = {attr: v, "index": i} if is_list else {attr: v}
            cid = await self._node(type=ntype, label=(_label_of(key) if is_doc else str(v)[:120]),
                                   attrs=attrs, det_key=f"{det_prefix}:{key}:{i}", source_ref=parent_id,
                                   text=(v if is_doc and isinstance(v, str) else None), semantic=nsem)
            await self._link(parent_id, cid, path, etype)
            if is_list and prev:
                await self._edge(prev, cid, "NEXT")
            prev = cid

    async def _merge(self, node_id, attrs):
        n = await _repo.get(self.org, node_id)
        if not n:
            return
        have = n.properties or {}
        add = {}
        for k, v in attrs.items():
            if v not in (None, "") and k not in have:
                add.setdefault(k, v)
                if _date_key(k) and _looks_date(v) and "valid_from" not in have:
                    add.setdefault("valid_from", v)
        if add:
            await _repo.patch_properties(self.org, [node_id], add, bump_version=False)

    def _queue_declared_edges(self, nid, ntype, path, attrs):
        """A node just landed at `path`. Any edge rule anchored there gets one attempt, queued with
        the record it was read from — resolution waits until the whole payload has been walked, so an
        edge naming a node that appears LATER in the file costs nothing.

        `_record_nodes` is snapshotted per anchor because a `context` endpoint means "the enclosing
        node of that type in THIS record" — the same scoping re-parenting already uses. Sharing one
        live dict would let record N's edge resolve against record N-1's parent.
        """
        if not self._edge_rules:
            return
        p = str(path or "")
        for r in self._edge_rules:
            rp = str(r.get("path") or "")
            if rp != p and rp != (p + "[]") and (rp[:-2] if rp.endswith("[]") else rp) != p:
                continue
            self.pending_edges.append({"anchor": nid, "type": ntype, "attrs": dict(attrs or {}),
                                       "rule": r, "parents": dict(self._record_nodes)})

    def _resolve_side(self, ref: dict, pend: dict) -> list:
        """One endpoint → the node ids it names. Three of the four reference kinds live here; the
        fourth (position) is already covered by declared zips/attachments, which correlate parallel
        arrays at walk time rather than after it."""
        kind = (ref or {}).get("kind") or "self"
        want = (ref or {}).get("type")
        if kind == "self":
            return [pend["anchor"]] if (not want or want == pend["type"]) else []
        if kind == "context":
            got = pend["parents"].get(want)
            return [got] if got else []
        if kind == "join":
            field = ref.get("field") or "id"
            on = ref.get("on") or field
            raw = pend["attrs"].get(field)
            vals = raw if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
            out = []
            for v in vals:
                if not isinstance(v, (str, int, float)):
                    continue
                got = self.typed_index.get((want, on, str(v)))
                if got:
                    out.append(got)
                else:
                    # Zero data loss: an unresolved join is a REPORTED miss, not a silent drop.
                    self.unresolved_joins.append({"type": want, "on": on, "value": str(v)})
            return out
        return []

    async def resolve_declared_edges(self):
        """Drain the declared edge rules. Every index is complete by now, so this is pure lookup."""
        pending, self.pending_edges = self.pending_edges, []
        for pend in pending:
            r = pend["rule"]
            srcs = self._resolve_side(r.get("from"), pend)
            dsts = self._resolve_side(r.get("to"), pend)
            props = {}
            for prop, field in (r.get("properties") or {}).items():
                v = pend["attrs"].get(field)
                if v not in (None, ""):
                    props[prop] = v
            for s in srcs:
                for d in dsts:
                    if s and d and s != d:
                        if await _repo.add_edge(workspace_id=self.org, source_id=s, target_id=d,
                                                type=str(r["type"]), properties=props or None,
                                                dedupe=True):
                            self.counts["declared_edges"] += 1
                            self.counts["edges"] += 1



def _plain(spec: dict, mapping, root_type) -> bool:
    """Nothing declared: no structure cards, field mapping or root type. The payload is
    then walked by the plain `SemiStructuredWalker` — objects, lists and values become nodes, nothing
    is inferred. Anything declared needs `_Walker`, the only walker that reads declarations."""
    return not (spec or mapping or root_type)


async def ingest_json(workspace_id: uuid.UUID, data: Any, *, source_label: str = "json",
                      collection_id=None, root_type: Optional[str] = None,
                      max_depth: int = 6, max_array: int = 500, max_records: int = 100000,
                      embed_nodes: bool = False, mapping: Optional[dict] = None,
                      collect_ids: bool = False,
                      walk_spec: Optional[dict] = None, auto_zip: bool = True,
                      checksum: Optional[str] = None, replace: bool = False,
                      external: Optional[dict] = None, whole_data: bool = False) -> dict:
    """Walk a parsed JSON payload into the graph. `whole_data` (Add data's "add the whole data"):
    with the records in Delta, walk every key and value into the graph as well. `external` ({format, location, connector_id}):
    the payload was read from a source that stays where it is (a MongoDB collection) - with the
    workspace's data in Delta it is registered in place instead of stored. A `Source` node is created for provenance; a list
    root attaches each element under it (HAS_RECORD), an object root walks directly.

    `mapping` (Mode A, guided) overrides the auto rules for each top-level record field —
    {field: "node"|"attribute"|"skip"}; `root_type` names the record node type. Omit both for pure
    Mode B (auto). Node ids are generated from position, never taken from an id field.

    `walk_spec` (schema.compile_walk_spec) makes DECLARED structure cards authoritative at every
    depth: per-path node types, edge types/directions, skips, promotions, declared zips/attachments,
    identity fields and edge rules. `auto_zip` gates the blanket same-length parallel-array zip — OFF
    when the user declared cards (only their declared correlations are zipped; nothing is inferred).

    `checksum` (content hash of the uploaded bytes) makes re-uploading the SAME file a no-op: skipped
    only when the content matches AND the previous run FINISHED (`__ingested_ok`, stamped last), so an
    ingest killed halfway is retried rather than skipped forever as "unchanged"."""
    spec = walk_spec or {}

    def _prep(rec):
        rec = apply_declared_zips(rec, spec.get("zips") or [])
        return normalize_parallel_arrays(rec) if auto_zip else rec

    src_id = uuid.uuid5(_JNS, f"{workspace_id}:source:{source_label}")
    if checksum and not replace:            # `replace`: the user asked for it to be read again
        prev = await _repo.get(workspace_id, src_id)
        pp = (prev.properties or {}) if prev else {}
        if pp.get("__checksum") == checksum and pp.get("__ingested_ok"):
            return {"status": "unchanged", "duplicate": True, "source": source_label,
                    "source_id": str(src_id), "nodes": 0, "edges": 0, "records": 0}
    src = await _repo.upsert_node(workspace_id=workspace_id, type="Source", label=source_label,
                                  properties={"filename": source_label, "__source": "json",
                                              **({"checksum": checksum, "__checksum": checksum}
                                                 if checksum else {})},
                                  collection_id=collection_id, node_id=src_id)
    delta_mode = await _uses_delta(workspace_id)
    rows: list = []
    if _plain(spec, mapping, root_type):
        walker = SemiStructuredWalker(workspace_id=workspace_id, repo=_repo, max_depth=max_depth, max_array=max_array,
                                      default_node_type=_KEYNODE)
        if delta_mode:
            # Nothing declared: every record a row in the data store, its fields profiled as the
            # asset's Attributes - and no node per record or per nested object. What the records
            # MEAN comes from a mapping (declared or detected), not from walking each one.
            is_list = isinstance(data, list)
            elems = list(enumerate(data[:max_records])) if is_list else [(0, data)]
            rows = [_sdp.record_row(f"{source_label}:{i}" if is_list else source_label, i, e) for i, e in elems]
            res = {"nodes": 0, "edges": 0, "created_by_type": {}}
            await _store_records(workspace_id, src.id, rows, source_label, {}, collection_id, spec, external)
            if whole_data:
                res = await walker.walk(data, source_label=source_label, collection_id=collection_id,
                                        root_id=src.id)
        else:
            res = await walker.walk(data, source_label=source_label, collection_id=collection_id,
                                    root_id=src.id)
        if checksum:
            await _repo.patch_properties(workspace_id, [src.id], {"__ingested_ok": True}, bump_version=False)
        out = {"status": "ok", "duplicate": False, "source": source_label, "source_id": str(src.id),
               "records": len(data) if isinstance(data, list) else 1,
               "nodes": res["nodes"], "edges": res["edges"],
               "unresolved_join_count": 0, "unresolved_joins": []}
        if collect_ids:
            out["created_by_type"] = walker.created_by_type
        return out

    walker = _Walker(workspace_id, collection_id, max_depth=max_depth, max_array=max_array,
                     embed_nodes=embed_nodes, spec=spec)
    rtype = _type_of(root_type) if root_type else None
    records = 0
    if isinstance(data, list):
        for i, elem in enumerate(data[:max_records]):
            if isinstance(elem, dict):
                if delta_mode:
                    rows.append(_sdp.record_row(f"{source_label}:{i}", i, elem))
                await walker.walk_object(_prep(elem), key=root_type or "record", parent_id=src.id,
                                         edge_type="HAS_RECORD", depth=1, det_prefix=f"{source_label}:{i}",
                                         field_map=mapping, type_override=rtype)
                records += 1
    elif isinstance(data, dict):
        if delta_mode:
            rows.append(_sdp.record_row(source_label, 0, data))
        await walker.walk_object(_prep(data), key=root_type or "record", parent_id=src.id,
                                 edge_type="HAS_RECORD", depth=1, det_prefix=source_label,
                                 field_map=mapping, type_override=rtype)
        records = 1
    await walker.resolve_declared_edges()   # declared join rules within this file
    if delta_mode:
        await _store_records(workspace_id, src.id, rows, source_label, walker.created, collection_id, spec,
                             external)
    if checksum:
        # Stamped LAST: a crash before this point leaves no `__ingested_ok`, so the next upload
        # re-walks instead of being skipped forever as an unchanged duplicate.
        await _repo.patch_properties(workspace_id, [src.id], {"__ingested_ok": True}, bump_version=False)
    # A declared join that matched nothing is a DEFECT in the mapping or the data, not a silence to
    # discover later — report it with the value that missed, capped so a broken rule cannot flood.
    out = {"status": "ok", "duplicate": False, "source": source_label,
           "source_id": str(src.id), "records": records, **walker.counts,
           "unresolved_join_count": len(walker.unresolved_joins),
           "unresolved_joins": walker.unresolved_joins[:50]}
    if collect_ids:                           # per-type created ids — for per-NODE extractor attachment
        out["created_by_type"] = walker.created
    return out


async def ingest_mongo_source(workspace_id: uuid.UUID, connector_id, collection_id=None,
                              only=None, max_records: int = 100000) -> dict:
    """A MongoDB database: each collection walked like a JSON file of its documents - the same
    KeyNodes, HAS_<KEY> edges, references and categorical values - read through the adapter
    (stores/data/adapters/mongodb.py) with the documents never copied: with the workspace's data in
    Delta each collection is registered as an external SemiStructuredAsset."""
    import anyio

    from opencortyx.core.dataplane.connectors import connector_url
    from opencortyx.stores.data.adapters.mongodb import MongoAdapter, database_of
    url = await connector_url(workspace_id, connector_id)
    adapter = await anyio.to_thread.run_sync(lambda: MongoAdapter(url, database_of(url)))
    try:
        names = await anyio.to_thread.run_sync(adapter.collections)
        if only:
            names = [n for n in names if n in set(only)]
        out = {"status": "ok", "collections": 0, "records": 0, "nodes": 0, "edges": 0, "per_collection": []}
        db = adapter.db.name
        for name in names:
            docs = await anyio.to_thread.run_sync(lambda n=name: list(adapter.documents(n, limit=max_records)))
            if not docs:
                continue
            res = await ingest_json(workspace_id, docs, source_label=f"mongodb:{db}.{name}",
                                    collection_id=collection_id, max_records=max_records, collect_ids=True,
                                    external={"format": "mongodb", "location": name,
                                              "connector_id": connector_id})
            out["collections"] += 1
            for k in ("records", "nodes", "edges"):
                out[k] += int(res.get(k) or 0)
            out["per_collection"].append({"collection": name, "source_id": res.get("source_id"),
                                          "records": res.get("records")})
        out["source_key"] = f"mongodb:{db}"
        return out
    finally:
        adapter.close()


async def _uses_delta(workspace_id) -> bool:
    from opencortyx.ingest.structured.dataplane import uses_delta
    return await uses_delta(workspace_id)


def _record_unit(asset_id, record_id: str, value):
    """The record everything written while walking it rests on (core/dataplane/evidence.py)."""
    import json as _json

    from opencortyx.core.dataplane import evidence
    quote = _json.dumps(value, ensure_ascii=False, default=str)[:300] if value is not None else None
    return evidence.unit(asset_id, "record", record_id, quote=quote, extractor="semistructured walker")


async def _store_records(workspace_id, asset_id, rows, source_label, created: dict, collection_id, spec,
                         external: Optional[dict] = None):
    """The records into Delta, the fields as Attribute nodes, the declared joins as JOINS_TO."""
    types = [t for t, ids in (created or {}).items() if ids]
    fmt = source_label.rsplit(".", 1)[-1].lower() if "." in str(source_label) else "json"
    out = await _sdp.store_records(workspace_id, asset_id, rows, name=source_label, fmt=fmt,
                                   source_key=f"json:{source_label}", record_types=types,
                                   collection_id=collection_id, external=external)
    if (spec or {}).get("edge_rules"):
        out["joins"] = await _sdp.link_join_rules(workspace_id, asset_id, spec["edge_rules"], types)
    return out


async def ingest_json_stream(workspace_id: uuid.UUID, source, *, source_label: str = "json",
                             collection_id=None, root_type: Optional[str] = None,
                             max_depth: int = 6, max_array: int = 500, max_records: int = 100000,
                             embed_nodes: bool = False, mapping: Optional[dict] = None,
                             collect_ids: bool = False, reporter=None,
                             walk_spec: Optional[dict] = None, auto_zip: bool = True) -> dict:
    """STREAMING ingest for large JSON — constant memory. `source` is a filesystem path or a binary
    file-like; the top-level ARRAY is iterated one record at a time with ijson (never loaded whole).

    A top-level OBJECT (not an array) has nothing to stream → falls back to the in-memory `ingest_json`."""
    import ijson

    opened = None
    if isinstance(source, (str, bytes, os.PathLike)):
        opened = open(source, "rb")
        f = opened
    else:
        f = source
    n = 0
    spec = walk_spec or {}
    plain = _plain(spec, mapping, root_type)
    delta_mode = await _uses_delta(workspace_id)
    rows: list = []
    walker = _Walker(workspace_id, collection_id, max_depth=max_depth, max_array=max_array,
                     embed_nodes=embed_nodes, spec=spec)
    simple = SemiStructuredWalker(workspace_id=workspace_id, repo=_repo, max_depth=max_depth, max_array=max_array,
                                  default_node_type=_KEYNODE)
    try:
        src = await _repo.upsert_node(workspace_id=workspace_id, type="Source", label=source_label,
                                      properties={"filename": source_label, "__source": "json"},
                                      collection_id=collection_id,
                                      node_id=uuid.uuid5(_JNS, f"{workspace_id}:source:{source_label}"))
        rtype = _type_of(root_type) if root_type else None
        for rec in ijson.items(f, "item"):        # 'item' = each element of the top-level array
            if n >= max_records:
                break
            if plain and isinstance(rec, (dict, list)):
                # one Source for the file; `path_suffix` keeps same-shaped records on distinct ids
                rid = f"{source_label}:{n}"
                if delta_mode:
                    rows.append(_sdp.record_row(rid, n, rec))   # a row, not a node (see ingest_json)
                    n += 1
                    continue
                with _record_unit(src.id, rid, rec):
                    res = await simple.walk(rec, source_label=source_label, collection_id=collection_id,
                                            root_id=src.id, path_suffix=str(n))
                walker.counts["nodes"] += res["nodes"]
                walker.counts["edges"] += res["edges"]
                for t, ids in res["created_by_type"].items():
                    walker.created.setdefault(t, []).extend(ids)
                n += 1
                if reporter is not None and n % 500 == 0:
                    await reporter.step(1, f"Streamed {n} records", "running", walker.counts["nodes"])
            elif isinstance(rec, dict):
                rec = apply_declared_zips(rec, spec.get("zips") or [])
                if auto_zip:
                    rec = normalize_parallel_arrays(rec)
                if delta_mode:
                    rows.append(_sdp.record_row(f"{source_label}:{n}", n, rec))
                await walker.walk_object(rec, key=root_type or "record", parent_id=src.id,
                                         edge_type="HAS_RECORD", depth=1,
                                         det_prefix=f"{source_label}:{n}",
                                         field_map=mapping, type_override=rtype)
                n += 1
                if reporter is not None and n % 500 == 0:
                    await reporter.step(1, f"Streamed {n} records", "running", walker.counts["nodes"])
        await walker.resolve_declared_edges()   # declared join rules within this file
        if delta_mode and rows:
            await _store_records(workspace_id, src.id, rows, source_label, walker.created,
                                 collection_id, spec)
    finally:
        if opened is not None:
            opened.close()

    if n == 0 and opened is not None:                 # top-level object (nothing to stream) → full parse
        import json as _json
        with open(source, "rb") as fo:
            data = _json.load(fo)
        return await ingest_json(workspace_id, data, source_label=source_label, collection_id=collection_id,
                                 root_type=root_type, max_depth=max_depth, max_array=max_array,
                                 max_records=max_records, embed_nodes=embed_nodes, mapping=mapping,
                                 collect_ids=collect_ids,
                                 walk_spec=walk_spec, auto_zip=auto_zip)
    out = {"status": "ok", "source": source_label, "source_id": str(src.id),
           "records": n, "streamed": True, **walker.counts}
    if collect_ids:
        out["created_by_type"] = walker.created
    return out
