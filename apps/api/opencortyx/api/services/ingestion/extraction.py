"""Services: extraction templates and the shapes ingestion creates."""



def template_view(t) -> dict:
    """Serialize a template for the Studio, deriving display metadata from the JSONB (no migration):
    category (structured/unstructured), deterministic (catalog/view-only), locked (built-in),
    executable (the generic enrichment engine can run it)."""
    mech = t.mechanism or {}
    ref = str(mech.get("ref") or "")
    deterministic = bool(mech.get("deterministic")) or ref.startswith("deterministic") or ref.startswith("pipeline")
    locked = (t.name or "").startswith("builtin:")
    # single source of truth: the ENGINE's own is_executable (LLM prompts, relate.*, and the
    # producible non-LLM mechanisms — regex/dictionary/keyword/ner/seed — all run generically)
    from opencortyx.ingest.extraction.templates import is_executable
    executable = is_executable(t)
    return {"id": str(t.id), "name": t.name, "source": t.source, "mechanism": mech,
            "emit": t.emit, "status": t.status,
            "category": mech.get("category") or "unstructured",
            "deterministic": deterministic, "locked": locked, "executable": executable}
