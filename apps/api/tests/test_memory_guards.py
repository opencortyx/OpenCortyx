"""The memory guards are configuration, so the thing that keeps them true is a test.

Two global OOM kills sit in this VM's kernel log — Celery children at 4.45 GiB and 4.51 GiB — taken
while every per-container `mem_limit` was within range. A per-container limit protects containers
from each other; nothing protects them from the VM running out except (a) the sum of the ceilings
fitting it and (b) no worker being able to reach a size like that in the first place.

These tests pin (b) and the shape of (a) from the compose file alone, so they run anywhere.
`scripts/check-memory-budget.py` does the live arithmetic against the actual VM.
"""
import re
from pathlib import Path

import pytest
import yaml

UNIT = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


def _find_compose():
    """Search upward rather than assume a depth: the api container mounts only `apps/api`, so a
    fixed `parents[3]` raises there instead of skipping — and a guard that cannot be collected is
    worse than one that says out loud that it did not run."""
    for parent in Path(__file__).resolve().parents:
        cand = parent / "compose.yaml"
        if cand.exists():
            return cand
    return None


COMPOSE = _find_compose()


def _bytes(v) -> int:
    m = re.match(r"([\d.]+)\s*([a-zA-Z]*)", str(v).strip())
    return int(float(m.group(1)) * UNIT.get((m.group(2) or "b").lower()[:1], 1)) if m else 0


@pytest.fixture(scope="module")
def services() -> dict:
    if COMPOSE is None:
        pytest.skip("compose.yaml not reachable from here (running inside a container?)")
    # Resolve every `${VAR:-default}` to its DEFAULT: the guards pin what ships. (Substituting "0"
    # made every child cap read as 0 bytes, so the caps were never actually checked.) What a `.env`
    # overrides is checked against the live VM by scripts/check-memory-budget.py.
    raw = re.sub(r"\$\{[A-Za-z0-9_]+(?::?-([^}]*))?\}", lambda m: m.group(1) or "0",
                 COMPOSE.read_text(encoding="utf-8"))
    return (yaml.safe_load(raw) or {}).get("services") or {}


def test_every_service_has_a_ceiling(services):
    """One unbounded container makes every other ceiling meaningless — vLLM was that container
    until 2026-08-30, so a single bad request could take the VM out from under Postgres."""
    unbounded = [n for n, s in services.items() if not s.get("mem_limit")]
    assert not unbounded, "no mem_limit on: %s" % ", ".join(sorted(unbounded))


def test_swap_is_pinned_to_the_limit(services):
    """memswap_limit == mem_limit means an overrun fails fast instead of thrashing the VM."""
    bad = [n for n, s in services.items()
           if s.get("mem_limit") and _bytes(s.get("memswap_limit", 0)) != _bytes(s["mem_limit"])]
    assert not bad, "memswap_limit != mem_limit on: %s" % ", ".join(sorted(bad))


def test_every_celery_worker_bounds_its_children(services):
    """`--max-memory-per-child` is what makes a 4.5 GiB child unreachable rather than unlikely."""
    seen = 0
    for name, spec in services.items():
        cmd = str(spec.get("command") or "")
        if "celery" not in cmd or "worker" not in cmd:
            continue
        seen += 1
        if "--pool=threads" in cmd:
            continue
        m = re.search(r"--max-memory-per-child=(\d+)", cmd)
        assert m, "%s runs a Celery worker with no --max-memory-per-child" % name
        kib = int(m.group(1))
        assert kib <= 1_500_000, \
            "%s allows a %.2f GiB child — the size that took the VM down" % (name, kib / 1024 ** 2)

        conc = re.search(r"--concurrency[= ](\d+)", cmd)
        n = max(1, int(conc.group(1))) if conc and conc.group(1) != "0" else 1
        # + the pool parent (and beat, where -B is set) + one task's growth PAST the cap: Celery
        # checks the cap only between tasks, so a child at 1.3 GiB mid-task in a 1.6 GiB container
        # was OOM-killed hundreds of times (each kill leaking its graph sessions) before it was
        # ever recycled.
        need = n * kib * 1024 + 512 * 1024 ** 2
        assert need < _bytes(spec["mem_limit"]), (
            "%s: %d child(ren) x %.0f MiB + parent = %.0f MiB, over mem_limit %.0f MiB — the "
            "container is killed before a child is ever recycled"
            % (name, n, kib / 1024, need / 1024 ** 2, _bytes(spec["mem_limit"]) / 1024 ** 2))
    assert seen >= 4, "expected the four Celery workers, found %d" % seen


def test_the_sum_is_documented(services):
    """A budget nobody can see is a budget nobody maintains."""
    doc = COMPOSE.parent / "docs" / "memory-budget.md"
    assert doc.exists(), "docs/memory-budget.md is missing"
    total = sum(sp and _bytes(sp["mem_limit"]) for sp in services.values() if sp.get("mem_limit"))
    row = re.search(r"\*\*sum\*\*[^\n]*", doc.read_text(encoding="utf-8"))
    assert row, "memory-budget.md no longer has a sum row"
    figs = re.findall(r"\*\*([\d.]+) GiB\*\*", row.group(0))
    assert len(figs) >= 2, "the sum row must state a measured AND a ceiling total"
    assert abs(float(figs[-1]) - total / 1024 ** 3) < 0.35, (
        "the documented ceiling sum (%s GiB) has drifted from the compose file (%.2f GiB)"
        % (figs[-1], total / 1024 ** 3))
