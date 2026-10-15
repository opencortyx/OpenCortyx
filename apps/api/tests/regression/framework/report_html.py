"""The regression report as one self-contained HTML page (written beside report.json by report.py)."""
from __future__ import annotations

from html import escape as e

CSS = """
:root{--bg:#f4f6f5;--panel:#ffffff;--ink:#17201d;--muted:#56625d;--rule:#d9dfdc;--accent:#0e6b66;
--pass:#2b7a48;--fail:#b3261e;--block:#94600a;--skip:#6b7470;--chip:#e9eeec;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#101513;--panel:#171e1b;--ink:#e2e8e5;
--muted:#9aa7a1;--rule:#2a3430;--accent:#4fb3ab;--pass:#5cbf80;--fail:#f07a70;--block:#e0a84a;--skip:#8b9591;--chip:#222b28;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#101513;--panel:#171e1b;--ink:#e2e8e5;--muted:#9aa7a1;--rule:#2a3430;--accent:#4fb3ab;
--pass:#5cbf80;--fail:#f07a70;--block:#e0a84a;--skip:#8b9591;--chip:#222b28;color-scheme:dark}
body{background:var(--bg);color:var(--ink);font:15px/1.55 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
padding-block:0;padding-inline:16px}
.wrap{max-width:1120px;margin:0 auto;padding-block:40px 64px;display:grid;gap:40px}
h1,h2,h3{font-family:"Bricolage Grotesque","IBM Plex Sans",system-ui,sans-serif;text-wrap:balance;margin:0;line-height:1.15}
h1{font-size:clamp(28px,4vw,40px);font-weight:700;letter-spacing:-.01em}
h2{font-size:22px;font-weight:650}
h3{font-size:16px;font-weight:600}
p{margin:0;max-width:72ch}
.mono,code,td.num{font-family:"IBM Plex Mono",ui-monospace,Consolas,monospace;font-variant-numeric:tabular-nums}
code{font-size:.9em;background:var(--chip);padding:1px 5px;border-radius:4px}
.eyebrow{font:600 12px/1 "IBM Plex Mono",monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
header{display:grid;gap:14px}
.verdict{display:inline-flex;align-items:center;gap:10px;font:700 14px/1 "IBM Plex Mono",monospace;letter-spacing:.06em;
padding:9px 14px;border-radius:6px;border:1.5px solid currentColor;width:max-content}
.v-bad{color:var(--fail)}.v-warn{color:var(--block)}.v-ok{color:var(--pass)}
.why{margin:0;padding-left:20px;color:var(--muted)}
.figures{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));border:1px solid var(--rule);border-radius:10px;
background:var(--panel);overflow:hidden}
.fig{padding:16px 18px;border-right:1px solid var(--rule);display:grid;gap:4px}
.fig:last-child{border-right:0}
.fig b{font:600 26px/1.1 "IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
.fig span{font-size:12px;color:var(--muted)}
.c-pass{color:var(--pass)}.c-fail{color:var(--fail)}.c-block{color:var(--block)}.c-skip{color:var(--skip)}
section{display:grid;gap:14px}
.lede{color:var(--muted)}
.tbl{overflow-x:auto;border:1px solid var(--rule);border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--rule);vertical-align:top}
th{font:600 11px/1.2 "IBM Plex Mono",monospace;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);background:var(--panel);
position:sticky;top:0}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;white-space:nowrap}
.sev{display:inline-block;font:600 11px/1 "IBM Plex Mono",monospace;padding:4px 7px;border-radius:4px;letter-spacing:.04em}
.sev-High{background:color-mix(in srgb,var(--fail) 16%,transparent);color:var(--fail)}
.sev-Medium{background:color-mix(in srgb,var(--block) 16%,transparent);color:var(--block)}
.sev-Low{background:var(--chip);color:var(--muted)}
.ids{font:12px/1.5 "IBM Plex Mono",monospace;color:var(--muted);word-break:break-all}
.bar{display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--chip);min-width:120px}
.bar i{display:block}
.b-pass{background:var(--pass)}.b-fail{background:var(--fail)}.b-block{background:var(--block)}.b-skip{background:var(--skip)}
.grid39{display:grid;grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:8px}
.cell{border:1px solid var(--rule);border-radius:8px;background:var(--panel);padding:10px;display:grid;gap:6px}
.cell b{font:600 14px/1 "IBM Plex Mono",monospace}
.cell small{font-size:11px;color:var(--muted)}
.dots{display:flex;gap:10px;font:11px/1 "IBM Plex Mono",monospace;color:var(--muted)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:0}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
.note{border-left:3px solid var(--block);padding:10px 14px;background:var(--panel);border-radius:0 8px 8px 0}
nav{display:flex;flex-wrap:wrap;gap:8px 16px;font-size:13px}
nav a{color:var(--accent);text-decoration:none}nav a:hover{text-decoration:underline}
a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
footer{color:var(--muted);font-size:13px;display:grid;gap:6px}
"""


