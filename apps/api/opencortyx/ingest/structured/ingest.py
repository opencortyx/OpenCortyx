"""Native v2 structured ingestion (Phase 2) — NO v1 tables, NO Dgraph.

Connect to a SQL source → introspect schema (SQLAlchemy `inspect`) → profile columns from a
small live sample → LLM-enrich (business name/description/domain + per-column semantics) → write
Table/Attribute nodes + HAS_ATTRIBUTE/JOINS_TO/REFERENCES edges DIRECTLY into the v2 substrate.

Relocated + retargeted from v1 (`routers/sql_connector.py` introspection + `workers/catalogue_tasks.py`
enrichment). Node ids are deterministic (uuid5 of org+source+table[/column]) → idempotent re-ingest.
After this, the structured catalogue no longer needs the v1 mirror.
"""
import os
import re
import uuid
from typing import Optional


import anyio
from sqlalchemy import create_engine, inspect, text

from opencortyx.core.config import settings
from opencortyx.core.db import V2Session
from opencortyx.core.substrate import NodeRepo
from opencortyx.core.inference.llm import complete
# the structured generators are built-in extractors, run through the generic engine.
from opencortyx.ingest.structured.catalog import _put, _uedge, _det_id, _parse_json, _s
from opencortyx.ingest.formats.structured.tabular import _jsonable
from opencortyx.ingest.pipelines.mapping_migrate import with_editor
from opencortyx.core.substrate import document as _node_document
from opencortyx.core.dataplane import evidence as _evidence
from opencortyx.ingest.structured import dataplane as _dataplane
from opencortyx.stores.data.layout import row_key as _row_key
from opencortyx.core.observability import get_logger
from opencortyx.domain.control import SchemaMapping
from opencortyx.ingest.extraction.templates import run_template_by_name
from opencortyx.knowledge.workspace import ensure_central_node

log = get_logger(__name__)

_repo = NodeRepo()


def _source_key(dsn: str) -> str:
    """Stable per-source identity (host:database) from the DSN, so catalogues from DIFFERENT
    databases never collide and re-ingesting the SAME database stays idempotent."""
    from urllib.parse import urlparse
    try:
        clean = (dsn or "").replace("+asyncpg", "").replace("+psycopg2", "").replace("+pymysql", "")
        u = urlparse(clean)
        if (u.scheme or "").startswith("sqlite"):
            import os
            return "sqlite:" + (os.path.basename(u.path or "") or "db")
        key = f"{(u.hostname or '').lower()}:{(u.path or '').lstrip('/')}".strip(":")
        return key or "db"
    except Exception:
        return "db"


# ── introspection + light profiling (sync; run in a worker thread) ────────
def _pg_sync_driver() -> str:
    """First installed SYNC psql driver (asyncpg is async-only and can't back create_engine)."""
    for name in ("psycopg2", "psycopg"):
        try:
            __import__(name)
            return name
        except Exception:
            continue
    return "psycopg2"          # default; surfaces a clear error if truly absent


def _sync_dsn(dsn: str) -> str:
    """Normalize a DSN to an installed sync driver for introspection/browse."""
    d = dsn or ""
    low = d.lower()
    if low.startswith("postgres"):
        i = d.find("://")
        rest = d[i + 3:] if i >= 0 else d
        return f"postgresql+{_pg_sync_driver()}://{rest}"
    if low.startswith("mysql://"):
        return "mysql+pymysql://" + d[len("mysql://"):]
    return d


def _sync_iter_rows(dsn: str, schema: Optional[str], table: str, batch_size: int = 2000):
    """Stream a SQL table's rows as dicts, in batches (sync; run each `next()` in a worker thread).
    Mirrors the file path's `tabular.iter_row_batches` so SQL row-mode shares the row-node logic.
    Server-side streaming keeps memory constant regardless of table size."""
    _d = _sync_dsn(dsn)
    eng = (create_engine(_d, connect_args={"connect_timeout": 5})
           if ("postgresql" in _d or "mysql" in _d) else create_engine(_d))
    ident = f"{schema}.{table}" if schema else table
    try:
        with eng.connect() as conn:
            res = conn.execution_options(stream_results=True).execute(text(f"SELECT * FROM {ident}"))
            keys = list(res.keys())
            while True:
                rows = res.fetchmany(batch_size)
                if not rows:
                    break
                yield [{keys[i]: r[i] for i in range(len(keys))} for r in rows]
    finally:
        eng.dispose()


def _sync_browse(dsn: str, schema: Optional[str] = None) -> list:
    """Fast, DataHub-style catalogue browse: schemas → tables & views (+ column names), with NO row
    profiling. Used by the ingestion picker to choose what to ingest. System schemas are filtered."""
    _d = _sync_dsn(dsn)
    eng = (create_engine(_d, connect_args={"connect_timeout": 5})
           if ("postgresql" in _d or "mysql" in _d) else create_engine(_d))
    _SYS = {"information_schema", "pg_catalog", "pg_toast", "pg_temp_1", "pg_toast_temp_1",
            "performance_schema", "mysql", "sys"}
    out = []
    try:
        insp = inspect(eng)
        try:
            schemas = [schema] if schema else (insp.get_schema_names() or [])
        except Exception:
            schemas = [schema] if schema else []
        if not schemas:
            schemas = [None]                              # dialects without schema concept
        for sch in schemas:
            if sch and sch.lower() in _SYS:
                continue
            try:
                tnames = insp.get_table_names(schema=sch)
            except Exception:
                tnames = []
            try:
                vnames = insp.get_view_names(schema=sch)
            except Exception:
                vnames = []
            entries = []
            for nm, kind in ([(t, "table") for t in tnames] + [(v, "view") for v in vnames]):
                try:
                    cols = [c["name"] for c in (insp.get_columns(nm, schema=sch) or [])]
                except Exception:
                    cols = []
                entries.append({"name": nm, "type": kind, "columns": cols})
            if entries:
                out.append({"schema": sch or "default", "tables": entries})
    finally:
        eng.dispose()
    return out


def _sync_introspect(dsn: str, schema: Optional[str], sample_n: Optional[int] = None) -> list:
    sample_n = sample_n or settings.STRUCTURED_SAMPLE_N
    _d = _sync_dsn(dsn)
    eng = (create_engine(_d, connect_args={"connect_timeout": 5})
           if ("postgresql" in _d or "mysql" in _d) else create_engine(_d))
    out = []
    try:
        insp = inspect(eng)
        dialect = eng.dialect.name
        sch = None if (schema in (None, "main") and dialect == "sqlite") else schema
        if sch is None and dialect == "postgresql":      # resolve so None ≡ 'public' (stable ids)
            try:
                with eng.connect() as c0:
                    sch = c0.execute(text("SELECT current_schema()")).scalar() or "public"
            except Exception:
                sch = "public"
        try:
            tables = insp.get_table_names(schema=sch)
        except Exception:
            tables = insp.get_table_names()
        try:
            views = insp.get_view_names(schema=sch)
        except Exception:
            views = []
        all_objs = [(t, False) for t in tables] + [(v, True) for v in views]
        with eng.connect() as conn:
            for t, is_view in all_objs:
                try:
                    cols = insp.get_columns(t, schema=sch)
                    pk = [] if is_view else (insp.get_pk_constraint(t, schema=sch).get("constrained_columns", []) or [])
                    fks = [] if is_view else (insp.get_foreign_keys(t, schema=sch) or [])
                    fkmap = {fk["constrained_columns"][0]: fk for fk in fks
                             if fk.get("constrained_columns")}
                    ident = f"{sch}.{t}" if sch else t
                    sample = []
                    try:
                        res = conn.execute(text(f"SELECT * FROM {ident} LIMIT {sample_n}"))
                        keys = list(res.keys())
                        for r in res:
                            sample.append({keys[i]: r[i] for i in range(len(keys))})
                    except Exception:
                        pass
                    colmeta = []
                    for c in cols:
                        name = c["name"]
                        vals = [row.get(name) for row in sample]
                        nulls = sum(1 for v in vals if v is None)
                        samples = [str(v) for v in vals if v is not None][:5]
                        fk = fkmap.get(name)
                        colmeta.append({
                            "name": name, "type": str(c.get("type")),
                            "nullable": bool(c.get("nullable", True)),
                            "primary_key": name in pk, "foreign_key": bool(fk),
                            "references_table": (fk.get("referred_table") if fk else None),
                            "references_column": (fk["referred_columns"][0]
                                                  if fk and fk.get("referred_columns") else None),
                            "null_rate": round(nulls / len(sample), 3) if sample else None,
                            "sample_values": samples,
                        })
                    rc = None
                    if not is_view:                       # COUNT(*) on a view can be expensive — skip
                        try:
                            if dialect == "postgresql":
                                rc = conn.execute(
                                    text("SELECT reltuples::bigint FROM pg_class WHERE relname=:t"),
                                    {"t": t}).scalar()
                            else:
                                rc = conn.execute(text(f"SELECT COUNT(*) FROM {ident}")).scalar()
                        except Exception:
                            pass
                    out.append({"table": t, "schema": sch, "row_count": rc, "columns": colmeta,
                                "is_view": is_view})
                except Exception as e:
                    log.warning("structured.introspect_skipped", table=str(t), error=str(e))
                    continue
    finally:
        eng.dispose()
    return out


