"""Worker crash and redelivery on the live stack: SIGKILL a worker mid-task and watch what happens (RCVR).

Runs on the HOST (it needs the docker CLI), against the API on localhost:

    OPENCORTYX_API_URL=http://localhost:8000 OPENCORTYX_TEST_RESULTS=apps/api/test-results/<run> \\
    JWT_SECRET=... DATABASE_URL=... python -m pytest -m regression apps/api/tests/regression/operations/test_worker_recovery.py

Workers run with acks_late + reject_on_worker_lost, so a killed task is redelivered; ResilientTask gives
a task up after MAX_LOST_ATTEMPTS lost attempts and fails its run with the reason. Every tenant here is
throwaway and dropped through the api container at the end.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid

import pytest

from tests.regression.framework.api import Client, principal_headers, wait_run
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
API = "http://localhost:8000"
WORKER, EXTRACTOR = "opencortyx-worker-1", "opencortyx-extraction-worker-1"


def docker(*args) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120).stdout.strip()


#: SIGKILL the busiest celery POOL process inside the container — what the kernel's OOM killer does in
#: production. The container, the celery main process and embedded beat keep running; the main process
#: sees WorkerLostError, forks a new child, and the broker redelivers the task (acks_late).
#: (`docker kill` would be the wrong model: Docker treats it as a manual stop and never restarts it.)
_KILL_BUSY_CHILD = r"""
import os, signal, time
def procs():
    out = {}
    for p in os.listdir('/proc'):
        if p.isdigit():
            try:
                cmd = open(f'/proc/{p}/cmdline', 'rb').read().replace(b'\0', b' ').decode()
                st = open(f'/proc/{p}/stat').read().rsplit(')', 1)[1].split()
                out[int(p)] = (cmd, int(st[1]), int(st[11]) + int(st[12]))
            except Exception:
                pass
    return out
a = procs(); time.sleep(2); b = procs()
cel = {p for p, (c, _pp, _t) in b.items() if 'celery' in c and not c.startswith('sh ') and 'inspect' not in c}
mains = {p for p in cel if b[p][1] not in cel}
def is_beat(p):
    try:
        return any('celerybeat' in os.readlink(f'/proc/{p}/fd/{fd}') for fd in os.listdir(f'/proc/{p}/fd'))
    except Exception:
        return False
kids = [p for p in cel if b[p][1] in mains and not is_beat(p)]   # never the embedded beat process
busy = max(kids, key=lambda p: b[p][2] - a.get(p, ('', 0, b[p][2]))[2])
os.kill(busy, signal.SIGKILL)
print(busy)
"""


def idle(container: str, timeout: float = 1800) -> bool:
    """Wait until the worker executes nothing: the single graphrag worker runs one task at a time, so
    a scenario started behind someone else's long ingest would measure the queue, not the recovery."""
    end = time.time() + timeout
    while time.time() < end:
        out = docker("exec", container, "celery", "-A", "opencortyx.core.worker.celery_app", "inspect", "active",
                     "--timeout", "10", "-d", "celery@" + docker("exec", container, "hostname"))
        if "empty" in out:
            return True
        time.sleep(10)
    return False


def kill_task_process(container: str) -> str:
    return docker("exec", container, "python", "-c", _KILL_BUSY_CHILD)


def healthy(container: str, timeout: float = 240) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        state = docker("inspect", container, "--format", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}")
        if state.startswith("running") and ("healthy" in state and "unhealthy" not in state or state == "running"):
            return True
        time.sleep(3)
    return False


def kill_when_running(c: Client, run_id: str, container: str, rec, attempt: int, timeout=300) -> bool:
    """SIGKILL `container` once the run is genuinely executing (status running, some progress)."""
    end = time.time() + timeout
    while time.time() < end:
        r = c.http.get(f"/api/v2/progress/{run_id}").json()
        if r.get("status") == "running":
            time.sleep(4)                                   # well inside the task, not at its first line
            pid = kill_task_process(container)
            rec.step(f"attempt {attempt}: SIGKILL pool process {pid} in {container} at {r.get('percent')}%")
            return True
        if r.get("status") in ("completed", "failed"):
            return False
        time.sleep(1)
    return False