def _bar(c: dict) -> str:
    total = sum(c.values()) or 1
    seg = "".join(f'<i class="b-{k}" style="width:{100 * c.get(n, 0) / total:.2f}%"></i>'
                  for k, n in (("pass", "passed"), ("fail", "failed"), ("block", "blocked"), ("skip", "skipped")))
    return f'<div class="bar" role="img" aria-label="{e(str(c))}">{seg}</div>'


def _dot(c: dict) -> str:
    if not c:
        return '<span class="dot" style="background:var(--chip);border:1px solid var(--rule)"></span>none'
    if c.get("failed"):
        return f'<span class="dot b-fail"></span>{c.get("failed")} fail'
    if c.get("passed"):
        return f'<span class="dot b-pass"></span>{c.get("passed")} pass'
    return '<span class="dot b-block"></span>blocked'


FAMILY_NAMES = {
    "FMTS": "Semi-structured formats × data variants", "FMTT": "Table formats × data variants",
    "FMTD": "Document formats × variants", "CHNK": "Chunking strategies × sizes × overlaps",
    "ROUT": "Routing: extension × content × override", "CORP": "Real files through the default walker",
    "WCFG": "Real files × pairwise walker configurations", "SCHM": "Structure inference on real records",
    "XMLC": "W3C XML conformance suite", "AUTH": "Every API operation × 9 kinds of caller",
    "TENA": "Tenant isolation (differential)", "CONT": "Request contracts from the OpenAPI schema",
    "E2EI": "Upload → worker → graph → API, every format", "EXTR": "Custom extractors", "GRPH": "Nodes, edges, relationships",
    "ACCS": "Users, roles, API keys, sign-in", "CURA": "Curation", "ONTO": "Ontology per organization", "RSLV": "Resolution after ingest",
    "PIPE": "Pipelines", "MAPS": "Schema mappings", "CONN": "Connectors", "INGS": "Ingestions (state machine)",
    "MODL": "Model registry", "QUAR": "Quarantine", "UI-NAV": "UI: every page × every role",
    "UI-ADD": "UI: add data, checked against the API", "UI-ACC": "UI: access and sign-in",
    "UI-WF": "UI: pipeline builder, mapper, curation, review, quarantine workflows",
    "SYNC": "Scheduled sync over real wall-clock minutes", "RCVR": "Worker SIGKILL and redelivery",
}


