"""The plan planner: a question as a small plan over the semantic layer, chosen, then compiled.

What the large platforms do - a semantic layer of types, measures and joins, the model choosing
within it, a compiler writing the query (dbt MetricFlow, Snowflake Cortex Analyst, Databricks Genie,
Palantir's object queries) - sized for a small model: it never writes SQL, it never sees an open
question. Every decision is a short list built from the data, and the model only says which entry
the question means (core/inference/choice.py). What the data can settle by itself, it settles:

    a record the question names        its key's value found in an object type's table: that record,
                                       with the records it links to (Palantir: find object, follow link)
    a file the question names          its name found in the question: that asset's table
    a filter                           a category value of the table found in the question
    a time                             a date, month, quarter or year written in the question

and the model chooses the rest from the options the data offers:

    which table                        among the object types closest to the question (embeddings)
    what is computed                   the number of records, or the total / average / highest /
                                       lowest of each measure the table has
    grouped by what                    nothing, or one of its categories
    which groups                       all of them, the highest, the lowest
    the date that matters, and how     its date properties; in / before / after
    how it is rounded                  as stored, or to 0-3 decimals

A verified query whose question is the same (by meaning) runs as it was checked. A question no plan
fits returns nothing, and the data route falls back as before.
"""
from __future__ import annotations

import math
import re
import time as _time
from dataclasses import dataclass, field
from typing import Optional

from opencortyx.core.observability import get_logger
from opencortyx.retrieval.l2_data import planner
from opencortyx.retrieval.l2_data.planner import Plan, _lit, _q
from opencortyx.retrieval.l2_data.schema import Schema, Table, words

log = get_logger(__name__)

#: Cosine similarity at which a verified query's question is the same question.
VERIFIED_SAME = 0.95
#: Object types offered to the model when it has to choose the table.
TABLE_CHOICES = 3
#: Documents offered beside the tables, so "not a table" is a real thing the model can pick.
DOCUMENT_CHOICES = 2
#: The option that says no table holds the answer (a relationship, something else).
NOT_DATA = "none of these"
_docs_cache: dict = {}
_PUNCT = re.compile(r"^[\W_]+|[\W_]+$")
_DATE = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")
_MONTH = re.compile(r"\b((?:19|20)\d{2})-(\d{2})\b(?!-)")
_vec_cache: dict = {}


@dataclass
class Decision:
    what: str
    options: list
    picked: str
    margin: float = 0.0


@dataclass
class Trace:
    decisions: list = field(default_factory=list)
    ms: int = 0

    def text(self) -> str:
        return "; ".join(f"{d.what}={d.picked}" for d in self.decisions)


class _Chooser:
    def __init__(self, spec, question: str, trace: Trace):
        self.spec, self.question, self.trace = spec, question, trace

    async def __call__(self, what: str, ask: str, options: list[str]) -> Optional[int]:
        from opencortyx.core.inference import choice
        if not options:
            return None
        if len(options) == 1:
            self.trace.decisions.append(Decision(what, options, options[0]))
            return 0
        c = await choice.choose(f"Question: {self.question}\n{ask}", options, model_spec=self.spec,
                                baseline=f"Question: \n{ask}")
        if c is None:
            return None
        self.trace.decisions.append(Decision(what, options, c.option, round(c.margin, 2)))
        return c.index


# ── what the data settles ──────────────────────────────────────────────────────────────────────

def _tokens(question: str) -> list[str]:
    out: list[str] = []
    for raw in (question or "").split():
        tok = _PUNCT.sub("", raw)
        if tok and tok.lower() not in (o.lower() for o in out):
            out.append(tok)
    return out


def _named_asset(schema: Schema, question: str) -> Optional[Table]:
    """An asset table the question names by its own name ("in licences-17")."""
    qtext = " " + " ".join(words(question)) + " "
    hits = [t for t in schema.tables.values() if t.kind != "object" and words(t.name)
            and f" {' '.join(words(t.name))} " in qtext]
    hits.sort(key=lambda t: -len(words(t.name)))
    return hits[0] if hits else None


