"""From a question to SQL over the workspace's views.

Two planners, tried in order:

  rules   a counting, summing or ranking question over ONE table (or two joined by a known path),
          its filters read from the question by the columns' own values (value_set: "EXPIRED",
          "APAC") and years, its measure a numeric column the question names. Deterministic, needs
          no model, and is what makes the data route work with the small built-in model.
  model   anything else: the generation model writes the SQL from the schema subgraph only - the
          relevant tables' columns, types, descriptions and value sets, the join path, defined
          Metrics and Segments, and the closest verified question -> SQL examples. Never rows.
          A query the guard rejects or the database refuses goes back with the error, twice at most.

A Metric that matches the question is used as written: "revenue" is its agreed expression, not a
formula the model invents.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from opencortyx.retrieval.l2_data.schema import Schema, Table, score_tables, words
from opencortyx.retrieval.l2_graph import join_path

_AGG = (
    (re.compile(r"\b(how many|number of|count of|count)\b", re.I), "count"),
    (re.compile(r"\b(total|sum of|sum)\b", re.I), "sum"),
    (re.compile(r"\b(average|avg|mean)\b", re.I), "avg"),
    (re.compile(r"\b(highest|largest|maximum|max|most expensive|biggest)\b", re.I), "max"),
    (re.compile(r"\b(lowest|smallest|minimum|min|cheapest)\b", re.I), "min"),
)
_RANK = re.compile(r"\bwhich\s+(\w+)\s+(?:has|have|had|holds?|with)\s+the\s+(most|highest|largest|fewest|least|lowest)\b", re.I)
_PER = re.compile(r"\b(?:per|by|for each|each)\s+([a-z_]+)", re.I)
_DISTINCT = re.compile(r"\bdistinct\b|\bunique\b", re.I)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_QUARTER = re.compile(r"\b((?:19|20)\d{2})-?Q([1-4])\b", re.I)
_ROUND = re.compile(r"\b(?:rounded to|to)\s+(one|two|three|\d)\s+decimal", re.I)
_NUM = {"one": 1, "two": 2, "three": 3}


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _lit(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


@dataclass
class Plan:
    sql: str
    tables: list
    method: str                          # rules | model | metric | verified
    confidence: float = 0.5
    notes: list = field(default_factory=list)


def _filters(question: str, tables: list[Table]) -> tuple[list[str], list[str]]:
    """WHERE terms from the category values the question names, and the columns they use."""
    qtext = " " + " ".join(words(question)) + " "
    out, used = [], []
    for t in tables:
        for c in t.columns:
            hits = [v for v in c.value_set if words(v) and f" {' '.join(words(v))} " in qtext]
            if not hits:
                continue
            # The longest value wins ("in progress" over "progress"); several values of one column OR.
            hits.sort(key=lambda v: -len(v))
            vals = [h for h in hits if not any(h != o and h.lower() in o.lower() for o in hits)]
            col = f"{_q(t.view)}.{_q(c.name)}"
            out.append(f"{col} = {_lit(vals[0])}" if len(vals) == 1
                       else f"{col} IN ({', '.join(_lit(v) for v in vals)})")
            used.append(c.name)
    return out, used


def _time_filters(question: str, t: Table) -> list[str]:
    """`issued in 2024`, `expire in 2027`, `in 2026-Q1`: a year or quarter on the temporal column the
    question names (or the only one there is)."""
    out = []
    q = set(words(question))
    temporal = [c for c in t.columns if c.temporal]
    named = [c for c in temporal if set(words(c.name)) & (q | {w + "d" for w in q} | {w.rstrip("s") for w in q}
                                                           | {w[:-1] for w in q if w.endswith("e")})]
    cols = named or (temporal if len(temporal) == 1 else [])
    if not cols:
        return out
    col = f"CAST({_q(t.view)}.{_q(cols[0].name)} AS VARCHAR)"
    qm = _QUARTER.search(question)
    if qm:
        y, n = qm.group(1), int(qm.group(2))
        months = [f"{y}-{m:02d}" for m in range(3 * n - 2, 3 * n + 1)]
        out.append("(" + " OR ".join(f"{col} LIKE {_lit(m + '%')}" for m in months) + ")")
        return out
    years = [m.group(0) for m in _YEAR.finditer(question)]
    if len(years) == 1:
        out.append(f"{col} LIKE {_lit(years[0] + '%')}")
    return out


def _measure(question: str, t: Table):
    q = set(words(question)) | {w.rstrip("s") for w in words(question)}
    nums = [c for c in t.columns if c.numeric and not c.primary_key and not c.name.lower().endswith("_id")]
    named = [c for c in nums if set(words(c.name)) & q]
    return named[0] if named else (nums[0] if len(nums) == 1 else None)


def _group_column(question: str, t: Table, filtered: list[str]):
    m = _PER.search(question) or _RANK.search(question)
    if not m:
        return None
    word = m.group(1).lower().rstrip("s")
    for c in t.columns:
        if c.name in filtered:
            continue
        if word in words(c.name) or c.name.lower().rstrip("s") == word:
            return c
    return None


def rule_plan(question: str, schema: Schema) -> Optional[Plan]:
    agg = next((name for rx, name in _AGG if rx.search(question)), None)
    rank = _RANK.search(question)
    if not agg and not rank:
        return None
    ranked = score_tables(schema, question)
    if not ranked:
        return None
    anchor = ranked[0][1]
    where, used = _filters(question, [anchor])
    where += _time_filters(question, anchor)
    tables = [anchor]
    joins_sql = ""
    # A filter value that lives only in another table joins it in, when a path exists - never one
    # the anchor can filter on itself ("EXPIRED" in the anchor's own status column).
    anchor_cols = {c.name.lower() for c in anchor.columns}
    for _, other in ranked[1:3]:
        w2, u2 = _filters(question, [other])
        keep = [(w, u) for w, u in zip(w2, u2) if u.lower() not in anchor_cols]
        if not keep:
            continue
        w2, u2 = [w for w, _ in keep], [u for _, u in keep]
        steps = join_path.path(schema, anchor.view, other.view)
        if steps is None:
            continue
        for s in steps:
            joins_sql += (f" JOIN {_q(s.right)} ON {_q(s.left)}.{_q(s.left_col)} = "
                          f"{_q(s.right)}.{_q(s.right_col)}")
        where += w2
        used += u2
        tables.append(other)
    measure = _measure(question, anchor)
    rnd = _ROUND.search(question)
    digits = None
    if rnd:
        g = rnd.group(1).lower()
        digits = int(g) if g.isdigit() else _NUM.get(g)
    frm = f"FROM {_q(anchor.view)}{joins_sql}"
    cond = f" WHERE {' AND '.join(where)}" if where else ""
    if rank:
        group = _group_column(question, anchor, used)
        if group is None:
            return None
        direction = "DESC" if rank.group(2).lower() in ("most", "highest", "largest") else "ASC"
        g = f"{_q(anchor.view)}.{_q(group.name)}"
        if measure is not None and re.search(r"\b(fee|amount|revenue|total|sum|value|price|cost)\b", question, re.I):
            val = f"sum({_q(anchor.view)}.{_q(measure.name)})"
        elif measure is not None and re.search(r"\baverage|avg|mean\b", question, re.I):
            val = f"avg({_q(anchor.view)}.{_q(measure.name)})"
        else:
            val = "count(*)"
        sql = f"SELECT {g} AS {_q(group.name)}, {val} AS value {frm}{cond} GROUP BY {g} ORDER BY value {direction} LIMIT 5"
        return Plan(sql=sql, tables=[t.view for t in tables], method="rules", confidence=0.7)
    if agg == "count":
        if _DISTINCT.search(question):
            key = next((c for c in anchor.columns if c.name.lower().endswith("_id") and set(words(c.name[:-3])) & set(words(question))), None)
            expr = f"count(DISTINCT {_q(anchor.view)}.{_q(key.name)})" if key else "count(*)"
        else:
            expr = "count(*)"
    else:
        if measure is None:
            return None
        col = f"{_q(anchor.view)}.{_q(measure.name)}"
        expr = f"{agg}({col})"
        if digits is not None:
            expr = f"round({expr}, {int(digits)})"
    group = _group_column(question, anchor, used)
    if group is not None:
        g = f"{_q(anchor.view)}.{_q(group.name)}"
        sql = f"SELECT {g} AS {_q(group.name)}, {expr} AS value {frm}{cond} GROUP BY {g} ORDER BY value DESC"
    else:
        sql = f"SELECT {expr} AS value {frm}{cond}"
    conf = 0.8 if (where or agg == "count") else 0.6
    return Plan(sql=sql, tables=[t.view for t in tables], method="rules", confidence=conf)


# ── the model planner ─────────────────────────────────────────────────────────────────────────

_PROMPT = """Write ONE DuckDB SQL SELECT that answers the question from these tables.

