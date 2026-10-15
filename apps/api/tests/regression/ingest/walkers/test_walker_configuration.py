"""Corpus files × pairwise walker configurations (WCFG)."""
from __future__ import annotations

import pathlib
import uuid
from collections import defaultdict

import pytest

from tests.regression.framework.matrix import CORPUS, read_capped, scenarios
from tests.regression.framework.support import ingest_json_cfg, MemRepo, walked_data

pytestmark = pytest.mark.regression


def _load(scenario, rec) -> bytes:
    if not CORPUS:
        rec.block("OPENCORTYX_CORPUS is not mounted")
    p = pathlib.Path(CORPUS) / scenario.params["path"]
    content = read_capped(p)
    rec.generated(file=scenario.params["path"], size_bytes=len(content))
    return content


def _parse(content: bytes, name: str):
    from opencortyx.ingest.formats import semistructured as P
    return P.parse(content, name)


# ── WCFG ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", scenarios("WCFG") or [pytest.param(None, marks=pytest.mark.skip("no corpus"))], ids=str)
async def test_walker_configuration(scenario, rec, monkeypatch):
    from opencortyx.ingest.semistructured import schema as ss
    p = scenario.params
    content = _load(scenario, rec)
    data = walked_data(_parse(content, p["path"]))
    kw = {"max_depth": p["max_depth"], "max_array": p["max_array"], "root_type": p["root_type"],
          "collect_ids": True}
    records = data if isinstance(data, list) else [data]
    if p["declared"]:
        first = next((r for r in records if isinstance(r, dict)), None)
        if first is None:
            rec.block("no object record to derive a structure card from")
        card = ss.build_structure(first, name="regression")
        kw["walk_spec"] = ss.compile_walk_spec(ss.normalize_structures([card]))
        kw["auto_zip"] = False
    rec.generated(records=len(records), config={k: v for k, v in kw.items() if k != "walk_spec"},
                  declared=bool(p["declared"]))
    rec.step(f"ingest_json(data, {', '.join(f'{k}={v}' for k, v in kw.items() if k != 'walk_spec')})")
    repo, res = await ingest_json_cfg(monkeypatch, data, p["path"], **kw)
    src = uuid.UUID(res["source_id"])
    depths = repo.depths(src)
    deepest = max(depths.values()) if depths else 0
    rec.result({"status": res["status"], "nodes": res["nodes"], "edges": res["edges"], "deepest": deepest})
    rec.verify("status ok", res["status"] == "ok", res["status"])
    rec.verify("depth bound honoured", deepest <= p["max_depth"] + 1, f"deepest {deepest} for max_depth {p['max_depth']}")
    # max_array bounds ARRAYS: the children of one parent that came from one array are chained by NEXT.
    # Several promoted long-text fields of one object (HAS_DOCUMENT per field) are distinct keys, not
    # an array, and are not bounded by it — counting them was the test's error (formerly "D32").
    nexts = {(s, d) for s, d, t in repo.edges if t == "NEXT"}
    groups = defaultdict(list)
    for s, d, t in repo.edges:
        if t.startswith("HAS_") and s != src:
            groups[(s, t)].append(d)
    widest = max((len(kids) for kids in groups.values()
                  if any((a, b) in nexts for a in kids for b in kids if a != b)), default=0)
    rec.verify("array bound honoured", widest <= p["max_array"], f"widest fan-out {widest} for max_array {p['max_array']}")
    if p["root_type"]:
        rec_types = {repo.nodes[d]["type"] for s, d, t in repo.edges if s == src and d in repo.nodes}
        rec.verify("root type applied to records", p["root_type"] in rec_types or not any(isinstance(r, dict) for r in records),
                   rec_types)
    if records and all(isinstance(r, dict) and r.get("id") not in (None, "") for r in records):
        # an `id` field is an attribute: records sharing one stay separate nodes, one per record
        rec_nodes = {d for s, d, t in repo.edges if s == src}
        rec.verify("ids are generated, not read from the data", len(rec_nodes) == len(records),
                   f"{len(rec_nodes)} record nodes for {len(records)} records")
    again = MemRepo()
    await ingest_json_cfg(monkeypatch, data, p["path"], repo=again, **kw)
    rec.verify("deterministic", set(again.nodes) == set(repo.nodes), f"{len(again.nodes)} vs {len(repo.nodes)}")
