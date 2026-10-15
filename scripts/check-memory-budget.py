"""Does the memory budget add up? Run it before trusting the stack with a long job.

A `mem_limit` is a PROMISE that a container may use that much. Per-container limits protect
containers from each other; they do nothing about the VM running out. When the promises sum to more
than the VM has, they cannot all be kept, and the kernel resolves that by killing something it
picks — which is exactly what `constraint=CONSTRAINT_NONE ... global_oom` in this VM's log was:
two Celery children, 4.45 GiB and 4.51 GiB, killed while every per-container limit was "fine".

So the number that matters is the SUM, and this fails when it exceeds what the VM has.

    python scripts/check-memory-budget.py            # check
    python scripts/check-memory-budget.py --stats    # check, and show live usage beside each ceiling

Exit code 1 means the budget does not fit. The fix is one of:
  * bring a ceiling down to its measured working set (`--stats` shows you which have slack), or
  * give the VM a share proportional to the stack (%USERPROFILE%\\.wslconfig -> memory=, then
    `wsl --shutdown`).
Never the third option of raising a ceiling so a container stops being killed: that converts a
container-level kill you can see into a VM-level kill that lands on something unrelated.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[1] / "compose.yaml"
UNIT = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
#: the VM needs room for its own kernel, page cache and the Docker daemon
VM_RESERVE_GIB = 0.6


def to_bytes(v: str) -> int:
    m = re.match(r"([\d.]+)\s*([a-zA-Z]*)", v.strip())
    if not m:
        return 0
    return int(float(m.group(1)) * UNIT.get((m.group(2) or "b").lower()[:1], 1))


def services() -> dict:
    """service -> mem_limit in bytes (None when unbounded). From `compose config`, so anything the
    file computes — env interpolation, overrides, profiles — is already resolved."""
    out = subprocess.run(["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("could not read the compose file:\n%s" % out.stderr.strip())
    cfg = json.loads(out.stdout)
    res = {}
    for name, spec in (cfg.get("services") or {}).items():
        lim = spec.get("mem_limit")
        res[name] = to_bytes(str(lim)) if lim else None
    CHILD_CAPS.clear()
    for name, spec in (cfg.get("services") or {}).items():
        cmd = spec.get("command")
        cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
        m = re.search(r"--max-memory-per-child=(\d+)", cmd)
        if m and "celery" in cmd:
            conc = re.search(r"--concurrency[= ](\d+)", cmd)
            CHILD_CAPS[name] = (int(m.group(1)) * 1024, max(1, int(conc.group(1))) if conc else 1)
    return res


#: service -> (per-child cap in bytes, concurrency) for Celery workers, filled by `services()`.
CHILD_CAPS: dict = {}
#: What a worker container must hold beyond its children's caps: the pool parent (and beat) plus
#: one task's growth past the cap, which Celery only checks between tasks.
CHILD_HEADROOM = 512 * 1024 ** 2


def vm_bytes() -> int:
    out = subprocess.run(["docker", "info", "--format", "{{.MemTotal}}"],
                         capture_output=True, text=True)
    return int(out.stdout.strip()) if out.returncode == 0 else 0


def live_usage() -> dict:
    out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
                         capture_output=True, text=True)
    used = {}
    for line in out.stdout.splitlines():
        if "\t" not in line:
            continue
        name, usage = line.split("\t", 1)
        used[name] = to_bytes(usage.split("/")[0].strip().replace("iB", "b"))
    return used


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="show live usage beside each ceiling")
    args = ap.parse_args()

    svcs = services()
    vm = vm_bytes()
    used = live_usage() if args.stats else {}
    # Compose lowercases the project name and strips anything that is not [a-z0-9_-], so deriving
    # container names from the directory verbatim finds nothing whenever the checkout has a capital
    # in it — `--stats` then reports every service as "(not running)" beside a running stack.
    project = os.environ.get("COMPOSE_PROJECT_NAME") or re.sub(
        r"[^a-z0-9_-]", "", COMPOSE.parent.name.lower())

    print("%-22s %12s %s" % ("SERVICE", "CEILING", "IN USE" if args.stats else ""))
    print("-" * (52 if args.stats else 36))
    total, unbounded = 0, []
    for name in sorted(svcs):
        lim = svcs[name]
        if lim is None:
            unbounded.append(name)
            print("%-22s %12s   <- can take the whole VM" % (name, "NONE"))
            continue
        total += lim
        cell = ""
        if args.stats:
            u = used.get("%s-%s-1" % (project, name))
            cell = ("%9.0f MiB  (%2.0f%%)" % (u / 1024 ** 2, u / lim * 100)) if u else "  (not running)"
        print("%-22s %11.0fM %s" % (name, lim / 1024 ** 2, cell))

    budget = vm / 1024 ** 3 - VM_RESERVE_GIB
    print("-" * (52 if args.stats else 36))
    print("sum of ceilings        : %6.2f GiB" % (total / 1024 ** 3))
    print("VM has                 : %6.2f GiB  (less %.1f reserved for the VM itself)"
          % (vm / 1024 ** 3, VM_RESERVE_GIB))
    print("usable budget          : %6.2f GiB" % budget)

    ok = True
    if unbounded:
        ok = False
        print("\nFAIL: unbounded service(s): %s" % ", ".join(unbounded))
        print("      One container with no ceiling makes every other ceiling meaningless.")
    if total / 1024 ** 3 > budget:
        ok = False
        over = total / 1024 ** 3 - budget
        print("\nFAIL: the budget is over by %.2f GiB (%.1fx)."
              % (over, (total / 1024 ** 3) / max(budget, 0.01)))
        print("      Every container can be within its own limit and the VM still runs out.")
        print("      Raise the VM (Docker Desktop -> Settings -> Resources -> Memory on macOS; memory= in")
        print("      %USERPROFILE%\\.wslconfig on Windows), or lower *_MEM_LIMIT values in .env toward")
        print("      the measured working sets — `--stats` shows which have slack.")
    for name, (cap, conc) in sorted(CHILD_CAPS.items()):
        lim = svcs.get(name)
        if lim and conc * cap + CHILD_HEADROOM > lim:
            ok = False
            print("\nFAIL: %s: %d child(ren) x %.0f MiB + %.0f MiB headroom = %.0f MiB, over its "
                  "limit of %.0f MiB." % (name, conc, cap / 1024 ** 2, CHILD_HEADROOM / 1024 ** 2,
                                          (conc * cap + CHILD_HEADROOM) / 1024 ** 2, lim / 1024 ** 2))
            print("      The container is OOM-killed mid-task before a child is ever recycled: raise "
                  "its *_MEM_LIMIT or lower its *_MAX_MEMORY_KB in .env.")
    if ok:
        print("\nOK: every container is bounded and the ceilings fit the VM.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
