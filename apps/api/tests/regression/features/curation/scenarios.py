"""Curation (glossary, synonyms, rules, business context, comments, ontology types) (CURA)."""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario

TEXTS = {"plain": ("ARR", "Annual recurring revenue."), "unicode": ("売上", "Umsatz — revenue — مبيعات"),
         "long": ("Long", "x" * 10000), "blank_term": ("   ", "defined"), "empty_definition": ("Term", "")}
KINDS = {
    "glossary": ("/glossary", lambda t, d, tg: {"term": t, "definition": d, "targets": tg}),
    "synonym": ("/synonyms", lambda t, d, tg: {"term": t, "targets": tg}),
    "rule": ("/rules", lambda t, d, tg: {"title": t, "content": d, "targets": tg}),
    "business_context": ("/business-context", lambda t, d, tg: {"title": t, "content": d}),
}
TARGETS = ("none", "existing_node", "missing_node")
ONTOLOGY_KINDS = ("node", "entity", "edge", "claim", "bogus_kind")   # the registry knows node | edge | entity
COMMENTS = {"plain": "Looks right.", "unicode": "確認済み ✅", "long": "c" * 5000, "empty": ""}


def _blank(term, definition, kind):
    if not term.strip():
        return True
    return kind in ("glossary", "rule", "business_context") and not definition.strip()


def build() -> list[Scenario]:
    out = []

    def add(fam, wf, inp, cfg, exp, typ, params):
        out.append(Scenario(fam, "features", "curation", wf, inp, cfg, exp, typ,
                            "API up; tenant A", params))

    for kind, (path, _) in KINDS.items():
        for tname, (term, d) in TEXTS.items():
            for tg in TARGETS if kind != "business_context" else ("none",):
                blank = _blank(term, d, kind)
                exp = "reject" if blank else "unresolved" if tg == "missing_node" else "accept"
                add("CURA", f"curate {kind}", f"text={tname}", f"targets={tg}",
                    {"reject": "refused", "unresolved": "stored; the missing target reported, never counted as linked",
                     "accept": "stored as a node; linked to targets"}[exp],
                    "negative" if exp != "accept" else "integration",
                    {"kind": kind, "text": tname, "targets": tg, "expect": exp})
    for ok in ONTOLOGY_KINDS:
        for name in ("Widget", "Wïdgét", "W" * 80, ""):
            valid = ok in ("node", "entity", "edge") and bool(name)
            add("CURA", "ontology type lifecycle", f"name={name[:20]!r}", f"kind={ok}",
                "registered → listed → updated → deleted" if valid else "refused", "state-transition" if valid else "negative",
                {"kind": ok, "name": name, "valid": valid})
    for cname, text in COMMENTS.items():
        add("CURA", "comment on an asset", f"text={cname}", "default", "listed on the asset; delete removes it"
            if text else "refused", "integration" if text else "negative", {"text": text})
    return out
