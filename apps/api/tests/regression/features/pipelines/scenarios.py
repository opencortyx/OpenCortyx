"""Pipelines: kind aliases × stage sets, and the protected system defaults (PIPE)."""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario

PIPELINE_KINDS = {"unstructured": "document", "document": "document", "doc": "document", "docs": "document",
                  "semistructured": "semistructured", "semi-structured": "semistructured", "semi": "semistructured",
                  "json": "semistructured", "structured": "structured", "tabular": "structured", "sql": "structured",
                  "bogus": None, "": None}
STAGE_SETS = {
    "empty": ([], []),
    "chunking_alias": ([{"type": "chunking_rules", "config": {"strategy": "paragraph"}}], ["chunking"]),
    "structure_card_alias": ([{"type": "structure_card", "config": {}}], ["json_structures"]),
    "introspect_alias": ([{"type": "introspect", "config": {}}], ["base_schema"]),
    "extractor_stage": ([{"type": "chunking"}, {"type": "builtin:py.date_normalizer"}], ["chunking", "builtin:py.date_normalizer"]),
    "config_alias_key": ([{"type": "json_structures", "config": {"auto_min_chars": 50}}], ["json_structures"]),
    "non_dict_stage": (["just-a-string"], None),
}

def build() -> list[Scenario]:
    out = []

    def add(fam, feature, wf, inp, cfg, exp, typ, params):
        out.append(Scenario(fam, "features", feature, wf, inp, cfg, exp, typ, "API up; tenant A", params))

    for kind, canon in PIPELINE_KINDS.items():
        for sname, (stages, types) in STAGE_SETS.items():
            ok = canon is not None and types is not None
            add("PIPE", "pipelines", "pipeline lifecycle", f"kind={kind!r}", f"stages={sname}",
                f"stored as kind={canon}, stages={types}; update; delete" if ok else "refused",
                "integration" if ok else "negative", {"kind": kind, "canon": canon, "stages": stages, "types": types})
    for target in ("sql", "json", "doc"):
        for action in ("update", "delete"):
            add("PIPE", "pipelines", "system default pipeline", f"default-pipeline-{target}", action,
                "refused; the default is unchanged", "negative", {"slug": target, "action": action})
    return out
