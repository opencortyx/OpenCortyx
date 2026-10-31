"""The Detector: the semantic layer proposed from the data itself.

    1. sources    every place records live: a table's rows; in a JSON / YAML / XML record, the
                  record itself, an object inside it ("$.project"), each element of a list inside
                  it ("$.customers[*]"), each element of a list of lists ("$.sessions[*][*]") -
                  found by reading a sample of the records. A list's element carries what places it:
                  its position, the record's identifying fields, and the values of the lists beside
                  it at the same position (session_ids[i] and dates[i] belong to sessions[i])
    2. groups     sources of one shape together (fifty licence files are one group of rows)
    3. profile    per field: how many values, how many distinct, how many empty, whether the values
                  read as numbers or dates, and - for a field with few values - its value set
    4. keys       a field that is never empty and never repeats identifies a record. When a group has
                  several, the one other data refers to wins (its values turn up in other fields); a
                  field whose values all come from a LARGER unique field elsewhere is a reference, not
                  a key. A tie that the data cannot break is the model's choice among the candidates
    5. types      groups whose keys hold the same values are one type (a customer in the CRM and the
                  same customer in billing); a group with no key is an event table (sales lines)
    6. links      a field whose values are (almost) all keys of another type refers to it: an
                  inclusion dependency, the statistical basis of foreign-key discovery
    7. names      what one record is called, chosen by the model among the words the data uses for it
                  (its files' names, its key's name, the path it sits at)

Confidence decides what applies without review (store.AUTO_ACCEPT); the rest waits in the Mapper.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from opencortyx.core.observability import get_logger
from opencortyx.knowledge.semantic.store import AUTO_ACCEPT, Binding, Layer, LinkType, ObjectType, Property, save_detected
from opencortyx.knowledge.semantic.views import binding_sql, q

log = get_logger(__name__)

SAMPLE = 200                 # records read per semi-structured asset to find its record paths
VALUES_CAP = 50000           # distinct values kept per field for overlap checks
CATEGORY_MAX = 50            # a field with at most this many distinct values has a value set
CONTAINED = 0.9              # share of a field's values found among a key's values: a reference
PROSE_WORDS = 8              # median words per value from which a text field is free text (prose)
MIN_LINK_VALUES = 3          # distinct values a link rests on: two small numbers inside another type's
                             # keys ({1, 2} in ids {1, 2}) are a coincidence, not a reference
LABEL_UNIQUE = 0.8           # share of distinct values a short text field needs to name its records
_WORD = re.compile(r"[A-Za-z]+")


@dataclass
class Source:
    asset_id: str
    asset_name: str
    kind: str
    view: str
    path: str
    fields: dict                                     # property name -> field (column or dotted path)


@dataclass
class FieldStats:
    rows: int = 0
    filled: int = 0
    distinct: int = 0
    data_type: str = "text"
    values: set = field(default_factory=set)
    value_set: dict = field(default_factory=dict)
    words: float = 0.0                               # median words per value (text fields)
    repeats: int = 0                                 # most times one file repeats a value of this field


@dataclass
class Group:
    kind: str
    path: str
    sources: list
    fields: dict                                     # property -> field
    rows: int = 0
    stats: dict = field(default_factory=dict)        # property -> FieldStats
    key: str = ""
    key_confidence: float = 0.0


# ── 1. sources ─────────────────────────────────────────────────────────────────────────────────

def _scalar(v) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _fields_of(obj: dict) -> dict:
    """Scalar fields of `obj`, and of each object directly inside it ("contact.email")."""
    out = {}
    for k, v in obj.items():
        if _scalar(v):
            out[str(k)] = str(k)
        elif isinstance(v, dict):
            for k2, v2 in v.items():
                if _scalar(v2):
                    out[f"{k}_{k2}"] = f"{k}.{k2}"
    return out


def _words(v) -> int:
    return len(str(v).split())


def _record_fields(recs: list, top: dict) -> dict:
    """The record's fields every list element carries, so it links back to its record: plain
    top-level values that are filled and different in every record, and short."""
    out = {}
    for k in top:
        if "." in top[k]:
            continue
        vals = [r.get(k) for r in recs]
        if all(_scalar(v) and v not in (None, "") and _words(v) < PROSE_WORDS for v in vals) \
                and len({str(v) for v in vals}) == len(vals) and len(vals) >= 2:
            out[k] = f"^.{k}"
    return out


def _beside(recs: list, k: str, keys: set) -> list[str]:
    """Lists of plain values that sit beside list `k`: as long as it in (nearly) every record, so
    their i-th value belongs to its i-th element."""
    out = []
    for s in sorted(keys - {k}):
        pairs = [(r.get(k), r.get(s)) for r in recs if isinstance(r.get(k), list) and r.get(k)]
        if pairs and sum(isinstance(b, list) and len(b) == len(a) and all(_scalar(x) for x in b)
                         for a, b in pairs) >= 0.9 * len(pairs):
            out.append(s)
    return out


def _with(own: dict, extra: dict) -> dict:
    """`own` fields, then `extra` ones under their names - or, when an own field has the name,
    as "record_<name>"."""
    out = dict(own)
    for k, v in extra.items():
        out[k if k not in out else f"record_{k}"] = v
    return out


def _semi_sources(asset_id, name, view, raws: list) -> list[Source]:
    recs = []
    for r in raws:
        try:
            v = json.loads(r) if isinstance(r, str) else r
        except ValueError:
            continue
        if isinstance(v, dict):
            recs.append(v)
    if not recs:
        return []
    half = max(1, len(recs) // 2)
    out = []
    top: dict = {}
    for r in recs:
        for k, v in _fields_of({k: v for k, v in r.items() if _scalar(v)}).items():
            top[k] = v
    if top:
        out.append(Source(asset_id, name, "semistructured", view, "", top))
    keys = {k for r in recs for k in r}
    record = _record_fields(recs, top)
    lists = lambda v, test: isinstance(v, list) and bool(v) and all(test(e) for e in v)   # noqa: E731
    used: set = set()
    for k in sorted(keys):
        vals = [r.get(k) for r in recs if k in r]
        beside = {s: f"^.{s}[i]" for s in _beside(recs, k, keys)}
        if sum(isinstance(v, dict) for v in vals) >= half:
            f: dict = {}
            for v in vals:
                if isinstance(v, dict):
                    f.update(_fields_of(v))
            if f:
                out.append(Source(asset_id, name, "semistructured", view, f"$.{k}", f))
        elif sum(lists(v, lambda e: isinstance(e, dict)) for v in vals) >= half:
            f = {}
            for v in vals:
                for e in (v if isinstance(v, list) else []):
                    if isinstance(e, dict):
                        f.update(_fields_of(e))
            if f:
                out.append(Source(asset_id, name, "semistructured", view, f"$.{k}[*]",
                                  _with(f, {**beside, **record, "position": "#i"})))
                used |= set(beside)
        elif sum(lists(v, lambda e: lists(e, lambda x: isinstance(x, dict))) for v in vals) >= half:
            # a list of lists of records (a conversation's sessions, each a list of turns): each
            # inner list is one record placed by the lists beside it, each element of it another
            f = {}
            for v in vals:
                for inner in (v if isinstance(v, list) else []):
                    for e in (inner if isinstance(inner, list) else []):
                        if isinstance(e, dict):
                            f.update(_fields_of(e))
            if beside:
                out.append(Source(asset_id, name, "semistructured", view, f"$.{k}[*]",
                                  _with(beside, {**record, "position": "#i"})))
            if f:
                out.append(Source(asset_id, name, "semistructured", view, f"$.{k}[*][*]",
                                  _with(f, {**beside, **record, "position": "#j"})))
            used |= set(beside)
    # a list of plain values nothing else is beside: each value one record of the record it is in
    for k in sorted(keys - used):
        vals = [r.get(k) for r in recs if k in r]
        if sum(lists(v, _scalar) for v in vals) >= half:
            out.append(Source(asset_id, name, "semistructured", view, f"$.{k}[*]",
                              _with({k: "#v"}, record)))
    return out


def _sources(views, refs) -> tuple[list[Source], dict]:
    """Every record source of every asset, and each view's columns."""
    by_asset = {a: v for v, a in views.names.items()}
    columns: dict = {}
    out: list[Source] = []
    for ref in refs:
        view = by_asset.get(ref.asset_id)
        if not view:
            continue
        try:
            cols = [r["column_name"] for r in views.query(f"DESCRIBE {q(view)}").to_pylist()]
        except Exception:                                     # noqa: BLE001 - an unreadable asset is skipped
            continue
        columns[view] = set(cols)
        if ref.kind == "structured":
            fields = {c: c for c in cols if not c.startswith("_")}
            if fields:
                out.append(Source(ref.asset_id, ref.name, "structured", view, "", fields))
        elif "raw" in cols:
            raws = [r["raw"] for r in views.query(f"SELECT raw FROM {q(view)} LIMIT {SAMPLE}").to_pylist()]
            out.extend(_semi_sources(ref.asset_id, ref.name, view, raws))
    return out, columns


