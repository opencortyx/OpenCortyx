"""Schema mappings (mappers): paradigm × payload × status lifecycle (MAPS)."""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario

MAPPING_PARADIGMS = ("structured", "semistructured", "unstructured", "bogus")
MAPPING_PAYLOADS = {"empty": {}, "structures": {"structures": [{"name": "Orders", "keys": [{"path": "", "kind": "node", "node_type": "Order"}]}]},
                    "table_mappings": {"table_mappings": [{"table": "orders", "row_node_type": "Order"}]}}
MAPPING_STATUSES = ("active", "draft", "bogus")

def build() -> list[Scenario]:
    out = []

    def add(fam, feature, wf, inp, cfg, exp, typ, params):
        out.append(Scenario(fam, "features", feature, wf, inp, cfg, exp, typ, "API up; tenant A", params))

    for para in MAPPING_PARADIGMS:
        for pname, payload in MAPPING_PAYLOADS.items():
            for st in MAPPING_STATUSES:
                ok = para != "bogus" and st != "bogus"
                add("MAPS", "schema mappings", "mapping lifecycle", f"paradigm={para}", f"payload={pname} status={st}",
                    "stored; usage empty; update; delete" if ok else "refused", "integration" if ok else "negative",
                    {"paradigm": para, "payload": payload, "status": st, "ok": ok})
    return out
