"""Regression suite plumbing: one recorded result per executed scenario.

Every test in this package takes a `scenario` (a row of the coverage matrix, see `matrix.py`) and the
`rec` fixture. The test logs its generated data, steps, actual result and each validation through
`rec`; the hooks below add the outcome, the failure reason and the captured log, and append one JSON
line per test to `$OPENCORTYX_TEST_RESULTS/results-<pid>.jsonl`. `report.py` builds the report from
those lines only — a scenario that was not executed has no line and is never counted as covered.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import uuid

import pytest

RESULTS = pathlib.Path(os.environ.get("OPENCORTYX_TEST_RESULTS", "/app/test-results"))


class Recorder:
    """What one scenario did, as it happened."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.steps: list[str] = []
        self.data: dict = {}
        self.actual = None
        self.checks: list[dict] = []
        self.blocked: str | None = None

    def step(self, text: str) -> None:
        self.steps.append(text)

    def generated(self, **data) -> None:
        self.data.update({k: _short(v) for k, v in data.items()})

    def result(self, actual) -> None:
        self.actual = _short(actual)

    def check(self, name: str, ok: bool, detail="") -> bool:
        """A validation of resulting data or state. Recorded, then asserted by the caller."""
        self.checks.append({"check": name, "ok": bool(ok), "detail": _short(detail)})
        return bool(ok)

    def verify(self, name: str, ok: bool, detail="") -> None:
        assert self.check(name, ok, detail), f"{name}: {detail}"

    def block(self, reason: str) -> None:
        """The scenario cannot run here (a missing writer, a missing service). Reported as BLOCKED."""
        self.blocked = reason
        pytest.skip(f"BLOCKED: {reason}")


def _short(v, limit: int = 600):
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    try:
        s = json.dumps(v, default=str)
    except Exception:                                     # noqa: BLE001
        s = repr(v)
    return v if len(s) <= limit and not isinstance(v, (set, tuple)) else s[:limit] + ("…" if len(s) > limit else "")


@pytest.fixture
def rec(request):
    sc = request.node.callspec.params.get("scenario") if hasattr(request.node, "callspec") else None
    r = Recorder(sc)
    request.node._recorder = r
    return r


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    r = getattr(item, "_recorder", None)
    if r is None:
        return
    # the call phase decides the outcome; a setup failure is recorded as an error in setup
    if report.when == "call" or (report.when == "setup" and not report.passed):
        _write(item, r, report)


def _write(item, r: Recorder, report) -> None:
    sc = r.scenario
    if r.blocked:
        status = "blocked"
    elif report.skipped:
        status = "skipped"
    elif report.passed:
        status = "passed"
    else:
        status = "failed" if report.when == "call" else "error"
    reason = ""
    if not report.passed:
        reason = str(getattr(report.longrepr, "reprcrash", None) and report.longrepr.reprcrash.message
                     or report.longrepr)[:1200]
    logs = "\n".join(t for _, t in getattr(report, "sections", []) if t)[-4000:]
    line = {
        "test_id": sc.id if sc else item.nodeid, "nodeid": item.nodeid,
        **(sc.as_record() if sc else {}),
        "generated_data": r.data, "steps": r.steps, "actual": r.actual, "checks": r.checks,
        "status": status, "failure_reason": reason, "duration_s": round(report.duration, 4),
        "logs": logs, "finished_at": time.time(),
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / f"results-{os.getpid()}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line, default=str) + "\n")


# ── live-stack fixtures ──────────────────────────────────────────────────────────────────────────
API = os.environ.get("OPENCORTYX_API_URL", "http://api:8000").rstrip("/")


def _stack_up() -> bool:
    import httpx
    try:
        return httpx.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:                                     # noqa: BLE001
        return False


@pytest.fixture(scope="session")
def live_api():
    """The running API, or every live scenario is reported BLOCKED with the reason."""
    if not _stack_up():
        pytest.skip(f"BLOCKED: API not reachable at {API}")
    return API


def token(workspace_id: uuid.UUID, role: str = "admin", **kw) -> str:
    """A signed access token for a throwaway tenant. Test-only: the org holds nothing but test data."""
    from opencortyx.core.security.auth import create_access_token
    return create_access_token(uuid.uuid4(), workspace_id, role=role, **kw)


@pytest.fixture(scope="session")
def tenants(live_api):
    """Two isolated throwaway tenants per run; everything they hold is deleted at the end."""
    orgs = {"A": uuid.uuid4(), "B": uuid.uuid4()}
    yield orgs
    from tests.conftest import _drop_org
    for org in orgs.values():
        try:
            _drop_org(org)
        except Exception:                                 # noqa: BLE001 — cleanup must not mask results
            pass
