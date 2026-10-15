"""The coverage matrix: Module → Feature → Workflow → Input → Configuration → Expected → Test type.

Every scenario the regression suite runs is a row derived here, from a REAL inventory — the format
catalog, the API's route table, the files of the corpus, the W3C conformance catalogue — crossed with
the configuration dimensions that change behaviour. Nothing is enumerated by hand beyond the variant
definitions in `data.py`, and a dimension is only crossed where it changes the expected result
(overlap for sentence chunking, null values in TOML, … are pruned, not padded).

IDs are stable hashes of the row, so a scenario keeps its ID across runs and a regression is traceable.

    python -m tests.regression.matrix --export matrix.csv     # the matrix, before anything runs
"""
from __future__ import annotations

import csv
import functools
import hashlib
import itertools
import json
import os
import pathlib
import re
from dataclasses import asdict, dataclass, field

from tests.regression.framework import data as D

CORPUS = os.environ.get("OPENCORTYX_CORPUS")


@dataclass(frozen=True)
class Scenario:
    family: str
    module: str
    feature: str
    workflow: str
    input: str
    config: str
    expected: str
    type: str
    prerequisites: str = ""
    params: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def id(self) -> str:
        key = "|".join((self.family, self.module, self.feature, self.workflow, self.input, self.config))
        return f"{self.family}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"

    @property
    def title(self) -> str:
        return f"{self.workflow}: {self.input} [{self.config}] → {self.expected}"

    def as_record(self) -> dict:
        d = asdict(self)
        d.pop("params")
        return {**d, "scenario": self.title}

    def __str__(self):                       # pytest id
        return self.id


def pairwise(dims: dict) -> list[dict]:
    """All-pairs combinations (greedy). Every pair of values of every two dimensions appears once."""
    names = list(dims)
    uncovered = {(a, va, b, vb) for a, b in itertools.combinations(names, 2)
                 for va in dims[a] for vb in dims[b]}
    rows = []
    while uncovered:
        best, best_cov = None, -1
        for cand in itertools.product(*(dims[n] for n in names)):
            row = dict(zip(names, cand))
            cov = sum(1 for a, b in itertools.combinations(names, 2) if (a, row[a], b, row[b]) in uncovered)
            if cov > best_cov:
                best, best_cov = row, cov
        rows.append(best)
        uncovered -= {(a, best[a], b, best[b]) for a, b in itertools.combinations(names, 2)}
    return rows


# ── families ─────────────────────────────────────────────────────────────────────────────────────
def fmt_semistructured() -> list[Scenario]:
    out = []
    for fmt in D.SEMI_FORMATS:
        for name, (value, expect) in D.SEMI_VARIANTS.items():
            if fmt == "toml" and not D.toml_applicable(value):
                continue                                   # TOML cannot express this value
            out.append(Scenario("FMTS", "ingest", f"reader:{fmt}", "default walker ingest", name, "default",
                                expect, "integration", "format writer", {"fmt": fmt, "variant": name}))
            if expect == "accept" and fmt not in ("yaml", "yml"):
                trunc_expect = "accept_partial" if fmt in ("jsonl", "ndjson") and isinstance(value, list) else "reject"
                out.append(Scenario("FMTS", "ingest", f"reader:{fmt}", "default walker ingest", f"{name}+truncated",
                                    "default", trunc_expect, "negative", "format writer",
                                    {"fmt": fmt, "variant": name, "truncate": True}))
        for raw in D.SEMI_RAW:
            out.append(Scenario("FMTS", "ingest", f"reader:{fmt}", "default walker ingest", raw, "default",
                                "reject", "negative", "", {"fmt": fmt, "raw": raw}))
    return out


def fmt_tables() -> list[Scenario]:
    out = []
    for fmt in D.TABLE_FORMATS:
        for name, (cols, rows, expect) in D.TABLE_VARIANTS.items():
            if fmt == "avro" and cols and not D.avro_safe_names(cols):
                continue                                   # Avro field names cannot hold these headers
            out.append(Scenario("FMTT", "ingest", f"reader:{fmt}", "tabular preview + row stream", name,
                                "default", expect, "integration" if expect != "reject" else "negative",
                                "format writer", {"fmt": fmt, "variant": name}))
    return out


