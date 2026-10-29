"""Checks inside a running stack, after the 300-file corpus was loaded (tests/goldset/e2e.py).

    python -m tests.goldset.stack_checks <workspace_id> /tmp/expected.json

Run in the API container: it reads the stores directly - the asset registry, the Delta tables, the
evidence ledger, the graph and the search index - and compares them with what the files hold.
Prints one line per check and a JSON summary; exits 1 when any check failed.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


async def main(ws: uuid.UUID, exp: dict) -> int:
    from sqlalchemy import select, text

    from opencortyx.core.dataplane import mode
    from opencortyx.core.db import V2Session
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.domain.data import DataAsset
    from opencortyx.stores.data import SEMISTRUCTURED, STRUCTURED, UNSTRUCTURED, DataStore
    from opencortyx.stores.vector import VectorFilter, get_vector_store

    repo, store = NodeRepo(), DataStore(ws)
    check("workspace on the data store", await mode.data_store(ws) == "delta", await mode.data_store(ws))

    async with V2Session() as db:
        assets = (await db.execute(select(DataAsset).where(DataAsset.workspace_id == ws))).scalars().all()
        failed_runs = (await db.execute(text(
            "SELECT asset, coalesce(error, '') FROM opencortyx_v2.ingest_run WHERE workspace_id = :w "
            "AND status = 'failed'"), {"w": ws})).all()
    by_kind: dict = {}
    for a in assets:
        by_kind.setdefault(a.kind, []).append(a)
    check("no failed ingest runs", not failed_runs, "; ".join(f"{a}: {e[:80]}" for a, e in failed_runs[:5]))
    for kind, n in exp["counts"].items():
        got = len(by_kind.get(kind, []))
        check(f"{kind} assets registered", got >= n, f"{got} registered for {n} files")

    # structured: every row of every file in its Delta table
    bad = []
    for a in by_kind.get(STRUCTURED, []):
        fname = (a.metadata_ or {}).get("filename")
        want = exp["rows"].get(fname)
        if want is None:
            continue
        t = store.read(STRUCTURED, a.id, columns=["_row_key"])
        got = t.num_rows if t is not None else 0
        if got != want:
            bad.append(f"{fname}: {got}/{want}")
    check("structured rows in Delta = rows in the files", not bad, "; ".join(bad[:5]))

    # semi-structured: one record per top-level record
    bad = []
    for a in by_kind.get(SEMISTRUCTURED, []):
        want = exp["records"].get(a.name)
        if want is None:
            continue
        t = store.read(SEMISTRUCTURED, a.id, columns=["record_id"])
        got = t.num_rows if t is not None else 0
        if got != want:
            bad.append(f"{a.name}: {got}/{want}")
    check("semi-structured records in Delta = records in the files", not bad, "; ".join(bad[:5]))

    # documents: chunks in Delta, none in the graph
    empty, in_graph = [], 0
    for a in by_kind.get(UNSTRUCTURED, []):
        t = store.read(UNSTRUCTURED, a.id, columns=["chunk_id"])
        if t is None or t.num_rows == 0:
            empty.append(a.name)
    for n in await repo.find(ws, source_refs=[a.id for a in by_kind.get(UNSTRUCTURED, [])], limit=20000):
        if (n.properties or {}).get("__reading_unit"):
            in_graph += 1
    check("every document has chunks in Delta", not empty, "; ".join(empty[:5]))
    check("no chunk nodes in the graph", in_graph == 0, f"{in_graph} chunk node(s)")

    # the graph holds knowledge, not records: no node per row, record or nested field by default
    data_ids = [a.id for a in by_kind.get(STRUCTURED, []) + by_kind.get(SEMISTRUCTURED, [])]
    record_nodes = [n for n in await repo.descendants(ws, data_ids, max_depth=3)
                    if n.type not in ("Attribute", "Table", "Source", "Asset", "Date")]
    check("no node per record or field in the graph", not record_nodes,
          f"{len(record_nodes)} record node(s), e.g. {sorted({n.type for n in record_nodes})[:5]}")

    # the semantic layer: types, keys and links detected from the data (knowledge/semantic)
    from opencortyx.knowledge.semantic import store as semantic
    layer = await semantic.load(ws)
    entities = [t for t in layer.types if t.kind == "entity" and t.keys]
    check("entity types detected with identifiers", len(entities) >= 5,
          ", ".join(f"{t.name}({'+'.join(t.keys)})" for t in layer.types))
    check("links detected between types", len(layer.links) >= 4,
          ", ".join(f"{lk.source}.{lk.source_prop}->{lk.target}" for lk in layer.links))
    merged = [t for t in entities if len({(b.kind, b.path) for b in t.bindings}) > 1]
    check("the same entity from two sources is one type", bool(merged),
          ", ".join(f"{t.name}: {sorted({b.path or 'rows' for b in t.bindings})}" for t in merged))

    # data layer: Attributes with value sets, no sample values
    attrs = await repo.find(ws, types=["Attribute"], limit=20000)
    vs = [a for a in attrs if (a.properties or {}).get("value_set")]
    samples = [a for a in attrs if (a.properties or {}).get("sample_values")]
    check("Attribute nodes profiled with value sets", len(vs) > 0, f"{len(vs)} of {len(attrs)}")
    check("no sample values on Attribute nodes", not samples, f"{len(samples)} with samples")

    # the duplicate customers (one in the CRM file, one in billing) read as ONE record each
    from opencortyx.retrieval.l2_data.schema import load as load_schema
    from opencortyx.stores.data import Views
    schema = await load_schema(ws, fresh=True)
    two = {t.name for t in merged if t.kind == "entity"}
    cust = next((t for t in schema.objects() if t.name in two and any(
        b.kind == SEMISTRUCTURED for b in layer.type(t.name).bindings)), None)
    n_cust = 0
    if cust is not None:
        with Views(ws, schema.refs, lock=True, logical=schema.logical) as v:
            n_cust = v.query(f'SELECT count(*) AS n FROM "{cust.view}"').to_pylist()[0]["n"]
    check("duplicate customers resolve to one record each", n_cust == exp["facts"]["customers"],
          f"{cust.name if cust else 'no customer type'}: {n_cust} records for {exp['facts']['customers']} customers")

    # resolution engine: document links
    links = {t: len(await repo.edges(ws, types=[t], limit=100000))
             for t in ("OBSOLETES", "REPLACES", "REQUIRES", "SUPERSEDED_BY", "UPDATES")}
    check("documents linked by their headers", sum(links.values()) > 0, json.dumps(links))

    # search index: passages, embedded
    vs_store = get_vector_store()
    passages = await vs_store.count(VectorFilter(workspace_id=ws, types=["Passage"]))
    check("passages in the search index", passages > 0, f"{passages} points")

    from opencortyx.doctor import OK, check_data_store
    check("doctor: data store", await check_data_store() == OK, "see the doctor line above")

    failed = [r for r in RESULTS if not r["ok"]]
    print(json.dumps({"checks": len(RESULTS), "failed": len(failed), "failures": failed}, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    ws_id, path = uuid.UUID(sys.argv[1]), sys.argv[2]
    with open(path, encoding="utf-8") as f:
        expected = json.load(f)
    sys.exit(asyncio.run(main(ws_id, expected)))