# ── 2. groups ──────────────────────────────────────────────────────────────────────────────────

def _jaccard(a, b) -> float:
    a, b = set(a), set(b)
    return len(a & b) / max(1, len(a | b))


def _group(sources: list[Source]) -> list[Group]:
    groups: list[Group] = []
    for s in sources:
        g = next((g for g in groups if g.kind == s.kind and _shape(g.path) == _shape(s.path)
                  and _jaccard(g.fields, s.fields) >= 0.8), None)
        if g is None:
            groups.append(Group(kind=s.kind, path=s.path, sources=[s], fields=dict(s.fields)))
        else:
            g.sources.append(s)
            for k, v in s.fields.items():
                g.fields.setdefault(k, v)
    return groups


def _shape(path: str) -> str:
    return "list" if path.endswith("[*]") else ("object" if path else "row")


# ── 3. profile ─────────────────────────────────────────────────────────────────────────────────

def _group_type(g: Group) -> ObjectType:
    t = ObjectType(name="_", kind="event", properties=[Property(name=p) for p in g.fields])
    t.bindings = [Binding(asset_id=s.asset_id, asset_name=s.asset_name, kind=s.kind, path=s.path,
                          fields={p: s.fields.get(p, f) for p, f in g.fields.items()}) for s in g.sources]
    return t