def fmt_documents() -> list[Scenario]:
    out = []
    for fmt in D.DOC_FORMATS:
        for name, (expect, _) in D.DOC_VARIANTS.items():
            out.append(Scenario("FMTD", "ingest", f"reader:{fmt}", "document text extraction", name, "default",
                                expect, "integration" if expect == "accept" else "negative", "format writer",
                                {"fmt": fmt, "variant": name}))
        if fmt in ("pdf", "docx", "pptx", "odt"):
            out.append(Scenario("FMTD", "ingest", f"reader:{fmt}", "document text extraction", "corrupt_container",
                                "default", "reject", "negative", "", {"fmt": fmt, "variant": "corrupt"}))
    return out


def chunking() -> list[Scenario]:
    out = []
    for text in D.CHUNK_TEXTS:
        for strat in D.CHUNK_STRATEGIES:
            overlaps = D.CHUNK_OVERLAPS if strat in ("sliding_window", "markdown_heading", "unknown_strategy") else (0,)
            for size in D.CHUNK_SIZES:
                for ov in overlaps:
                    if ov >= size:
                        continue
                    out.append(Scenario("CHNK", "ingest", "unstructured chunking", "chunk_document", text,
                                        f"strategy={strat} size={size} overlap={ov}", "invariants hold", "unit",
                                        "", {"text": text, "strategy": strat, "size": size, "overlap": ov}))
    return out


CONTENT_KINDS = {
    "json_object": b'{"a": 1, "b": {"c": 2}}',
    "json_array": b'[{"a": 1}, {"a": 2}]',
    "ndjson": b'{"a": 1}\n{"a": 2}\n{"a": 3}\n',
    "xml": b'<?xml version="1.0"?><root><a>1</a></root>',
    "html": b"<!DOCTYPE html><html><body><p>hello</p></body></html>",
    "csv": b"id,name,amount\n1,a,2\n2,b,3\n3,c,4\n",
    "pdf_magic": b"%PDF-1.7\n%\xe2\xe3\n1 0 obj<<>>endobj\n",
    "parquet_magic": b"PAR1\x00\x00\x00\x00PAR1",
    "prose": b"This is an ordinary paragraph of prose about quarterly results.",
    "empty": b"",
}
SNIFF_EXPECT = {"json_object": "semistructured", "json_array": "semistructured", "ndjson": "semistructured",
                "xml": "semistructured", "html": "document", "csv": "structured", "pdf_magic": "document",
                "parquet_magic": "structured", "prose": "document", "empty": "document"}


def routing() -> list[Scenario]:
    from opencortyx.ingest.formats.catalog import FORMATS
    out = []
    for ext in list(FORMATS) + ["unknownext", ""]:
        for kind in CONTENT_KINDS:
            for override in (None, "document", "semistructured", "structured", "bogus"):
                # The detector's documented policy (detection/detector.py, pinned by
                # test_file_type_detector::test_conflict_resolution_wrong_extension): an override wins;
                # otherwise specific content beats the filename label; generic prose or an empty file
                # leaves the extension in charge.
                if override in ("document", "semistructured", "structured"):
                    expect, rule = override, "override wins"
                elif ext in FORMATS and kind in ("prose", "empty"):
                    expect, rule = FORMATS[ext].route, "extension decides"
                elif ext in FORMATS:
                    expect, rule = SNIFF_EXPECT[kind], "content beats extension"
                else:
                    expect, rule = SNIFF_EXPECT[kind], "content decides"
                out.append(Scenario("ROUT", "ingest", "detection routing", "route_for", f"ext=.{ext} content={kind}",
                                    f"override={override}", f"{expect} ({rule})", "unit", "",
                                    {"ext": ext, "kind": kind, "override": override, "expect": expect}))
    return out


# ── corpus-driven families ───────────────────────────────────────────────────────────────────────
SEMI_EXTS = None


def _semi_exts() -> dict:
    global SEMI_EXTS
    if SEMI_EXTS is None:
        from opencortyx.ingest.formats import semistructured as P
        owners = {e: p["name"] for p in P.describe() for e in p["extensions"]}
        SEMI_EXTS = {e for e, n in owners.items() if n in ("json", "jsonl", "yaml", "xml")}
    return SEMI_EXTS


def _json_suite_expect(path: pathlib.Path) -> str:
    if "test_parsing" in path.parts and path.name[1:2] == "_":
        return {"y": "accept", "n": "reject", "i": "no_crash"}[path.name[0]]
    return "no_crash"


@functools.lru_cache(maxsize=1)
def corpus_files() -> list[pathlib.Path]:
    if not CORPUS:
        return []
    root = pathlib.Path(CORPUS)
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lstrip(".").lower() in _semi_exts())


