"""L2b - the data route: answer from the data itself, with SQL, when the question is about rows.

    question ─► schema (tables, columns, value sets, joins)          schema.py
             ─► plan: a defined Metric / a verified query / rules / the model      planner.py
             ─► guard: one read-only SELECT over this workspace's views           guard.py
             ─► DuckDB over managed Delta + external lake / database views         stores/data
             ─► check: empty = "no matching rows"; an error goes back to the planner (twice)
             ─► a table-result: the rows, the SQL, the assets and versions read = its evidence

A question the data cannot answer returns None and retrieval answers as before - except when the
data route was the only way to answer and it failed for a reason worth stating (no matching rows,
the source unavailable): then the result says so, and the answer is UNKNOWN with that reason rather
than a guess.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from opencortyx.core.observability import get_logger
from opencortyx.retrieval.l2_data import guard, planner
from opencortyx.retrieval.l2_data.schema import Schema, load as load_schema

log = get_logger(__name__)

#: Questions that are about rows: counting, totals, rankings, listings, comparisons of quantities.
_DATA_QUESTION = re.compile(
    r"\b(how many|number of|count|total|sum|average|avg|mean|median|percentage|percent|share of|"
    r"rate|ratio|highest|lowest|most|fewest|least|top \d+|list (all|every)|which .+ (has|have|had) the|"
    r"per |by (region|type|month|year|quarter|status|customer|country)|revenue|between \d)", re.I)
MAX_RETRIES = 2


@dataclass
class DataResult:
    ok: bool
    sql: str = ""
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    assets: list = field(default_factory=list)          # [{asset_id, name, storage, format, version_ref}]
    method: str = ""
    unknown_reason: Optional[str] = None
    attempts: list = field(default_factory=list)
    ms: int = 0
    notes: str = ""                                   # how a plan was chosen (semantic.py)

    def render(self) -> str:
        head = f"DATA RESULT ({', '.join(a['name'] for a in self.assets)})"
        if not self.rows:
            return f"{head}: no matching rows.\nSQL: {self.sql}"
        lines = [head, "| " + " | ".join(str(c) for c in self.columns) + " |"]
        for r in self.rows[:50]:
            lines.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
        if len(self.rows) > 50:
            lines.append(f"... {len(self.rows) - 50} more rows")
        lines.append(f"SQL: {self.sql}")
        versions = [f"{a['name']}@{a['version_ref']}" for a in self.assets if a.get("version_ref")]
        if versions:
            lines.append("Read: " + ", ".join(versions))
        return "\n".join(lines)


def _clean(v):
    """A float as it was meant: 89615.04999999999 is 89615.05 (twelve significant digits)."""
    return float(f"{v:.12g}") if isinstance(v, float) and math.isfinite(v) else v


def is_data_question(question: str) -> bool:
    return bool(_DATA_QUESTION.search(question or ""))


async def _definitions(workspace_id, question: str) -> tuple[list, list]:
    """Metrics and verified queries the question is closest to (data-layer nodes, Phase 5b)."""
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.retrieval.l2_data.schema import words
    repo = NodeRepo()
    q = set(words(question))
    metrics: list[dict] = []
    examples: list[dict] = []
    try:
        for m in await repo.find(workspace_id, types=["Metric"], limit=500):
            p = m.properties or {}
            # A metric is named in full - by its name or one synonym, every word of it - never by
            # one shared word: "licence fees" is not what "when does licence X expire" asks for.
            stems = q | {w.rstrip("s") for w in q}
            for name in [m.label or p.get("name")] + list(p.get("synonyms") or []):
                ws = {w.rstrip("s") for w in words(str(name or ""))}
                if ws and ws <= stems:
                    metrics.append({"name": m.label or p.get("name"), "expression": p.get("expression"),
                                    "description": p.get("description") or "", "table": p.get("table"),
                                    "filters": p.get("filters") or "", "score": len(ws) / max(1, len(stems))})
                    break
        for v in await repo.find(workspace_id, types=["VerifiedQuery"], limit=2000):
            p = v.properties or {}
            ws = set(words(p.get("question") or v.label))
            if ws:
                examples.append({"question": p.get("question") or v.label, "sql": p.get("sql"),
                                 "score": len(ws & q) / len(ws | q)})
    except Exception:                                         # noqa: BLE001 - definitions are optional
        pass
    metrics.sort(key=lambda m: -m["score"])
    examples.sort(key=lambda e: -e["score"])
    return [m for m in metrics if m["expression"]][:5], [e for e in examples if e["sql"] and e["score"] > 0.2][:5]


async def _with_terms(workspace_id, question: str) -> str:
    """The question with the workspace's own words for things added: a glossary Synonym ("permit",
    "licence no.") linked to an Attribute or a Metric brings that column's or metric's name in, so
    the planner finds what the user meant by it."""
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.retrieval.l2_data.schema import words
    repo = NodeRepo()
    qtext = " " + " ".join(words(question)) + " "
    extra = []
    try:
        syns = await repo.find(workspace_id, types=["Synonym"], limit=2000)
        hits = [s for s in syns if words(s.label) and f" {' '.join(words(s.label))} " in qtext]
        targets = [str((s.properties or {}).get("target_id") or "") for s in hits]
        for n in await repo.get_many(workspace_id, [t for t in targets if t]):
            if n.type in ("Attribute", "Metric", "Segment"):
                extra.append(n.label or "")
    except Exception:                                         # noqa: BLE001 - terms are optional
        return question
    return question + (" (" + ", ".join(e for e in extra if e) + ")" if extra else "")


async def _records(workspace_id, question: str, schema: Schema) -> list:
    """(id, table) for each record id the question names, found through the identity map
    (source_key - the ids ingestion saw, per asset)."""
    from opencortyx.core.dataplane import source_keys
    out: list = []
    for tok in planner.identifiers(question):
        try:
            hits = await source_keys.nodes_for(workspace_id, tok)
        except Exception:                                     # noqa: BLE001 - no identity map, no lookup
            hits = []
        seen: set = set()
        for h in hits:
            t = schema.table_of(h["asset_id"])
            if t is not None and t.view not in seen:
                seen.add(t.view)
                out.append((tok, t))
    return out


def _metric_plan(question: str, schema: Schema, metric: dict) -> Optional[planner.Plan]:
    """A defined Metric, used exactly as written: its expression over its table, with the filters
    the question names."""
    from opencortyx.retrieval.l2_data.schema import named_table
    t = schema.tables.get(metric.get("table") or "") or schema.table_of(metric.get("table") or "")
    # The metric's expression over the table the question names, when it names one ("the total fee
    # of licences-17"): same meaning, the rows asked about. A column it lacks fails and the rule
    # planner takes over.
    t = named_table(schema, question) or t
    if t is None:
        return None
    where, _ = planner._filters(question, [t])
    where += planner._time_filters(question, t)
    if metric.get("filters"):
        where.insert(0, f"({metric['filters']})")
    cond = f" WHERE {' AND '.join(where)}" if where else ""
    expr = metric["expression"]
    digits = planner.decimals(question)
    if digits is not None:
        expr = planner.rounded(expr, digits)
    sql = f'SELECT {expr} AS {planner._q(metric["name"])} FROM {planner._q(t.view)}{cond}'
    return planner.Plan(sql=sql, tables=[t.view], method="metric", confidence=0.9,
                        notes=[f"metric {metric['name']}"])


def _execute(workspace_id, schema: Schema, sql: str, timeout: float, tables=None):
    """Run `sql`; with `tables` (the views it reads, from the guard) only those assets are opened."""
    from opencortyx.stores.data import Views
    opened = schema.needs(tables) if tables else {"assets": schema.refs, "logical": schema.logical}
    with Views(workspace_id, lock=True, **opened) as v:
        t = v.query(sql, timeout=timeout)
        return t.column_names, [[_clean(x) for x in r.values()] for r in t.to_pylist()]


def planner_mode() -> str:
    """`plan` (default): the plan planner over the semantic layer first (semantic.py), the rules
    after it; `rules`: the rules alone - kept to measure one against the other."""
    import os
    return (os.environ.get("OPENCORTYX_DATA_PLANNER") or "plan").strip().lower()


async def answer(workspace_id, question: str, complete=None, *, timeout: float = 30.0,
                 embed=None, collection_ids=None) -> Optional[DataResult]:
    """The data route for `question`, or None when the data has nothing to say about it."""
    import anyio
    t0 = time.perf_counter()
    try:
        schema = await load_schema(workspace_id)
    except Exception as e:                                    # noqa: BLE001 - no data plane, no route
        log.info("data_route.schema_unavailable", error=str(e)[:200])
        return None
    if collection_ids:
        from opencortyx.core.dataplane import assets
        want = {str(c) for c in collection_ids}
        schema = schema.scoped([str(a.id) for a in await assets.list_assets(workspace_id)
                                if str((a.metadata_ or {}).get("collection_id") or "") in want])
    if schema.empty():
        return None
    question = await _with_terms(workspace_id, question)
    metrics, examples = await _definitions(workspace_id, question)
    plans: list = []
    plan_notes = ""
    if planner_mode() == "plan":
        from opencortyx.retrieval.l2_data import semantic
        try:
            sp, trace = await semantic.plans(workspace_id, question, schema, embed=embed)
            plans += sp
            plan_notes = trace.text()
            if not sp and schema.objects():
                # The semantic layer found no table for this question: refuse rather than have the
                # model write SQL for it. The deterministic rules below still get their turn.
                complete = None
        except Exception as e:                                # noqa: BLE001 - the rules still answer
            log.warning("data_route.plan_failed", error=str(e)[:300])
    if not planner.is_aggregate(question):
        # "What is the annual fee of licence LIC-1057?": the record itself, read by its key. A
        # lookup that finds nothing hands over to the next plan; it is not an answer of its own.
        plans += planner.lookup_plans(await _records(workspace_id, question, schema))
        metrics = []
    if metrics:
        p = _metric_plan(question, schema, metrics[0])
        if p:
            plans.append(p)
    rp = planner.rule_plan(question, schema)
    if rp:
        plans.append(rp)
    views = list(schema.tables)
    res = DataResult(ok=False)
    error, previous = "", ""
    tries = 0
    while tries <= MAX_RETRIES:
        if plans:
            plan = plans.pop(0)
        elif complete is not None:
            plan = await planner.model_plan(question, schema, complete, error=error, metrics=metrics,
                                            examples=examples, previous=previous)
            tries += 1
            if plan is None:
                break
        else:
            break
        try:
            checked = guard.check(plan.sql, views)
        except guard.Rejected as e:
            res.attempts.append({"sql": plan.sql, "method": plan.method, "error": str(e)})
            error, previous = str(e), plan.sql
            continue
        try:
            cols, rows = await anyio.to_thread.run_sync(
                lambda: _execute(workspace_id, schema, checked.sql, timeout, checked.tables))
        except Exception as e:                                # noqa: BLE001 - classified below
            msg = str(e)
            res.attempts.append({"sql": checked.sql, "method": plan.method, "error": msg[:300]})
            if _unreachable(msg):
                res.unknown_reason = "the source is unavailable"
                break
            error, previous = msg[:300], checked.sql
            continue
        if not rows and plan.method in ("lookup", "plan"):
            res.attempts.append({"sql": checked.sql, "method": plan.method, "error": "no such record"})
            continue
        res = DataResult(ok=True, sql=checked.sql, columns=cols, rows=rows, tables=checked.tables,
                         method=plan.method, attempts=res.attempts, notes=plan_notes if plan.method == "plan" else "")
        if not rows:
            # "No rows" is not "no": the answer says nothing matched, it does not deny anything.
            res.unknown_reason = "no matching rows"
        break
    res.assets = [{"asset_id": t.asset_id, "name": t.name, "storage": t.storage, "format": t.format,
                   "version_ref": t.version_ref}
                  for v in (res.tables or []) for t in [schema.tables.get(v) or schema.tables.get(v.lower())] if t]
    res.ms = int((time.perf_counter() - t0) * 1000)
    if not res.ok and not res.unknown_reason:
        return None
    return res


def _unreachable(msg: str) -> bool:
    m = msg.lower()
    return any(s in m for s in ("could not connect", "connection refused", "timed out", "unable to open",
                                "no such file", "server selection", "name or service not known",
                                "failed to connect", "cannot open file", "stopped after"))
