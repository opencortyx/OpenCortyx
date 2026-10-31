"""Each object type as ONE logical table, in SQL over the asset views (stores/data Views).

    binding_sql   one asset's records of the type: its rows, an object inside each record ("$.K",
                  "$.K.L"), each element of a list inside it ("$.K[*]", "$.K.L[*]"), of each list in
                  a list ("$.K[*][*]") or of a list inside each element ("$.K[*].M[*]"), every
                  property read from its field and cast to the property's type. A list element's
                  field can also be read from the object the list sits in ("^.F"), from a list beside
                  it at its position ("^.F[i]"), or be its position ("#i", "#j") or, for a plain
                  value, the value itself ("#v")
    type_sql      every binding together (UNION ALL BY NAME); for an entity, one row per key - the
                  same customer in the CRM and in billing is ONE row with the fields of both

Nothing is copied: the views read the Delta tables the assets already are, at question time.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from opencortyx.knowledge.semantic.store import Binding, ObjectType


def q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def lit(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _json_path(*keys: str) -> str:
    return "$" + "".join("." + '"' + k.replace('"', '\\"') + '"' for k in keys)


class Path(NamedTuple):
    """A record source's path: the keys to an object (`object`), or to a list (`outer`) and, for a
    second level, the keys inside each element to its list (`inner`; [] for a list of lists)."""
    object: list
    outer: Optional[list] = None
    inner: Optional[list] = None


def parse_path(path: str) -> Path:
    """"" -> each record; "$.a.b" -> an object; "$.a.b[*]" -> each element of a list;
    "$.a[*][*]" -> each element of each list in a list; "$.a[*].c[*]" -> each element of a list
    inside each element. Deeper lists are not record sources."""
    body = (path or "").strip()
    body = body[2:] if body.startswith("$.") else body.lstrip("$")
    keys = lambda part: [k for k in part.strip(".").split(".") if k]   # noqa: E731
    parts = body.split("[*]")
    if len(parts) == 1:
        return Path(keys(parts[0]))
    if len(parts) == 2:
        return Path([], keys(parts[0]))
    if len(parts) == 3 and not parts[2]:
        return Path([], keys(parts[0]), keys(parts[1]))
    raise ValueError(f"unsupported record path {path!r}")


def _within(json_expr: str, keys: list) -> str:
    """The JSON at `keys` inside `json_expr`."""
    return f"json_extract({json_expr}, {lit(_json_path(*keys))})" if keys else json_expr


def _container(path: Path) -> tuple[str, list]:
    """Where "^.F" reads F: (JSON expression, keys) of the object the innermost list sits in -
    the record (or the object holding the list) for one level and for a list of lists, the outer
    element for a list inside each element."""
    if path.inner:
        return "_o._o", path.inner[:-1]
    return "raw", (path.outer or [])[:-1]


def _cast(expr: str, data_type: str) -> str:
    if data_type == "number":
        return f"TRY_CAST({expr} AS DOUBLE)"
    if data_type == "date":
        return f"TRY_CAST({expr} AS DATE)"
    return f"CAST({expr} AS VARCHAR)"


def _field_expr(b: Binding, src: str, columns: Optional[set]) -> str:
    """The SQL reading field `src` of one record of binding `b`."""
    if b.kind != "semistructured":
        if columns is not None and src not in columns:
            return "NULL"
        return q(src)
    path = parse_path(b.path)
    if path.outer is not None:
        if src in ("#i", "#j"):
            return f"_{src[1]}.{src[1]}"
        if src == "#v":
            return "json_extract_string(_r, '$')"
        if src.startswith("^."):
            at = src[2:]
            where, keys = _container(path)
            if at.endswith("[i]"):
                return (f"json_extract_string({where}, {lit(_json_path(*keys, *at[:-3].split('.')))} "
                        f"|| '[' || _i.i || ']')")
            return f"json_extract_string({where}, {lit(_json_path(*keys, *at.split('.')))})"
        return f"json_extract_string(_r, {lit(_json_path(*src.split('.')))})"
    return f"json_extract_string(raw, {lit(_json_path(*path.object, *src.split('.')))})"


def binding_sql(t: ObjectType, b: Binding, view: str, columns: Optional[set] = None) -> str:
    cols = []
    for p, src in b.fields.items():
        prop = t.prop(p)
        cols.append(f"{_cast(_field_expr(b, src, columns), prop.data_type if prop else 'text')} AS {q(p)}")
    cols.append(f"{lit(b.asset_name)} AS _asset")
    frm = q(view)
    path = parse_path(b.path) if b.kind == "semistructured" else Path([])
    if path.outer is not None:
        # each element by its position, so a field can be read beside it ("^.F[i]") or be it ("#i")
        arr = lit(_json_path(*path.outer))
        outer = (f"{q(view)}, range(CAST(coalesce(json_array_length(raw, {arr}), 0) AS BIGINT)) AS _i(i), "
                 f"LATERAL (SELECT json_extract(raw, {arr} || '[' || _i.i || ']') AS _o) AS _o")
        if path.inner is None:
            frm = f"{outer}, LATERAL (SELECT _o._o AS _r) AS _e"
        else:
            # the second level: each list in the list, or the list inside each element
            inner = _within("_o._o", path.inner)
            frm = (f"{outer}, range(CAST(coalesce(json_array_length({inner}), 0) AS BIGINT)) AS _j(j), "
                   f"LATERAL (SELECT json_extract({inner}, '$[' || _j.j || ']') AS _r) AS _e")
    return f"SELECT {', '.join(cols)} FROM {frm}"


def type_sql(t: ObjectType, views: dict, columns: Optional[dict] = None) -> Optional[str]:
    """The logical table of `t`. `views`: asset_id -> its view name; `columns`: view -> its columns
    (a structured asset without a property's column reads NULL for it)."""
    parts = [binding_sql(t, b, views[b.asset_id], (columns or {}).get(views[b.asset_id]))
             for b in t.bindings if b.asset_id in views]
    if not parts:
        return None
    union = parts[0] if len(parts) == 1 else " UNION ALL BY NAME ".join(f"({p})" for p in parts)
    if t.kind != "entity" or not t.keys:
        return union
    keys = [q(k) for k in t.keys]
    # A property no bound asset provides is not a column of the union: asked for, it failed the whole
    # view (a type's properties are merged across mappings; its assets carry only their own fields).
    present = {f for b in t.bindings for f in b.fields}
    others = [p.name for p in t.properties if p.name not in t.keys and p.name in present]
    agg = ", ".join(keys + [f"any_value({q(p)}) AS {q(p)}" for p in others]
                    + ["string_agg(DISTINCT _asset, ', ') AS _assets"])
    return (f"SELECT {agg} FROM ({union}) WHERE {' AND '.join(f'{k} IS NOT NULL' for k in keys)} "
            f"GROUP BY {', '.join(keys)}")


def conflicts_sql(t: ObjectType, views: dict, limit: int = 500) -> Optional[str]:
    """Where the files bound to an identified type disagree: one row per (key, attribute) whose
    values differ, with `seen` - each value and the file it came from. The logical table shows one of
    them (`type_sql`); this is what says there was more than one. None when nothing can disagree."""
    parts = [binding_sql(t, b, views[b.asset_id]) for b in t.bindings if b.asset_id in views]
    present = {f for b in t.bindings for f in b.fields}
    others = [p.name for p in t.properties if p.name not in t.keys and p.name in present]
    if t.kind != "entity" or not t.keys or len(parts) < 2 or not others:
        return None
    union = " UNION ALL BY NAME ".join(f"({p})" for p in parts)
    keys = [q(k) for k in t.keys]
    whole = " AND ".join(f"{k} IS NOT NULL" for k in keys)
    cells = " UNION ALL ".join(
        f"SELECT {', '.join(keys)}, {lit(p)} AS attribute, CAST({q(p)} AS VARCHAR) AS value, _asset "
        f"FROM u WHERE {whole} AND {q(p)} IS NOT NULL" for p in others)
    return (f"WITH u AS ({union}) SELECT {', '.join(keys)}, attribute, "
            f"list(DISTINCT struct_pack(value := value, asset := _asset)) AS seen FROM ({cells}) "
            f"GROUP BY {', '.join(keys)}, attribute HAVING count(DISTINCT value) > 1 LIMIT {int(limit)}")


def view_name(t: ObjectType, taken: set) -> str:
    """`t`'s view: its name in snake case, never one an asset view already has."""
    base = "".join(c if c.isalnum() else "_" for c in t.name.strip().lower()).strip("_") or "object"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name