@functools.lru_cache(maxsize=1)
def corpus_index() -> dict:
    """{relpath: accepted?} — which corpus files parse, cached beside the results by size+mtime."""
    from tests.regression.conftest import RESULTS
    from opencortyx.ingest.formats import semistructured as P
    from opencortyx.ingest.walkers.semistructured import _has_records
    cache_f = RESULTS / "corpus-index.json"
    try:
        cache = json.loads(cache_f.read_text())
    except Exception:                                     # noqa: BLE001
        cache = {}
    out, changed = {}, False
    for p in corpus_files():
        rel = str(p.relative_to(CORPUS))
        key = f"{p.stat().st_size}:{int(p.stat().st_mtime)}"
        if cache.get(rel, {}).get("key") == key:
            out[rel] = cache[rel]["ok"]
            continue
        try:
            r = P.parse(read_capped(p), rel)
            ok = _has_records(r.data)
        except Exception:                                 # noqa: BLE001
            ok = False
        out[rel], cache[rel], changed = ok, {"key": key, "ok": ok}, True
    if changed:
        RESULTS.mkdir(parents=True, exist_ok=True)
        cache_f.write_text(json.dumps(cache))
    return out


MAX_BYTES = int(os.environ.get("OPENCORTYX_CORPUS_MAX_BYTES", 20_000_000))


def read_capped(p: pathlib.Path) -> bytes:
    with p.open("rb") as f:
        b = f.read(MAX_BYTES + 1)
    return b[: b.rfind(b"\n", 0, MAX_BYTES) + 1] if len(b) > MAX_BYTES else b


def corpus_walk() -> list[Scenario]:
    out = []
    for p in corpus_files():
        rel = str(p.relative_to(CORPUS))
        out.append(Scenario("CORP", "ingest", f"reader:{p.suffix.lstrip('.').lower()}", "real file, default walker",
                            rel, "default", _json_suite_expect(p), "integration", "corpus mounted", {"path": rel}))
    return out


WALK_CONFIGS = pairwise({"max_depth": [1, 3, 6], "max_array": [1, 10, 500], "root_type": [None, "Item"],
                         "declared": [False, True]})


def corpus_walk_configs() -> list[Scenario]:
    out = []
    idx = corpus_index() if CORPUS else {}
    for rel, ok in idx.items():
        if not ok:
            continue
        for cfg in WALK_CONFIGS:
            c = " ".join(f"{k}={v}" for k, v in cfg.items())
            out.append(Scenario("WCFG", "ingest", "semi-structured walker configuration", "ingest_json with config",
                                rel, c, "bounds honoured, ids generated", "integration", "corpus mounted",
                                {"path": rel, **cfg}))
    return out


def corpus_schema() -> list[Scenario]:
    idx = corpus_index() if CORPUS else {}
    return [Scenario("SCHM", "ingest", "structure inference", "build → match → compile", rel, "first record",
                     "record matches its own structure; deterministic", "unit", "corpus mounted", {"path": rel})
            for rel, ok in idx.items() if ok]


def xml_conformance() -> list[Scenario]:
    if not CORPUS:
        return []
    root = pathlib.Path(CORPUS) / "xml-w3c-tests" / "xmlconf"
    if not root.exists():
        return []
    import xml.etree.ElementTree as ET
    out = []
    for cat in sorted(root.rglob("*.xml")):
        try:
            text = cat.read_text(encoding="utf-8", errors="ignore")
        except Exception:                                 # noqa: BLE001
            continue
        if "<TEST " not in text:
            continue
        base = re.search(r'<TESTCASES[^>]*xml:base="([^"]+)"', text)
        for m in re.finditer(r"<TEST\b[^>]*>", text):
            try:
                el = ET.fromstring(m.group(0).rstrip(">").rstrip("/") + "/>")
            except ET.ParseError:
                continue
            a = el.attrib
            uri = a.get("URI")
            if not uri:
                continue
            path = (cat.parent / (base.group(1) if base else "") / uri).resolve()
            if not path.exists():
                continue
            t, ents, ver = a.get("TYPE"), a.get("ENTITIES", "none"), a.get("VERSION", "1.0")
            doc = path.read_bytes()
            if t == "not-wf" and ents == "none" and ver == "1.0" and "EDITION" not in a:
                expect = "reject"
            elif t in ("valid", "invalid") and ents == "none" and ver == "1.0" and "EDITION" not in a:
                decl = re.sub(rb"<!--.*?-->", b"", doc, flags=re.S)             # a comment declares nothing
                expect = "reject_by_policy" if b"<!ENTITY" in decl else "accept"
            else:
                expect = "no_crash"
            rel = str(path.relative_to(pathlib.Path(CORPUS)))
            out.append(Scenario("XMLC", "ingest", "reader:xml", "W3C XML conformance", rel,
                                f"TYPE={t} ENTITIES={ents} VERSION={ver}", expect, "contract", "corpus mounted",
                                {"path": rel, "id": a.get("ID")}))
    return out


