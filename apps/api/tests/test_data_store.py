"""The data store (stores/data): what delta-rs writes, DuckDB reads back - for every operation used.

These pin the pairing the design depends on (docs/knowledge-layer-implementation.md, 2.6): a
dependency bump that makes the DuckDB `delta` extension unable to read what delta-rs writes fails
here, not in a customer's ingest. They need the `delta` extension: bundled in the image, installed
on first use elsewhere.
"""
import os
import pathlib
import re
import uuid

import pyarrow as pa
import pytest

pytest.importorskip("deltalake")
pytest.importorskip("duckdb")

from opencortyx.stores.data import (  # noqa: E402
    STRUCTURED, UNSTRUCTURED, AssetRef, DataStore, DataStoreError, delta, layout,
)

WS = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture()
def store(tmp_path):
    return DataStore(WS, root=str(tmp_path))


def _chunks(asset, n, version=1, start=0):
    return pa.table({
        "asset_id": [str(asset)] * n, "version": [version] * n,
        "chunk_id": [f"{asset}:{i}" for i in range(start, start + n)],
        "ordinal": list(range(start, start + n)), "element_type": ["Text"] * n,
        "text": [f"paragraph {i} of {asset}" for i in range(start, start + n)],
    })


def _rows(keys, status="ACTIVE"):
    return pa.table({"_row_key": keys, "licence": [f"LIC-{k}" for k in keys],
                     "status": [status] * len(keys)})


def test_paths_take_only_uuids(tmp_path):
    with pytest.raises(ValueError):
        layout.table_path(str(tmp_path), "../other", STRUCTURED, uuid.uuid4())
    with pytest.raises(ValueError):
        layout.table_path(str(tmp_path), WS, STRUCTURED, "x/../../y")
    assert layout.table_path("/r", WS, UNSTRUCTURED).endswith(f"{WS}/unstructured")


def test_view_names_read_like_the_source_and_never_collide():
    taken: set = set()
    assert layout.view_name("Licences 2026.csv", taken) == "licences_2026"
    assert layout.view_name("licences-2026", taken) == "licences_2026_2"
    assert layout.view_name("2026 report", taken) == "t_2026_report"


def test_written_chunks_read_back_per_document(store):
    a, b = uuid.uuid4(), uuid.uuid4()
    v1 = store.append(UNSTRUCTURED, a, _chunks(a, 3))
    store.append(UNSTRUCTURED, b, _chunks(b, 2))
    assert store.version(UNSTRUCTURED) == v1 + 1
    page = store.read(UNSTRUCTURED, a)
    assert page.column("chunk_id").to_pylist() == [f"{a}:0", f"{a}:1", f"{a}:2"]
    assert store.read(UNSTRUCTURED, b, limit=1, offset=1).column("ordinal").to_pylist() == [1]


def test_batched_lookup_returns_exactly_the_asked_units(store):
    a = uuid.uuid4()
    store.append(UNSTRUCTURED, a, _chunks(a, 5))
    got = store.lookup(UNSTRUCTURED, a, [f"{a}:1", f"{a}:4", f"{a}:9"], columns=["text"])
    assert set(got) == {f"{a}:1", f"{a}:4"}
    assert got[f"{a}:4"]["text"] == f"paragraph 4 of {a}"


def test_deleting_one_document_keeps_the_others(store):
    a, b = uuid.uuid4(), uuid.uuid4()
    store.append(UNSTRUCTURED, a, _chunks(a, 2))
    store.append(UNSTRUCTURED, b, _chunks(b, 2))
    store.delete_asset(UNSTRUCTURED, a)
    assert store.read(UNSTRUCTURED, a).num_rows == 0
    assert store.read(UNSTRUCTURED, b).num_rows == 2


def test_merge_replaces_matching_rows_and_adds_new_ones(store):
    t = uuid.uuid4()
    store.append(STRUCTURED, t, _rows(["1", "2"]))
    store.merge(STRUCTURED, t, _rows(["2", "3"], status="EXPIRED"))
    rows = {r["_row_key"]: r["status"] for r in store.read(STRUCTURED, t).to_pylist()}
    assert rows == {"1": "ACTIVE", "2": "EXPIRED", "3": "EXPIRED"}


def test_new_columns_are_added_to_the_schema(store):
    t = uuid.uuid4()
    store.append(STRUCTURED, t, _rows(["1"]))
    store.append(STRUCTURED, t, pa.table({"_row_key": ["2"], "licence": ["LIC-2"],
                                          "status": ["ACTIVE"], "region": ["EU"]}))
    rows = store.read(STRUCTURED, t).to_pylist()
    assert rows[0]["region"] is None and rows[1]["region"] == "EU"


