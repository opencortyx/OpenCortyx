"""A mapping's knowledge checked against the data, before anything is published.

    validate   per entity: its sources exist, each field it reads holds values, its key is never empty
               and (within one source) never repeated. Per relationship: the target type exists and
               is keyed by the field named, the references point at keys that exist (missing ones are
               counted, with examples), and each points at ONE record (an ambiguous join is reported)
    preview    the first records each entity yields and the links each relationship would draw

Everything is read through the same logical tables the questions read (views.py), so what the
check says is what an answer will see.
"""
from __future__ import annotations

from typing import Optional

from opencortyx.knowledge.semantic import store
from opencortyx.knowledge.semantic.views import binding_sql, q, type_sql, view_name


def _issue(level: str, where: str, text: str, **extra) -> dict:
    return {"level": level, "where": where, "text": text, **extra}


async def _context(workspace_id, mapping):
    """(the mapping's own layer, the whole workspace's layer, asset refs, asset views)."""
    from opencortyx.core.dataplane import assets
    from opencortyx.stores.data import layout
    own = store.layer_of([mapping])
    for t in own.types:
        t.status = "accepted"                                   # checked as if it were live
    whole = await store.load(workspace_id)
    for t in own.types:
        if whole.type(t.name) is None:
            whole.types.append(t)
    refs = await assets.refs(workspace_id, kinds=[layout.STRUCTURED, layout.SEMISTRUCTURED])
    taken: set = set()
    views = {r.asset_id: layout.view_name(r.name, taken) for r in refs}
    return own, whole, refs, views, taken


def _logical(layer, views: dict, taken: set, columns: dict) -> dict:
    """{type name: (view, sql)} for every type of `layer` that builds."""
    out = {}
    for t in layer.types:
        sql = type_sql(t, views, columns)
        if sql:
            out[t.name] = (view_name(t, taken), sql)
    return out


def _columns(workspace_id, refs, views: dict, layer) -> dict:
    """{view: its columns} for the structured assets `layer` reads (a missing column reads NULL)."""
    from opencortyx.stores.data import Views
    wanted = {views[b.asset_id] for t in layer.types for b in t.bindings
              if b.kind == "structured" and b.asset_id in views}
    out: dict = {}
    if not wanted:
        return out
    with Views(workspace_id, [r for r in refs if views.get(r.asset_id) in wanted], lock=True) as v:
        for view in v.names:
            out[view] = {r["column_name"] for r in v.query(f"DESCRIBE {q(view)}").to_pylist()}
    # Views names its views itself; map them back by asset id so they match `views`.
    by_asset = {a: n for n, a in v.names.items()}
    return {views[a]: out[n] for a, n in by_asset.items() if a in views and n in out}