def _union(g: Group, columns: dict) -> str:
    t = _group_type(g)
    parts = [binding_sql(t, b, s.view, columns.get(s.view)) for b, s in zip(t.bindings, g.sources)]
    return parts[0] if len(parts) == 1 else " UNION ALL BY NAME ".join(f"({p})" for p in parts)


def _profile(views, g: Group, columns: dict) -> None:
    src = f"({_union(g, columns)}) AS g"
    g.rows = int(views.query(f"SELECT count(*) AS n FROM {src}").to_pylist()[0]["n"])
    for p in g.fields:
        c = q(p)
        r = views.query(
            f"SELECT count({c}) AS filled, count(DISTINCT {c}) AS distinct_n, "
            f"count(TRY_CAST({c} AS DOUBLE)) AS nums, count(TRY_CAST({c} AS DATE)) AS dates FROM {src}").to_pylist()[0]
        st = FieldStats(rows=g.rows, filled=int(r["filled"]), distinct=int(r["distinct_n"]))
        # Repeats are counted within each file: the same customer in the CRM and in billing is one
        # customer seen twice, not a key that repeats.
        st.repeats = int(views.query(
            f"SELECT coalesce(max(n - d), 0) AS r FROM (SELECT count({c}) AS n, count(DISTINCT {c}) AS d "
            f"FROM {src} GROUP BY _asset)").to_pylist()[0]["r"])
        if st.filled:
            if r["dates"] >= 0.95 * st.filled:
                st.data_type = "date"
            elif r["nums"] >= 0.95 * st.filled:
                st.data_type = "number"
        if st.data_type == "text" and st.filled:
            st.words = float(views.query(
                f"SELECT median(len(string_split(trim(CAST({c} AS VARCHAR)), ' '))) AS w FROM {src} "
                f"WHERE {c} IS NOT NULL").to_pylist()[0]["w"] or 0.0)
        vals = views.query(f"SELECT DISTINCT {c} AS v FROM {src} WHERE {c} IS NOT NULL LIMIT {VALUES_CAP}").to_pylist()
        st.values = {str(x["v"]) for x in vals}
        if st.data_type == "text" and 0 < st.distinct <= CATEGORY_MAX and st.distinct <= 0.5 * max(1, st.filled):
            counts = views.query(f"SELECT {c} AS v, count(*) AS n FROM {src} WHERE {c} IS NOT NULL "
                                 f"GROUP BY 1 ORDER BY 2 DESC LIMIT {CATEGORY_MAX}").to_pylist()
            st.value_set = {str(x["v"]): int(x["n"]) for x in counts}
        g.stats[p] = st


