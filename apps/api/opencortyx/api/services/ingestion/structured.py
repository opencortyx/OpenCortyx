"""Services: structured sources: SQL databases and table files."""
from opencortyx.ingest.formats.catalog import FORMATS, asset_kind


def _fingerprint(simhash) -> str | None:
    try:
        return format(int(simhash), "016x")
    except (TypeError, ValueError):
        return None


def asset_view(node) -> dict:
    """One catalogue row, built only from what ingestion recorded on the asset node."""
    props = node.properties or {}
    name = props.get("source_file") or props.get("filename") or ""
    fmt = FORMATS.get(name.rsplit(".", 1)[-1].lower()) if "." in name else None
    kind = asset_kind(node)
    return {
        "id": str(node.id),
        "label": node.label,
        "type": node.type,
        "table": props.get("table_name") or props.get("filename"),
        "domain": props.get("domain"),
        "mime": props.get("mime"),
        "description": props.get("description"),
        "kind": kind,
        "format": fmt.name if fmt else (props.get("source_system") if kind == "structured" else None),
        "source": props.get("source_system"),
        "fingerprint": _fingerprint(props.get("__simhash")),
        "rows": props.get("row_count"),
        "columns": props.get("column_count"),
        "words": props.get("word_count"),
        "sensitivity": node.sensitivity,
        "updated_at": node.updated_at.isoformat() if node.updated_at else None,
    }