def test_evidence_opens_the_row_as_it_was(store):
    t = uuid.uuid4()
    v0 = store.append(STRUCTURED, t, _rows(["7"]))
    store.merge(STRUCTURED, t, _rows(["7"], status="SUSPENDED"))
    then, current = store.open_unit(STRUCTURED, t, "7", version_ref=v0)
    assert current and then["status"] == "ACTIVE"
    now, _ = store.open_unit(STRUCTURED, t, "7")
    assert now["status"] == "SUSPENDED"


def test_a_vacuumed_version_opens_the_latest_and_says_so(store):
    t = uuid.uuid4()
    store.append(STRUCTURED, t, _rows(["7"]))
    row, current = store.open_unit(STRUCTURED, t, "7", version_ref=999)
    assert row["_row_key"] == "7" and current is False


def test_compaction_keeps_every_row(store):
    a = uuid.uuid4()
    for i in range(4):
        store.append(UNSTRUCTURED, a, _chunks(a, 2, start=i * 2))
    store.optimize(UNSTRUCTURED)
    store.vacuum(UNSTRUCTURED, retention_hours=0)
    assert store.read(UNSTRUCTURED, a).num_rows == 8


def test_an_asset_with_no_data_yet_reads_as_nothing(store):
    assert store.read(STRUCTURED, uuid.uuid4()) is None
    assert store.lookup(STRUCTURED, uuid.uuid4(), ["1"]) == {}


def test_views_join_managed_and_external_tables(store, tmp_path):
    managed = uuid.uuid4()
    store.append(STRUCTURED, managed, _rows(["1", "2"]))
    lake = tmp_path / "customer-lake" / "regions"
    delta.append(str(lake), pa.table({"licence": ["LIC-1", "LIC-2"], "region": ["EU", "US"]}))
    assets = [AssetRef(asset_id=str(managed), name="Licences.csv", kind=STRUCTURED),
              AssetRef(asset_id=str(uuid.uuid4()), name="regions", kind=STRUCTURED,
                       storage="external", format="delta", location=str(lake))]
    with store.views(assets, lock=True) as v:
        assert set(v.names) == {"licences", "regions"}
        got = v.query("SELECT l.licence, r.region FROM licences l JOIN regions r USING (licence) "
                      "ORDER BY 1").to_pylist()
        assert got == [{"licence": "LIC-1", "region": "EU"}, {"licence": "LIC-2", "region": "US"}]
        with pytest.raises(Exception):
            v.query("SET memory_limit = '64GB'")


def test_an_attached_sqlite_database_is_read_in_place_and_read_only(store, tmp_path):
    db = tmp_path / "ops.sqlite"
    import sqlite3
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")
    c.executemany("INSERT INTO customers VALUES (?, ?)", [(1, "ABC"), (2, "XYZ")])
    c.commit()
    c.close()
    ref = AssetRef(asset_id=str(uuid.uuid4()), name="customers", kind=STRUCTURED,
                   storage="external", format="sqlite", location=str(db),
                   options={"table": "customers"})
    try:
        with store.views([ref]) as v:
            assert v.query("SELECT name FROM customers WHERE id = 2").to_pylist() == [{"name": "XYZ"}]
            with pytest.raises(Exception):
                v.con.execute("INSERT INTO src1.customers VALUES (3, 'NEW')")
    except DataStoreError as e:                              # offline machine without the bundle
        pytest.skip(str(e))


def test_a_long_query_is_interrupted(store):
    from opencortyx.stores.data import duck
    con = duck.connect()
    with pytest.raises(DataStoreError, match="stopped"):
        duck.run(con, "SELECT count(*) FROM range(100000000000) a", timeout=0.2)


def test_only_the_data_store_imports_the_engines():
    pkg = pathlib.Path(__file__).resolve().parents[1] / "opencortyx"
    pattern = re.compile(r"^\s*(?:import|from)\s+(duckdb|deltalake)\b", re.M)
    offenders = [str(p.relative_to(pkg)) for p in pkg.rglob("*.py")
                 if "stores" + os.sep + "data" not in str(p) and pattern.search(p.read_text("utf-8"))]
    assert not offenders, "import DuckDB / delta-rs through opencortyx.stores.data: %s" % offenders