async def validate(workspace_id, mapping, *, examples: int = 5) -> dict:
    import anyio

    from opencortyx.stores.data import Views
    own, whole, refs, views, taken = await _context(workspace_id, mapping)
    issues: list = []
    for e in ((mapping.payload or {}).get("knowledge") or {}).get("entities") or []:
        for a in e.get("assets") or []:
            if not a.get("id"):
                issues.append(_issue("error", e.get("type") or "entity",
                                     f"source {a.get('name') or '(unnamed)'} is not in the data store"))
    for t in own.types:
        if not t.name:
            issues.append(_issue("error", "entity", "an entity has no type name"))
        if t.kind == "entity" and not t.keys:
            issues.append(_issue("error", t.name, "no identifier: choose the field (or fields) that identify a record"))
        for b in t.bindings:
            if b.asset_id not in views:
                issues.append(_issue("error", t.name, f"source {b.asset_name or b.asset_id} is not in the data store"))
    columns = await anyio.to_thread.run_sync(lambda: _columns(workspace_id, refs, views, whole))
    logical = _logical(whole, views, set(taken), columns)
    for t in own.types:
        for b in t.bindings:
            have = columns.get(views.get(b.asset_id, ""))
            for p, f in b.fields.items():
                if have is not None and f not in have:
                    issues.append(_issue("error", t.name, f"{b.asset_name}: no column {f} (for {p})"))

    def run():
        out: list = []
        with Views(workspace_id, refs, lock=True, logical=list(logical.values())) as v:
            for t in own.types:
                if t.name not in logical:
                    continue
                for b in t.bindings:
                    if b.asset_id not in views:
                        continue
                    src = f"({binding_sql(t, b, views[b.asset_id], columns.get(views[b.asset_id]))}) AS s"
                    n = v.query(f"SELECT count(*) AS n FROM {src}").to_pylist()[0]["n"]
                    if not n:
                        out.append(_issue("error", t.name, f"{b.asset_name}: no records at {b.path or 'the rows'}"))
                        continue
                    for p in b.fields:
                        filled = v.query(f"SELECT count({q(p)}) AS n FROM {src}").to_pylist()[0]["n"]
                        if not filled:
                            out.append(_issue("warning", t.name, f"{b.asset_name}: field {b.fields[p]} is always empty"))
                    if t.keys:
                        keys = " AND ".join(f"{q(k)} IS NOT NULL" for k in t.keys)
                        r = v.query(f"SELECT count(*) - count(*) FILTER (WHERE {keys}) AS empty, "
                                    f"count(*) FILTER (WHERE {keys}) - count(DISTINCT ({', '.join(q(k) for k in t.keys)})) "
                                    f"FILTER (WHERE {keys}) AS repeated FROM {src}").to_pylist()[0]
                        if r["empty"]:
                            out.append(_issue("error", t.name, f"{b.asset_name}: {r['empty']} records have no identifier"))
                        if r["repeated"]:
                            out.append(_issue("warning", t.name, f"{b.asset_name}: {r['repeated']} identifiers repeat"))
                if t.keys and len(t.bindings) > 1:
                    # Across this mapping's sources too: files of one shape hold different records, so
                    # an identifier two of them share names two records as one. (Across mappings, a
                    # shared identifier is the point - the same customer in the CRM and in billing.)
                    parts = [binding_sql(t, b, views[b.asset_id], columns.get(views[b.asset_id]))
                             for b in t.bindings if b.asset_id in views]
                    union = " UNION ALL BY NAME ".join(f"({p})" for p in parts)
                    keys = ", ".join(q(k) for k in t.keys)
                    r = v.query(f"SELECT count(*) AS n, count(DISTINCT ({keys})) AS d FROM ({union}) "
                                f"WHERE {' AND '.join(f'{q(k)} IS NOT NULL' for k in t.keys)}").to_pylist()[0]
                    if r["n"] > r["d"]:
                        out.append(_issue("error", t.name, f"{r['n'] - r['d']} of {r['n']} records share an identifier "
                                                           f"with another record of these sources: "
                                                           f"{', '.join(t.keys)} does not identify a record"))
            for lk in own.links:
                target = whole.type(lk.target)
                where = f"{lk.source}.{lk.source_prop} → {lk.target}.{lk.target_prop}"
                if target is None:
                    out.append(_issue("error", where, f"no entity type {lk.target}"))
                    continue
                if lk.target_prop not in target.keys:
                    out.append(_issue("warning", where, f"{lk.target_prop} is not {lk.target}'s identifier"))
                if lk.source not in logical or lk.target not in logical:
                    continue
                sv, tv = logical[lk.source][0], logical[lk.target][0]
                sp, tp = q(lk.source_prop), q(lk.target_prop)
                r = v.query(
                    f"WITH refs AS (SELECT DISTINCT CAST({sp} AS VARCHAR) AS k FROM {q(sv)} WHERE {sp} IS NOT NULL), "
                    f"targets AS (SELECT CAST({tp} AS VARCHAR) AS k, count(*) AS n FROM {q(tv)} GROUP BY 1) "
                    f"SELECT count(*) AS refs, count(*) FILTER (WHERE t.k IS NULL) AS missing, "
                    f"count(*) FILTER (WHERE t.n > 1) AS ambiguous, "
                    f"list(refs.k) FILTER (WHERE t.k IS NULL) AS missing_examples "
                    f"FROM refs LEFT JOIN targets t ON refs.k = t.k").to_pylist()[0]
                if r["missing"]:
                    out.append(_issue("warning", where, f"{r['missing']} of {r['refs']} references point to no {lk.target}",
                                      examples=(r["missing_examples"] or [])[:examples]))
                if r["ambiguous"]:
                    out.append(_issue("error", where, f"{r['ambiguous']} references match more than one {lk.target}: "
                                                      "the join is ambiguous"))
        return out
    try:
        issues += await anyio.to_thread.run_sync(run)
    except Exception as e:                                    # noqa: BLE001 - the data store's reason
        issues.append(_issue("error", "data", f"could not read the data: {str(e)[:300]}"))
    return {"valid": not any(i["level"] == "error" for i in issues), "issues": issues}


