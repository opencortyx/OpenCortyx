"""The Drafter contract.

One method decides whether a drafter applies, one produces the draft. That is the whole surface,
and it is small on purpose: the registry can then choose a drafter without knowing what any of
them do, and a new one (drafting a connector config, a collection policy, a glossary) is a class
plus one `register()` line rather than an edit to a dispatch table.

Two rules every implementation must honour, because callers depend on them:

  1. **Never raise for lack of a model.** A drafter produces its best heuristic result when the
     LLM is unreachable, disabled (`DraftRequest.use_model=False`) or returns nonsense, and says
     so through `Confidence` and `warnings`. Authoring is the wrong place to discover that vLLM
     is down: the user is holding a file, not debugging inference.
  2. **Write nothing.** `draft()` is a pure read. Drafts are proposals; persisting one is the
     user's act, through the CRUD that already owns that object.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from opencortyx.drafting.models import Draft, DraftKind, DraftRequest


class Drafter(ABC):
    """Produces one kind of draft from one kind of sample."""

    #: Stable registry key, dotted: "mapping.semistructured", "extractor.llm". Persisted in
    #: `Draft.drafter` and read back in telemetry, so renaming one is a breaking change.
    name: str = ""

    #: Which control-plane object this drafts.
    kind: DraftKind

    #: Shown in the UI when offering the drafter. One sentence, no trailing full stop.
    description: str = ""

    @abstractmethod
    def supports(self, req: DraftRequest) -> bool:
        """Can this drafter say anything useful about `req`?

        Cheap and side-effect free — the registry calls it on every drafter for every request.
        Answering True commits to producing a Draft, so check the sample facet you actually read
        (a structured drafter handed a sample with no `columns` must answer False, not produce an
        empty mapping).
        """

    @abstractmethod
    async def draft(self, req: DraftRequest) -> Draft:
        """Produce the proposal. Must not raise for a reachable-but-unhelpful model."""

    def describe(self) -> dict:
        return {"name": self.name, "kind": self.kind.value, "description": self.description}


__all__ = ["Drafter"]