# ── 4. keys ────────────────────────────────────────────────────────────────────────────────────

def _contained(small: set, big: set) -> float:
    return len(small & big) / len(small) if small else 0.0


def _unique(g: Group) -> list[str]:
    """Fields never empty and never repeated within a file. A date is a moment and free text a
    statement, not an identity."""
    return [p for p, s in g.stats.items() if g.rows >= 2 and s.filled == g.rows and s.repeats == 0
            and s.data_type != "date" and s.words < PROSE_WORDS]


_SHAPE = (re.compile(r"[A-Za-z]"), re.compile(r"\d"))


def _uniformity(values: set) -> tuple[float, float]:
    """(share of values with the most common shape, minus mean length). Identifiers are written one
    way ("P-000", "P-001": letter, dash, digits); names and free text are not."""
    if not values:
        return 0.0, 0.0
    shapes: dict = defaultdict(int)
    for v in values:
        shapes[_SHAPE[1].sub("9", _SHAPE[0].sub("A", v))] += 1
    return max(shapes.values()) / len(values), -sum(len(v) for v in values) / len(values)


async def _keys(groups: list[Group], choose) -> None:
    uniq = {id(g): _unique(g) for g in groups}
    for g in groups:
        cands = []
        for p in uniq[id(g)]:
            vals = g.stats[p].values
            # A reference: all its values come from a larger unique field of another group.
            if any(_contained(vals, h.stats[u].values) >= CONTAINED and len(h.stats[u].values) > 1.2 * len(vals)
                   for h in groups if h is not g for u in uniq[id(h)]):
                continue
            refs = sum(1 for h in groups if h is not g for f, s in h.stats.items()
                       if len(s.values) >= 2 and _contained(s.values, vals) >= CONTAINED)
            cands.append((refs, p))
        if not cands:
            continue
        cands.sort(key=lambda c: -c[0])
        if cands[0][0] > 0 and (len(cands) == 1 or cands[0][0] > cands[1][0]):
            g.key, g.key_confidence = cands[0][1], 1.0
        elif len(cands) == 1:
            g.key, g.key_confidence = cands[0][1], 0.95
        else:
            top = [p for r, p in cands if r == cands[0][0]]
            shaped = sorted(top, key=lambda p: _uniformity(g.stats[p].values), reverse=True)
            u0, u1 = _uniformity(g.stats[shaped[0]].values), _uniformity(g.stats[shaped[1]].values)
            if u0[0] >= 0.99 and u0[0] - u1[0] >= 0.1:
                g.key, g.key_confidence = shaped[0], 0.95
                continue
            picked = await choose(f"Records have the fields {', '.join(g.fields)}.\n"
                                  f"The field that identifies each record is", top,
                                  "The field that identifies each record is")
            g.key, g.key_confidence = (picked if picked else (top[0], 0.6))