@pytest.fixture(scope="module")
def stack():
    if not shutil.which("docker"):
        pytest.skip("BLOCKED: needs the docker CLI (run this module on the host)")
    try:
        import httpx
        httpx.get(f"{API}/health", timeout=5).raise_for_status()
    except Exception as e:                                  # noqa: BLE001
        pytest.skip(f"BLOCKED: API not reachable at {API}: {e}")
    orgs = []
    yield orgs
    for org in orgs:                                        # drop through the container, which reaches the stores
        subprocess.run(["docker", "compose", "-f", "compose.yaml", "run", "--rm", "--no-deps", "-T", "api", "python", "-c",
                        f"from tests.conftest import _drop_org; import uuid; _drop_org(uuid.UUID('{org}'))"],
                       capture_output=True, timeout=300, cwd=_repo_root())


def _repo_root():
    import pathlib
    return str(pathlib.Path(__file__).resolve().parents[5])


def _tenant(stack, rec):
    org = uuid.uuid4()
    stack.append(org)
    return Client(API, principal_headers("admin", org), rec, timeout=120)


def _json(n: int) -> bytes:
    return json.dumps([{"id": f"R-{i}", "name": f"record {i}", "tags": ["a", "b"], "n": i} for i in range(n)]).encode()


def _upload(c, name, content):
    r = c.post("/api/v2/sources/upload", files={"file": (name, content, "application/octet-stream")})
    r.raise_for_status()
    return r.json()["run_id"]


@pytest.mark.parametrize("scenario", scenarios("RCVR"), ids=str)
def test_worker_recovery(scenario, rec, stack):
    case = scenario.params["case"]
    target = EXTRACTOR if case == "kill_extraction_worker" else WORKER
    rec.verify("worker idle before the scenario", idle(target), target)
    c = _tenant(stack, rec)
    try:
        if case == "kill_idle_worker":
            pid = kill_task_process(WORKER)
            rec.step(f"SIGKILL idle pool process {pid} in {WORKER}")
            rec.verify("worker restarted healthy", healthy(WORKER), docker("inspect", WORKER, "--format", "{{.State.Status}}"))
            run = wait_run(c, _upload(c, "after-crash.json", _json(5)), timeout=900)
            rec.result({"run": run.get("status")})
            rec.verify("the next upload completes", run.get("status") == "completed", run)
        elif case == "kill_once_mid_ingest":
            content = _json(400)
            ref = _tenant(stack, rec)
            ref_run = wait_run(ref, _upload(ref, "recovery.json", content), timeout=1200)
            ref_total = ref.get(f"/api/v2/progress/{ref_run['id']}/outcome").json().get("total_nodes")
            rid = _upload(c, "recovery.json", content)
            rec.verify("killed while running", kill_when_running(c, rid, WORKER, rec, 1, 900))
            rec.verify("worker restarted healthy", healthy(WORKER))
            run = wait_run(c, rid, timeout=1500)
            total = c.get(f"/api/v2/progress/{rid}/outcome").json().get("total_nodes")
            rec.result({"run": run.get("status"), "error": run.get("error"), "total_nodes": total, "uncrashed_total": ref_total})
            rec.verify("redelivered and completed", run.get("status") == "completed", run.get("error"))
            rec.verify("graph equals an uncrashed ingest of the same file", total == ref_total, f"{total} vs {ref_total}")
        elif case == "kill_twice_gives_up":
            rid = _upload(c, "doomed.json", _json(400))
            for attempt in (1, 2):
                rec.verify(f"attempt {attempt} killed while running", kill_when_running(c, rid, WORKER, rec, attempt, 600))
                rec.verify("worker restarted healthy", healthy(WORKER))
            run = wait_run(c, rid, timeout=900)
            rec.result({"run": run.get("status"), "error": run.get("error")})
            rec.verify("never left running", run.get("status") in ("failed", "completed"), run.get("status"))
            rec.verify("given up with the reason after the lost attempts",
                       run.get("status") == "failed" and "attempts" in (run.get("error") or ""), run.get("error"))
        elif case == "kill_extraction_worker":
            doc = ("# Recovery report\n\n" + "Acme AS in Oslo signed a contract with Globex in Bergen. " * 60).encode()
            rid = _upload(c, "recovery-doc.md", doc)
            killed = kill_when_running(c, rid, EXTRACTOR, rec, 1, 600)
            if not killed:        # the run finished before the extractor took it: nothing was crashed
                rec.block("the document run finished before the extraction worker picked it up; nothing to kill")
            rec.verify("extraction worker restarted healthy", healthy(EXTRACTOR))
            run = wait_run(c, rid, timeout=1500)
            rec.result({"run": run.get("status"), "error": run.get("error")})
            rec.verify("the run reaches a final state", run.get("status") in ("completed", "failed"), run.get("status"))
            if run.get("status") == "failed":
                rec.verify("a failure carries its reason", bool(run.get("error")), run)
    finally:
        c.close()
