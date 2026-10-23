"""Services: schema mappings and their conformance."""
from opencortyx.core.observability import get_logger

log = get_logger(__name__)



MAPPING_PARADIGMS = ("structured", "semistructured", "unstructured")


def mapping_summary(paradigm: str, payload: dict, fallback: dict | None = None) -> dict:
    """Denormalized {targets, nodes, edges} for the hub table. Semi-structured is computed
    authoritatively from its declared structures (the graph it will actually build); other paradigms
    use the client-provided summary/fallback (their builders know their own target entities)."""
    try:
        if paradigm == "semistructured":
            from opencortyx.ingest.semistructured.registry import structure_types
            targets, nodes, edges = [], 0, 0
            for st in (payload or {}).get("structures") or []:
                for r in structure_types(st):
                    if r.get("kind") == "node":
                        nodes += 1
                        if r.get("name") and r["name"] not in targets:
                            targets.append(r["name"])
                    elif r.get("kind") == "edge":
                        edges += 1
            return {"targets": targets, "nodes": nodes, "edges": edges}
        if paradigm == "structured":
            tm = (payload or {}).get("table_mappings") or {}
            node_specs = tm.get("nodes") or []
            targets = []
            for n in node_specs:
                nt = n.get("nodeType")
                if nt and nt not in targets:
                    targets.append(nt)
            return {"targets": targets, "nodes": len(node_specs), "edges": len(tm.get("edges") or [])}
        if paradigm == "unstructured":
            # The vocabulary the conformance report grades against — what the mapping declares. With
            # no branch here a document mapping kept whatever summary the client sent, and a saved
            # Add-data reading (which sends none) listed as "0 nodes → 0 edges" beside 3 node rules.
            from opencortyx.ingest.pipelines.conformance import declared_shape
            shape = declared_shape({"paradigm": paradigm, "payload": payload or {}})
            return {"targets": sorted(shape["nodes"]), "nodes": len(shape["nodes"]),
                    "edges": len(shape["edges"])}
    except Exception as e:                             # noqa: BLE001 — summary is best-effort display data
        log.warning("schema_mapping.summary_failed", error=str(e))
    return fallback or {"targets": [], "nodes": 0, "edges": 0}


#: Paradigms whose sample is DATA the Mapper walks. An unstructured sample is a document's prose, and prose
#: that happens to parse as JSON ("42", "true") must never be reinterpreted as data.
_JSON_SAMPLE_PARADIGMS = ("semistructured", "structured")


def _sample_as_data(paradigm: str, sample):
    """A mapping's sample, as the data it encodes.

    The Mapper's Materialized graph walks `payload.sample`. The older semi-structured page sent an uploaded
    file's TEXT, and nothing normalised it on the way in — so five saved mappings stored a JSON-encoded
    string, and a string has no keys to walk: the preview drew 0 nodes for a mapping whose data had already
    ingested 10 dialogues and 6,079 turns. All five strings were valid JSON; the shape had simply never been
    decided in one place.

    It is decided here because `_prepare_mapping_payload` is the one function every writer AND every reader
    passes through — the create and update endpoints on save, and `_mapping_view` on every read — so legacy
    rows are served correctly without rewriting stored data. Text that does not parse is returned exactly
    as written, never destroyed. A reader that needs text (the semi-structured page edits the sample in a
    textarea) converts at its own boundary.
    """
    if paradigm not in _JSON_SAMPLE_PARADIGMS or not isinstance(sample, str):
        return sample
    text = sample.strip()
    if not text:
        return sample
    import json as _json
    try:
        return _json.loads(text)
    except ValueError:                                  # JSONDecodeError is a ValueError
        return sample


def prepare_mapping_payload(paradigm: str, name: str, payload: dict) -> dict:
    """For a semi-structured mapping authored in the engine (payload carries an `editor` rule model),
    translate that rule model into the walker's `structures` payload so ingestion can RUN it — while
    keeping the raw `editor` so the engine UI rehydrates exactly. Editor is the source of truth:
    structures are regenerated on every save. Other paradigms pass through unchanged."""
    payload = dict(payload or {})
    # The sample's SHAPE is settled before anything below can return early: a derived, untouched editor
    # returns a few lines down, and it must still serve its sample as data.
    if "sample" in payload:
        payload["sample"] = _sample_as_data(paradigm, payload["sample"])
    _ed = payload.get("editor") or {}
    if _ed.get("__derived") and not _ed.get("__touched"):
        return payload
    if _ed.get("__derived_view") and _ed.get("__touched"):
        try:
            from opencortyx.ingest.pipelines.mapping_migrate import promote_key_rows
            payload["editor"] = _ed = promote_key_rows(paradigm, _ed)
        except Exception as e:                     # noqa: BLE001 — never lose authoring on this
            log.warning("schema_mapping.key_rows_promotion_failed", error=str(e))
    if paradigm == "semistructured" and payload.get("editor"):
        try:
            from opencortyx.ingest.semistructured.translate import translate_editor
            payload.update(translate_editor(payload["editor"], name))   # sets structures/auto_accept/rules
        except Exception as e:                 # noqa: BLE001 — never lose authoring on a translate error
            log.warning("schema_mapping.translate_failed", direction="editor->structures", error=str(e))
    elif paradigm == "structured" and payload.get("editor"):
        try:
            from opencortyx.ingest.structured.translate import (
                translate_structured_editor,
            )
            payload.update(translate_structured_editor(payload["editor"], name))  # sets table_mappings/table
        except Exception as e:                 # noqa: BLE001
            log.warning("schema_mapping.translate_failed", direction="editor->table_mappings", error=str(e))
    elif paradigm == "unstructured" and payload.get("editor"):
        # This branch did not exist: an unstructured mapping's node/attribute/edge rules were saved and
        # never translated into anything ingestion reads, so only `editor.chunking` ever had an effect.
        try:
            from opencortyx.ingest.unstructured.translate import (
                translate_document_editor,
            )
            payload.update(translate_document_editor(payload["editor"], name))    # sets step1
        except Exception as e:                 # noqa: BLE001
            log.warning("schema_mapping.translate_failed", direction="editor->step1", error=str(e))
    return payload