def _with_type_values(t: Table, objects: list[Table]) -> Table:
    """An asset's table with the value sets of the type it belongs to: one six-row file is too small
    for its `region` to read as a category, the type's three hundred rows are not."""
    import dataclasses
    owner = next((o for o in objects if t.asset_id in o.assets), None)
    if owner is None:
        return t
    cols = []
    for c in t.columns:
        oc = owner.column(c.name)
        cols.append(dataclasses.replace(c, value_set=dict(oc.value_set)) if oc is not None and oc.value_set
                    and not c.value_set else c)
    return dataclasses.replace(t, columns=cols)


def _value_filters(question: str, t: Table) -> list[tuple]:
    """(column, values) for each category value of `t` the question contains."""
    qtext = " " + " ".join(words(question)) + " "
    out = []
    for c in t.columns:
        hits = [v for v in c.value_set if words(v) and f" {' '.join(words(v))} " in qtext]
        hits = [h for h in hits if not any(h != o and h.lower() in o.lower() for o in hits)]
        if hits:
            out.append((c.name, hits))
    return out


def _time_literal(question: str) -> Optional[tuple]:
    """("date"|"month"|"quarter"|"year", value) written in the question - formats, not words."""
    m = _DATE.search(question)
    if m:
        return "date", m.group(0)
    m = planner._QUARTER.search(question)
    if m:
        return "quarter", (m.group(1), int(m.group(2)))
    m = _MONTH.search(question)
    if m:
        return "month", m.group(0)
    years = [m.group(0) for m in planner._YEAR.finditer(question)]
    return ("year", years[0]) if len(years) == 1 else None


def _time_sql(col: str, op: str, lit: tuple) -> str:
    kind, v = lit
    c = f"TRY_CAST({col} AS DATE)"
    if kind == "quarter":
        y, n = v
        start, end = f"{y}-{3 * n - 2:02d}-01", (f"{y}-{3 * n + 1:02d}-01" if n < 4 else f"{int(y) + 1}-01-01")
    elif kind == "year":
        start, end = f"{v}-01-01", f"{int(v) + 1}-01-01"
    elif kind == "month":
        y, mth = v.split("-")
        start = f"{y}-{mth}-01"
        end = f"{int(y) + (mth == '12')}-{(int(mth) % 12) + 1:02d}-01"
    else:
        start, end = v, None
    if op == "before":
        return f"{c} < DATE {_lit(start)}"
    if op == "after":
        return f"{c} >= DATE {_lit(end)}" if end else f"{c} > DATE {_lit(start)}"
    return (f"{c} >= DATE {_lit(start)} AND {c} < DATE {_lit(end)}" if end else f"{c} = DATE {_lit(start)}")


#: Keys held in memory per entity type for naming a record; a type with more is looked up in SQL.
KEY_INDEX_MAX = 200_000
_key_index: dict = {}


def _keys(workspace_id, schema: Schema) -> dict:
    """{view: {lowercased key: key}} for every entity type - read once for the data as it stands (its
    types, their assets and sizes), so a question names a record without a query."""
    from opencortyx.stores.data import Views
    shape = tuple(sorted((t.view, t.rows or 0, tuple(t.assets)) for t in schema.objects()))
    hit = _key_index.get(str(workspace_id))
    if hit is not None and hit[0] == shape:
        return hit[1]
    out: dict = {}
    for t in [t for t in schema.objects() if t.key_columns]:
        k = _q(t.key_columns[0])
        try:
            with Views(workspace_id, lock=True, **schema.needs([t.view])) as v:
                rows = v.query(f"SELECT DISTINCT CAST({k} AS VARCHAR) AS k FROM {_q(t.view)} "
                               f"LIMIT {KEY_INDEX_MAX + 1}", timeout=30).to_pylist()
        except Exception:                                     # noqa: BLE001 - a view that does not build
            continue
        out[t.view] = None if len(rows) > KEY_INDEX_MAX else {str(r["k"]).lower(): r["k"] for r in rows if r["k"]}
    _key_index[str(workspace_id)] = (shape, out)
    return out