async def ingest_sql_source(workspace_id: uuid.UUID, dsn: str, schema: Optional[str] = None,
                            collection_id=None, enrich: bool = True, only_tables=None,
                            prune: bool = True, reading_mode: str = "catalog",
                            row_node_type: str | None = None, row_cols: list | None = None,
                            step1_cfg: dict | None = None, connector_id=None) -> dict:
    """Introspect a SQL source and write its catalogue natively into v2. Idempotent.

    With the workspace's data in Delta and a saved connector, each table is also REGISTERED as a
    live external asset (ingest/structured/dataplane.py): read in place through DuckDB, never copied,
    and profiled there instead of keeping sample values in the graph.

    `enrich` gates the per-table LLM business-semantics step; `only_tables` (a set/list of table
    names) restricts ingestion to the tables the Structured Pipeline Builder selected. `prune=False`
    disables dropped-table cleanup — required for SCOPED/parallel ingestions (one schema per task)
    so a partial run never deletes tables another selection or schema is responsible for.

    `reading_mode` mirrors the Step-1 "reading rules" card and gives SQL sources the SAME choice as
    file ingestion: 'catalog' (table→Table, columns→Attribute — the default) or 'row' (each row → one
    node, columns→attributes, foreign-key columns→edges). When mode='auto', `auto_mode` in the
    Step-1 config selects the sub-mode ('catalog' or 'row'); without it, defaults to 'row' for
    backward compatibility. `row_node_type` / `row_cols` (the card's
    declared column roles) drive row mode. The catalog is ALWAYS written first (provenance/schema);
    row mode adds the per-row nodes on top."""
    # Resolve effective mode: "auto" defers to auto_mode (catalog|row), defaulting to "row" (legacy).
    step1_cfg = step1_cfg or {}
    effective_mode = reading_mode
    if reading_mode == "auto":
        effective_mode = (step1_cfg.get("auto_mode") or "row").strip()
    from opencortyx.ingest.pipelines import signature as _sig
    if _sig.extract_only_on(step1_cfg):              # "Extract only": tables and columns, no rows
        effective_mode = "catalog"
    tables = await anyio.to_thread.run_sync(lambda: _sync_introspect(dsn, schema))
    counts = {"tables": 0, "columns": 0, "joins": 0, "edges": 0}
    if not tables:
        return {"status": "no_tables", **counts}
    if only_tables:                                  # honor the builder's target-table selection
        sel = set(only_tables)
        tables = [t for t in tables if t["table"] in sel] or tables

    # per-source identity (host:db) + resolved schema → ids unique per database, idempotent re-ingest
    source_id_key = _source_key(dsn)
    tschema = {t["table"]: (t.get("schema") or "") for t in tables}

    def _mid(table):
        return _det_id(workspace_id, "mso", f"{source_id_key}.{tschema.get(table, '')}.{table}")

    def _pid(table, col):
        return _det_id(workspace_id, "prop", f"{source_id_key}.{tschema.get(table, '')}.{table}.{col}")

    table_ids = {t["table"]: _mid(t["table"]) for t in tables}
    from opencortyx.core.dataplane.connectors import kind_of as _dsn_kind
    live_kind = _dsn_kind(dsn) if (connector_id and await _dataplane.uses_delta(workspace_id)) else None

    for tbl in tables:
        table_id = table_ids[tbl["table"]]
        meta, colmeta = {}, {}
        live: dict = {}
        if live_kind:
            qualified = f"{tbl['schema']}.{tbl['table']}" if tbl.get("schema") else tbl["table"]
            live = await _dataplane.register_live_table(
                workspace_id, table_id, connector_id=connector_id, kind=live_kind, table=qualified,
                name=tbl["table"], columns=tbl["columns"], source_key=source_id_key)
        pk_field = next((c["name"] for c in tbl["columns"] if c["primary_key"]), "")
        desc = meta.get("description", "")
        domain = meta.get("domain", "")
        await _put(node_id=table_id, workspace_id=workspace_id, type="Table",
                   label=meta.get("name") or tbl["table"],
                   properties={
                       "table_name": tbl["table"], "schema_name": tbl["schema"],
                       "description": desc, "domain": domain, "row_count": tbl["row_count"],
                       "column_count": len(tbl["columns"]), "primary_key_field": pk_field,
                       "synonyms": meta.get("synonyms", []),
                       "sample_questions": meta.get("sample_questions", []),
                       "source_table": f"{tbl['schema'] or ''}.{tbl['table']}".strip("."),
                       "source_system": "sql", "source_key": source_id_key, "native": True,
                       **({"storage": "external", "asset_kind": "structured", "live": True}
                          if live_kind else {})},
                   source_ref=None, collection_id=collection_id)
        counts["tables"] += 1

        for c in tbl["columns"]:
            cid = _pid(tbl["table"], c["name"])
            cm = colmeta.get(c["name"], {})
            is_pii = bool(cm.get("is_pii"))
            await _put(
                node_id=cid, workspace_id=workspace_id, type="Attribute", sensitivity="confidential" if is_pii else None,
                label=cm.get("canonical_name") or c["name"],
                properties={
                    "physical_column": c["name"], "physical_table": tbl["table"],
                    "data_type": c["type"], "semantic_type": cm.get("semantic_type", ""),
                    "description": cm.get("description", ""), "parent_table_id": str(table_id),
                    "is_primary_key": c["primary_key"], "is_foreign_key": c["foreign_key"],
                    "references_table": c["references_table"],
                    "references_column": c["references_column"],
                    "is_pii": is_pii, "nullable": c["nullable"], "null_rate": c["null_rate"],
                    "source_key": source_id_key, "native": True,
                    **(_dataplane.attribute_profile_props((live.get("profile") or {}).get(c["name"]))
                       if live_kind else {"sample_values": c["sample_values"]})},
                source_ref=table_id, collection_id=collection_id)
            await _uedge(workspace_id=workspace_id, source_id=table_id, target_id=cid, type="HAS_ATTRIBUTE")
            counts["columns"] += 1

    # FK edges after every Table/Attribute exists
    for tbl in tables:
        table_id = table_ids[tbl["table"]]
        for c in tbl["columns"]:
            rt = c["references_table"]
            if not (c["foreign_key"] and rt and rt in table_ids):
                continue
            jprops = {"from_key": c["name"], "to_key": c["references_column"],
                      "relationship_type": "FOREIGN_KEY", "cardinality": "MANY_TO_ONE"}
            if await _uedge(workspace_id=workspace_id, source_id=table_id, target_id=table_ids[rt],
                            type="JOINS_TO", properties=jprops):
                counts["joins"] += 1
            # directional semantics (R2/R3): child BELONGS_TO parent, parent HAS_MANY child
            await _uedge(workspace_id=workspace_id, source_id=table_id, target_id=table_ids[rt],
                         type="BELONGS_TO", properties=jprops)
            await _uedge(workspace_id=workspace_id, source_id=table_ids[rt], target_id=table_id,
                         type="HAS_MANY", properties={"from_key": c["references_column"],
                                                      "to_key": c["name"], "cardinality": "ONE_TO_MANY"})
            if c["references_column"]:
                fcol = _pid(tbl["table"], c["name"])
                tcol = _pid(rt, c["references_column"])
                if await _uedge(workspace_id=workspace_id, source_id=fcol, target_id=tcol, type="REFERENCES"):
                    counts["edges"] += 1
    # idempotent re-ingest: drop the Attribute nodes of columns that no longer exist
    try:
        current_prop_ids = {str(_pid(tbl["table"], c["name"])) for tbl in tables for c in tbl["columns"]}
        for tbl in tables:
            table_id = table_ids[tbl["table"]]
            stale = [n.id for n in await _repo.find(workspace_id, source_refs=[table_id], exact_types=["Attribute"])
                     if str(n.id) not in current_prop_ids]
            if stale:
                counts["pruned"] = counts.get("pruned", 0) + await _repo.delete_nodes(workspace_id, stale)
    except Exception as e:                       # pruning must never break ingestion
        log.warning("structured.prune_skipped", error=str(e))

    # prune DROPPED TABLES — only on a FULL source sync (not a subset, not a scoped ingestion),
    # else a partial/parallel run would wrongly delete tables it isn't responsible for.
    if prune and not only_tables:
        try:
            current_tables = {t["table"] for t in tables}
            table_nodes = await _repo.find(workspace_id, types=["Table"], source_key=source_id_key)
            drop_ids = [m.id for m in table_nodes if (m.properties or {}).get("table_name") not in current_tables]
            if drop_ids:
                columns = [n.id for n in await _repo.find(workspace_id, source_refs=drop_ids, exact_types=["Attribute"])]
                await _repo.delete_nodes(workspace_id, columns + drop_ids)
                counts["pruned_tables"] = len(drop_ids)
        except Exception as e:
            log.warning("structured.dropped_table_prune_skipped", error=str(e))

    if effective_mode == "row":
        # full Step-1 config (per-table cards + pattern rules + id patterns); legacy args fold in
        cfg = dict(step1_cfg or {})
        cfg.setdefault("columns", row_cols or [])
        if row_node_type and not cfg.get("row_node_type"):
            cfg["row_node_type"] = row_node_type
        _SENTINEL = object()
        counts["rows"] = 0
        from opencortyx.ingest.pipelines import signature as _sig
        _cards = cfg.get("tables") or []
        # With a mapping bound and auto-accept off, tables no card matches are skipped; otherwise
        # they are read with the default rules (signature.auto_create_on).
        _gate = bool(_cards) and not _sig.auto_create_on(cfg, bound=bool(_cards))
        for tbl in tables:
            if tbl.get("is_view"):
                continue                                   # views have no stable row identity
            if _gate and _sig.match_table_card(_cards, tbl["table"], tbl["columns"]) is None:
                counts["ignored_tables"] = counts.get("ignored_tables", 0) + 1
                continue
            table_id = table_ids[tbl["table"]]
            roles = _col_roles(cfg, tbl["columns"], table_name=tbl["table"])
            ntype = _row_node_type(cfg, tbl["table"])
            id_col = (next((c["name"] for c in tbl["columns"]
                            if roles.get(c["name"], {}).get("role") == "identity"), None)
                      or next((c["name"] for c in tbl["columns"] if c["primary_key"]), None))
            attr_cols = ([c["name"] for c in tbl["columns"]
                          if roles.get(c["name"], {}).get("role") in _ATTR_ROLES]
                         or [c["name"] for c in tbl["columns"]   # nothing declared → all non-dropped
                             if roles.get(c["name"], {}).get("role") not in _DROP_ROLES])
            fk_cols = [c["name"] for c in tbl["columns"]
                       if roles.get(c["name"], {}).get("role") == "fk"
                       or (c["foreign_key"] and roles.get(c["name"], {}).get("role") not in _DROP_ROLES)]
            gen = _sync_iter_rows(dsn, tbl.get("schema"), tbl["table"], 2000)
            ordinal = 0
            seen_rows: set = set()
            while True:
                batch = await anyio.to_thread.run_sync(lambda g=gen: next(g, _SENTINEL))
                if batch is _SENTINEL:
                    break
                for row in batch:
                    if id_col and row.get(id_col) not in (None, ""):
                        rid = uuid.uuid5(uuid.NAMESPACE_OID, f"{workspace_id}:{source_id_key}:{ntype}:{row[id_col]}")
                        label = str(row[id_col])
                    else:
                        rid = uuid.uuid5(uuid.NAMESPACE_OID,
                                         f"{workspace_id}:{source_id_key}:{ntype}:{tbl['table']}:row:{ordinal}")
                        label = f"{ntype} #{ordinal}"
                    props = {"__source_key": source_id_key, "native": True}
                    for c in attr_cols:
                        if row.get(c) is not None:      # card `attr_name` renames the attribute
                            props[roles.get(c, {}).get("attr_name") or c] = _jsonable(row[c])
                    props[_node_document.STORED_KEY] = _node_document.document(props, label, ntype)
                    if rid in seen_rows:
                        _note_duplicate_identity(counts, label)
                    await _put(node_id=rid, workspace_id=workspace_id, type=ntype, label=label,
                               properties=props, source_ref=table_id, collection_id=collection_id)
                    await _uedge(workspace_id=workspace_id, source_id=table_id, target_id=rid, type="CONTAINS")
                    seen_rows.add(rid)
                    counts["rows"] += 1
                    for c in fk_cols:                  # FK column → edge to the referenced entity
                        val = row.get(c)
                        if val in (None, ""):
                            continue
                        introspected = next((cc["references_table"] for cc in tbl["columns"]
                                             if cc["name"] == c and cc.get("references_table")), None)
                        _poly_rules = (_table_card(cfg, tbl["table"]) or {}).get("polymorphic") or []
                        # Every row-entity type this run declares, so a foreign key into one of
                        # them lands on the real row instead of on a value-keyed twin of it.
                        _row_types = {str(tc.get("node_type") or "").strip()
                                      for tc in ((cfg or {}).get("tables") or [])
                                      if str(tc.get("node_type") or "").strip()}
                        if await _fk_edge(workspace_id, rid, val, c, roles.get(c, {}),
                                          introspected_ref=introspected, collection_id=collection_id,
                                          row=row, polymorphic=_poly_rules,
                                          row_types=_row_types, source_key=source_id_key):
                            counts["edges"] += 1
                    ordinal += 1
            # Rows the source no longer has. Columns and tables were pruned on re-sync; rows were
            # not, so a deleted order stayed in the graph as a current order. The table was read in
            # full (no row cap on this path), so what this read did not produce is gone at the source.
            # Unlike dropped-TABLE cleanup this is safe on a scoped run too: it touches only the table
            # just read, and only rows of the type this read writes.
            try:
                stale = [n.id for n in await _repo.find(workspace_id, source_refs=[table_id], exact_types=[ntype])
                         if n.id not in seen_rows]
                if stale:
                    counts["pruned_rows"] = counts.get("pruned_rows", 0) + await _repo.delete_nodes(workspace_id, stale)
            except Exception as e:               # pruning must never break ingestion
                log.warning("structured.row_prune_skipped", table=tbl["table"], error=str(e))
    return {"status": "ok", "source_key": source_id_key, "source_ids": [str(v) for v in table_ids.values()],
            "mode": reading_mode, "effective_mode": effective_mode, **counts}


