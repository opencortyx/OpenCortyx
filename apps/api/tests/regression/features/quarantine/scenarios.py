"""Quarantine: actions on unknown items and the write-accounting summary (QUAR)."""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario


def build() -> list[Scenario]:
    out = []

    def add(fam, feature, wf, inp, cfg, exp, typ, params):
        out.append(Scenario(fam, "features", feature, wf, inp, cfg, exp, typ, "API up; tenant A", params))

    for action in ("delete", "retry", "rerun", "retry_invalid_json"):
        add("QUAR", "quarantine", "quarantine action", "unknown item", action, "404 or 422, never a server error",
            "negative", {"action": action})
    add("QUAR", "quarantine", "quarantine summary", "fresh tenant", "default", "empty and internally consistent",
        "contract", {"action": "summary"})

    return out
