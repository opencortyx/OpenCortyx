"""Connectors: kind × configuration lifecycle, secrets, connection tests, recipes (CONN)."""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario

CONNECTOR_KINDS = ("postgres", "mysql", "mssql", "s3", "gcs", "adls", "local", "delta", "iceberg", "hudi", "files", "bogus")
CONNECTOR_CONFIGS = {
    "complete_unreachable": {"host": "10.255.255.1", "port": 5432, "database": "db", "username": "u",
                             "connection_string": "postgresql://u:s3cret-marker@10.255.255.1:5432/db"},
    "missing_host": {"port": 5432, "database": "db"},
    "directory_only": {"directory": "/host"},
    "connection_string_only": {"connection_string": "postgresql://u:s3cret-marker@10.255.255.1:1/db"},
}

def build() -> list[Scenario]:
    out = []

    def add(fam, feature, wf, inp, cfg, exp, typ, params):
        out.append(Scenario(fam, "features", feature, wf, inp, cfg, exp, typ, "API up; tenant A", params))

    for kind in CONNECTOR_KINDS:
        for cname, cfg in CONNECTOR_CONFIGS.items():
            add("CONN", "connectors", "connector lifecycle", f"kind={kind}", f"config={cname}",
                "refused" if kind == "bogus" else "stored; secrets never echoed; test reports failure cleanly; "
                "recipe round-trips; delete", "negative" if kind == "bogus" else "integration",
                {"kind": kind, "cfg": cfg, "cname": cname})
    return out
