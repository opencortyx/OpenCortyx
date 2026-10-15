"""Capability platform — the stable interface every knowledge-platform concern plugs into.

A *capability* is a self-contained concern (Versioning, Contradiction Detection, Resolution,
Trust, Abstain, …) that OWNS its own config + data and participates in the pipeline through a
small set of well-defined lifecycle HOOKS. Built-in capabilities register at import time; a
future SDK plugin registers the exact same way (`registry.register(MyCapability())`).

Design (SOLID):
  • Interface Segregation — a capability implements ONLY the hook Protocols it participates in
    (a post-ingest capability need not know anything about retrieval, and vice-versa).
  • Dependency Inversion — the pipeline depends on these abstract hooks, not on concrete modules;
    dispatch iterates whatever is registered.
  • Single source of config truth — `config_schema()` drives BOTH the API and the UI generically,
    so a new capability appears in the Settings UI with zero UI code.

Config is deliberately backed by the existing `runtime_setting` store (via `core.config.runtime`),
so there is no data migration and every value/tuned-default currently in use carries over.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


# ── config schema (same shape as runtime_config.SCHEMA so the UI renderer is shared) ──────────────
@dataclass
class ConfigField:
    key: str
    label: str
    kind: str = "bool"                 # bool | int | float | select | text | json
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    options: Optional[list] = None
    help: str = ""
    section: str = ""
    #: One line under that heading explaining what this group of settings governs.
    section_help: str = ""

    def to_dict(self) -> dict:
        d = {"key": self.key, "label": self.label, "kind": self.kind, "help": self.help}
        if self.section:
            d["section"] = self.section
            d["section_help"] = self.section_help
        if self.min is not None:
            d["min"] = self.min
        if self.max is not None:
            d["max"] = self.max
        if self.options is not None:
            d["options"] = self.options
        return d


# ── contexts handed to hooks ──────────────────────────────────────────────────────────────────────
@dataclass
class IngestContext:
    """Passed to PostIngestHook after a source has finished ingesting."""
    workspace_id: uuid.UUID
    source_ref: Optional[uuid.UUID]         # the unit node (Source / dataset) id, or None (schema-level)
    kind: str = "document"                  # document | structured | semistructured
    unit_type: str = "Source"               # hint for the versioning unit's node type
    meta: dict = field(default_factory=dict)


# ── lifecycle hook Protocols — implement only what you participate in ──────────────────────────────
@runtime_checkable
class PostIngestHook(Protocol):
    async def on_source_ingested(self, ctx: IngestContext) -> dict: ...


@runtime_checkable
class RetrievalScorer(Protocol):
    async def score(self, query: str, context: str, **kw) -> Any: ...


@runtime_checkable
class AnswerGate(Protocol):
    # Retrieval-time gates are enforced inline in retrieve/answer.py; this marker lets the capability
    # own its config + advertise the hook. `describe()` returns the human-readable gate list.
    def gate_names(self) -> list[str]: ...


# ── the capability base class ──────────────────────────────────────────────────────────────────────
class Capability:
    name: str = ""                          # stable slug (persisted / API key) — never rename
    title: str = ""
    group: str = ""                         # platform-tree node, e.g. "Conflict Detection"
    description: str = ""
    ui_route: Optional[str] = None          # where it is configured in the app (deep link)
    enable_key: Optional[str] = None        # runtime_config key that turns it on/off (None → always on)

    def config_schema(self) -> list[ConfigField]:
        return []

    def enabled(self, workspace_id=None) -> bool:
        from opencortyx.core.config import runtime as rc
        if not self.enable_key:
            return True
        return bool(rc.get(self.enable_key))

    def values(self, workspace_id=None) -> dict:
        """Current config values, defaults filled in. Sync + DB-free (reads runtime_config only).
        DB-backed fields (e.g. a template's compare-passes) are added via `async_values`."""
        from opencortyx.core.config import runtime as rc
        out = {}
        for f in self.config_schema():
            v = rc.get(f.key)
            out[f.key] = f.default if v is None else v
        return out

    async def async_values(self, workspace_id=None) -> dict:
        """Extra config values that require a DB read (override in capabilities that need it)."""
        return {}

    async def apply(self, values: dict) -> None:
        """Persist config. Default: write recognised runtime_config keys. Capabilities with extra
        config (e.g. a template's compare-passes) override this and call super().apply()."""
        from opencortyx.core.config import runtime as rc
        allowed = {f.key for f in self.config_schema() if f.kind != "json" or f.key in rc._DEFAULTS}
        if self.enable_key:
            allowed.add(self.enable_key)
        clean = {k: v for k, v in (values or {}).items() if k in allowed}
        if clean:
            await rc.update(clean, specs=[f.to_dict() for f in self.config_schema()])

    def hooks(self) -> list[str]:
        h = []
        if isinstance(self, PostIngestHook):
            h.append("post_ingest")
        if isinstance(self, RetrievalScorer):
            h.append("retrieval_scorer")
        if isinstance(self, AnswerGate):
            h.append("answer_gate")
        return h

    def to_dict(self, workspace_id=None) -> dict:
        return {
            "name": self.name, "title": self.title, "group": self.group,
            "description": self.description, "ui_route": self.ui_route,
            "enable_key": self.enable_key, "enabled": self.enabled(workspace_id),
            "schema": [f.to_dict() for f in self.config_schema()],
            "values": self.values(workspace_id), "hooks": self.hooks(),
        }

    async def to_dict_async(self, workspace_id=None) -> dict:
        """Full descriptor including DB-backed values — awaited by the API layer."""
        d = self.to_dict(workspace_id)
        extra = await self.async_values(workspace_id)
        if extra:
            d["values"] = {**d.get("values", {}), **extra}
        return d
