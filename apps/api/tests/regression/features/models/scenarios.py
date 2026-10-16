"""Model registry: role × provider × endpoint lifecycle (MODL)."""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario

MODEL_ROLES = ("generation", "embedding", "rerank", "bogus")
MODEL_PROVIDERS = ("openai_compatible", "openai", "fastembed", "bogus")



def build() -> list[Scenario]:
    out = []

    def add(fam, feature, wf, inp, cfg, exp, typ, params):
        out.append(Scenario(fam, "features", feature, wf, inp, cfg, exp, typ, "API up; tenant A", params))

    for role in MODEL_ROLES:
        for prov in MODEL_PROVIDERS:
            for url in ("none", "unreachable"):
                ok = role != "bogus" and prov != "bogus"
                add("MODL", "model registry", "model lifecycle", f"role={role} provider={prov}", f"base_url={url}",
                    "registered; key never echoed; test fails cleanly; default is unique per role; delete" if ok
                    else "refused", "integration" if ok else "negative", {"role": role, "provider": prov, "url": url, "ok": ok})
    return out
