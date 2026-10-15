"""Write-time ontology validation — keeps the graph clean without sacrificing flexibility.

Philosophy: the platform stays *open* (you can add any type), so an UNREGISTERED type is always
allowed. But once a type IS defined in the dictionary, writes must respect that definition:
  • nodes  → required fields declared in the type's `properties_schema` must be present;
  • edges  → endpoints must satisfy the edge type's `allowed_source_types` / `allowed_target_types`.

These run on the user-facing authoring API (POST /nodes, /edges). Trusted ingestion paths write
through NodeRepo directly and are not gated, so bulk extraction stays fast.
"""
from opencortyx.domain.ontology import TypeRegistry, find_type, visible_types


async def validate_node(db, workspace_id, type_name: str, properties: dict | None) -> tuple[bool, str]:
    """OK unless the type is registered in this organization's ontology AND declares required fields
    that are missing."""
    rows = await visible_types(db, workspace_id, TypeRegistry.name == type_name,
                               TypeRegistry.kind.in_(["node", "entity"]))
    if not rows:
        return True, ""                                   # unregistered type → flexible, allowed
    props = properties or {}
    for reg in rows:
        fields = (reg.properties_schema or {}).get("fields") or []
        missing = [f.get("name") for f in fields
                   if isinstance(f, dict) and f.get("required") and f.get("name") not in props]
        if missing:
            return False, f"type '{type_name}' requires field(s): {', '.join(m for m in missing if m)}"
    return True, ""


async def validate_edge(db, workspace_id, *, etype: str, source_type: str, target_type: str) -> tuple[bool, str]:
    """OK unless the edge type is registered in this organization's ontology with endpoint constraints
    that the endpoints violate."""
    reg = await find_type(db, workspace_id, etype, "edge")
    if not reg:
        return True, ""                                   # unregistered edge type → allowed
    _src = str(source_type or "").strip().lower()
    _tgt = str(target_type or "").strip().lower()
    if reg.allowed_source_types and _src not in {str(t).strip().lower() for t in reg.allowed_source_types}:
        return False, (f"edge '{etype}' does not allow source type '{source_type}' "
                       f"(allowed: {', '.join(reg.allowed_source_types)})")
    if reg.allowed_target_types and _tgt not in {str(t).strip().lower() for t in reg.allowed_target_types}:
        return False, (f"edge '{etype}' does not allow target type '{target_type}' "
                       f"(allowed: {', '.join(reg.allowed_target_types)})")
    return True, ""
