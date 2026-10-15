"""Build the regression report from EXECUTED results only.

    python -m tests.regression.framework.report <run-dir>      # → report.json, junit.xml, report.md

Inputs: every `results-*.jsonl` the backend suite wrote in <run-dir> (one line per executed scenario)
and <run-dir>/ui/playwright.json from the UI suite. The coverage matrix (matrix.csv) is read only to
say what was DEFINED; everything reported as covered comes from a result line.
"""
from __future__ import annotations

import collections
import csv
import json
import pathlib
import sys
import time
from xml.sax.saxutils import escape

from tests.regression.framework.defects import CODE_REVIEW_FINDINGS, DEFECTS, OBSERVATIONS, triage

GAPS = [
    ("Live dependency outages", "PostgreSQL, NebulaGraph, Qdrant, Redis and vLLM were not taken down; failure handling is covered "
     "with unreachable endpoints (connectors, models), worker SIGKILLs, and in-process fakes."),
    ("Unreadable folder during a sync", "A folder that disappears AFTER files were synced from it (D37) is covered by unit "
     "tests only: making a mounted folder vanish mid-run was not done against the live stack."),
    ("Global configuration changes", "PUT /config was executed only to toggle allow_local_files for the sync window (restored "
     "after); capability switches were never executed."),
    ("Shared-queue operations", "DELETE /reset, POST /queue/purge and POST /worker/reset were never executed live; the D8 fix "
     "is verified by unit tests."),
    ("Model-backed extraction quality", "LLM/NER extractor OUTPUT is not asserted; deterministic mechanisms are."),
    ("SQL sources", "No live source database was provisioned; SQL connectors are tested against unreachable hosts."),
    ("UI depth", "Connections, ingestion wizard, assistant, models and settings pages have render checks per role, not full "
     "UI-driven workflows (pipeline builder, mapper, curation, review and quarantine now do)."),
    ("Worker crash tests", "Run from the host (they need the docker CLI); in CI they need the host step in bin/opencortyx."),
    ("Formats without writers", ".xls, .doc and .msg files could not be generated in the test image (BLOCKED)."),
    ("Volume", "The largest executed upload is 5,000 JSON records / a 20 MB corpus slice; no load or soak testing."),
    ("Browsers", "UI suite runs on Chromium at 1440×900 only."),
]


def load(run: pathlib.Path) -> tuple[list[dict], list[dict]]:
    raw = [json.loads(line) for f in sorted(run.glob("results-*.jsonl")) for line in f.open(encoding="utf-8")]
    # a scenario re-executed after a test fix keeps only its LATEST result; the count is reported
    latest = {}
    for r in sorted(raw, key=lambda r: r.get("finished_at", 0)):
        latest[r["test_id"]] = r
    backend = list(latest.values())
    load.reexecuted = len(raw) - len(backend)
    ui = []
    for pw in sorted(run.glob("ui*/playwright.json"), key=lambda f: (f.parent.name != "ui", f.stat().st_mtime)):  # later runs override by title
        if not pw.exists():
            continue
        def walk(suite, path):
            for s in suite.get("suites", []):
                yield from walk(s, path + [s.get("title", "")])
            for spec in suite.get("specs", []):
                for t in spec.get("tests", []):
                    res = (t.get("results") or [{}])[-1]
                    status = {"passed": "passed", "failed": "failed", "timedOut": "failed", "skipped": "skipped",
                              "interrupted": "blocked"}.get(res.get("status", "skipped"), "failed")
                    title = spec["title"]
                    parts = title.split(" ", 2)
                    yield {"test_id": f"{parts[0]}-{abs(hash(spec.get('file', '') + title)) % 10**10:010d}",
                           "family": parts[0] if parts[0].startswith("UI-") else "UI", "module": "ui",
                           "feature": spec.get("file", "").split("/")[0], "workflow": title, "input": "",
                           "config": "chromium 1440x900", "expected": "as described", "type": "ui",
                           "status": status, "duration_s": round(res.get("duration", 0) / 1000, 2),
                           "failure_reason": " | ".join(e.get("message", "")[:400] for e in res.get("errors", [])),
                           "attachments": [a.get("path") for a in res.get("attachments", []) if a.get("path")],
                           "checks": []}
        for s in json.loads(pw.read_text(encoding="utf-8")).get("suites", []):
            ui.extend(walk(s, [s.get("title", "")]))
    by_title = {}
    for r in ui:
        by_title[r["workflow"]] = r
    load.ui_reexecuted = len(ui) - len(by_title)
    return backend, list(by_title.values())


