"""DrafterRegistry — the single place drafters are registered and chosen.

Built-ins register at import (see `authoring/__init__.py`); anything else registers the same way.
The routes never import a drafter module, they ask the registry — so adding a drafter is
register-only, and the API surface does not grow a branch per kind.

Selection is `supports()` filtered by kind, in registration order. Order is therefore meaningful
and stated where the built-ins are registered: the first drafter that supports a request is the
one a caller gets when it does not name one.
"""
from __future__ import annotations

from opencortyx.drafting.models import DraftKind, DraftRequest
from opencortyx.drafting.port import Drafter


class DrafterRegistry:
    def __init__(self) -> None:
        self._drafters: dict[str, Drafter] = {}

    def register(self, drafter: Drafter) -> Drafter:
        if not getattr(drafter, "name", ""):
            raise ValueError("drafter must define a stable `name`")
        if not isinstance(getattr(drafter, "kind", None), DraftKind):
            raise ValueError(f"drafter {drafter.name!r} must declare a DraftKind")
        self._drafters[drafter.name] = drafter
        return drafter

    def get(self, name: str) -> Drafter | None:
        return self._drafters.get(name)

    def all(self) -> list[Drafter]:
        return list(self._drafters.values())

    def for_kind(self, kind: DraftKind) -> list[Drafter]:
        return [d for d in self._drafters.values() if d.kind == kind]

    def candidates(self, req: DraftRequest) -> list[Drafter]:
        """Every drafter that both matches the kind and claims the request, in registration order.

        `supports()` is treated as untrusted: a drafter that raises while deciding is skipped
        rather than allowed to fail the request, because one broken third-party drafter must not
        be able to take authoring down for the built-ins.
        """
        out = []
        for d in self.for_kind(req.kind):
            try:
                if d.supports(req):
                    out.append(d)
            except Exception:                                    # noqa: BLE001 — see docstring
                continue
        return out


#: process-wide singleton, mirroring `capabilities.registry`
registry = DrafterRegistry()

__all__ = ["DrafterRegistry", "registry"]