async def preview(workspace_id, mapping, *, limit: int = 10) -> dict:
    import anyio

    from opencortyx.stores.data import Views
    own, whole, refs, views, taken = await _context(workspace_id, mapping)
    columns = await anyio.to_thread.run_sync(lambda: _columns(workspace_id, refs, views, whole))
    logical = _logical(whole, views, set(taken), columns)

    def run():
        out: dict = {"entities": [], "relationships": []}
        with Views(workspace_id, refs, lock=True, logical=list(logical.values())) as v:
            for t in own.types:
                if t.name not in logical:
                    continue
                view = q(logical[t.name][0])
                n = v.query(f"SELECT count(*) AS n FROM {view}").to_pylist()[0]["n"]
                rows = v.query(f"SELECT * FROM {view} LIMIT {int(limit)}").to_pylist()
                out["entities"].append({"type": t.name, "keys": t.keys, "records": n,
                                        "sources": sorted({b.asset_name for b in t.bindings}),
                                        "fields": {p: f for b in t.bindings[:1] for p, f in b.fields.items()},
                                        "rows": _jsonable(rows)})
            for lk in own.links:
                if lk.source not in logical or lk.target not in logical:
                    continue
                s, t = q(logical[lk.source][0]), q(logical[lk.target][0])
                src_keys = (whole.type(lk.source).keys if whole.type(lk.source) else []) or [lk.source_prop]
                cols = [f"s.{q(k)} AS {q(lk.source + '.' + k)}" for k in src_keys]
                cols.append(f"t.{q(lk.target_prop)} AS {q(lk.target + '.' + lk.target_prop)}")
                on = f"CAST(s.{q(lk.source_prop)} AS VARCHAR) = CAST(t.{q(lk.target_prop)} AS VARCHAR)"
                rows = v.query(f"SELECT {', '.join(cols)} FROM {s} AS s JOIN {t} AS t ON {on} "
                               f"LIMIT {int(limit)}").to_pylist()
                out["relationships"].append({"name": lk.name, "from": f"{lk.source}.{lk.source_prop}",
                                             "to": f"{lk.target}.{lk.target_prop}", "origin": lk.origin,
                                             "rows": _jsonable(rows)})
        return out
    return await anyio.to_thread.run_sync(run)


def _jsonable(rows: list) -> list:
    def one(x):
        if isinstance(x, (str, int, float, bool)) or x is None:
            return x
        return str(x)
    return [{k: one(v) for k, v in r.items()} for r in rows]


def version(mapping, number: Optional[int] = None) -> Optional[dict]:
    """The mapping's knowledge at version `number` (the current one when None)."""
    payload = mapping.payload or {}
    cur = payload.get("knowledge")
    if number is None or (cur and cur.get("version") == number):
        return cur
    return next((h["knowledge"] for h in payload.get("history") or [] if h.get("version") == number), None)