def summarise(run: pathlib.Path) -> dict:
    backend, ui = load(run)
    rows = backend + ui
    by = lambda key: collections.Counter((r.get(key), r["status"]) for r in rows)
    status = collections.Counter(r["status"] for r in rows)
    defined = 0
    matrix = run / "matrix.csv"
    if matrix.exists():
        with matrix.open(encoding="utf-8") as f:
            defined = sum(1 for _ in csv.DictReader(f))
    failures = [r for r in rows if r["status"] in ("failed", "error")]
    linked, untriaged = triage(failures)

    def table(key):
        out = collections.defaultdict(lambda: collections.Counter())
        for (k, st), n in by(key).items():
            out[k][st] += n
        return {k: dict(v) for k, v in sorted(out.items(), key=lambda kv: str(kv[0]))}

    from tests.regression.framework.matrix import route_inventory
    ops = {(o["method"], o["path"]) for o in route_inventory()}
    touched = {tuple(r["workflow"].split(" ", 1)) for r in backend
               if r.get("family") in ("AUTH", "CONT", "TENA") and r["status"] in ("passed", "failed")}
    from opencortyx.ingest.formats.catalog import FORMATS
    fmt = {}
    for ext in FORMATS:
        reader = [r for r in backend if r.get("feature") == f"reader:{ext}"]
        e2e = [r for r in backend if r.get("family") == "E2EI" and r.get("input", "").startswith(f".{ext} ")]
        fmt[ext] = {"route": FORMATS[ext].route,
                    "reader": dict(collections.Counter(r["status"] for r in reader)),
                    "e2e": dict(collections.Counter(r["status"] for r in e2e))}
    nav = [r for r in ui if r["family"] == "UI-NAV"]
    ui_status = collections.Counter(r["status"] for r in ui)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "defined_scenarios": defined,
        "executed": len(rows), "backend_executed": len(backend), "ui_executed": len(ui),
        "reexecuted_after_test_fix": getattr(load, "reexecuted", 0) + getattr(load, "ui_reexecuted", 0),
        "status": dict(status),
        "by_family": table("family"), "by_module_feature": {f"{m}/{f}": v for (m, f), v in
                                                              _pairs(rows).items()},
        "by_type": table("type"),
        "api": {"operations": len(ops), "exercised": len(touched & ops),
                "not_exercised": sorted(f"{m} {p}" for m, p in ops - touched)},
        "formats": fmt,
        "ui": {"total": len(ui), "status": dict(ui_status), "pages_x_roles": len(nav),
               "passed": sum(r["status"] == "passed" for r in nav),
               "workflows": [f'{r["workflow"]} — {r["status"]}' for r in ui if r["family"] != "UI-NAV"]},
        "defects": [{"id": d, "severity": s, "component": c, "title": t, "note": n,
                     "failing_tests": len(linked[d]), "examples": [r["test_id"] for r in linked[d][:5]]}
                    for d, s, c, t, _rules, n in DEFECTS if linked[d]],
        "code_review_findings": [dict(zip(("id", "severity", "component", "title", "note", "fixed"), f)) for f in CODE_REVIEW_FINDINGS],
        "observations": [dict(zip(("id", "text"), o)) for o in OBSERVATIONS],
        "untriaged_failures": [{"test_id": r["test_id"], "family": r.get("family"), "workflow": r.get("workflow"),
                                "input": r.get("input"), "reason": r.get("failure_reason", "")[:300]} for r in untriaged],
        "gaps": [{"area": a, "detail": d} for a, d in GAPS],
    }


def _pairs(rows):
    out = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        out[(r.get("module"), r.get("feature"))][r["status"]] += 1
    return {k: dict(v) for k, v in sorted(out.items(), key=lambda kv: str(kv[0]))}


def verify_fixes(run: pathlib.Path, baseline: pathlib.Path) -> list[dict]:
    """Per defect: scenarios that FAILED in `baseline` and what the SAME scenarios did in `run`. A defect
    counts as fixed only when every one of its previously failing scenarios passes now."""
    before, _ = load(baseline)
    results = load(run)[0]
    now = {r["test_id"]: r for r in results}
    # An id includes the route's guard level, so a fix that changes a guard (D9) renames the scenario:
    # the same scenario is then found by its title.
    by_title = {r.get("scenario"): r for r in results if r.get("scenario")}
    linked, _u = triage([r for r in before if r["status"] in ("failed", "error")])
    out = []
    for did, sev, comp, title, _rules, _note in DEFECTS:
        was = linked.get(did, [])
        if not was:
            continue
        ids = [r["test_id"] for r in was]
        states = collections.Counter((now.get(r["test_id"]) or by_title.get(r.get("scenario")) or {}).get("status", "not_rerun")
                                     for r in was)
        out.append({"id": did, "severity": sev, "component": comp, "title": title, "previously_failing": len(ids),
                    "now_passing": states.get("passed", 0), "still_failing": states.get("failed", 0) + states.get("error", 0),
                    "not_rerun": states.get("not_rerun", 0) + states.get("blocked", 0) + states.get("skipped", 0),
                    "fixed": states.get("passed", 0) == len(ids)})
    return out