async def register_mapping_types(db, workspace_id, paradigm: str, name: str, payload: dict) -> None:
    """Publish a mapping's declared node/edge types into the ontology.

    A mapping's vocabulary IS its schema: until those words exist in the type registry they cannot be
    picked in an extractor's source selector, cannot be validated as edge endpoints, and do not appear
    in the ontology/lineage views — so a STRUCTURED or DOCUMENT mapping's Customer/Account/SectionNode
    were invisible everywhere except the canvas that drew them. Only the semi-structured engine
    published its types; this is the other two, derived from the same `declared_shape` the conformance
    report grades against, so the ontology and the report can never disagree about what was declared.
    Registered as CANDIDATES (like any emitted type) — reviewable on the Ontology page.
    """
    from opencortyx.ontology.extractors import register_emit_types
    from opencortyx.ingest.pipelines.conformance import declared_shape
    shape = declared_shape({"paradigm": paradigm, "payload": payload or {}})
    # platform reading types are dropped inside register_emit_types, for every caller
    emit = {"nodes": [{"type": t} for t in sorted(shape["nodes"])],
            "edges": [{"type": t} for t in sorted(shape["edges"])]}
    if emit["nodes"] or emit["edges"]:
        await register_emit_types(db, workspace_id, emit, source=paradigm)


def mapping_view(m):
    payload = m.payload or {}
    try:
        payload = prepare_mapping_payload(m.paradigm, m.name, payload)
    except Exception as e:                        # noqa: BLE001 — a stale view must never 500 a read
        log.warning("schema_mapping.rederive_failed", mapping_id=str(m.id), error=str(e),
                    action="serving stored payload")
    try:
        from opencortyx.ingest.pipelines.mapping_migrate import with_editor
        payload = with_editor(m.paradigm, payload)
    except Exception as e:                        # noqa: BLE001
        log.warning("schema_mapping.editor_derive_failed", mapping_id=str(m.id), error=str(e),
                    action="serving payload without editor")
    return {"id": str(m.id), "name": m.name, "paradigm": m.paradigm,
            "source_type": m.source_type or "", "payload": payload,
            # Derived from the payload on read, as the payload itself is: a stored copy goes stale.
            "summary": mapping_summary(m.paradigm, payload, m.summary or {}), "status": m.status,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None}


def mapping_refs(stages, mapping_id: str) -> bool:
    """Whether any stage of a pipeline binds this schema mapping (single `mapping_id` or `mapping_ids`)."""
    for s in (stages or []):
        cfg = (s or {}).get("config") or {} if isinstance(s, dict) else {}
        ids = [str(x) for x in (cfg.get("mapping_ids") or []) if x]
        if cfg.get("mapping_id"):
            ids.append(str(cfg["mapping_id"]))
        if mapping_id in ids:
            return True
    return False


async def save_mapping_references(db, workspace_id, mapping_id, payload: dict | None) -> list[str]:
    """Save a mapping's declared join edges as reference rules on their types (the resolution engine
    links by them across files), replacing what this mapping declared before. Returns the types whose
    rules changed. The caller commits."""
    from opencortyx.resolution.spec import references_from_mapping, save_references
    source = f"mapping:{mapping_id}"
    return await save_references(db, workspace_id, source, references_from_mapping(payload or {}, source))


def link_existing_later(workspace_id, types: list[str]) -> None:
    """Queue linking the nodes already in the graph by the changed rules. Best-effort: rules apply to
    every file read from now on whatever happens here."""
    if not types:
        return
    try:
        from opencortyx.tasks.resolution import v2_link_existing
        v2_link_existing.delay(str(workspace_id), list(types))
    except Exception as e:                              # noqa: BLE001 — never fail a mapping save
        log.warning("schema_mapping.link_existing_not_queued", error=str(e))