def _records(workspace_id, schema: Schema, tokens: list[str]) -> list[tuple]:
    """(table, key value) for each token that is the key of a record of an object type."""
    from opencortyx.stores.data import Views
    if not tokens:
        return []
    index = _keys(workspace_id, schema)
    out = []
    for t in schema.objects():
        keys = index.get(t.view, {})
        if keys is not None:
            out += [(t, keys[tok.lower()]) for tok in tokens if tok.lower() in keys]
            continue
        k, low = _q(t.key_columns[0]), ", ".join(_lit(x.lower()) for x in tokens)
        with Views(workspace_id, lock=True, **schema.needs([t.view])) as v:     # too many keys to hold
            rows = v.query(f"SELECT DISTINCT CAST({k} AS VARCHAR) AS k FROM {_q(t.view)} "
                           f"WHERE lower(CAST({k} AS VARCHAR)) IN ({low}) LIMIT 5", timeout=10).to_pylist()
        out += [(t, r["k"]) for r in rows]
    return out


# ── what the model chooses ─────────────────────────────────────────────────────────────────────

def _table_text(t: Table) -> str:
    vals = [v for c in t.columns for v in list(c.value_set)[:4]]
    return f"{t.name}: {', '.join(c.name for c in t.columns)}" + (f". Values: {', '.join(vals[:12])}" if vals else "")


async def _vec(embed, text: str):
    hit = _vec_cache.get(text)
    if hit is None:
        hit = await embed(text)
        if len(_vec_cache) > 4000:
            _vec_cache.clear()
        _vec_cache[text] = hit
    return hit


def _cos(a, b) -> float:
    if not a or not b:
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


def _stems(text: str) -> set:
    return {w.rstrip("s") for w in words(text) if len(w) > 1}


def grounding(question: str, t: Table) -> float:
    """How much of the table's own vocabulary the question uses: its name, its columns' names,
    and - counting double - its real values. Zero means the question says nothing this table
    knows about (schema linking, read from the data)."""
    q = _stems(question)
    s = 2.0 * sum(len(vals) for _, vals in _value_filters(question, t))
    s += len(q & _stems(t.name))
    s += sum(1 for c in t.columns if _stems(c.name) and _stems(c.name) <= q)
    return s


async def _closest_tables(question: str, tables: list[Table], embed) -> list[Table]:
    if len(tables) <= 1 or embed is None:
        return tables
    qv = await _vec(embed, question)
    scored = []
    for t in tables:
        s = _cos(qv, await _vec(embed, _table_text(t)))
        s += 0.05 * len(_value_filters(question, t))      # a value of its own in the question
        scored.append((s, t))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored]


def _named(question: str, name: str) -> bool:
    """The question uses every word of `name` (a column's or a table's own name)."""
    s = _stems(name)
    return bool(s) and s <= _stems(question)


def _grounded(question: str, options: list, name_of) -> list:
    """The options whose own words the question uses - or all of them when it uses none."""
    kept = [o for o in options if name_of(o) is None or _named(question, name_of(o))]
    named = [o for o in kept if name_of(o) is not None]
    return kept if named else options


def _measures(t: Table) -> list[tuple[str, str, str]]:
    """(phrase, function, column) of everything the table can compute."""
    name = t.name.lower()
    out = [(f"the number of {name}", "count", "")]
    for c in t.columns:
        if c.numeric and not c.primary_key and c.role not in ("key", "reference"):
            n = " ".join(words(c.name))
            out += [(f"the total {n}", "sum", c.name), (f"the average {n}", "avg", c.name),
                    (f"the highest {n}", "max", c.name), (f"the lowest {n}", "min", c.name)]
    for c in t.columns:
        if c.role == "reference":
            out.append((f"the number of different {' '.join(words(c.name))}", "count_distinct", c.name))
    out.append((f"a list of {name}", "list", ""))
    return out