# ── 5. types, 6. links, 7. names ───────────────────────────────────────────────────────────────

def _merge(groups: list[Group]) -> list[list[Group]]:
    """Keyed groups whose keys hold the same values are one type."""
    out: list[list[Group]] = []
    for g in groups:
        if not g.key:
            out.append([g])
            continue
        vals = g.stats[g.key].values
        hit = None
        for m in out:
            if not m[0].key:
                continue
            other = m[0].stats[m[0].key].values
            small, big = (vals, other) if len(vals) <= len(other) else (other, vals)
            if _contained(small, big) >= 0.5:
                hit = m
                break
        if hit is not None:
            hit.append(g)
        else:
            out.append([g])
    return out


def _name_words(members: list[Group]) -> list[str]:
    """The words a type could be called: what describes the record itself - its key's name and the
    path it sits at ("customer_id", "$.customers[*]") - and only when those say nothing, the names of
    the files that hold it (a file is named after its export, "billing-07.json", not its records)."""
    def clean(words) -> list[str]:
        seen, out = set(), []
        for w in words:
            lw = w.lower()
            if len(lw) > 2 and lw not in seen:
                seen.add(lw)
                out.append(lw)
        return out
    own = clean(w for g in members for w in _WORD.findall(g.key or "") + _WORD.findall(g.path or ""))
    return own or clean(w for g in members for s in g.sources[:5] for w in _WORD.findall(s.asset_name))[:12]


def _role(p: str, st: FieldStats, key: str, refs: set) -> str:
    if p == key:
        return "key"
    if p in refs:
        return "reference"
    if st.data_type == "date":
        return "time"
    if st.data_type == "number":
        return "measure"
    if st.words >= PROSE_WORDS:
        return "prose"           # free text: what an extractor can read entities and relations from
    if st.value_set:
        return "category"
    return "text"


async def _label(t: ObjectType, stats: dict, choose) -> str:
    """The property that names a record - what a mention in text is matched against. Candidates are
    the short text fields (and a text key) that are nearly unique; the ones whose values are phrases
    rather than single tokens win, the model picks among any that remain tied, and without a model
    the most filled one is taken."""
    if t.kind != "entity":
        return ""
    cands = [p.name for p in t.properties
             if (p.role == "text" or (p.role == "key" and stats[p.name].data_type == "text"))
             and stats[p.name].filled
             # nearly unique - within each file: one customer named in thirty billing files is one name
             and (stats[p.name].repeats == 0 or stats[p.name].distinct >= LABEL_UNIQUE * stats[p.name].filled)
             and any(_WORD.search(v) for v in list(stats[p.name].values)[:20])]
    if len(cands) <= 1:
        return cands[0] if cands else ""
    # What a person calls a record by is a phrase ("Alice Smith"), not a token (an email, a code):
    # the candidates with the most words per value go forward, and the model decides only a tie.
    most = max(stats[c].words for c in cands)
    cands = [c for c in cands if stats[c].words == most]
    if len(cands) == 1:
        return cands[0]
    picked = await choose(f"A {t.name} record has the fields {', '.join(cands)}.\n"
                          f"A person refers to one {t.name} by its", cands,
                          f"A person refers to one {t.name} by its")
    return picked[0] if picked else max(cands, key=lambda c: stats[c].filled)