{schema}
{joins}{metrics}{examples}
Rules:
- Use only the tables and columns listed. Quote names with double quotes.
- Filter category columns with the exact values listed after "values:".
- Return only the SQL, no explanation.
{error}
Question: {question}
SQL:"""


def schema_text(schema: Schema, tables: list[Table], allow_pii: bool = False) -> str:
    lines = []
    for t in tables:
        lines.append(f'TABLE "{t.view}"' + (f" -- {t.description}" if t.description else "")
                     + (f" ({t.rows} rows)" if t.rows else ""))
        for c in t.columns:
            if c.pii and not allow_pii:
                continue
            extra = f" -- {c.description}" if c.description else ""
            if c.value_set:
                extra += " values: " + ", ".join(list(c.value_set)[:20])
            lines.append(f'  "{c.name}" {c.data_type or "?"}{extra}')
    return "\n".join(lines)


async def model_plan(question: str, schema: Schema, complete, *, error: str = "",
                     metrics: Optional[list] = None, examples: Optional[list] = None,
                     previous: str = "") -> Optional[Plan]:
    ranked = score_tables(schema, question)
    tables = [t for _, t in ranked[:4]] or list(schema.tables.values())[:4]
    views = [t.view for t in tables]
    steps = join_path.connect(schema, views) or []
    joins = "".join(f'JOIN: "{s.left}"."{s.left_col}" = "{s.right}"."{s.right_col}"\n' for s in steps)
    mtext = "".join(f"METRIC {m['name']}: {m['expression']}" + (f" -- {m['description']}" if m.get("description") else "") + "\n"
                    for m in (metrics or []))
    etext = "".join(f"EXAMPLE Q: {e['question']}\nEXAMPLE SQL: {e['sql']}\n" for e in (examples or [])[:5])
    err = f"\nThe previous query failed: {error}\nPrevious query: {previous}\nFix it.\n" if error else ""
    prompt = _PROMPT.format(schema=schema_text(schema, tables), joins=joins, metrics=mtext,
                            examples=etext, error=err, question=question.strip())
    try:
        text = await complete(prompt, max_tokens=400, temperature=0.0)
    except Exception:                                         # noqa: BLE001 - no model, no plan
        return None
    sql = _extract_sql(text or "")
    if not sql:
        return None
    return Plan(sql=sql, tables=views, method="model", confidence=0.5)


def _extract_sql(text: str) -> str:
    m = re.search(r"```(?:sql)?\s*(.+?)```", text, re.S | re.I)
    body = m.group(1) if m else text
    m = re.search(r"\b(WITH|SELECT)\b.+", body, re.S | re.I)
    return m.group(0).strip().rstrip(";").strip() if m else ""