# ── live-stack families ──────────────────────────────────────────────────────────────────────────
PRINCIPALS = ("anonymous", "garbage_token", "wrong_secret", "expired", "refresh_as_access",
              "viewer", "editor", "admin", "agent_readonly")
_INVALID = {"anonymous", "garbage_token", "wrong_secret", "expired", "refresh_as_access"}


@functools.lru_cache(maxsize=1)
def route_inventory() -> list[dict]:
    """Every API operation with the guard its dependency tree actually enforces."""
    import opencortyx.main as main
    from fastapi.routing import APIRoute

    def name_of(call):
        n = getattr(call, "__name__", "") or ""
        q = getattr(call, "__qualname__", "") or ""
        if n in ("require_auth", "require_write", "require_admin") or "require_role" in q:
            return "require_admin" if "require_role" in q else n
        return None

    def guards(dep):
        out = set()
        for d in dep.dependencies:
            if name_of(d.call):
                out.add(name_of(d.call))
            out |= guards(d)
        return out

    def walk(routes, prefix="", inherited=()):
        """Flatten routers. FastAPI ≥0.140 keeps included routers lazy (`_IncludedRouter`), with the
        include-time prefix and dependencies on `include_context` rather than baked into each route."""
        for x in routes:
            if isinstance(x, APIRoute):
                yield prefix + x.path, x, inherited
            elif hasattr(x, "original_router"):
                ic = x.include_context
                deps = tuple(name_of(getattr(d, "dependency", None)) for d in (ic.dependencies or []))
                yield from walk(x.original_router.routes, prefix + (ic.prefix or ""),
                                inherited + tuple(d for d in deps if d))

    rows = []
    for full_path, r, inherited in walk(main.app.routes):
        g = guards(r.dependant) | set(inherited)
        level = ("admin" if "require_admin" in g else "write" if "require_write" in g
                 else "auth" if "require_auth" in g else "public")
        body_required = any(getattr(f, "required", False) for f in r.dependant.body_params)
        for m in sorted(r.methods):
            rows.append({"method": m, "path": full_path, "level": level, "body_required": body_required,
                         "path_params": [p.name for p in r.dependant.path_params]})
    return rows


#: POST operations that READ: queries, previews, dry-run drafts, connection/model tests, discovery.
READ_POST = re.compile(r"(/answer(/stream)?|/context|/graph/query|/preview(-file)?|/draft(/upload|/preview)?"
                       r"|/discover|/test|/introspect-sql|/anchors/preview)$")


def mutates(method: str, path: str) -> bool:
    return method in ("PUT", "PATCH", "DELETE") or (method == "POST" and not READ_POST.search(path))


def expected_status(level: str, method: str, principal: str, path: str = "") -> str:
    """The contract: who may call what. `viewer` and read-only keys never mutate (auth.py: "viewer is
    read-only — block it from mutating endpoints"); a declared write guard refuses them even on a read."""
    if level == "public":
        return "allowed"
    if principal in _INVALID:
        return "401"
    if level == "admin":
        return "allowed" if principal == "admin" else "403"
    read_only = principal in ("viewer", "agent_readonly")
    if read_only and (level == "write" or mutates(method, path)):
        return "403"
    return "allowed"


def api_authz() -> list[Scenario]:
    out = []
    for op in route_inventory():
        for pr in PRINCIPALS:
            exp = expected_status(op["level"], op["method"], pr, op["path"])
            out.append(Scenario("AUTH", "api", f"{op['level']} guard", f"{op['method']} {op['path']}",
                                f"principal={pr}", "throwaway tenant", exp, "security", "API up; tenant A",
                                {**op, "principal": pr, "expect": exp}))
    return out