def _expr(function: str, col: str, view: str) -> str:
    if function == "count":
        return "count(*)"
    c = f"{_q(view)}.{_q(col)}"
    if function == "count_distinct":
        return f"count(DISTINCT {c})"
    return f"{function}({c})"


async def _closest_documents(workspace_id, question: str, embed) -> list[str]:
    """The names of the documents closest to the question - the options that say "a document
    holds this, not a table"."""
    if embed is None:
        return []
    from opencortyx.core.dataplane import assets
    from opencortyx.stores.data import layout
    hit = _docs_cache.get(str(workspace_id))
    if hit is None or _time.monotonic() - hit[0] > 300:
        rows = await assets.list_assets(workspace_id, kinds=[layout.UNSTRUCTURED])
        hit = (_time.monotonic(), [r.name for r in rows][:2000])
        _docs_cache[str(workspace_id)] = hit
    if not hit[1]:
        return []
    qv = await _vec(embed, question)
    scored = [(_cos(qv, await _vec(embed, n)), n) for n in hit[1]]
    scored.sort(reverse=True)
    return [n for _, n in scored[:DOCUMENT_CHOICES]]


async def _verified(workspace_id, question: str, embed) -> Optional[Plan]:
    """A verified query asking the same question: its checked SQL, as it was checked."""
    if embed is None:
        return None
    from opencortyx.core.substrate import NodeRepo
    try:
        vqs = await NodeRepo().find(workspace_id, types=["VerifiedQuery"], limit=2000)
    except Exception:                                         # noqa: BLE001
        return None
    if not vqs:
        return None
    qv = await _vec(embed, question)
    best, best_s = None, 0.0
    for n in vqs:
        p = n.properties or {}
        text = p.get("question") or n.label
        if not text or not p.get("sql"):
            continue
        s = 1.0 if " ".join(words(text)) == " ".join(words(question)) else _cos(qv, await _vec(embed, text))
        if s > best_s:
            best, best_s = p, s
    if best is not None and best_s >= VERIFIED_SAME:
        return Plan(sql=best["sql"], tables=[], method="verified", confidence=1.0,
                    notes=[f"verified query ({best_s:.2f}): {best.get('question')}"])
    return None


async def names_record(workspace_id, question: str) -> bool:
    """Does the question name a record of an object type by its key? Read from the data, so the
    data route runs for "which customer does project P-004 belong to" whatever words it uses."""
    import anyio

    from opencortyx.retrieval.l2_data.schema import load
    try:
        schema = await load(workspace_id)
    except Exception:                                         # noqa: BLE001
        return False
    if not schema.objects():
        return False
    hits = await anyio.to_thread.run_sync(lambda: _records(workspace_id, schema, _tokens(question)))
    return bool(hits)


