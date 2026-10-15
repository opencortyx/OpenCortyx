"""CapabilityRegistry — the single place capabilities are registered and looked up.

Built-ins register at import (see `capabilities/__init__.py`). SDK plugins do the same:
    from opencortyx.capabilities.registry import registry
    registry.register(MyCapability())
The pipeline never imports capability modules directly — it asks the registry for whatever
implements a given hook, so adding a capability is register-only (Open/Closed).
"""
from __future__ import annotations

from opencortyx.capabilities.base import (
    AnswerGate, Capability, PostIngestHook, RetrievalScorer,
)


class CapabilityRegistry:
    def __init__(self):
        self._caps: dict[str, Capability] = {}

    def register(self, cap: Capability) -> Capability:
        if not getattr(cap, "name", ""):
            raise ValueError("capability must define a stable `name`")
        self._caps[cap.name] = cap
        return cap

    def get(self, name: str) -> Capability | None:
        return self._caps.get(name)

    def all(self) -> list[Capability]:
        return list(self._caps.values())

    # ── hook selectors — what the dispatcher iterates ──
    def post_ingest_hooks(self) -> list[Capability]:
        return [c for c in self._caps.values() if isinstance(c, PostIngestHook)]

    def scorers(self) -> list[Capability]:
        return [c for c in self._caps.values() if isinstance(c, RetrievalScorer)]

    def gates(self) -> list[Capability]:
        return [c for c in self._caps.values() if isinstance(c, AnswerGate)]


# process-wide singleton
registry = CapabilityRegistry()
