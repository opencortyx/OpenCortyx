"""Capability config surface — thin helpers over the capabilities' own `values()` / `apply()`.

Config is stored in the existing `runtime_setting` table (via core.config.runtime), so there is
no new table and no migration; each capability declares its schema and the values live under the
same keys the Settings UI already persists.
"""
from __future__ import annotations

from opencortyx.capabilities.registry import registry


async def describe_all(workspace_id=None) -> list[dict]:
    """Every registered capability as a UI/API-ready descriptor (identity + schema + values)."""
    return [await c.to_dict_async(workspace_id) for c in registry.all()]


async def describe(name: str, workspace_id=None) -> dict | None:
    cap = registry.get(name)
    return await cap.to_dict_async(workspace_id) if cap else None


async def apply(name: str, values: dict, workspace_id=None) -> dict | None:
    """Persist a capability's config (only keys in its schema / its enable_key are honoured)."""
    cap = registry.get(name)
    if cap is None:
        return None
    await cap.apply(values or {})
    return cap.to_dict(workspace_id)
