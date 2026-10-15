"""Custom extractors: mechanism × configuration × sample text, and the extractor lifecycle (EXTR).

Deterministic mechanisms only (regex, dictionary, keyword, seed, python) so the expected nodes can be
computed independently; model-backed mechanisms are exercised by the live ingestion suites.
"""
from __future__ import annotations

import re

from tests.regression.framework.matrix import Scenario

TEXTS = {
    "invoice": "Invoice INV-12 and INV-34 from Acme AS in Oslo. Repeat INV-12. Contact ann@acme.test.",
    "unicode": "請求書 INV-99 — Zürich GmbH, São Paulo Ltda. Repeat INV-99.",
    "no_match": "Nothing of interest in this sentence at all.",
    "empty": "",
    "large": " ".join(f"Order INV-{i} shipped to Oslo." for i in range(400)),
}
REGEXES = {
    "invoice_ids": r"INV-\d+",
    "named_group": r"(?P<code>INV)-(?P<number>\d+)",
    "email": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "case_insensitive": r"inv-\d+",
    "anchored_start": r"^Invoice",
}
DICTIONARIES = {
    "cities": ["Oslo", "Zürich", "São Paulo", "Bergen"],
    "mixed_case": ["acme", "OSLO"],
    "term_objects": [{"term": "Acme"}, {"term": "GmbH"}],
    "none_present": ["Tokyo", "Lima"],
}
PYTHON = {
    "returns_two": "def extract(inputs):\n    return [{'name': 'a'}, {'name': 'b'}]",
    "uses_text": "def extract(inputs):\n    return [{'name': w} for w in sorted(set(inputs['text'].split()))[:5]]",
    "returns_empty": "def extract(inputs):\n    return []",
    "raises": "def extract(inputs):\n    raise ValueError('boom')",
    "wrong_type": "def extract(inputs):\n    return 42",
}
INVALID = {
    "empty_name": ({"name": "   ", "mechanism": {"ref": "unstructured.regex", "pattern": "x"}}, 422),
    "bad_regex": ({"name": "r", "mechanism": {"ref": "unstructured.regex", "pattern": "INV-(\\d+"}}, 422),
    "python_syntax_error": ({"name": "p", "mechanism": {"ref": "unstructured.python", "code": "def extract(:"}}, 422),
    "unknown_mechanism": ({"name": "u", "mechanism": {"ref": "unstructured.telepathy"}}, 422),
    "llm_without_prompt": ({"name": "l", "mechanism": {"ref": "unstructured.llm"}}, 422),
    "no_mechanism": ({"name": "n"}, 422),
    "name_missing": ({"mechanism": {"ref": "seed.literal", "items": []}}, 422),
    "mechanism_not_object": ({"name": "m", "mechanism": "regex"}, 422),
}
STATUSES = ("active", "disabled", "draft", "bogus-status")
VALID_STATUSES = ("active", "disabled")        # the API accepts exactly these
PREVIEW_MAX_NODES = 12                         # templates.preview_template(max_nodes=12): a sample, by design


def expected_records(ref: str, cfg: dict, text: str) -> list[str] | None:
    """Distinct node labels the mechanism must produce from `text`; None when not computable here."""
    if ref == "unstructured.regex":
        flags = re.IGNORECASE if cfg.get("ignore_case") else 0
        return sorted({m.group(0) for m in re.finditer(cfg["pattern"], text, flags)})
    if ref == "unstructured.dictionary":
        low = text.lower()
        terms = [t if isinstance(t, str) else t["term"] for t in cfg["terms"]]
        return sorted({t for t in terms if t.lower() in low})
    if ref == "seed.literal":
        return sorted({i["name"] for i in cfg["items"]})
    return None


def build() -> list[Scenario]:
    out = []
    emit = lambda field: {"nodes": [{"type": "Extracted", "label": f"${field}", "id": [field]}]}

    def add(workflow, inp, cfg, expected, typ, params):
        out.append(Scenario("EXTR", "features", "extractors", workflow, inp, cfg, expected, typ,
                            "API up; tenant A", params))

    for tname, text in TEXTS.items():
        for rname, pattern in REGEXES.items():
            for ic in (False, True):
                mech = {"ref": "unstructured.regex", "pattern": pattern, "ignore_case": ic}
                add("preview regex extractor", f"text={tname}", f"regex={rname} ignore_case={ic}",
                    "nodes = distinct matches", "integration",
                    {"mechanism": mech, "emit": emit("match"), "text": text})
        for dname, terms in DICTIONARIES.items():
            mech = {"ref": "unstructured.dictionary", "terms": terms}
            add("preview dictionary extractor", f"text={tname}", f"dictionary={dname}",
                "nodes = terms present", "integration", {"mechanism": mech, "emit": emit("term"), "text": text})
        for n in (1, 3, 8):
            mech = {"ref": "unstructured.keyword", "top_n": n}
            add("preview keyword extractor", f"text={tname}", f"top_n={n}", f"at most {n} keywords",
                "integration", {"mechanism": mech, "emit": emit("term"), "text": text, "top_n": n})
        for pname, code in PYTHON.items():
            mech = {"ref": "unstructured.python", "code": code}
            exp = "reported error, no nodes" if pname in ("raises", "wrong_type") else "nodes from returned records"
            add("preview python extractor", f"text={tname}", f"python={pname}", exp, "integration",
                {"mechanism": mech, "emit": emit("name"), "text": text, "python": pname})
    for k in (0, 1, 5, 50):
        mech = {"ref": "seed.literal", "items": [{"name": f"seed-{i}"} for i in range(k)]}
        add("preview seed extractor", f"{k} literal items", "seed.literal", f"{k} nodes", "integration",
            {"mechanism": mech, "emit": emit("name"), "text": "any"})
    for iname, (body, status) in INVALID.items():
        add("save extractor", iname, "invalid definition", f"refused with {status}", "negative",
            {"body": body, "status": status})
    for mname, mech in (("regex", {"ref": "unstructured.regex", "pattern": "INV-\\d+"}),
                        ("dictionary", {"ref": "unstructured.dictionary", "terms": ["Oslo"]}),
                        ("python", {"ref": "unstructured.python", "code": PYTHON["returns_two"]}),
                        ("seed", {"ref": "seed.literal", "items": [{"name": "x"}]})):
        for st in STATUSES:
            add("extractor lifecycle", f"mechanism={mname}", f"status→{st}",
                "create → listed → status change → reset → delete → gone" if st in VALID_STATUSES
                else "invalid status refused; extractor unchanged", "state-transition",
                {"mechanism": mech, "status": st})
    return out