async def detect(workspace_id, *, apply: bool = True) -> Layer:
    """Propose the semantic layer from the workspace's structured and semi-structured data, and
    (with `apply`) save it - curated types and links keep what people decided."""
    import anyio

    from opencortyx.core.dataplane import assets
    from opencortyx.core.inference import choice
    from opencortyx.stores.data import Views, layout

    refs = await assets.refs(workspace_id, kinds=[layout.STRUCTURED, layout.SEMISTRUCTURED])
    spec = await choice.workspace_model(workspace_id)

    async def choose(prompt: str, options: list, baseline: str):
        c = await choice.choose(prompt, options, model_spec=spec, baseline=baseline)
        if c is None:
            return None
        return c.option, (0.9 if c.margin >= 1.0 else 0.75)

    with Views(workspace_id, refs) as views:
        sources, columns = await anyio.to_thread.run_sync(lambda: _sources(views, refs))
        groups = _group(sources)
        for g in groups:
            await anyio.to_thread.run_sync(lambda g=g: _profile(views, g, columns))
    await _keys(groups, choose)

    layer = Layer()
    members_of: dict = {}
    for members in _merge(groups):
        main = max(members, key=lambda g: g.rows)
        key = main.key
        props: dict = {}
        for g in members:
            for p, st in g.stats.items():
                name = key if (g.key and p == g.key) else p
                cur = props.get(name)
                if cur is None:
                    props[name] = FieldStats(rows=st.rows, filled=st.filled, distinct=st.distinct,
                                             data_type=st.data_type, values=set(st.values),
                                             value_set=dict(st.value_set), words=st.words)
                else:
                    cur.filled += st.filled
                    cur.words = max(cur.words, st.words)
                    cur.values |= st.values
                    cur.distinct = max(cur.distinct, len(cur.values))
                    for v, n in st.value_set.items():
                        cur.value_set[v] = cur.value_set.get(v, 0) + n
        bindings = [Binding(asset_id=s.asset_id, asset_name=s.asset_name, kind=s.kind, path=s.path,
                            fields={(key if (g.key and p == g.key) else p): f for p, f in s.fields.items()})
                    for g in members for s in g.sources]
        words = _name_words(members)
        named = await choose(f"Records with the fields {', '.join(list(props)[:12])} come from "
                             f"{', '.join(s.asset_name for s in main.sources[:3])}.\nOne such record is called a",
                             words, "One such record is called a") if words else None
        name = (named[0] if named else (words[0] if words else (key or "record"))).capitalize()
        conf = min([g.key_confidence for g in members if g.key] or [1.0])
        t = ObjectType(name=name, keys=[key] if key else [], kind="entity" if key else "event",
                       confidence=round(conf, 3), status="accepted" if conf >= AUTO_ACCEPT else "suggested",
                       rows=sum(g.rows for g in members), bindings=bindings,
                       properties=[Property(name=p, data_type=st.data_type, distinct=st.distinct,
                                            value_set=st.value_set, words=round(st.words, 1))
                                   for p, st in props.items()])
        layer.types.append(t)
        members_of[id(t)] = props

    # Distinct names: two types the data calls the same word get their key's or path's word too.
    seen: dict = defaultdict(int)
    for t in layer.types:
        seen[t.name] += 1
    for t in layer.types:
        if seen[t.name] > 1:
            t.name = f"{t.name} ({t.key or t.bindings[0].asset_name})"

    refs_of: dict = defaultdict(set)
    for s in layer.types:
        sp = members_of[id(s)]
        for p, st in sp.items():
            if p == s.key or len(st.values) < MIN_LINK_VALUES:
                continue
            for t in layer.types:
                if t is s or not t.key:
                    continue
                kv = members_of[id(t)][t.key].values
                cov = _contained(st.values, kv)
                if cov >= CONTAINED:
                    layer.links.append(LinkType(name=_relation(t.name), source=s.name, source_prop=p,
                                                target=t.name, target_prop=t.key, coverage=round(cov, 3),
                                                status="accepted" if cov >= AUTO_ACCEPT else "suggested"))
                    refs_of[s.name].add(p)
    for t in layer.types:
        sp = members_of[id(t)]
        for prop in t.properties:
            prop.role = _role(prop.name, sp[prop.name], t.key, refs_of[t.name])
            if prop.role != "category":
                prop.value_set = {}
        t.label = await _label(t, sp, choose)
    log.info("semantic_layer.detected", types=len(layer.types), links=len(layer.links),
             accepted=sum(t.status == "accepted" for t in layer.types))
    if apply:
        await save_detected(workspace_id, layer)
    return layer