def verdict(s: dict) -> tuple[str, list[str]]:
    reasons = []
    high = [d for d in s["defects"] if d["severity"] == "High"] + [f for f in s["code_review_findings"] if f["severity"] == "High" and not f.get("fixed")]
    if high:
        reasons.append(f"{len(high)} open High-severity defect(s): {', '.join(d['id'] for d in high)}")
    if s["untriaged_failures"]:
        reasons.append(f"{len(s['untriaged_failures'])} failure(s) not yet explained by a known defect")
    ex = s["status"].get("passed", 0) + s["status"].get("failed", 0) + s["status"].get("error", 0)
    rate = s["status"].get("passed", 0) / ex if ex else 0
    reasons.append(f"pass rate {rate:.2%} of {ex:,} executed-and-decided scenarios")
    return ("NOT READY" if high or rate < 0.98 else "READY WITH KNOWN ISSUES" if s["defects"] else "READY"), reasons


def junit(run: pathlib.Path) -> str:
    backend, ui = load(run)
    cases = []
    for r in backend + ui:
        body = ""
        if r["status"] in ("failed", "error"):
            body = f'<failure message="{escape(r.get("failure_reason", "")[:500], {chr(34): "&quot;"})}"/>'
        elif r["status"] in ("skipped", "blocked"):
            body = f'<skipped message="{escape(r["status"])}"/>'
        name = escape(f'{r["test_id"]} {r.get("workflow", "")} {r.get("input", "")}', {'"': "&quot;"})
        cases.append(f'<testcase classname="{escape(str(r.get("family")))}" name="{name}" time="{r.get("duration_s", 0)}">{body}</testcase>')
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="opencortyx-regression" tests="{len(cases)}">' + "".join(cases) + "</testsuite>\n"


def markdown(s: dict) -> str:
    v, why = verdict(s)
    st = s["status"]
    lines = [f"# OpenCortyx regression report ({s['generated_at']})", "",
             f"**Verdict: {v}** — " + "; ".join(why), "",
             "| defined | executed | passed | failed | blocked | skipped |", "|---|---|---|---|---|---|",
             f"| {s['defined_scenarios']:,} | {s['executed']:,} | {st.get('passed', 0):,} | {st.get('failed', 0) + st.get('error', 0):,} "
             f"| {st.get('blocked', 0):,} | {st.get('skipped', 0):,} |", "",
             f"API operations exercised: {s['api']['exercised']}/{s['api']['operations']}  ·  UI pages×roles: "
             f"{s['ui']['passed']}/{s['ui']['pages_x_roles']} passed", "", "## Defects", ""]
    lines += [f"- **{d['id']}** ({d['severity']}, {d['component']}): {d['title']} — {d['failing_tests']} failing test(s)"
              for d in s["defects"]]
    lines += [f"- **{f['id']}** ({f['severity']}, found by code review, not by a test): {f['title']}"
              + (f" — FIXED: {f['fixed']}" if f.get("fixed") else "") for f in s["code_review_findings"]]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    run = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/app/test-results/full")
    s = summarise(run)
    s["fixes"] = verify_fixes(run, pathlib.Path(sys.argv[2])) if len(sys.argv) > 2 else []
    s["verdict"], s["verdict_reasons"] = verdict(s)
    (run / "report.json").write_text(json.dumps(s, indent=1, default=str))
    (run / "junit.xml").write_text(junit(run))
    (run / "report.md").write_text(markdown(s))
    from tests.regression.framework.report_html import render
    (run / "report.html").write_text(render(s), encoding="utf-8")
    print(markdown(s))
    import os
    # CI gate: known defects are tracked in the register; a failure it does not explain is a regression.
    if os.environ.get("OPENCORTYX_REGRESSION_GATE") == "1" and s["untriaged_failures"]:
        print(f"GATE: {len(s['untriaged_failures'])} untriaged failure(s)")
        sys.exit(1)
