"""Each object type as ONE logical table, in SQL over the asset views (stores/data Views).

    binding_sql   one asset's records of the type: its rows, an object inside each record ("$.K"),
                  or each element of a list inside it ("$.K[*]"), every property read from its field
                  and cast to the property's type
    type_sql      every binding together (UNION ALL BY NAME); for an entity, one row per key - the
                  same customer in the CRM and in billing is ONE row with the fields of both

Nothing is copied: the views read the Delta tables the assets already are, at question time.
"""
from __future__ import annotations

from typing import Optional

from opencortyx.knowledge.semantic.store import Binding, ObjectType


def q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def lit(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _json_path(*keys: str) -> str:
    return "$" + "".join("." + '"' + k.replace('"', '\\"') + '"' for k in keys)


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
    parts = src.split(".")
    if b.path.endswith("[*]"):
        return f"json_extract_string(_r, {lit(_json_path(*parts))})"
    if b.path.startswith("$."):
        return f"json_extract_string(raw, {lit(_json_path(b.path[2:], *parts))})"
    return f"json_extract_string(raw, {lit(_json_path(*parts))})"


def binding_sql(t: ObjectType, b: Binding, view: str, columns: Optional[set] = None) -> str:
    cols = []
    for p, src in b.fields.items():
        prop = t.prop(p)
        cols.append(f"{_cast(_field_expr(b, src, columns), prop.data_type if prop else 'text')} AS {q(p)}")
    cols.append(f"{lit(b.asset_name)} AS _asset")
    frm = q(view)
    if b.kind == "semistructured" and b.path.endswith("[*]"):
        arr = lit(_json_path(b.path[2:-3]))
        frm = f"{q(view)}, unnest(CAST(json_extract(raw, {arr}) AS JSON[])) AS _u(_r)"
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


def view_name(t: ObjectType, taken: set) -> str:
    """`t`'s view: its name in snake case, never one an asset view already has."""
    base = "".join(c if c.isalnum() else "_" for c in t.name.strip().lower()).strip("_") or "object"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name)
    return name
