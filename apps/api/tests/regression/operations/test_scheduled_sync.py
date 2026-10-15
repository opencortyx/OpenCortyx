"""Scheduled sync observed over real wall-clock minutes, driven by the live celery-beat tick (SYNC).

One observation window (~7 minutes) runs every configuration side by side as tenant A, mutates the
watched folder between ticks, resumes a paused ingestion part-way, and then reads every run the tick
dispatched straight from `ingest_run`. Each scenario asserts on that record.

`allow_local_files` is a platform-wide switch that watch folders need. It is turned on for the window
only and restored to its original value in a `finally`, whatever happens.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import time
import uuid
from datetime import datetime, timezone

import pytest

from tests.regression.framework.api import Client, principal_headers
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
HOSTDATA = pathlib.Path(os.environ.get("OPENCORTYX_HOSTDATA", "/hostdata"))   # = /host inside the workers
V2 = "/api/v2"


def _write(p: pathlib.Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def _runs(org, asset):
    """Every run the tick dispatched for one ingestion, oldest first, from the database."""
    import asyncio
    from sqlalchemy import select
    from opencortyx.core.db import V2Session
    from opencortyx.domain.control import IngestRun

    async def q():
        async with V2Session() as db:
            rows = (await db.execute(select(IngestRun).where(IngestRun.workspace_id == org, IngestRun.asset == asset)
                                     .order_by(IngestRun.created_at))).scalars().all()
            return [{"id": str(r.id), "status": r.status, "result": r.result or {}, "error": r.error,
                     "created": r.created_at, "finished": r.finished_at} for r in rows]
    return asyncio.run(q())


def _wait(pred, timeout, step=5):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


@pytest.fixture(scope="module")
def observed(live_api, tenants):
    if not HOSTDATA.exists():
        pytest.skip(f"BLOCKED: the watched host folder is not mounted at {HOSTDATA}")
    org = tenants["A"]
    a = Client(live_api, principal_headers("admin", org), timeout=60)
    original = a.get(f"{V2}/config").json()["tunable"].get("allow_local_files")
    base = f"regression-sync-{uuid.uuid4().hex[:8]}"
    root = HOSTDATA / base
    obs = {"events": {}, "names": {}, "org": org}
    try:
        a.put(f"{V2}/config", json={"values": {"allow_local_files": True}}).raise_for_status()
        for sub in ("sched", "paused", "ondemand", "realtime", "badcron"):
            _write(root / sub / "a.json", [{"id": f"{sub}-1", "v": 1}])
        _write(root / "sched" / "b.json", [{"id": "sched-2", "v": 2}])
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "note.md").write_text("# Sync note\n\nScheduled sync regression marker sentence.\n")
        con = a.post(f"{V2}/connectors", json={"name": f"{base}-local", "kind": "local", "directory": "/host"}).json()["id"]
        plan = {"sched": ("semistructured", "scheduled", "* * * * *"), "paused": ("semistructured", "scheduled", "* * * * *"),
                "ondemand": ("semistructured", "on_demand", None), "realtime": ("semistructured", "realtime", None),
                "badcron": ("semistructured", "scheduled", "not-a-cron"), "missing": ("semistructured", "scheduled", "* * * * *"),
                "docs": ("unstructured", "scheduled", "* * * * *")}
        ids = {}
        for key, (kind, mode, cron) in plan.items():
            name = f"{base}-{key}"
            r = a.post(f"{V2}/ingestions", json={"name": name, "kind": kind, "connector_id": con, "mode": mode,
                                                 "schedule": cron, "selections": [],
                                                 "directories": [f"/host/{base}/{key}"]})
            obs["names"][key] = f"{name} · sync"
            if key == "badcron":                  # refused at creation; if it were accepted, the tick is watched too
                obs["badcron_status"] = r.status_code
                if r.status_code != 200:
                    continue
            r.raise_for_status()
            ids[key] = r.json()["id"]
        a.post(f"{V2}/ingestions/{ids['paused']}/pause").raise_for_status()
        obs["events"]["start"] = datetime.now(timezone.utc)

        done = lambda k, after=None: [x for x in _runs(org, obs["names"][k])
                                      if x["status"] in ("completed", "failed") and (after is None or x["created"] > after)]
        _wait(lambda: len(done("sched")) >= 2, 200)                   # first pass + an unchanged pass
        (root / "sched" / "c.json").write_text(json.dumps([{"id": "sched-3", "v": 3}]))
        (root / "sched" / "a.json").write_text(json.dumps([{"id": "sched-1", "v": 100}]))
        (root / "sched" / "b.json").unlink()
        obs["events"]["mutated"] = datetime.now(timezone.utc)
        _wait(lambda: done("sched", obs["events"]["mutated"]), 150)
        obs["events"]["resumed"] = datetime.now(timezone.utc)
        a.post(f"{V2}/ingestions/{ids['paused']}/resume").raise_for_status()
        _wait(lambda: done("paused", obs["events"]["resumed"]), 150)
        _wait(lambda: len(done("missing")) >= 2, 330)                 # hibernation backoff is 120 s
        obs["events"]["end"] = datetime.now(timezone.utc)
        obs["runs"] = {k: _runs(org, n) for k, n in obs["names"].items()}
        obs["ingestions"] = {k: a.get(f"{V2}/ingestions/{i}").json() for k, i in ids.items()}
        yield obs
    finally:
        try:
            a.put(f"{V2}/config", json={"values": {"allow_local_files": bool(original)}})
        finally:
            a.close()
            shutil.rmtree(root, ignore_errors=True)


def _res(r):
    return {k: r["result"].get(k) for k in ("added", "changed", "removed", "unchanged")}


@pytest.mark.parametrize("scenario", scenarios("SYNC"), ids=str)
def test_scheduled_sync(scenario, rec, observed):
    runs, ev, case = observed["runs"], observed["events"], scenario.params["case"]
    window = (ev["end"] - ev["start"]).total_seconds()
    rec.generated(window_seconds=round(window), events={k: v.isoformat() for k, v in ev.items()})
    rec.step("observed the live beat tick over the window; runs read from ingest_run")
    sched = [r for r in runs["sched"] if r["status"] == "completed"]
    rec.result({"runs": {k: [(r["created"].strftime("%H:%M:%S"), r["status"], _res(r)) for r in v][:12]
                         for k, v in runs.items() if k in _KEYS.get(case, (case,))}})
    if case == "fires_per_minute":
        minutes = [r["created"].replace(second=0, microsecond=0) for r in runs["sched"]]
        rec.verify("fired repeatedly", len(runs["sched"]) >= 3, len(runs["sched"]))
        rec.verify("never twice in one minute", len(minutes) == len(set(minutes)), minutes)
    elif case == "first_pass_adds_all":
        rec.verify("first run added both files", bool(sched) and sched[0]["result"].get("added") == 2, _res(sched[0]) if sched else None)
    elif case == "unchanged_pass_skips":
        quiet = [r for r in sched[1:] if r["created"] < ev["mutated"]]
        rec.verify("an unchanged pass touched nothing", any(_res(r) == {"added": 0, "changed": 0, "removed": 0, "unchanged": 2}
                                                            for r in quiet), [_res(r) for r in quiet])
    elif case in ("added_file_detected", "changed_file_detected", "removed_file_detected"):
        after = [r for r in sched if r["created"] > ev["mutated"]]
        key = {"added_file_detected": "added", "changed_file_detected": "changed", "removed_file_detected": "removed"}[case]
        rec.verify("a run after the change completed", bool(after), len(after))
        rec.verify(f"{key}=1 on the first run after the change", bool(after) and after[0]["result"].get(key) == 1,
                   _res(after[0]) if after else None)
    elif case == "paused_never_runs":
        while_paused = [r for r in runs["paused"] if r["created"] < ev["resumed"]]
        rec.verify("no run while paused", not while_paused, len(while_paused))
    elif case == "resume_restarts":
        rec.verify("ran after resume", any(r["created"] > ev["resumed"] for r in runs["paused"]), len(runs["paused"]))
    elif case == "on_demand_never_runs":
        rec.verify("never auto-run", not runs["ondemand"], len(runs["ondemand"]))
    elif case == "realtime_every_tick":
        rec.verify("runs on repeated ticks", len(runs["realtime"]) >= max(3, int(window // 60)), len(runs["realtime"]))
    elif case == "invalid_cron_is_inert":
        rec.verify("refused when created", observed.get("badcron_status") in (400, 422), observed.get("badcron_status"))
        rec.verify("never fired", not runs["badcron"], len(runs["badcron"]))
        rec.verify("the tick kept serving the others", len(runs["sched"]) >= 3, len(runs["sched"]))
    elif case == "failure_hibernates":
        ing = observed["ingestions"]["missing"]
        rec.verify("a run over a missing folder fails with a reason",
                   any(r["status"] == "failed" and r["error"] for r in runs["missing"]),
                   [(r["status"], (r["error"] or "")[:80]) for r in runs["missing"]])
        rec.verify("the ingestion hibernated", ing.get("status") in ("paused", "hibernated") or ing.get("hibernated"), ing.get("status"))
    elif case == "hibernation_self_heals":
        f = [r for r in runs["missing"] if r["status"] == "failed"]
        gaps = [(b["created"] - a["created"]).total_seconds() for a, b in zip(f, f[1:])]
        rec.verify("retried", len(f) >= 2, len(f))
        rec.verify("not before the 120 s backoff", all(g >= 110 for g in gaps), gaps)
    elif case == "documents_scheduled":
        docs = [r for r in runs["docs"] if r["status"] == "completed"]
        rec.verify("the tick ingested the document", bool(docs) and docs[0]["result"].get("added") == 1,
                   [(r["status"], _res(r), (r["error"] or "")[:80]) for r in runs["docs"]])


_KEYS = {"fires_per_minute": ("sched",), "first_pass_adds_all": ("sched",), "unchanged_pass_skips": ("sched",),
         "added_file_detected": ("sched",), "changed_file_detected": ("sched",), "removed_file_detected": ("sched",),
         "paused_never_runs": ("paused",), "resume_restarts": ("paused",), "on_demand_never_runs": ("ondemand",),
         "realtime_every_tick": ("realtime",), "invalid_cron_is_inert": ("badcron", "sched"),
         "failure_hibernates": ("missing",), "hibernation_self_heals": ("missing",), "documents_scheduled": ("docs",)}
