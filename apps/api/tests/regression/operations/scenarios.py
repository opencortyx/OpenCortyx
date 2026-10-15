"""Operations over wall-clock time: scheduled sync (SYNC) and worker crash & redelivery (RCVR)."""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario

SYNC = {
    "fires_per_minute": ("scheduled '* * * * *'", "fires once per matching minute; never twice in one minute"),
    "first_pass_adds_all": ("scheduled, 2 JSON files", "first run: added=2"),
    "unchanged_pass_skips": ("scheduled, no change", "a later run: added=changed=removed=0, unchanged=2"),
    "added_file_detected": ("file added between ticks", "next run: added=1"),
    "changed_file_detected": ("file modified between ticks", "next run: changed=1"),
    "removed_file_detected": ("file deleted between ticks", "next run: removed=1"),
    "paused_never_runs": ("scheduled, paused", "no run while paused"),
    "resume_restarts": ("scheduled, paused then resumed", "runs again after resume"),
    "on_demand_never_runs": ("on-demand", "no automatic run"),
    "realtime_every_tick": ("realtime", "runs on repeated ticks"),
    "invalid_cron_is_inert": ("scheduled 'not-a-cron'", "never fires, and the tick keeps serving others"),
    "failure_hibernates": ("scheduled, folder missing", "run fails; ingestion hibernates"),
    "hibernation_self_heals": ("scheduled, folder missing", "retried after the backoff, not before"),
    "documents_scheduled": ("scheduled, 1 Markdown file", "document ingested by the tick"),
}
RCVR = {
    "kill_once_mid_ingest": ("graphrag worker's task process SIGKILLed once mid-run (OOM model)",
                             "task redelivered; run completes; graph equals a single ingest"),
    "kill_twice_gives_up": ("task process SIGKILLed on two consecutive attempts",
                            "given up after MAX_LOST_ATTEMPTS with the reason; never stuck running"),
    "kill_idle_worker": ("idle pool process SIGKILLed", "the pool respawns; the next upload completes"),
    "kill_extraction_worker": ("extraction worker's task process SIGKILLed during a document run",
                               "restarts; the run reaches a final state"),
}


def build() -> list[Scenario]:
    out = [Scenario("SYNC", "operations", "scheduled sync", "celery beat tick over wall-clock minutes", k, cfg, exp,
                    "state-transition", "API + workers + beat up; allow_local_files toggled for the test", {"case": k})
           for k, (cfg, exp) in SYNC.items()]
    out += [Scenario("RCVR", "operations", "worker recovery", "crash a live worker", k, cfg, exp, "failure-injection",
                     "host docker CLI; API + workers up", {"case": k}) for k, (cfg, exp) in RCVR.items()]
    return out