async def plans(workspace_id, question: str, schema: Schema, *, embed=None) -> tuple[list[Plan], Trace]:
    """The plan(s) for `question`, best first, and how each choice was made. Empty when no plan fits."""
    import anyio

    from opencortyx.core.inference import choice
    trace = Trace()
    t0 = _time.perf_counter()
    vq = await _verified(workspace_id, question, embed)
    if vq is not None:
        trace.decisions.append(Decision("verified", [], vq.notes[0]))
        return [vq], trace
    objects = schema.objects()
    named = _named_asset(schema, question)
    if not objects and named is None:
        return [], trace
    spec = await choice.workspace_model(workspace_id)
    if spec is None:
        return [], trace
    pick = _Chooser(spec, question, trace)

    # A record the question names: that record and what it links to - unless what is asked is
    # computed over the records that refer to it ("how many orders does customer C-152 have").
    hits = await anyio.to_thread.run_sync(lambda: _records(workspace_id, schema, _tokens(question)))
    if hits:
        t, key = hits[0]
        label = f"{t.name.lower()} {key}"
        # (phrase, link, measure, the words that ground it)
        options: list[tuple] = [(f"the {' '.join(words(c.name))} of {label}", None, None, c.name)
                                for c in t.columns if not c.primary_key]
        for j in schema.joins:
            if j.right == t.view and j.left in schema.tables and schema.tables[j.left].kind == "object":
                src = schema.tables[j.left]
                for phrase, fn, col in _measures(src):
                    if fn not in ("list", "count_distinct"):
                        options.append((f"{phrase} of {t.name.lower()} {key}", (src, j), (fn, col),
                                        src.name if fn == "count" else col))
        kept = [o for o in options if _named(question, o[3])]
        options = kept or options
        i = await pick("record", "The answer is", [o[0] for o in options])
        if i is not None and options[i][1] is None:
            trace.ms = int((_time.perf_counter() - t0) * 1000)
            return [_lookup(schema, t, key)], trace
        if i is not None:
            src, j = options[i][1]
            fn, col = options[i][2]
            where = [f"{_q(src.view)}.{_q(j.left_col)} = {_lit(key)}"]
            scale = await anyio.to_thread.run_sync(lambda: _scale(workspace_id, schema, src, col)) if col else None
            sql = await _compile(pick, question, src, fn, col, where, group=False, scale=scale)
            trace.ms = int((_time.perf_counter() - t0) * 1000)
            return [Plan(sql=sql, tables=[src.view], method="plan", confidence=0.8, notes=[trace.text()])], trace

    # The table: the asset the question names, else the object type it means.
    if named is not None:
        t = _with_type_values(named, objects)
        trace.decisions.append(Decision("table", [], f"{t.name} (named)"))
    else:
        scored = sorted(((grounding(question, o), o) for o in objects), key=lambda x: -x[0])
        if not scored or scored[0][0] <= 0:
            trace.decisions.append(Decision("table", [], "none (the question uses no table's words)"))
            return [], trace
        best = [o for s, o in scored if s >= scored[0][0]]
        cands = (await _closest_tables(question, best, embed))[:TABLE_CHOICES]
        i = await pick("table", "The answer is found in", [f"the {c.name.lower()} table" for c in cands])
        if i is None:
            return [], trace
        t = cands[i]

    measures = _grounded(question, _measures(t), lambda m: m[2] or None)
    i = await pick("compute", "In other words, what is wanted is", [m[0] for m in measures])
    if i is None:
        return [], trace
    _, fn, col = measures[i]
    scale = await anyio.to_thread.run_sync(lambda: _scale(workspace_id, schema, t, col)) if col else None
    sql = await _compile(pick, question, t, fn, col, [], group=fn != "list", scale=scale)
    trace.ms = int((_time.perf_counter() - t0) * 1000)
    log.info("data_route.plan", question=question[:120], plan=trace.text(), ms=trace.ms)
    return [Plan(sql=sql, tables=[t.view], method="plan", confidence=0.8, notes=[trace.text()])], trace


_scales: dict = {}


def _scale(workspace_id, schema: Schema, t: Table, col: str) -> Optional[int]:
    """The decimals `col` is stored with (fees in cents: 2), read from its values."""
    from opencortyx.stores.data import Views
    key = (str(workspace_id), t.view, col)
    if key in _scales:
        return _scales[key]
    c = _q(col)
    try:
        with Views(workspace_id, lock=True, **schema.needs([t.view])) as v:
            r = v.query(f"SELECT max(length(split_part(CAST({c} AS VARCHAR), '.', 2))) AS s "
                        f"FROM (SELECT {c} FROM {_q(t.view)} WHERE {c} IS NOT NULL LIMIT 5000)", timeout=10).to_pylist()
        s = int(r[0]["s"]) if r and r[0]["s"] is not None else None
    except Exception:                                         # noqa: BLE001
        s = None
    s = s if s is not None and 0 < s <= 6 else None
    _scales[key] = s
    return s


