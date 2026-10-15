"""Capability platform — public entry point.

Importing this package registers every built-in capability as a side effect (register-only, so the
pipeline/UI pick them up automatically). Import order also fixes post-ingest run order: Versioning
retires a duplicate source before Contradiction Detection scans it.

    from opencortyx.capabilities import registry
    from opencortyx.capabilities.dispatch import run_post_ingest
"""
from opencortyx.capabilities.registry import registry
from opencortyx.capabilities.base import (
    Capability, ConfigField, IngestContext,
    PostIngestHook, RetrievalScorer, AnswerGate,
)

# ── register built-ins (the knowledge-quality capabilities) ──
from opencortyx.capabilities.modules.versioning import SimHashVersioning
from opencortyx.capabilities.modules.contradiction import ContradictionDetection
from opencortyx.capabilities.modules.trust import TrustScoring
from opencortyx.capabilities.modules.abstain import AbstainGate
from opencortyx.capabilities.modules.tree import ADAPTERS

registry.register(SimHashVersioning())        # runs first at post-ingest (retire dup → then detect)
registry.register(ContradictionDetection())
registry.register(TrustScoring())
registry.register(AbstainGate())
for _adapter in ADAPTERS:
    registry.register(_adapter)

__all__ = [
    "registry", "Capability", "ConfigField", "IngestContext",
    "PostIngestHook", "RetrievalScorer", "AnswerGate",
]