def _relation(target: str) -> str:
    """A link's relationship type: the type it names ("Customer" -> CUSTOMER), until a person renames it."""
    return re.sub(r"[^A-Za-z0-9]+", "_", target).strip("_").upper() or "REFERENCES"


def summary(layer: Layer) -> dict:
    return {"types": [{"id": t.id, "name": t.name, "keys": t.keys, "kind": t.kind, "status": t.status,
                       "materialize": t.materialize,
                       "confidence": t.confidence, "rows": t.rows, "bindings": len(t.bindings),
                       "assets": sorted({b.asset_name for b in t.bindings})[:5],
                       "properties": [{"name": p.name, "type": p.data_type, "role": p.role} for p in t.properties]}
                      for t in layer.types],
            "links": [{"id": lk.id, "name": lk.name, "from": f"{lk.source}.{lk.source_prop}",
                       "to": f"{lk.target}.{lk.target_prop}", "origin": lk.origin,
                       "coverage": lk.coverage, "status": lk.status}
                      for lk in layer.links]}


def describe(layer: Layer) -> Optional[str]:
    return "\n".join(f"{t.name} [{t.status} {t.confidence}] key={t.key or '-'} rows={t.rows} "
                     f"bindings={len(t.bindings)}" for t in layer.types) or None


# ── the shape of a few assets, for the Mapper's editor ────────────────────────────────────────

#: Rows the editor's preview shows per record source.
PREVIEW_ROWS = 3


async def shape(workspace_id, asset_ids) -> list[dict]:
    """Every record source of `asset_ids` - a table's rows, an object or a list inside JSON records -
    with its fields measured the way the Detector measures them (filled, repeats within each file,
    data type, words per value) and a few rows, so the Mapper offers only choices the data supports:
    an identifier must be filled and never repeat in a file; a name is a short, nearly unique text."""
    import anyio

    from opencortyx.core.dataplane import assets
    from opencortyx.stores.data import Views, layout

    wanted = {str(a) for a in asset_ids or ()}
    refs = [r for r in await assets.refs(workspace_id, kinds=[layout.STRUCTURED, layout.SEMISTRUCTURED])
            if r.asset_id in wanted]
    if not refs:
        return []

    def read() -> list[dict]:
        out = []
        with Views(workspace_id, refs) as views:
            sources, columns = _sources(views, refs)
            for s in sources:
                g = Group(kind=s.kind, path=s.path, sources=[s], fields=dict(s.fields))
                try:
                    _profile(views, g, columns)
                    t = _group_type(g)
                    rows = views.query(f"SELECT * EXCLUDE (_asset) FROM ({binding_sql(t, t.bindings[0], s.view, columns.get(s.view))}) "
                                       f"LIMIT {PREVIEW_ROWS}").to_pylist()
                except Exception as e:                        # noqa: BLE001 - an unreadable shape is reported
                    out.append({"asset_id": s.asset_id, "asset_name": s.asset_name, "kind": s.kind,
                                "path": s.path, "error": str(e)[:200], "fields": [], "rows": []})
                    continue
                out.append({
                    "asset_id": s.asset_id, "asset_name": s.asset_name, "kind": s.kind, "path": s.path,
                    "records": g.rows,
                    "fields": [{"name": p, "source": s.fields[p], "data_type": st.data_type, "filled": st.filled,
                                "distinct": st.distinct, "repeats": st.repeats, "words": round(st.words, 1),
                                "identifier": g.rows >= 2 and st.filled == g.rows and st.repeats == 0
                                              and st.data_type != "date" and st.words < PROSE_WORDS,
                                "nameable": st.data_type == "text" and 0 < st.words < PROSE_WORDS
                                        and (st.repeats == 0 or st.distinct >= LABEL_UNIQUE * max(1, st.filled))}
                               for p, st in g.stats.items()],
                    "rows": [{k: (v if isinstance(v, (str, int, float, bool)) or v is None else str(v))
                              for k, v in r.items()} for r in rows],
                })
        return out
    return await anyio.to_thread.run_sync(read)