async def _compile(pick, question: str, t: Table, fn: str, col: str, where: list, *, group: bool,
                   scale: Optional[int] = None) -> str:
    """The SQL of one plan, its remaining choices made: filters, the date, grouping, rounding."""
    view = _q(t.view)
    used = set()
    for c, vals in _value_filters(question, t):
        used.add(c)
        where.append(f"{view}.{_q(c)} = {_lit(vals[0])}" if len(vals) == 1
                     else f"{view}.{_q(c)} IN ({', '.join(_lit(v) for v in vals)})")
    lit = _time_literal(question)
    dates = [c for c in t.columns if c.temporal]
    if lit is not None and dates:
        i = await pick("date", "The date that matters is the", [" ".join(words(c.name)) for c in dates])
        j = await pick("when", "The time asked about is", [f"in {_lit_text(lit)}", f"before {_lit_text(lit)}",
                                                            f"after {_lit_text(lit)}"])
        if i is not None:
            where.append(_time_sql(f"{view}.{_q(dates[i].name)}", ("in", "before", "after")[j or 0], lit))
    cond = f" WHERE {' AND '.join(where)}" if where else ""
    if fn == "list":
        return f"SELECT * FROM {view}{cond} LIMIT 50"
    expr = _expr(fn, col, t.view)
    if fn in ("sum", "avg"):
        k = await pick("round", "In other words, the answer is",
                       ["as stored", "0 decimals", "1 decimal", "2 decimals", "3 decimals"])
        if k:
            expr = planner.rounded(expr, k - 1)
        elif fn == "sum" and scale:
            expr = planner.rounded(expr, scale)        # a total keeps the precision of what it adds up
    elif fn in ("min", "max") and scale:
        expr = planner.rounded(expr, scale)
    groups = [c for c in t.columns if c.name not in used and (c.role in ("category", "reference") if c.semantic
                                                              else bool(c.value_set))] if group else []
    g = None
    groups = [c for c in groups if _named(question, c.name)]
    if groups:
        i = await pick("group", "Group the result by", ["one overall value"]
                       + [f"a value per {' '.join(words(c.name))}" for c in groups])
        g = groups[i - 1] if i else None
    if g is None:
        return f"SELECT {expr} AS value FROM {view}{cond}"
    gn = " ".join(words(g.name))
    k = await pick("which", "The answer lists", [f"every {gn}", f"the {gn} with the highest value",
                                                 f"the {gn} with the lowest value"])
    gc = f"{view}.{_q(g.name)}"
    order = " ORDER BY value DESC"
    if k == 1:
        order += " LIMIT 1"
    elif k == 2:
        order = " ORDER BY value ASC LIMIT 1"
    return f"SELECT {gc} AS {_q(g.name)}, {expr} AS value FROM {view}{cond} GROUP BY {gc}{order}"


def _lit_text(lit: tuple) -> str:
    kind, v = lit
    return f"{v[0]}-Q{v[1]}" if kind == "quarter" else str(v)


def _lookup(schema: Schema, t: Table, key: str) -> Plan:
    """One record of `t` and, for each type it links to, that record's fields beside it."""
    view = _q(t.view)
    sel = [f"{view}.*"]
    joins = ""
    n = 0
    for j in schema.joins:
        if j.left == t.view and j.right in schema.tables and schema.tables[j.right].kind == "object":
            n += 1
            other = schema.tables[j.right]
            a = f"_l{n}"
            joins += f" LEFT JOIN {_q(other.view)} AS {a} ON {view}.{_q(j.left_col)} = {a}.{_q(j.right_col)}"
            sel += [f"{a}.{_q(c.name)} AS {_q(other.name + '.' + c.name)}" for c in other.columns
                    if c.name != j.right_col]
    k = _q(t.key_columns[0])
    sql = (f"SELECT {', '.join(sel)} FROM {view}{joins} "
           f"WHERE lower(CAST({view}.{k} AS VARCHAR)) = {_lit(key.lower())}")
    return Plan(sql=sql, tables=[t.view], method="plan", confidence=0.9, notes=[f"record {t.name} {key}"])