def render(s: dict) -> str:
    st = s["status"]
    decided = st.get("passed", 0) + st.get("failed", 0) + st.get("error", 0)
    rate = st.get("passed", 0) / decided if decided else 0
    vclass = {"NOT READY": "v-bad", "READY WITH KNOWN ISSUES": "v-warn"}.get(s["verdict"], "v-ok")
    fam_rows = "".join(
        f'<tr><td><code>{e(k)}</code></td><td>{e(FAMILY_NAMES.get(k, k))}</td><td class="num">{sum(v.values()):,}</td>'
        f'<td class="num c-pass">{v.get("passed", 0):,}</td><td class="num c-fail">{v.get("failed", 0) + v.get("error", 0):,}</td>'
        f'<td class="num c-block">{v.get("blocked", 0):,}</td><td>{_bar(v)}</td></tr>'
        for k, v in s["by_family"].items())
    mod_rows = "".join(
        f'<tr><td>{e(k)}</td><td class="num">{sum(v.values()):,}</td><td class="num c-pass">{v.get("passed", 0):,}</td>'
        f'<td class="num c-fail">{v.get("failed", 0) + v.get("error", 0):,}</td><td class="num c-block">{v.get("blocked", 0):,}</td>'
        f'<td>{_bar(v)}</td></tr>' for k, v in s["by_module_feature"].items())
    type_rows = "".join(f'<tr><td>{e(str(k))}</td><td class="num">{sum(v.values()):,}</td><td class="num c-pass">{v.get("passed", 0):,}</td>'
                        f'<td class="num c-fail">{v.get("failed", 0):,}</td><td>{_bar(v)}</td></tr>' for k, v in s["by_type"].items())
    sev_order = {"High": 0, "Medium": 1, "Low": 2}
    defects = sorted(s["defects"], key=lambda d: (sev_order[d["severity"]], int(d["id"][1:])))
    def_rows = "".join(
        f'<tr><td class="mono">{e(d["id"])}</td><td><span class="sev sev-{d["severity"]}">{e(d["severity"])}</span></td>'
        f'<td>{e(d["title"])}{"<br><span class=ids>" + e(d["note"]) + "</span>" if d["note"] else ""}</td>'
        f'<td class="mono" style="font-size:12px">{e(d["component"])}</td><td class="num">{d["failing_tests"]}</td>'
        f'<td class="ids">{" ".join(e(x) for x in d["examples"][:3])}</td></tr>' for d in defects)
    cr_rows = "".join(f'<tr><td class="mono">{e(f["id"])}</td><td><span class="sev sev-{f["severity"]}">{e(f["severity"])}</span></td>'
                      f'<td>{e(f["title"])}<br><span class="ids">{e(f["note"])}{(" — FIXED: " + e(f["fixed"])) if f.get("fixed") else ""}</span></td></tr>' for f in s["code_review_findings"])
    by_route = {"structured": [], "semistructured": [], "document": []}
    for ext, f in s["formats"].items():
        by_route[f["route"]].append((ext, f))
    grids = "".join(
        f'<div><h3>{title} <span class="eyebrow">{len(by_route[r])} extensions</span></h3><div class="grid39" style="margin-top:10px">'
        + "".join(f'<div class="cell"><b>.{e(x)}</b><div class="dots"><span>{_dot(f["reader"])}</span></div>'
                  f'<small>reader</small><div class="dots"><span>{_dot(f["e2e"])}</span></div><small>upload → graph</small></div>'
                  for x, f in sorted(by_route[r])) + "</div></div>"
        for r, title in (("structured", "Tables"), ("semistructured", "Records"), ("document", "Documents")))
    ui = s["ui"]
    ui_wf = "".join(f"<li>{e(w)}</li>" for w in ui["workflows"])
    gaps = "".join(f"<tr><td>{e(g['area'])}</td><td>{e(g['detail'])}</td></tr>" for g in s["gaps"])
    obs = "".join(f"<li><b class='mono'>{e(o['id'])}</b> {e(o['text'])}</li>" for o in s["observations"])
    why = "".join(f"<li>{e(r)}</li>" for r in s["verdict_reasons"])
    high = [d for d in defects if d["severity"] == "High"]
    fix_rows = "".join(
        f'<tr><td class="mono">{e(f["id"])}</td><td><span class="sev sev-{f["severity"]}">{e(f["severity"])}</span></td>'
        f'<td>{e(f["title"])}</td><td class="num">{f["previously_failing"]}</td><td class="num c-pass">{f["now_passing"]}</td>'
        f'<td class="num c-fail">{f["still_failing"]}</td><td>{"<b class=c-pass>fixed</b>" if f["fixed"] else "<b class=c-fail>not fixed</b>"}</td></tr>'
        for f in s.get("fixes", []))
    fixes_section = (f'''<section id="fixes"><h2>Fixes, verified against the previous run</h2>
  <p class="lede">Each row: scenarios that failed in the previous run for that defect, and what the same scenarios
  did in this run: matched by ID, or by the same scenario title where a fix changed a route guard and so its ID.
  A defect is marked fixed only when every one of them passes now.</p>
  <div class="tbl"><table><thead><tr><th>ID</th><th>Severity</th><th>Defect</th><th class="num">Failed before</th>
  <th class="num">Pass now</th><th class="num">Still fail</th><th>Result</th></tr></thead><tbody>{fix_rows}</tbody></table></div>
</section>''' if fix_rows else "")
    return f"""<title>OpenCortyx Regression Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
<header>
  <span class="eyebrow">OpenCortyx · regression run · {e(s["generated_at"])} UTC</span>
  <h1>Regression report</h1>
  <span class="verdict {vclass}">{e(s["verdict"])}</span>
  <ul class="why">{why}</ul>
  <nav aria-label="Sections"><a href="#fixes">Fixes</a><a href="#defects">Defects</a><a href="#coverage">Coverage</a><a href="#formats">Formats</a>
  <a href="#api">API</a><a href="#ui">UI</a><a href="#gaps">Gaps</a><a href="#method">Method</a></nav>
</header>
<div class="figures">
  <div class="fig"><b>{s["defined_scenarios"] + ui["total"]:,}</b><span>scenarios defined</span></div>
  <div class="fig"><b>{s["executed"]:,}</b><span>executed</span></div>
  <div class="fig"><b class="c-pass">{st.get("passed", 0):,}</b><span>passed</span></div>
  <div class="fig"><b class="c-fail">{st.get("failed", 0) + st.get("error", 0):,}</b><span>failed</span></div>
  <div class="fig"><b class="c-block">{st.get("blocked", 0):,}</b><span>blocked</span></div>
  <div class="fig"><b>{rate:.2%}</b><span>pass rate of decided</span></div>
</div>
{fixes_section}
<section id="defects">
  <h2>Defects found</h2>
  <p class="lede">{len(defects)} defects, each shown by executed failing scenarios (IDs are the test IDs in the results).
  {len(high)} are High severity. {len(s["untriaged_failures"])} failures are not explained by a defect below.</p>
  <div class="tbl"><table><thead><tr><th>ID</th><th>Severity</th><th>Defect</th><th>Component</th><th class="num">Failing tests</th><th>Example scenarios</th></tr></thead>
  <tbody>{def_rows}</tbody></table></div>
  <h3>Found by code review, not by a test (each says how a fix was verified)</h3>
  <div class="tbl"><table><tbody>{cr_rows}</tbody></table></div>
  <div class="note"><b>Observations, not defects:</b><ul class="why" style="margin-top:6px">{obs}</ul></div>
</section>
<section id="coverage">
  <h2>Coverage by scenario family</h2>
  <p class="lede">Every family is derived from a real inventory (format catalog, route table, OpenAPI schema, corpus on disk,
  W3C catalogue) crossed with the configuration dimensions that change the expected result.</p>
  <div class="tbl"><table><thead><tr><th>Family</th><th>What it covers</th><th class="num">Executed</th><th class="num">Passed</th>
  <th class="num">Failed</th><th class="num">Blocked</th><th>Result</th></tr></thead><tbody>{fam_rows}</tbody></table></div>
  <div class="two">
    <div class="tbl"><table><thead><tr><th>Module / feature</th><th class="num">Run</th><th class="num">Pass</th><th class="num">Fail</th>
    <th class="num">Blk</th><th></th></tr></thead><tbody>{mod_rows}</tbody></table></div>
    <div class="tbl"><table><thead><tr><th>Test type</th><th class="num">Run</th><th class="num">Pass</th><th class="num">Fail</th><th></th></tr></thead>
    <tbody>{type_rows}</tbody></table></div>
  </div>
</section>
<section id="formats">
  <h2>The 39 file formats</h2>
  <p class="lede">For each catalogued extension: its reader tested with generated data variants, and the full upload → worker →
  graph → API path. Blocked means no writer for that format exists in the test image, so no real file could be produced.</p>
  {grids}
</section>
<section id="api">
  <h2>API</h2>
  <p class="lede"><b class="mono">{s["api"]["exercised"]}/{s["api"]["operations"]}</b> operations exercised by at least one executed scenario
  (permission matrix, request contracts, tenant isolation), plus the feature suites that drive them as workflows.</p>
</section>
<section id="ui">
  <h2>UI</h2>
  <p class="lede"><b class="mono">{ui["passed"]}/{ui["pages_x_roles"]}</b> page × role checks passed (every dashboard page as admin, editor
  and viewer: renders, no page error, no API 5xx). Workflows driven through the browser and verified against the API:</p>
  <ul class="why">{ui_wf}</ul>
  <p class="lede">Failed UI tests keep a screenshot, video and Playwright trace under <code>test-results/&lt;run&gt;/ui/artifacts</code>.</p>
</section>
<section id="gaps">
  <h2>Not covered by this run</h2>
  <div class="tbl"><table><tbody>{gaps}</tbody></table></div>
</section>
<section id="method">
  <h2>Method</h2>
  <p>Every scenario runs against the live stack as a throwaway tenant that is deleted afterwards; platform-wide operations
  (<code>DELETE /reset</code>, queue purge, worker reset, global config) are never sent by an authorised caller. Each result records its
  test ID, scenario, prerequisites, generated data, steps, expected and actual result, the validations of resulting data, status,
  failure reason and logs. {s.get("reexecuted_after_test_fix", 0):,} scenarios were re-executed after a defect in the test itself was fixed;
  their latest result is the one counted.</p>
  <p>Re-run: <code>bin/opencortyx regression &lt;run&gt;</code> · CI: <code>.github/workflows/regression.yml</code>, which fails only on a failure
  the defect register does not explain.</p>
</section>
<footer><span>Results: <code>apps/api/test-results/full/</code> (report.json, junit.xml, matrix.csv, results-*.jsonl, ui/)</span></footer>
</div>
"""