def _node_from_ref(ref: str) -> str:
    """'customers' → 'Customer' (singularise + PascalCase) for a FK edge's target entity type."""
    w = (ref or "").strip()
    if w.endswith("ies"):
        w = w[:-3] + "y"
    elif w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    parts = [p for p in w.replace("-", "_").split("_") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Entity"


def _rule_match(name: str, rules: list) -> dict:
    """First pattern rule (P23 for tables) whose key pattern matches this COLUMN name — {} if none.
    Rule: {op: contains|equals|regex, value, set: {role|edge_type|target_type|skip}}."""
    import re as _re
    leaf = (name or "").lower()
    for r in (rules or []):
        st = (r or {}).get("set") or {}
        val = str((r or {}).get("value") or "").lower()
        op = (r or {}).get("op") or "contains"
        if not val or not st:
            continue
        try:
            if (op == "equals" and leaf == val) or (op == "regex" and _re.search(val, leaf)) \
                    or (op == "contains" and val in leaf):
                return st
        except _re.error:
            continue
    return {}


def _table_card(step1_cfg: dict, table_name: str | None):
    """The Step-1 per-TABLE card for this table, or None. Matching is file-flexible, because for a
    tabular FILE the "table" is the filename stem: exact (case-insensitive) → separator-insensitive
    ("order-items" == "order_items") → suffixed-stem prefix ("orders_2024-07" matches card "orders"),
    so one card covers a whole folder of dated exports. Most specific (longest) card name wins."""
    name = str(table_name or "").strip().lower()
    if not name:
        return None
    norm = re.sub(r"[^a-z0-9]", "", name)
    cards = [(str(t.get("table") or "").strip().lower(), t)
             for t in (step1_cfg or {}).get("tables") or [] if isinstance(t, dict)]
    for cname, t in cards:
        if cname and cname == name:
            return t
    for cname, t in cards:
        if cname and re.sub(r"[^a-z0-9]", "", cname) == norm:
            return t
    best = None
    for cname, t in cards:
        if cname and name.startswith(cname) and re.match(r"[^a-z0-9]", name[len(cname):] or " "):
            if best is None or len(cname) > len(best[0]):
                best = (cname, t)
    return best[1] if best else None


KEYNODE = "KeyNode"


def _row_node_semantic(step1_cfg: dict, table_name: str) -> str:
    """What a row MEANS: the per-table card wins, then the legacy row_node_type, then the name
    derived from the table ('orders' → 'Order'). Used for the label, the `key` attribute and any
    edge name — never as the node's type unless it was DECLARED."""
    card = _table_card(step1_cfg, table_name)
    return ((card or {}).get("node_type") or (step1_cfg or {}).get("row_node_type")
            or _node_from_ref(table_name)).strip() or "Record"


def _row_node_type(step1_cfg: dict, table_name: str) -> str:
    """What a row IS. A DECLARED type (card / row_node_type) is honoured exactly as written; a name
    merely derived from the table becomes the structural default."""
    card = _table_card(step1_cfg, table_name)
    declared = ((card or {}).get("node_type") or (step1_cfg or {}).get("row_node_type") or "").strip()
    return declared or KEYNODE


def _col_roles(step1_cfg: dict, columns: list, table_name: str | None = None) -> dict:
    """Map each physical column → its full graph decision:
        {role, ref, target_type, edge_type, edge_dir, attr_name}
    Resolution order (declared always wins over inferred):
      per-TABLE card column  >  legacy global card column  >  pattern rule  >  id_patterns  >  guess.
    role vocabulary: identity | fk | measure | dimension | text | ignore (skip == ignore)."""
    from opencortyx.ingest.formats.structured import tabular as _T
    cfg = step1_cfg or {}
    declared: dict = {}
    card = _table_card(cfg, table_name)
    for src in (((card or {}).get("columns") or []), (cfg.get("columns") or [])):
        for c in src:
            if isinstance(c, dict) and c.get("name"):
                declared.setdefault(str(c["name"]).lower(), c)
    rules = cfg.get("rules") or []
    idpats = [str(p).strip().lower() for p in (cfg.get("id_patterns") or []) if str(p).strip()]
    out = {}
    for col in columns:
        name = col["name"]
        d = declared.get(name.lower())
        if d:
            out[name] = {"role": d.get("role") or _T.guess_role(name, col["type"]), "ref": d.get("ref"),
                         "target_type": d.get("target_type"), "edge_type": d.get("edge_type"),
                         "edge_dir": d.get("edge_dir"), "attr_name": d.get("attr_name")}
            continue
        st = _rule_match(name, rules)
        if st.get("skip"):
            role = "ignore"
        elif st.get("role"):
            role = st["role"]
        elif any(p in name.lower() for p in idpats):
            role = "identity"
        else:
            role = _T.guess_role(name, col["type"])
        out[name] = {"role": role, "ref": None, "target_type": st.get("target_type"),
                     "edge_type": st.get("edge_type"), "edge_dir": st.get("edge_dir"), "attr_name": None}
    return out


_ATTR_ROLES = ("measure", "dimension", "text", "identity")
_DROP_ROLES = ("ignore", "skip")


def _polymorphic_target(col_name: str, row: dict, rules: list) -> str:
    """The node type a POLYMORPHIC foreign key points at for THIS row.

    `owner_id` is a Customer when `owner_type='customer'` and a Supplier when it is `'supplier'`: the
    target is a property of the row, not of the column, so a per-column `target_type` cannot say it.
    Returns "" when no rule matches, leaving the column's declared/derived type in charge.
    """
    for p in rules or []:
        if str(p.get("column") or "") != col_name:
            continue
        val = row.get(p.get("discriminator"))
        if val is None:
            continue
        if str(val).strip().lower() == str(p.get("value") or "").strip().lower():
            return str(p.get("target_type") or "")
    return ""


async def _fk_edge(workspace_id, rid, row_val, col_name, r: dict, introspected_ref=None,
                   collection_id=None, row: dict | None = None, polymorphic: list | None = None,
                   row_types: set | None = None, source_key: str = "") -> bool:
    """Write ONE row-entity foreign-key edge honouring the card: target node type override (including
    a per-ROW polymorphic override), edge type override, and direction ('in' flips it: target → row).
    Creates/dedupes the target by value."""
    from opencortyx.stores.graph.query import normalize_name
    ref = r.get("ref") or introspected_ref
    declared_target = (_polymorphic_target(col_name, row or {}, polymorphic or [])
                       or (r.get("target_type") or "").strip())
    # What the FK target MEANS ('employee_id' → 'Employee'); used for the edge name and `key`.
    target_sem = declared_target or _node_from_ref(
        ref if ref else (col_name[:-3] if col_name.lower().endswith("_id") else col_name))
    target_type = declared_target or KEYNODE
    if declared_target and source_key and row_types and declared_target in row_types:
        tid = uuid.uuid5(uuid.NAMESPACE_OID, f"{workspace_id}:{source_key}:{declared_target}:{row_val}")
        target_type = declared_target
        _tprops = {"native": True}
    else:
        tid = uuid.uuid5(uuid.NAMESPACE_OID, f"{workspace_id}:{target_sem}:{normalize_name(str(row_val))}")
        _tprops = {"name": str(row_val), "native": True}
        if target_type == KEYNODE and target_sem:
            _tprops["key"] = target_sem
    await _put(node_id=tid, workspace_id=workspace_id, type=target_type, label=str(row_val),
               properties=_tprops, collection_id=collection_id)
    # Edge from the SEMANTIC name — `HAS_KEYNODE` would erase the only readable part of the link.
    etype = (r.get("edge_type") or "").strip() or f"HAS_{target_sem}".upper()
    if r.get("edge_dir") == "in":
        return bool(await _uedge(workspace_id=workspace_id, source_id=tid, target_id=rid, type=etype))
    return bool(await _uedge(workspace_id=workspace_id, source_id=rid, target_id=tid, type=etype))


async def ingest_tabular_file(workspace_id: uuid.UUID, path: str, filename: str,
                              step1_cfg: dict | None = None, collection_id=None,
                              overrides: dict | None = None, enrich: bool = False,
                              reporter=None, batch_size: int = 2000) -> dict:
    """Ingest a CSV/TSV/Parquet/Excel file into the v2 graph via DuckDB. Idempotent per (org, file).

    `step1_cfg` is the structured pipeline's Step-1 config: {mode, row_node_type, columns:[{name,
    role, ref}]}. mode='catalog' → Table + Attribute nodes (schema catalog, like a DB table);
    mode='row' → one entity node per row, columns→attributes, foreign-key columns→edges.

    A MULTI-SHEET workbook is several tables and every one of them is ingested. `read_xlsx` reads a
    single sheet, so this used to take `tables[0]` and drop every later sheet silently — no error, no
    count, nothing saying the data was there. Each sheet is gated, keyed and reported on its own.
    """
    import anyio
    from opencortyx.ingest.formats.structured import tabular as _T

    from opencortyx.core.config import runtime as _rc
    step1_cfg = step1_cfg or {}
    # No pipeline Step-1 card → fall back to the configured default rather than a hardcoded
    # "catalog", which produced `rows: 0` for every ad-hoc CSV upload.
    mode = (step1_cfg.get("mode") or _rc.structured_default_mode()).strip()
    # Resolve effective mode: "auto" defers to auto_mode (catalog|row), defaulting to "row" (legacy).
    effective_mode = mode
    if mode == "auto":
        effective_mode = (step1_cfg.get("auto_mode") or "row").strip()
    from opencortyx.ingest.pipelines import signature as _sig
    if _sig.extract_only_on(step1_cfg):              # "Extract only": tables and columns, no rows
        effective_mode = "catalog"
    stem = _T.table_name_of(filename)

    # 1) introspect (columns + sample) off-thread — DuckDB is blocking
    tables = await anyio.to_thread.run_sync(lambda: _T.introspect_file(path, filename, overrides))
    if not tables:
        return {"status": "no_columns", "tables": 0, "columns": 0, "rows": 0, "edges": 0}

    if len(tables) == 1:
        return await _ingest_one_table(
            workspace_id, path, filename, tables[0], source_key=f"file:{stem}",
            step1_cfg=step1_cfg, collection_id=collection_id, overrides=overrides,
            mode=mode, effective_mode=effective_mode, reporter=reporter, batch_size=batch_size)

    # Multi-sheet workbook: the sheet is part of the identity, or two sheets in one file would
    # collide on `file:<stem>` and overwrite each other's rows.
    results, agg = [], {"tables": 0, "columns": 0, "rows": 0, "edges": 0}
    for t in tables:
        sheet = t.get("sheet") or t.get("table")
        res = await _ingest_one_table(
            workspace_id, path, filename, t, source_key=f"file:{stem}#{sheet}",
            step1_cfg=step1_cfg, collection_id=collection_id,
            overrides={**(overrides or {}), "sheet": sheet},
            mode=mode, effective_mode=effective_mode, reporter=reporter, batch_size=batch_size)
        for k in agg:
            agg[k] += int(res.get(k) or 0)
        results.append({"sheet": sheet, **{k: res.get(k) for k in
                                           ("status", "source_key", "rows", "rows_available",
                                            "rows_truncated", "reason")}})
    # A sheet the gate ignored is REPORTED, not invisible: "3 of 4 sheets read" is the difference
    # between a mapping that needs a card and a workbook you think ingested whole.
    ignored = [r for r in results if r.get("status") == "ignored"]
    return {"status": "ok", "source": filename, "sheets": len(tables),
            "sheets_ingested": len(tables) - len(ignored), "sheets_ignored": len(ignored),
            "per_sheet": results, "mode": mode, "effective_mode": effective_mode, **agg}


async def _ingest_one_table(workspace_id: uuid.UUID, path: str, filename: str, tbl: dict, *,
                            source_key: str, step1_cfg: dict, collection_id, overrides: dict | None,
                            mode: str, effective_mode: str, reporter, batch_size: int) -> dict:
    """One relation — a file, or ONE SHEET of a workbook — into the graph.

    `overrides` must already select this table (the sheet is pinned by the caller), and `source_key`
    is its identity: everything below derives node ids from it.
    """
    import anyio
    from opencortyx.ingest.formats.structured import tabular as _T

    from opencortyx.core.config import runtime as _rc
    counts = {"tables": 0, "columns": 0, "rows": 0, "edges": 0}

    from opencortyx.ingest.pipelines import signature as _sig
    _cards = step1_cfg.get("tables") or []
    if _cards and not _sig.auto_create_on(step1_cfg, bound=True):
        if _sig.match_table_card(_cards, tbl["table"], tbl["columns"]) is None:
            cols = ", ".join(str(c.get("name")) for c in tbl["columns"][:12])
            return {"status": "ignored", "source_key": source_key, "table": tbl["table"],
                    "reason": f"columns ({cols}) match no bound schema mapping and auto-accept is off",
                    **counts}

    roles = _col_roles(step1_cfg, tbl["columns"], table_name=tbl["table"])
    table_id = _det_id(workspace_id, "mso", source_key)
    node_type = _row_node_type(step1_cfg, tbl["table"])
    node_sem = _row_node_semantic(step1_cfg, tbl["table"])

    # Data in Delta (workspace data_store = delta): EVERY row goes to the asset's Delta table, keyed
    # the way row mode keys its evidence, whatever the mode - a catalog-only table is still fully
    # answerable through SQL. The graph below is written exactly as before.
    delta_mode = await _dataplane.uses_delta(workspace_id)
    stored: dict = {}
    if delta_mode:
        _key = next((c["name"] for c in tbl["columns"]
                     if roles.get(c["name"], {}).get("role") == "identity"), None)
        stored = await _dataplane.store_file_table(
            workspace_id, table_id, path=path, filename=filename, overrides=overrides,
            name=tbl["table"], key_columns=[_key] if _key else [], columns=tbl["columns"],
            source_key=source_key, collection_id=collection_id)
        counts["rows_stored"] = stored.get("rows", 0)

    # 2) always write the file's catalog node (Table) + its columns (Attribute) — provenance + schema.
    await _put(node_id=table_id, workspace_id=workspace_id, type="Table", label=tbl["table"],
               # no `schema_name`: a file has no database schema, and an empty one reads as a field
               # left unfilled on every CSV and spreadsheet
               properties={"table_name": tbl["table"], "row_count": tbl["row_count"],
                           "column_count": len(tbl["columns"]), "source_system": "file",
                           "source_file": filename, "source_key": source_key,
                           "reading_mode": mode, "native": True,
                           **({"storage": "managed", "asset_kind": "structured",
                               "rows_stored": stored.get("rows", 0)} if delta_mode else {})},
               source_ref=None, collection_id=collection_id)
    counts["tables"] = 1
    for c in tbl["columns"]:
        r = roles.get(c["name"], {})
        cid = _det_id(workspace_id, "prop", f"{source_key}.{c['name']}")
        aprops = {"physical_column": c["name"], "physical_table": tbl["table"],
                  "data_type": c["type"], "raw_data_type": c.get("raw_type"),
                  "parent_table_id": str(table_id),
                  "graph_role": r.get("role"), "references_table": r.get("ref"),
                  "is_primary_key": c["primary_key"], "is_foreign_key": c["foreign_key"],
                  "null_rate": c["null_rate"], "sample_values": c["sample_values"],
                  "source_key": source_key, "native": True}
        if delta_mode:
            # No sample values in the graph: the rows are in Delta. A category column keeps its
            # values and counts (value_set), which the SQL planner needs to write a filter that matches.
            aprops.pop("sample_values", None)
            aprops.update(_dataplane.attribute_profile_props((stored.get("profile") or {}).get(c["name"])))
        await _put(node_id=cid, workspace_id=workspace_id, type="Attribute", label=c["name"],
                   properties=aprops, source_ref=table_id, collection_id=collection_id)
        await _uedge(workspace_id=workspace_id, source_id=table_id, target_id=cid, type="HAS_ATTRIBUTE")
        counts["columns"] += 1
    if effective_mode != "row":                       # catalog mode → schema only, done
                                                     # (auto+catalog = auto-inferred schema only)
        return {"status": "ok", "source_key": source_key, "source_id": str(table_id), "mode": mode, "effective_mode": effective_mode, **counts}

    # 3) row mode → stream rows in batches, one entity per row + attributes + FK edges.
    id_col = next((c["name"] for c in tbl["columns"] if roles.get(c["name"], {}).get("role") == "identity"), None)
    attr_cols = ([c["name"] for c in tbl["columns"]
                  if roles.get(c["name"], {}).get("role") in _ATTR_ROLES]
                 or [c["name"] for c in tbl["columns"]
                     if roles.get(c["name"], {}).get("role") not in _DROP_ROLES])
    fk_cols = [c["name"] for c in tbl["columns"] if roles.get(c["name"], {}).get("role") == "fk"]

    _SENTINEL = object()
    gen = _T.iter_row_batches(path, filename, overrides, batch_size)
    ordinal = 0
    max_rows = (_parse_max_rows(step1_cfg["max_rows"]) if "max_rows" in step1_cfg
                else _rc.structured_default_max_rows())
    if reporter:
        _planned = min(max_rows, tbl["row_count"] or max_rows) if max_rows else (tbl["row_count"] or 0)
        await reporter.start(max(1, _planned // batch_size + 1))
    step = 0
    truncated = False
    seen_rows: set = set()
    _occ: dict = {}                                   # duplicate rows, for their _row_key
    _keys: list = []                                  # (source id, entity type, node) per batch
    # The key a row is known by - the declared identity, else the natural key the data store found
    # (the same one its Delta row is keyed by, so evidence opens the row).
    _key_cols = [id_col] if id_col else list(stored.get("key_columns") or [])
    while True:
        if max_rows and counts["rows"] >= max_rows:
            truncated = (tbl["row_count"] or 0) > counts["rows"]
            break
        batch = await anyio.to_thread.run_sync(lambda: next(gen, _SENTINEL))
        if batch is _SENTINEL:
            break
        if max_rows:                                   # never overshoot inside the final batch
            remaining = max_rows - counts["rows"]
            if len(batch) > remaining:
                batch = batch[:remaining]
                truncated = True
        for row in batch:
            # stable id: by identity value (idempotent) else by ordinal — ALWAYS org-scoped so
            # two orgs (or two files) with the same key never share a node.
            if id_col and row.get(id_col) not in (None, ""):
                rid = uuid.uuid5(uuid.NAMESPACE_OID, f"{workspace_id}:{source_key}:{node_type}:{row[id_col]}")
                label = str(row[id_col])
            else:
                rid = uuid.uuid5(uuid.NAMESPACE_OID, f"{workspace_id}:{source_key}:{node_type}:row:{ordinal}")
                label = f"{node_sem} #{ordinal}"      # "Employee #0", never "KeyNode #0"
            # `key` carries what the type used to say, so nothing the vocabulary encoded is lost
            # and it stays queryable — same contract as the semi-structured walker.
            props = {"__source_key": source_key, "native": True}
            if node_type == KEYNODE and node_sem:
                props["key"] = node_sem
            for c in attr_cols:
                if row.get(c) is not None:              # card `attr_name` renames the attribute
                    props[roles.get(c, {}).get("attr_name") or c] = _jsonable(row[c])
            props[_node_document.STORED_KEY] = _node_document.document(props, label, node_sem)
            if rid in seen_rows:
                _note_duplicate_identity(counts, label)
            # The row this node and its FK edges rest on (core/dataplane/evidence.py); the same key
            # the row has in the asset's Delta table, so the pointer opens it.
            with _evidence.unit(table_id, "row", _row_key(row, _key_cols, _occ),
                                quote=str(props[_node_document.STORED_KEY])[:300],
                                extractor="row mode"):
                await _put(node_id=rid, workspace_id=workspace_id, type=node_type, label=label,
                           properties=props, source_ref=table_id, collection_id=collection_id)
                await _uedge(workspace_id=workspace_id, source_id=table_id, target_id=rid, type="CONTAINS")
                if delta_mode and _key_cols and row.get(_key_cols[0]) not in (None, ""):
                    _keys.append((row[_key_cols[0]], node_sem or node_type, rid))
                seen_rows.add(rid)
                counts["rows"] += 1
                # FK columns → edge to the referenced entity (created/deduped by value), honouring
                # the card's target type / edge type / direction overrides (incl. per-ROW polymorphic)
                _poly_rules = (_table_card(step1_cfg or {}, tbl["table"]) or {}).get("polymorphic") or []
                # Every row-entity type this run declares, so an FK into one of them lands on the real
                # row rather than on a value-keyed twin of it.
                _row_types = {str(c.get("node_type") or "").strip()
                              for c in ((step1_cfg or {}).get("tables") or [])
                              if str(c.get("node_type") or "").strip()}
                for c in fk_cols:
                    val = row.get(c)
                    if val in (None, ""):
                        continue
                    if await _fk_edge(workspace_id, rid, val, c, roles.get(c, {}),
                                      collection_id=collection_id, row=row, polymorphic=_poly_rules,
                                      row_types=_row_types, source_key=source_key):
                        counts["edges"] += 1
            ordinal += 1
        if _keys:
            # The source's own id for each row -> its node: how "customer 101" binds to the node,
            # and how the SQL planner turns a node back into the key it filters the source on.
            from opencortyx.core.dataplane import source_keys as _source_keys
            await _source_keys.record(workspace_id, table_id, _keys)
            _keys = []
        step += 1
        if reporter:
            await reporter.step(step, f"Rows {counts['rows']}", "completed", counts["rows"])
    # A re-uploaded file that lost rows: prune the rows it no longer has — but only when it was read
    # in FULL. A capped read (max_rows) saw a sample, and the rows past the cap are not "gone".
    if not truncated:
        try:
            stale = [n.id for n in await _repo.find(workspace_id, source_refs=[table_id], exact_types=[node_type])
                     if n.id not in seen_rows]
            if stale:
                counts["pruned_rows"] = await _repo.delete_nodes(workspace_id, stale)
        except Exception as e:                   # pruning must never break ingestion
            log.warning("structured.row_prune_skipped", table=tbl["table"], error=str(e))
    # `rows_available` vs `rows` makes sampling VISIBLE. Silently ingesting 5 of 3,000,000 rows and
    # reporting only "rows: 5" is how someone concludes the table was tiny.
    return {"status": "ok", "source_key": source_key, "source_id": str(table_id), "mode": mode,
            "node_type": node_type, "max_rows": max_rows or "all",
            "rows_available": tbl["row_count"], "rows_truncated": truncated, **counts}


def _note_duplicate_identity(counts: dict, value) -> None:
    """A row whose identity value an earlier row already had. It MERGES into that node — the id is
    derived from the value — so the table's row count and the graph's disagree. Counted with a few of
    the values, so the run can say so instead of the row silently disappearing."""
    counts["duplicate_ids"] = int(counts.get("duplicate_ids") or 0) + 1
    examples = counts.setdefault("duplicate_id_examples", [])
    if len(examples) < 5 and str(value) not in examples:
        examples.append(str(value))


def _parse_max_rows(value) -> int:
    """Step-1 `max_rows` → an integer cap, where 0 means NO cap.

    Accepts what a UI actually sends for "how many rows should become nodes?":
        None / "" / "all" / "full" / "*" / 0 / -1   → 0 (every row)
        5 / "5" / "5 rows" / "1_000" / "1,000"      → that many

    Unparseable input falls back to "all" rather than to some invented number: silently ingesting a
    different amount than the author asked for is worse than ingesting everything, which is at least
    what the field's absence has always meant.
    """
    if value is None:
        return 0
    if isinstance(value, bool):                      # True == "all", False == "all" (not zero rows)
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if not isinstance(value, str):
        # Anything else (a dict/list that leaked in from a malformed config) is not a row count.
        # Without this the digit scan below reads `{"weird": 1}` as "1 row" — a silent, absurd cap.
        return 0
    s = value.strip().lower()
    if s in ("", "all", "full", "*", "everything", "unlimited", "none"):
        return 0
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return 0
    return max(0, int(digits))


async def enrich_catalogue_native(workspace_id: uuid.UUID, collection_id=None, stages=None,
                                  source_key=None, stage_tables=None) -> dict:
    """Generate the derived business nodes from the v2 catalogue (Table/Attribute nodes) — no v1.

    `stages` (a set of builder action names) selects which LLM-heavy generators run — when None,
    everything runs (backward compatible). `source_key` scopes generation to ONE ingested database
    so computations don't try to join unrelated tables across sources. Lineage always runs.
    Idempotent (deterministic ids + de-duped edges)."""
    def _on(name: str) -> bool:
        return stages is None or name in stages

    def _table_ok(action: str, tname: str) -> bool:           # per-stage target-table scoping
        sel = (stage_tables or {}).get(action)
        return (not sel) or (tname in sel)
    counts = {k: 0 for k in ("segment", "computation", "data_quality", "lineage", "context_rule")}
    table_nodes = await _repo.find(workspace_id, types=["Table"], source_key=source_key)
    props = await _repo.find(workspace_id, exact_types=["Attribute"], source_key=source_key)
    if not table_nodes:
        return {"status": "no_table", **counts}

    cols_by_table = {}
    for p in props:
        cols_by_table.setdefault((p.properties or {}).get("parent_table_id"), []).append(p)

    await ensure_central_node(workspace_id)             # the workspace node org-wide nodes hang from

    def _tname(m):
        return (m.properties or {}).get("table_name") or m.label

    async def _run(key, name, node_ids=None):
        """Run one built-in extractor and fold its emitted-node count into this stage's counter."""
        if node_ids is not None and not node_ids:
            return
        try:
            r = await run_template_by_name(workspace_id, name, collection_id=collection_id,
                                           node_ids=list(node_ids) if node_ids else None)
            counts[key] += int(r.get("nodes") or 0)
        except Exception as e:                            # noqa: BLE001 — isolate per-generator
            log.warning("structured.enrich_failed", key=str(key), error=str(e))

    llm_cap = settings.STRUCTURED_MAX_LLM_TABLES          # cost guard on the LLM generators
    all_table_ids = [m.id for m in table_nodes]
    attr_ids_by_table = {k: [p.id for p in v] for k, v in cols_by_table.items()}

    def _scoped(action):
        return [m.id for m in table_nodes if _table_ok(action, _tname(m))]

    if _on("segments"):
        await _run("segment", "builtin:struct.segment", _scoped("segments")[:llm_cap])
    if _on("data_quality"):
        dq_props = [pid for m in table_nodes if _table_ok("data_quality", _tname(m))
                    for pid in attr_ids_by_table.get(str(m.id), [])]
        await _run("data_quality", "builtin:struct.data_quality", dq_props)
    await _run("lineage", "builtin:struct.lineage", all_table_ids)                    # always (rule)
    if _on("computations"):
        await _run("computation", "builtin:struct.computation", _scoped("computations")[:llm_cap])

    if _on("business_rules"):          # context rules are the "business rules" stage
        await _run("context_rule", "builtin:struct.context_rule", _scoped("business_rules")[:llm_cap])
    return {"status": "ok", **counts}


STRUCT_EXTRACTOR_ATTRS = {
    "business_semantics": [
        {"key": "description", "label": "Description", "default": True},
        {"key": "domain", "label": "Domain", "default": True},
        {"key": "synonyms", "label": "Synonyms", "default": True},
    ],
    "segments": [
        {"key": "description", "label": "Description", "default": True},
        {"key": "condition_logic", "label": "Condition (SQL)", "default": True},
        {"key": "definition_type", "label": "Definition Type", "default": True},
    ],
    "computations": [
        {"key": "description", "label": "Description", "default": True},
        {"key": "formula_type", "label": "Formula Type", "default": True},
        {"key": "formula_expression", "label": "Formula Expression", "default": True},
        {"key": "output_type", "label": "Output Type", "default": True},
        {"key": "unit", "label": "Unit", "default": True},
    ],
    "data_quality": [
        {"key": "severity", "label": "Severity", "default": True},
        {"key": "description", "label": "Description", "default": True},
        {"key": "null_pct", "label": "Null %", "default": True},
    ],
}


def _attr_allowed(cfg, kind=None):
    """Resolve which attribute keys a stage may emit → a set, or None for 'no filter (all on)'.
    `config.attributes` absent → all on (backward compatible); [] → all off. Works for built-in
    extractors AND custom templates (whose attributes are derived from their emit, not hardcoded):
    for a built-in `kind` we intersect with its known catalog defensively; for a custom template
    the enabled set is used as-is."""
    sel = (cfg or {}).get("attributes")
    if sel is None:
        return None                                  # all on
    allowed = set(sel)
    catalog = STRUCT_EXTRACTOR_ATTRS.get(kind) if kind else None
    if catalog:
        allowed &= {a["key"] for a in catalog}
    return allowed


async def seed_domain(workspace_id: uuid.UUID, domain: str, n_tables: int = 6, collection_id=None) -> dict:
    """Bootstrap a full catalogue for a business domain (e.g. 'E-commerce', 'Healthcare') with an
    LLM — tables, columns, FKs, synonyms — then generate all derived nodes + absence markers. Lets
    anyone populate every node/edge type from just a domain, with no database connected."""
    prompt = (
        f"Design a realistic relational schema for the business domain '{domain}'. "
        f"Produce {n_tables} core tables with columns and foreign keys.\n"
        'Return ONLY JSON: {"tables":[{"name":"snake_case_table","description":"...","synonyms":["..."],'
        '"columns":[{"name":"col","type":"TEXT|INTEGER|DECIMAL|DATE|BOOLEAN","primary_key":false,'
        '"references_table":null,"references_column":null}]}]}')
    try:
        from opencortyx.core.inference.registry import resolve_model
        raw = await complete(prompt, system="You are a senior data architect. JSON only.",
                             max_tokens=3000, json_mode=True, temperature=0.3,
                             model_spec=await resolve_model(workspace_id, "generation"))
    except Exception as e:
        return {"status": "error", "error": str(e)}
    data = _parse_json(raw) or {}
    tables = data.get("tables") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    tables = [t for t in (tables or []) if isinstance(t, dict) and t.get("name")]
    if not tables:
        return {"status": "no_tables"}

    src_key = (domain or "domain").lower().replace(" ", "_")
    counts = {"tables": 0, "columns": 0, "joins": 0}
    table_ids = {t["name"]: _det_id(workspace_id, "mso", f"{src_key}.{t['name']}") for t in tables}
    await ensure_central_node(workspace_id)
    for t in tables:
        mid = table_ids[t["name"]]
        cols = [c for c in (t.get("columns") or []) if isinstance(c, dict) and c.get("name")]
        pk = next((c["name"] for c in cols if c.get("primary_key")), "")
        await _put(node_id=mid, workspace_id=workspace_id, type="Table", label=t["name"],
                   properties={"table_name": t["name"], "description": _s(t.get("description")),
                               "domain": domain, "primary_key_field": pk,
                               "synonyms": t.get("synonyms") or [], "source_system": "domain_template",
                               "column_count": len(cols)},
                   source_ref=workspace_id, collection_id=collection_id)
        counts["tables"] += 1
        for c in cols:
            cid = _det_id(workspace_id, "prop", f"{src_key}.{t['name']}.{c['name']}")
            await _put(node_id=cid, workspace_id=workspace_id, type="Attribute", label=c["name"],
                       properties={"physical_column": c["name"], "physical_table": t["name"],
                                   "data_type": c.get("type", ""), "parent_table_id": str(mid),
                                   "is_primary_key": bool(c.get("primary_key")),
                                   "is_foreign_key": bool(c.get("references_table")),
                                   "references_table": c.get("references_table"),
                                   "references_column": c.get("references_column")},
                       source_ref=mid, collection_id=collection_id)
            await _uedge(workspace_id=workspace_id, source_id=mid, target_id=cid, type="HAS_ATTRIBUTE")
            counts["columns"] += 1
    for t in tables:                       # FK edges once all tables exist
        for c in (t.get("columns") or []):
            rt = c.get("references_table") if isinstance(c, dict) else None
            if rt and rt in table_ids and t["name"] in table_ids:
                jp = {"from_key": c.get("name"), "to_key": c.get("references_column"), "cardinality": "MANY_TO_ONE"}
                if await _uedge(workspace_id=workspace_id, source_id=table_ids[t["name"]], target_id=table_ids[rt], type="JOINS_TO", properties=jp):
                    counts["joins"] += 1
                await _uedge(workspace_id=workspace_id, source_id=table_ids[t["name"]], target_id=table_ids[rt], type="BELONGS_TO", properties=jp)
                await _uedge(workspace_id=workspace_id, source_id=table_ids[rt], target_id=table_ids[t["name"]], type="HAS_MANY", properties={"cardinality": "ONE_TO_MANY"})
    try:
        counts["enrich"] = await enrich_catalogue_native(workspace_id, collection_id)
    except Exception as e:
        counts["enrich"] = {"status": "error", "error": str(e)}
    return {"status": "ok", "domain": domain, **counts}


_KNOWN_STRUCTURED_STAGES = {"business_semantics", "segments", "computations",
                            "data_quality", "business_rules"}


def _map_builtin_to_stage(name: str):
    """Route a built-in structured catalog name (e.g. 'builtin:struct.segment') to the generator
    stage that produces it, or None when it has no dedicated stage — the caller then runs it as an
    ordinary template, so nothing is a silent no-op either way.

    Shares `_STRUCT_FIXED_STAGES` with the DAG router deliberately: two routing tables for the same
    question is how a stage ends up meaning one thing when a pipeline is parsed and another when it
    runs. Only the four GENERATOR stages are relevant here — introspection is base_schema's job."""
    kind = _STRUCT_FIXED_STAGES.get(name)
    return kind if kind in ("segments", "computations", "data_quality", "business_rules",
                            "business_semantics") else None


def _parse_structured_plan(pipeline) -> dict:
    """Parse the Structured Pipeline Builder stages into
    {target_tables, stages, custom_mappings, templates}.

    An `action` that isn't a fixed stage / base_schema / custom_mappings is treated as a declarative
    TEMPLATE NAME (chosen from the dropdown) → run over the structured nodes after ingestion."""
    base_tables, stages, templates = set(), set(), []
    stage_tables: dict = {}                # per-stage table scoping: {action: set(tables)}
    custom = {"nodes": [], "edges": []}
    for st in (pipeline or []):
        if not isinstance(st, dict):
            continue
        action, cfg = st.get("action"), (st.get("config") or {})
        if action == "base_schema":
            for t in (cfg.get("target_tables") or []):
                base_tables.add(t)
        elif action == "custom_mappings":
            custom["nodes"].extend(cfg.get("nodes") or [])
            custom["edges"].extend(cfg.get("edges") or [])
        elif action in _KNOWN_STRUCTURED_STAGES:
            stages.add(action)
            tt = cfg.get("target_tables") or []
            if tt:
                stage_tables[action] = set(tt)
        elif action and action.startswith("builtin:"):
            mapped = _map_builtin_to_stage(action)
            if mapped:
                stages.add(mapped)
                tt = cfg.get("target_tables") or []
                if tt:
                    stage_tables[mapped] = set(tt)
            else:
                templates.append(action)
        elif action:
            templates.append(action)        # a genuine custom (user) template name
    return {"target_tables": base_tables, "stages": stages, "stage_tables": stage_tables,
            "custom_mappings": custom, "templates": templates}


def _parse_col(c):
    """'schema.table.col' | 'table.col' | 'col' → (table|None, col|None)."""
    parts = [p for p in str(c or "").split(".") if p]
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, (parts[0] if parts else None)


def _sync_fetch_rows(dsn: str, schema, table: str, cols: list, limit: int = 1000) -> list:
    _d = _sync_dsn(dsn)
    eng = (create_engine(_d, connect_args={"connect_timeout": 5})
           if ("postgresql" in _d or "mysql" in _d) else create_engine(_d))
    out = []
    try:
        q = "`" if "mysql" in dsn else '"'      # dialect-aware identifier quoting
        tbl_ident = f"{q}{schema}{q}.{q}{table}{q}" if (schema and schema != "main") else f"{q}{table}{q}"
        collist = ", ".join(f"{q}{c}{q}" for c in cols) if cols else "*"
        with eng.connect() as conn:
            res = conn.execute(text(f"SELECT {collist} FROM {tbl_ident} LIMIT {int(limit)}"))
            keys = list(res.keys())
            for r in res:
                out.append({keys[i]: r[i] for i in range(len(keys))})
    finally:
        eng.dispose()
    return out


async def apply_custom_mappings(workspace_id: uuid.UUID, dsn: str, schema, mappings: dict,
                                collection_id=None, row_limit: int = None) -> dict:
    row_limit = row_limit or settings.STRUCTURED_MAPPING_ROW_LIMIT
    """Create nodes/edges from explicit column→attribute mappings. Same-table node pairs are
    linked per row; cross-table edges are skipped (need a join key). Deterministic + idempotent."""
    import anyio
    node_specs = (mappings or {}).get("nodes") or []
    edge_specs = (mappings or {}).get("edges") or []
    counts = {"nodes": 0, "edges": 0, "skipped": 0}
    if not node_specs:
        return counts

    # plan each node mapping → its table + [(property, column)]
    node_plans, table_cols = [], {}
    for nm in node_specs:
        ntype = (nm.get("nodeType") or "").strip()
        table = None
        pairs = []
        for a in (nm.get("attributes") or []):
            t, col = _parse_col(a.get("column"))
            if not col:
                continue
            table = table or t
            pairs.append((a.get("property") or col, col))
        node_plans.append({"type": ntype, "table": table, "pairs": pairs})
        if table:
            table_cols.setdefault(table, set()).update(col for _p, col in pairs)

    for _es in edge_specs:
        for _ref, _key in ((_es.get("sourceRef"), "joinFrom"), (_es.get("targetRef"), "joinTo")):
            try:
                _plan = node_plans[int(_ref)]
            except (TypeError, ValueError, IndexError):
                continue
            _col = str(_es.get(_key) or "").strip()
            if _col and _plan.get("table"):
                table_cols.setdefault(_plan["table"], set()).add(_col)

    # fetch each needed table once (in a worker thread)
    rows_by_table = {}
    for table, cols in table_cols.items():
        if not table or not cols:
            continue
        try:
            rows_by_table[table] = await anyio.to_thread.run_sync(
                lambda t=table, c=list(cols): _sync_fetch_rows(dsn, schema, t, c, row_limit))
        except Exception as e:
            log.warning("structured.mapping_fetch_failed", table=str(table), error=str(e))
            rows_by_table[table] = []

    ids_by_node = {}    # node_index → (table, [node_id per row])
    for idx, plan in enumerate(node_plans):
        table, ntype = plan["table"], plan["type"]
        if not (table and ntype) or table not in rows_by_table:
            continue
        ids = []
        for row in rows_by_table[table]:
            props = {prop: _jsonable(row.get(col)) for prop, col in plan["pairs"]}
            label = next((str(v) for v in props.values() if isinstance(v, str) and v.strip()), ntype)[:200]
            key = "|".join(f"{p}={props.get(p)}" for p, _c in plan["pairs"])
            nid = _det_id(workspace_id, "custom", f"{ntype}:{table}:{key}")
            await _put(node_id=nid, workspace_id=workspace_id, type=ntype, label=label,
                       properties={**props, "__source": "custom_mapping", "source_table": table},
                       collection_id=collection_id)
            ids.append(nid)
            counts["nodes"] += 1
        ids_by_node[idx] = (table, ids, rows_by_table[table])

    for es in edge_specs:
        try:
            si, ti = int(es.get("sourceRef")), int(es.get("targetRef"))
        except Exception:
            counts["skipped"] += 1
            continue
        etype = (es.get("edgeType") or "RELATES_TO").strip() or "RELATES_TO"
        src, tgt = ids_by_node.get(si), ids_by_node.get(ti)
        if not src or not tgt:
            counts["skipped"] += 1
            continue
        if src[0] == tgt[0]:                          # same table → the row IS the pairing
            for sid, tid in zip(src[1], tgt[1]):
                if await _uedge(workspace_id=workspace_id, source_id=sid, target_id=tid, type=etype):
                    counts["edges"] += 1
            continue
        jf = str(es.get("joinFrom") or "").strip()
        jt = str(es.get("joinTo") or jf).strip()
        if not jf:
            counts["skipped"] += 1
            continue
        by_value = {}
        for tid, trow in zip(tgt[1], tgt[2]):
            tv = trow.get(jt)
            if tv is not None:
                by_value.setdefault(str(tv), tid)     # first row wins, as a key should be unique
        if not by_value:
            counts["skipped"] += 1
            continue
        for sid, srow in zip(src[1], src[2]):
            sv = srow.get(jf)
            tid = by_value.get(str(sv)) if sv is not None else None
            if not tid:
                counts["skipped"] += 1
                continue
            if await _uedge(workspace_id=workspace_id, source_id=sid, target_id=tid, type=etype):
                counts["edges"] += 1
    return counts


def resolve_bound_mapping_ids(cfg: dict) -> list:
    """The structured schema mappings a stage config binds. `mapping_ids` is the multi-binding form (a
    pipeline may consume several one-table mappings); `mapping_id` is the older single form, still read
    so pipelines saved before multi-binding keep working."""
    ids = [str(x) for x in (cfg or {}).get("mapping_ids") or [] if x]
    single = (cfg or {}).get("mapping_id")
    if single and str(single) not in ids:
        ids.append(str(single))
    return ids


async def load_mapping_custom(workspace_id, mapping_ids: list) -> dict:
    """The `{nodes, edges}` a bound mapping declares beyond its row shape — junction (many-to-many)
    associations and any explicit node/edge specs.

    `translate_structured_editor` has always written this to `payload.table_mappings`, and NOTHING read
    it: `apply_custom_mappings` only ran from an explicit `custom_mappings` STAGE carrying its own
    inline config. So a mapping's junction rules were saved, drawn, and never built. Re-derived from the
    live `editor` for the same reason the semi-structured runner does (a compiled copy is frozen at
    whatever the translator understood when it was saved).
    """
    import uuid as _uuid
    from opencortyx.ingest.structured.translate import translate_structured_editor
    nodes: list = []
    edges: list = []
    for mid in mapping_ids or []:
        try:
            async with V2Session() as db:
                m = await db.get(SchemaMapping, _uuid.UUID(str(mid)))
            if m is None or m.workspace_id != workspace_id:
                continue
            payload = with_editor(m.paradigm, m.payload or {})
            ed = payload.get("editor")
            from opencortyx.ingest.structured.translate import (
                is_key_row_editor, mapping_declares_entities)
            if ed and is_key_row_editor(ed) and mapping_declares_entities(ed):
                continue
            tm = (translate_structured_editor(ed, m.name).get("table_mappings")
                  if ed else payload.get("table_mappings")) or {}
            base = len(nodes)                       # edge refs are INDICES — rebase when merging
            nodes.extend(tm.get("nodes") or [])
            for e in tm.get("edges") or []:
                e = dict(e)
                e["sourceRef"] = int(e.get("sourceRef", 0)) + base
                e["targetRef"] = int(e.get("targetRef", 0)) + base
                edges.append(e)
        except Exception as e:                      # noqa: BLE001 — one bad ref must not sink the run
            log.warning("structured.custom_mappings_failed", mapping_id=str(mid), error=str(e))
    return {"nodes": nodes, "edges": edges}


async def load_mapping_step1(workspace_id, mapping_ids: list) -> dict:
    """Everything a bound structured mapping contributes to Step-1: the per-table cards AND the
    READING MODE they imply.

    The mode is the half that used to be missing. Cards were regenerated from the live mapping while
    the mode still came from the pipeline's Step-1 card (default 'catalog'), so a mapping that declared
    entities produced a catalogue instead — the table's schema, not the graph its author drew. A mapping
    that names a row node label or gives a column a pk/fk/node role is declaring entities, and this
    reports that so the run can read rows. See `mapping_declares_entities`.

    One card per mapping (each structured mapping covers one table); several mappings compose into a
    multi-table run. A bad/foreign reference is skipped, never fatal."""
    import uuid as _uuid
    from opencortyx.ingest.structured.translate import (
        editor_to_table_cards, mapping_declares_entities)
    cards: list = []
    seen: set = set()
    entities = False
    for mid in mapping_ids or []:
        try:
            async with V2Session() as db:
                m = await db.get(SchemaMapping, _uuid.UUID(str(mid)))
            # A mapping saved as compiled `table_mappings` alone (an Add data draft) has no editor
            # until one is derived; the API derives it for display, so ingestion must too.
            ed = (with_editor(m.paradigm, m.payload or {}).get("editor")
                  if (m is not None and m.workspace_id == workspace_id) else None)
            if not ed:
                continue
            entities = entities or mapping_declares_entities(ed)
            for card in editor_to_table_cards(ed):
                if card.get("table") in seen:      # two mappings for the same table: first wins
                    continue
                seen.add(card.get("table"))
                cards.append(card)
        except Exception as e:                     # noqa: BLE001 — one bad ref must not sink the run
            log.warning("structured.mapping_resolve_failed", mapping_id=str(mid), error=str(e))
    return {"tables": cards, "mode": "row" if entities else ""}


async def load_mapping_table_cards(workspace_id, mapping_ids: list) -> list:
    """The cards alone — for callers that only need the table/column vocabulary (verification,
    sync, scheduling). Ingestion wants the mode too and calls `load_mapping_step1`."""
    return (await load_mapping_step1(workspace_id, mapping_ids))["tables"]


_STRUCT_FIXED_STAGES = {
    "builtin:struct.business_semantics": "business_semantics",
    "builtin:struct.column_semantics": "business_semantics",
    "builtin:struct.segment": "segments",
    "builtin:struct.computation": "computations",
    "builtin:struct.data_quality": "data_quality",
    "builtin:struct.context_rule": "business_rules",
}


def _struct_stage_kind(action):
    a = (action or "").lower()
    if not a:
        return None
    if action in _STRUCT_FIXED_STAGES:
        return _STRUCT_FIXED_STAGES[action]
    if action == "base_schema" or a == "introspect":
        return "introspect"
    if action == "custom_mappings":
        return "custom_mappings"
    if a in _KNOWN_STRUCTURED_STAGES:            # the structured builder's generator stages
        return a
    return "template"


def _struct_stage_label(action) -> str:
    """Human-friendly name for a structured stage (for the Progress tab)."""
    k = _struct_stage_kind(action)
    return {"business_semantics": "Business Semantics", "segments": "Segments",
            "computations": "Computations", "data_quality": "Data Quality",
            "business_rules": "Business Rules", "custom_mappings": "Granular Mapping",
            }.get(k, action or "Stage")


async def _load_tables_cols(workspace_id, table_ids):
    from opencortyx.stores.graph import NodeQuery
    from opencortyx.stores.graph.query import select_nodes
    table_nodes = select_nodes(await _repo.get_many(workspace_id, table_ids), NodeQuery(types=["Table"]))
    cols_by_table = {}
    for col in await _repo.find(workspace_id, source_refs=[m.id for m in table_nodes], exact_types=["Attribute"]):
        cols_by_table.setdefault((col.properties or {}).get("parent_table_id"), []).append(col)
    return table_nodes, cols_by_table


async def _task_business_rules(workspace_id, in_ids, collection_id) -> int:
    """Context rules over the given tables — the built-in extractor behind "business rules"."""
    if not in_ids:
        return 0
    try:
        r = await run_template_by_name(workspace_id, "builtin:struct.context_rule", collection_id,
                                       node_ids=list(in_ids))
        return int((r or {}).get("nodes") or 0)
    except Exception as e:                                # noqa: BLE001 — isolate the stage
        log.warning("structured.dag_business_rules_failed", error=str(e))
        return 0


def _node_pairs(nodes):
    """(node_id, text) pairs for chaining — text = best available content of each node."""
    out = []
    for n in nodes:
        p = n.properties or {}
        out.append((n.id, p.get("text") or p.get("description") or p.get("name") or n.label or ""))
    return out


async def ingest_lake_source(workspace_id: uuid.UUID, connector_id, collection_id=None,
                             only_tables=None) -> dict:
    """A Delta / Iceberg / Hudi connector's tables: catalogued like a database's (Table + Attribute
    nodes, the same shape the SQL introspector gives), and - with the workspace's data in Delta -
    registered as external assets read where they are (ingest/structured/dataplane.py)."""
    from opencortyx.domain.control import Connector
    from opencortyx.ingest.sources import datalake
    async with V2Session() as db:
        c = await db.get(Connector, uuid.UUID(str(connector_id)))
        if not c or c.workspace_id != workspace_id:
            raise RuntimeError("connector not found")
        cfg = c.config or {}
        fmt, directory = (cfg.get("kind") or "delta").lower(), cfg.get("directory") or ""
        storage_cfg = {"kind": "local"}
        if cfg.get("lake_connector_id"):
            lc = await db.get(Connector, uuid.UUID(cfg["lake_connector_id"]))
            if not lc or lc.workspace_id != workspace_id:
                raise RuntimeError("referenced data-lake connector not found")
            storage_cfg = lc.config or {}
    directory = datalake.translate_local_path(directory) if storage_cfg.get("kind", "local") == "local" else directory
    tables = await anyio.to_thread.run_sync(
        lambda: datalake.introspect(storage_cfg, fmt, directory, only_tables=only_tables))
    counts = {"tables": 0, "columns": 0}
    source_key = f"lake:{fmt}:{directory}"
    delta_mode = await _dataplane.uses_delta(workspace_id)
    for tbl in tables:
        if tbl.get("error"):
            continue
        table_id = _det_id(workspace_id, "mso", f"{source_key}.{tbl['table']}")
        version = None
        if delta_mode:
            if fmt == "delta" and os.path.isdir(str(tbl.get("path") or "")):
                from opencortyx.stores.data import delta as _delta
                version = _delta.version(tbl["path"])
            await _dataplane.register_lake_table(
                workspace_id, table_id, connector_id=connector_id, fmt=fmt, location=tbl.get("path") or "",
                name=tbl["table"], columns=tbl["columns"], source_key=source_key,
                version_ref=None if version is None else str(version))
        await _put(node_id=table_id, workspace_id=workspace_id, type="Table", label=tbl["table"],
                   properties={"table_name": tbl["table"], "row_count": tbl.get("row_count"),
                               "column_count": len(tbl["columns"]), "source_system": fmt,
                               "source_key": source_key, "native": True,
                               **({"storage": "external", "asset_kind": "structured",
                                   "version_ref": version} if delta_mode else {})},
                   source_ref=None, collection_id=collection_id)
        counts["tables"] += 1
        for col in tbl["columns"]:
            cid = _det_id(workspace_id, "prop", f"{source_key}.{tbl['table']}.{col['name']}")
            await _put(node_id=cid, workspace_id=workspace_id, type="Attribute", label=col["name"],
                       properties={"physical_column": col["name"], "physical_table": tbl["table"],
                                   "data_type": col["type"], "parent_table_id": str(table_id),
                                   "source_key": source_key, "native": True},
                       source_ref=table_id, collection_id=collection_id)
            await _uedge(workspace_id=workspace_id, source_id=table_id, target_id=cid, type="HAS_ATTRIBUTE")
            counts["columns"] += 1
    return {"status": "ok", "source_key": source_key, "format": fmt, **counts}


async def run_structured_dag(workspace_id, dsn, schema, stages, collection_id=None, reporter=None,
                             prune: bool = True, connector_id=None) -> dict:
    """Execute the structured pipeline as an ordered DAG of TASKS. `introspect` (base_schema) makes
    the Table/Attribute base; each later task runs over its Input Source — a prior step's output nodes
    or the base tables. Structured generators (segments/computations/semantics/business_rules) and
    any other extractor emit their nodes. Per-stage isolation — one failure never aborts.

    `reporter` threads live progress (introspect → structural graph → each runnable stage) and lets a
    delete cooperatively cancel the run between stages."""
    base_tables = set()
    base_cfg: dict = {}
    for s in stages:
        if isinstance(s, dict) and s.get("action") == "base_schema":
            cfg = s.get("config") or {}
            base_cfg = cfg
            base_tables.update(cfg.get("target_tables") or [])
    _bound_ids = resolve_bound_mapping_ids(base_cfg)
    _mapping_mode = ""
    if _bound_ids:
        _bound = await load_mapping_step1(workspace_id, _bound_ids)
        cards, _mapping_mode = _bound["tables"], _bound["mode"]
        if cards:
            base_cfg = {**base_cfg, "tables": cards,
                        "row_node_type": cards[0]["node_type"] if len(cards) == 1 else "",
                        "columns": cards[0]["columns"] if len(cards) == 1 else []}
            base_tables.update(c["table"] for c in cards if c.get("table"))
    reading_mode = (base_cfg.get("mode") or _mapping_mode or "catalog").strip()
    # When mode="auto", the actual sub-mode (catalog|row) is in auto_mode; passed through to ingest.
    row_node_type = (base_cfg.get("row_node_type") or "").strip() or None
    row_cols = base_cfg.get("columns") or []

    has_sem_stage = any(isinstance(s, dict) and _struct_stage_kind(s.get("action")) == "business_semantics"
                        for s in stages)

    if reporter is not None:
        runnable = sum(1 for s in stages if isinstance(s, dict)
                       and _struct_stage_kind(s.get("action")) not in (None, "introspect"))
        await reporter.start(2 + runnable)              # introspect (+ enrich) + structural graph + stages
        await reporter.step(1, "Introspect schema", "running")

    ing = await ingest_sql_source(workspace_id, dsn, schema, collection_id, enrich=(not has_sem_stage),
                                  only_tables=(base_tables or None), prune=prune,
                                  reading_mode=reading_mode, row_node_type=row_node_type, row_cols=row_cols,
                                  step1_cfg=base_cfg,   # per-table cards + pattern rules + id patterns
                                  connector_id=connector_id)
    src_key = ing.get("source_key")
    totals = {"introspect": ing}

    # A bound mapping's own node/edge specs — junction (many-to-many) associations above all — run here
    # rather than waiting for an explicit `custom_mappings` stage nobody adds. See load_mapping_custom.
    if _bound_ids:
        _custom = await load_mapping_custom(workspace_id, _bound_ids)
        if _custom.get("edges"):
            try:
                totals["mapping_associations"] = await apply_custom_mappings(
                    workspace_id, dsn, schema, _custom, collection_id)
            except Exception as e:                  # noqa: BLE001 — associations must not sink the run
                log.warning("structured.bound_mapping_associations_failed", error=str(e))

    base_nodes = await _repo.find(workspace_id, types=["Table"], source_key=src_key)
    base_pairs = _node_pairs(base_nodes)
    base_ids = [i for i, _ in base_pairs]

    if reporter is not None:
        await reporter.set_source(src_key)
        await reporter.step(1, "Introspect schema", "completed", len(base_pairs))
        await reporter.step(2, "Structural graph", "running")

    outputs = {"document": base_pairs}
    for s in stages:
        if isinstance(s, dict) and s.get("action") == "base_schema":
            outputs[s.get("id")] = base_pairs

    try:
        await ensure_central_node(workspace_id)
        async def _always(label, name, node_ids=None):
            try:
                await run_template_by_name(workspace_id, name, collection_id, node_ids=node_ids)
            except Exception as e:                        # noqa: BLE001 — one generator never stops the rest
                log.warning("structured.dag_stage_failed", stage=str(label), error=str(e))

        if base_ids:
            await _always("lineage", "builtin:struct.lineage", base_ids)
        if has_sem_stage and base_ids:
            prop_ids = [e.target_id for e in await _repo.edges(workspace_id, source_ids=base_ids, types=["HAS_ATTRIBUTE"])]
            if prop_ids:
                try:
                    await run_template_by_name(workspace_id, "builtin:struct.column_semantics",
                                               collection_id, node_ids=prop_ids)
                except Exception as e:
                    log.warning("structured.dag_stage_failed", stage="column_semantics", error=str(e))
    except Exception as e:
        log.warning("structured.dag_stage_failed", stage="always_on", error=str(e))
    if reporter is not None:
        await reporter.step(2, "Structural graph", "completed", len(base_pairs))

    idx = 2
    for s in stages:
        if not isinstance(s, dict):
            continue
        action = s.get("action")
        kind = _struct_stage_kind(action)
        if kind in (None, "introspect"):
            continue
        idx += 1
        if reporter is not None and await reporter.is_canceled():
            break                                       # cooperative cancel at stage boundary
        slabel = _struct_stage_label(action)
        if reporter is not None:
            await reporter.step(idx, slabel, "running")
        cfg = s.get("config") or {}
        in_pairs = outputs.get(s.get("source"), base_pairs)
        if cfg.get("input_nodes"):
            from opencortyx.ingest.unstructured.ingest import _pairs_for_input_nodes
            picked = await _pairs_for_input_nodes(workspace_id, base_ids, cfg["input_nodes"])
            if picked:
                in_pairs = picked
        _in_fields = cfg.get("input_fields")             # cross-step attribute wiring (same as docs)
        if _in_fields is None:
            _in_fields = cfg.get("input_field")
        if _in_fields:                                   # remap the text of text-consuming stages
            from opencortyx.ingest.unstructured.ingest import _remap_pairs_to_field
            in_pairs = await _remap_pairs_to_field(workspace_id, in_pairs, _in_fields)
        from opencortyx.ingest.unstructured.ingest import _apply_stage_filters
        in_pairs = await _apply_stage_filters(workspace_id, in_pairs, cfg)
        in_ids = [i for i, _ in in_pairs]
        sid = s.get("id")
        sel = set(cfg.get("target_tables") or [])
        try:
            allowed = _attr_allowed(cfg, kind)              # per-stage attribute enable/disable
            if kind in ("segments", "data_quality", "computations"):
                tname, out_edge = {
                    "segments": ("builtin:struct.segment", "HAS_SEGMENT"),
                    "data_quality": ("builtin:struct.data_quality", "HAS_QUALITY_ISSUE"),
                    "computations": ("builtin:struct.computation", "HAS_METRIC"),
                }[kind]
                mlist, cols_by_table = await _load_tables_cols(workspace_id, in_ids)
                if sel:
                    mlist = [m for m in mlist
                             if ((m.properties or {}).get("table_name") or m.label) in sel]
                table_ids = [m.id for m in mlist]
                # Data quality is a property of a COLUMN (the null rate lives there), so the
                # stage's tables are expanded to their columns before the extractor runs.
                run_ids = ([p.id for m in mlist for p in cols_by_table.get(str(m.id), [])]
                           if kind == "data_quality" else table_ids)
                r = await run_template_by_name(workspace_id, tname, collection_id,
                                               node_ids=run_ids, attrs=allowed) if run_ids else {}
                totals[kind] = int((r or {}).get("nodes") or 0)
                # Downstream chaining follows the extractor's OWN emit edge, not a property
                # convention — so a stage can chain off whatever the extractor actually produced.
                oids = list({e.target_id for e in await _repo.edges(workspace_id, source_ids=table_ids, types=[out_edge])}) \
                    if table_ids else []
                outn = await _repo.get_many(workspace_id, oids)
                outputs[sid] = _node_pairs(outn)
            elif kind == "business_semantics":
                totals["business_semantics"] = await run_template_by_name(
                    workspace_id, "builtin:struct.business_semantics", collection_id, node_ids=in_ids,
                    attrs=allowed)
                outputs[sid] = in_pairs               # enriched tables flow downstream
            elif kind == "business_rules":
                totals["business_rules"] = await _task_business_rules(workspace_id, in_ids, collection_id)
                outputs[sid] = []
            elif kind == "custom_mappings":
                totals["custom_mappings"] = await apply_custom_mappings(
                    workspace_id, dsn, schema,
                    {"nodes": cfg.get("nodes") or [], "edges": cfg.get("edges") or []}, collection_id)
            elif kind == "template":
                ran = await run_template_by_name(workspace_id, action, collection_id, node_ids=in_ids,
                                                 attrs=_attr_allowed(cfg))   # custom extractor attrs
                outputs[sid] = []
                if (ran or {}).get("status") in ("not_found", "skipped"):
                    # the pipeline outlived this extractor, or it is switched off — said so, not
                    # reported as a run that found nothing
                    why = ("extractor no longer exists" if ran.get("status") == "not_found"
                           else "extractor is turned off")
                    totals.setdefault("skipped_stages", []).append(action)
                    if reporter is not None:
                        await reporter.step(idx, f"{slabel} — {why}", "skipped")
                    continue
                totals.setdefault("templates", []).append(ran)
            if reporter is not None:
                c = totals.get(kind)
                await reporter.step(idx, slabel, "completed", c if isinstance(c, int) else None)
        except Exception as e:                      # one stage never aborts the pipeline
            totals.setdefault("errors", []).append(f"{action}: {e}")
            if reporter is not None:
                await reporter.step(idx, slabel, "failed")
            log.warning("structured.dag_stage_failed", stage=str(action), error=str(e))
    return totals


def structured_step1(pipeline) -> dict:
    """Pull the structured Step-1 reading config out of a structured pipeline's stages: {mode,
    auto_mode, row_node_type, columns} plus the richer keys — tables (per-table cards: node_type +
    per-column role/ref/target_type/edge_type/edge_dir/attr_name), rules (pattern rules),
    id_patterns, the bound schema-mapping refs, and the two Step-1 options (`auto_accept`,
    `extract_only`). Any stage whose config carries one of these is it. A stage without a mode reads
    with the configured default, and "Extract only" always reads the schema (catalog). Default to
    catalog mode when no stage is present."""
    from opencortyx.core.config import runtime as rc
    from opencortyx.ingest.pipelines import signature as _sig
    keys = ("mode", "columns", "row_node_type", "tables", "rules", "id_patterns",
            "mapping_ids", "mapping_id", "auto_accept", "extract_only")
    for s in (pipeline or []):
        c = (s.get("config") if isinstance(s, dict) else None) or {}
        if any(k in c for k in keys):
            out = {"mode": c.get("mode") or rc.structured_default_mode(),
                   "row_node_type": c.get("row_node_type"),
                   "columns": c.get("columns") or [], "tables": c.get("tables") or [],
                   "rules": c.get("rules") or [], "id_patterns": c.get("id_patterns") or []}
            # auto_mode picks catalog-vs-row under mode="auto"; the mapping refs are what a bound
            # schema mapping rides on. Dropping either made a bound mapping a silent no-op here.
            for k in ("auto_mode", "mapping_ids", "mapping_id", "auto_accept", "extract_only"):
                if c.get(k) is not None:
                    out[k] = c[k]
            if _sig.extract_only_on(c):
                out["mode"] = "catalog"
            return out
    return {"mode": "catalog"}


async def resolve_structured_step1(workspace_id, pipeline) -> dict:
    """`structured_step1` + BY-REFERENCE schema mappings: bound structured mapping(s) regenerate
    their per-table cards at ingest time, so the live mapping drives the graph (same contract as the
    uploaded-file and DBMS paths)."""
    step1 = structured_step1(pipeline)
    bound = resolve_bound_mapping_ids(step1)
    if not bound:
        return step1
    try:
        cards = await load_mapping_table_cards(
            workspace_id if isinstance(workspace_id, uuid.UUID) else uuid.UUID(str(workspace_id)), bound)
    except Exception as e:                             # noqa: BLE001 — never sink the file on this
        log.warning("structured.mapping_cards_load_failed", bound=str(bound), error=str(e))
        return step1
    if not cards:
        return step1
    return {**step1, "tables": cards, "mode": "auto", "auto_mode": "row",
            "row_node_type": cards[0]["node_type"] if len(cards) == 1 else "",
            "columns": cards[0]["columns"] if len(cards) == 1 else []}
