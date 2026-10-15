"""Ingestions: pairwise configuration × action sequences as a state machine (INGS)."""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario, pairwise

INGESTION_ACTIONS = {
    "pause": ["pause"], "pause_pause": ["pause", "pause"], "resume_unpaused": ["resume"],
    "pause_resume": ["pause", "resume"], "pause_run": ["pause", "run"], "run": ["run"], "sync": ["sync"],
    "pause_sync": ["pause", "sync"],
}

def build() -> list[Scenario]:
    out = []

    def add(fam, feature, wf, inp, cfg, exp, typ, params):
        out.append(Scenario(fam, "features", feature, wf, inp, cfg, exp, typ, "API up; tenant A", params))

    for row in pairwise({"kind": ["unstructured", "structured", "semistructured"], "pipeline": ["none", "default"],
                         "schedule": [None, "*/30 * * * *", "not-a-cron"], "actions": list(INGESTION_ACTIONS)}):
        add("INGS", "ingestions", "ingestion state machine", f"kind={row['kind']} pipeline={row['pipeline']}",
            f"schedule={row['schedule']} actions={row['actions']}", "each transition allowed or refused as specified",
            "state-transition", row)
    return out
