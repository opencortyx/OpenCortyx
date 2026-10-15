"""The defect register: every defect the suite found, and the rule that ties failing tests to it.

A defect is listed only with the executed, failing scenarios that demonstrate it (matched below by
family and failing check). A failure that matches no defect is reported as UNTRIAGED, never hidden.
"""
from __future__ import annotations

DEFECTS = [
    # id, severity, component, title, matcher(family, check-substring, input-substring or "")
    ("D1", "High", "ingest/formats/structured", "`.xls` is advertised as supported but read as CSV (binary garbage)",
     [("FMTT", "", "reader:xls")], "tabular.file_format('a.xls') == 'csv'; EXCEL_EXTS lacks .xls"),
    ("D2", "Medium", "ingest/formats/structured", "An empty (0-byte) CSV/TSV is accepted as a table with a phantom `column0`",
     [("FMTT", "outcome matches expectation", "empty_file")], ""),
    ("D3", "Medium", "ingest/formats/unstructured", "A PDF with no text is accepted: the page separator '-----' counts as content",
     [("FMTD", "outcome matches expectation", "empty"), ("FMTD", "outcome matches expectation", "whitespace")], ""),
    ("D4", "Medium", "ingest/formats/unstructured", "`.eml` bodies in base64/quoted-printable are stored undecoded",
     [("FMTD", "text extracted", "unicode")], ""),
    ("D5", "Low", "ingest/formats/structured", "A valid zero-row Avro file cannot be read ('must have at least one column')",
     [("FMTT", "outcome matches expectation", "header_only")], ""),
    ("D6", "Low", "ingest/detection", "`route_for` returns 'unknown' (not a pipeline) for extensionless prose or empty files",
     [("ROUT", "route", "")], ""),
    ("D7", "Low", "ingest/formats/semistructured", "XML reader deviates from W3C XML 1.0 on a handful of conformance cases",
     [("XMLC", "", "")], "expat character-class strictness"),
    ("D9", "High", "api/security", "A viewer or read-only key can create always-on business context (injected into every answer)",
     [("AUTH", "refused with 403", "")], "POST /business-context only requires require_auth"),
    ("D10", "Medium", "api/graph", "`POST /relationships` reports a failed link (missing/other-tenant node) as HTTP 200",
     [("GRPH", "refused as not found", "")], "returns 200 {status: endpoint_not_found}"),
    ("D11", "Medium", "api/graph", "`POST /relationships` with a malformed id is an HTTP 500",
     [("GRPH", "no server error", "malformed")], ""),
    ("D12", "Low", "api/graph", "A self-loop edge is accepted (200) but never appears when the node is read back",
     [("GRPH", "self-loop recorded", "")], ""),
    ("D14", "Medium", "api/access", "Users can be created with an email that has no '@'",
     [("ACCS", "refused", "no_at_sign")], ""),
    ("D13", "High", "api/access", "Users can be created with any role string; an unknown role is granted write access",
     [("ACCS", "refused", "superuser")], "require_write only blocks the exact role 'viewer'"),
    ("D15", "Medium", "api/curation", "Glossary terms, synonyms, rules and business context accept blank terms/definitions",
     [("CURA", "refused", "blank_term"), ("CURA", "refused", "empty_definition")], ""),
    ("D16", "Medium", "api/curation", "Curation reports a non-existent target node as linked (`targets: 1`)",
     [("CURA", "refused", "missing_node")], ""),
    ("D17", "Medium", "api/curation", "Ontology types accept empty names and unknown kinds; an 80-char name is an HTTP 500",
     [("CURA", "", "name=")], ""),
    ("D20", "Medium", "api/connectors", "MSSQL connectors cannot work: the `pyodbc` driver is not installed",
     [("CONN", "driver for this kind is installed", "kind=mssql")], ""),
    ("D21", "Medium", "api/ingestions", "An ingestion with an invalid cron schedule is accepted",
     [("INGS", "invalid schedule refused", "")], ""),
    ("D23", "Medium", "api/schema-mappings", "Schema mappings accept unknown paradigms and statuses",
     [("MAPS", "refused", "")], ""),
    ("D24", "Medium", "api/models", "The model registry accepts unknown roles and providers", [("MODL", "refused", "")], ""),
    ("D25", "Medium", "api/models", "Testing a model with an unreachable endpoint does not answer within 60 s",
     [("MODL", "answers within 60 s", ""), ("MODL", "timed out", "")], "the dependency call has no bound"),
    ("D26", "Low", "api/models", "An embedding model test reports ok=true with 0 dimensions for an unusable model",
     [("MODL", "reported as a failure", "")], ""),
    ("D27", "Medium", "api/quarantine", "Quarantine actions on non-existent items report success (200)",
     [("QUAR", "unknown item refused", "")], ""),
    ("D28", "Medium", "api/connectors", "Connector recipes do not round-trip: every non-mysql kind re-imports as postgres",
     [("CONN", "recipe round-trips the kind", "")], ""),
    ("D31", "Medium", "ingest/semistructured", "The default (plain) walker ignores max_array: arrays are never capped",
     [("WCFG", "array bound honoured", "root_type=None declared=False")],
     "REGRESSION introduced 2026-09-27 when SemiStructuredWalker became the default path"),
    ("D33", "Medium", "ingest/runs", "A 5,000-record JSON upload takes over 10 minutes; runs queued behind it starve",
     [("E2EI", "completed", "records"), ("E2EI", "every run reaches a final state", "")],
     "measured: still 'running' at the 600 s budget on the single graphrag worker"),
    ("D34", "Medium", "web/auth", "Sign-in intermittently fails with MissingCSRF and tells the user 'Invalid email or password'",
     [("UI-ACC", "toHaveURL", "")], "web log: MissingCSRF at the attempt; the API received no sign-in request for it"),
    ("D35", "Medium", "ingest/sources/sync", "A scheduled sync over a folder that does not exist reports 'completed' with 0 files every minute",
     [("SYNC", "fails with a reason", ""), ("SYNC", "retried", ""), ("SYNC", "backoff", "")],
     "no error, no hibernation: a typo or an unmounted drive goes unnoticed; consequently the hibernation self-heal "
     "path could not be exercised live"),
    ("D29", "High", "api (19 operations)", "A NUL/control character in a string field is an HTTP 500 (incl. /auth/token)",
     [("CONT", "never a server error", "control_chars")], "Postgres rejects \\x00 and the error is unhandled"),
    ("D30", "Medium", "api (several operations)", "Oversized strings are an HTTP 500 instead of a 422",
     [("CONT", "never a server error", "oversized")], ""),
]
#: Found by reading code, NOT by an executed test: reported separately and never counted as tested.
#: The last field, when set, says how a fix was verified. It is still not a live execution.
CODE_REVIEW_FINDINGS = [
    ("D8", "High", "api/maintenance", "`DELETE /reset` (require_write) purges the SHARED task queue of every tenant",
     "api/routes/system/maintenance.py: reset_database → purge_task_queue(); not executed, it would affect the live stack",
     "reset is admin-only and revokes only the caller's org's queued/running tasks (revoke_tasks), no broker purge; "
     "verified by unit tests in tests/test_reset_scope.py, not by a live reset"),
    ("D37", "High", "ingest/sources/sync", "A watched folder that cannot be read makes every file ever synced from it "
     "look deleted: one unmount soft-removes them all",
     "tasks/sync.py: a listing error was recorded and the diff still applied its removals",
     "files under an unreadable folder are kept and judged on the next readable pass (sync.spare_unreadable); a pass "
     "that can list nothing fails and hibernates; verified by unit tests in tests/test_sync_unreadable.py"),
    ("D36", "High", "ontology (tenancy)", "The ontology is shared by every organization: one tenant's candidates, "
     "promotions, description edits and on/off toggles change every tenant's ontology and extraction",
     "type_registry had no workspace_id; found reading the reset code (D8). The TENA family checked graph data but "
     "never the ontology",
     "per-organization ontology over read-only shared built-ins (copy-on-write), migration 0023; verified by the "
     "executed ONTO family (tenant A acts, both tenants read) and unit tests in tests/test_ontology_scope.py"),
    ("D38", "Medium", "ingest/formats/unstructured", "`.doc` (Word 97-2003) is advertised as supported but has no reader: "
     "the binary is decoded as text and stored",
     "formats/catalog.py lists doc under Microsoft Word; documents.parse_content falls through to a UTF-8 decode. "
     "Not executable here: the test image cannot write a .doc (those E2EI cases are BLOCKED). Not fixed, by decision",
     ""),
    ("D39", "Low", "ingest/formats/unstructured", "`.rtf` is ingested with its control words (`{\\rtf1\\ansi ...`) as content",
     "documents.parse_content decodes rtf as plain text. Not fixed, by decision", ""),
]
OBSERVATIONS = [
    ("O1", "Extractor preview caps output at 12 nodes and reports the capped count, not the would-be total"),
    ("O2", "Pre-existing (documented in e2e/negative.spec.ts): GET /nodes/{missing} answers 200 {status: not_found}"),
    ("O4", "Withdrawn D32: the declared walker's same-type fan-out was promoted long-text fields of one object "
           "(one HAS_DOCUMENT per key), not an array exceeding max_array — a test-oracle error, corrected"),
    ("O5", "Intermittent: in the full run the new-pipeline page once showed TWO 'Pipeline name' inputs at the same "
           "time (document and structured builders), failing 2 UI-WF tests; the same build passed them in the run before "
           "and in the re-runs. The page code renders one builder; the cause is not established"),
    ("O6", "Governed proposals (agent-proposed facts, review-gated extraction and anchors) were removed by "
           "decision on 2026-09-28, so D18 and D19, defects of that feature, no longer apply. What needs a "
           "decision now comes from resolution: Conflicts and Versions in the Review queue"),
    ("O3", "Seen during cleanup, not by a test: a task redelivered for a deleted tenant recreates some of its rows "
           "(write accounting, quarantine entries, seeded extractors), so deleting a tenant is not final while its tasks are in flight"),
]


def triage(failures: list[dict]) -> tuple[dict, list[dict]]:
    """{defect id: [failing results]} and the failures no defect explains."""
    linked, untriaged = {d[0]: [] for d in DEFECTS}, []
    for r in failures:
        bad = " ".join(c["check"] for c in r.get("checks", []) if not c["ok"]) + " " + r.get("failure_reason", "")
        where = " ".join(str(r.get(k, "")) for k in ("feature", "input", "config"))
        hit = next((did for did, _s, _c, _t, rules, _n in DEFECTS
                    if any(r.get("family") == fam and check in bad and inp in where for fam, check, inp in rules)), None)
        if hit:
            linked[hit].append(r)
        else:
            untriaged.append(r)
    return linked, untriaged