def api_tenancy() -> list[Scenario]:
    out = []
    for op in route_inventory():
        if op["method"] != "GET" or op["level"] == "public":
            continue
        kind = "collection read" if not op["path_params"] else "object read by tenant A's id"
        out.append(Scenario("TENA", "api", "tenant isolation", f"GET {op['path']}", kind,
                            "tenant B reads after tenant A ingested", "no tenant-A data visible", "security",
                            "API + worker up; tenant A holds data", op))
    return out


def e2e_ingest() -> list[Scenario]:
    from opencortyx.ingest.formats.catalog import FORMATS
    out = []
    for ext, f in FORMATS.items():
        for variant, expect in (("valid", "completed with nodes"), ("corrupt", "failed with reason"),
                                ("empty", "rejected or failed with reason")):
            out.append(Scenario("E2EI", "e2e", f"upload:{f.route}", "upload → run → graph → API", f".{ext} {variant}",
                                "default pipeline", expect, "e2e", "API + worker up", {"ext": ext, "variant": variant}))
        out.append(Scenario("E2EI", "e2e", f"upload:{f.route}", "re-upload same bytes", f".{ext} valid ×2",
                            "default pipeline", "second run is a duplicate; graph unchanged", "e2e",
                            "API + worker up", {"ext": ext, "variant": "duplicate"}))
    for n in (1, 100, 5000):
        out.append(Scenario("E2EI", "e2e", "upload:semistructured", "volume", f"{n} JSON records", "default",
                            f"completed with {n} records", "e2e", "API + worker up", {"ext": "json", "variant": f"volume:{n}"}))
    for k in (3, 8):
        out.append(Scenario("E2EI", "e2e", "upload:semistructured", "concurrency", f"{k} distinct files at once",
                            "default", "every run completes; no cross-talk", "e2e", "API + worker up",
                            {"ext": "json", "variant": f"concurrent:{k}"}))
        out.append(Scenario("E2EI", "e2e", "upload:semistructured", "concurrency", f"same file ×{k} at once",
                            "default", "one asset; graph equals a single ingest", "e2e", "API + worker up",
                            {"ext": "json", "variant": f"same:{k}"}))
    return out


def _feature(module: str):
    """A feature package's own slice of the matrix: tests/regression/features/<module>/scenarios.py."""
    def build():
        import importlib
        return importlib.import_module(f"tests.regression.features.{module}.scenarios").build()
    return build


FAMILIES = {
    "FMTS": fmt_semistructured, "FMTT": fmt_tables, "FMTD": fmt_documents, "CHNK": chunking,
    "ROUT": routing, "CORP": corpus_walk, "WCFG": corpus_walk_configs, "SCHM": corpus_schema,
    "XMLC": xml_conformance, "AUTH": api_authz, "TENA": api_tenancy, "E2EI": e2e_ingest,
    "EXTR": _feature("extractors"), "GRPH": _feature("graph"), "ACCS": _feature("access"),
    "CURA": _feature("curation"), "ONTO": _feature("ontology"), "RSLV": _feature("resolution"), "PIPE": _feature("pipelines"),
    "MAPS": _feature("schema_mappings"), "CONN": _feature("connectors"), "INGS": _feature("ingestions"),
    "MODL": _feature("models"), "QUAR": _feature("quarantine"),
    "CONT": lambda: __import__("tests.regression.api.contracts.scenarios", fromlist=["build"]).build(),
    "SYNC": lambda: __import__("tests.regression.operations.scenarios", fromlist=["build"]).build(),
    "RCVR": lambda: __import__("tests.regression.operations.scenarios", fromlist=["build"]).build(),
}


@functools.lru_cache(maxsize=None)
def scenarios(family: str) -> list[Scenario]:
    rows = [s for s in FAMILIES[family]() if s.family == family]
    ids = [s.id for s in rows]
    assert len(ids) == len(set(ids)), f"{family}: duplicate scenario rows"
    return rows


def export(path: str) -> int:
    fields = ["id", "family", "module", "feature", "workflow", "input", "config", "expected", "type", "prerequisites"]
    n = 0
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for fam in FAMILIES:
            for s in scenarios(fam):
                w.writerow({"id": s.id, **{k: getattr(s, k) for k in fields[1:]}})
                n += 1
    return n


if __name__ == "__main__":
    import sys
    target = sys.argv[sys.argv.index("--export") + 1] if "--export" in sys.argv else "matrix.csv"
    total = export(target)
    for fam in FAMILIES:
        print(f"{fam:5} {len(scenarios(fam)):6}")
    print(f"TOTAL {total:6}  → {target}")
