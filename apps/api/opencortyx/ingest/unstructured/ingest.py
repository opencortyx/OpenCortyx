"""Native v2 document ingestion (Phase 2) — NO v1 import.

Bytes → parse (parsing.parse_content) → chunk → write Source + Chunk nodes (CONTAINS/NEXT) with
embeddings DIRECTLY into v2 → run the existing ontology-guided extractor + enrichment
(summaries, hypothetical questions). Replaces the v1 mirror (`adapt_asset`) for documents.

IDEMPOTENT: a document's identity is deterministic (uuid5 of org+filename), and a content
`checksum` is stored on the Source. Re-ingesting:
  • identical content  → no-op ("unchanged"), nothing duplicated.
  • changed content    → the old chunks + chunk-derived nodes are purged and the doc is rebuilt
                         in place under the same Source id, with `__version` incremented.
Shared entities are NOT deleted on re-ingest (they may be referenced by other documents); only
their stale edges to removed chunks cascade away.
"""
import asyncio
import re
import uuid

from opencortyx.core.db import V2Session
from opencortyx.core.substrate import NodeRepo
from opencortyx.core.inference.llm import complete
from opencortyx.ingest.formats.unstructured.documents import parse_content, extract_title
from opencortyx.ingest.unstructured.chunking import chunk_document
from opencortyx.ingest.semistructured.ingest import evaluate_key_filters
from opencortyx.core.observability import get_logger
from opencortyx.domain.control import SchemaMapping
from opencortyx.core.inference.registry import resolve_model
from opencortyx.ingest.extraction.templates import existing_template_names, run_named_templates_on_chunks

log = get_logger(__name__)

_repo = NodeRepo()

#: Stamped on every node the reader writes to carry the document's text in reading order — chunks,
#: sections, layout elements. They are passages, not assertions; the claims extracted from them are.
READING_UNIT_KEY = "__reading_unit"

_CONFIG_STAGES = {"json_structures", "structure_card", "structure_cards",
                  "base_schema", "introspect", "table_mappings", "custom_mappings"}
_CHUNK_STAGES = {"chunking", "chunking_rules"}


def _stage_kind(t: str):
    """Classify a pipeline stage type into an executor kind."""
    t = (t or "").strip()
    tl = t.lower()
    if not tl or tl in _CONFIG_STAGES:
        return None                              # config pseudo-stage / unnamed — not an executor
    if tl in _CHUNK_STAGES:
        return "chunk"                           # Step-1 reading rules → resolves to the chunks
    return "template"                            # a built-in extractor, or a custom template name


async def _pairs_for_input_nodes(workspace_id, roots, spec) -> list:
    """[(node_id, text)] for the NODES + ATTRIBUTES a stage picked as its input.

    A stage's input is "which nodes, read through which attributes". The node types come from the
    schema mapping bound to the ingestion (or the defaults when there is none), so this is the same
    question in all three paradigms — the semi-structured builder has always asked it, and documents
    and tables now do too. Empty selection → the caller's default (the reading units).

    Scoped to the run's OWN nodes (`roots` and their descendants) so a stage never silently reads
    another document's — or another table's — nodes. `roots` is the asset for a document ingest, the
    base tables for a structured one; the roots themselves are candidates too, so a stage can select
    the asset or the table node itself."""
    wanted = {}
    for s in (spec or []):
        if isinstance(s, dict) and s.get("node_type"):
            wanted[s["node_type"]] = [a for a in (s.get("attrs") or []) if a]
    roots = [r for r in ([roots] if roots and not isinstance(roots, (list, tuple, set)) else (roots or [])) if r]
    if not wanted or not roots:
        return []
    from opencortyx.stores.graph import NodeQuery
    from opencortyx.stores.graph.query import select_nodes
    candidates = await _repo.get_many(workspace_id, roots) + await _repo.descendants(workspace_id, roots, max_depth=3)
    nodes = select_nodes(candidates, NodeQuery(types=list(wanted)))
    out = []
    for n in nodes:
        p = n.properties or {}
        attrs = wanted.get(n.type) or wanted.get(p.get("__base_type") or "") or []
        if attrs:
            text = "\n".join(f"{a}: {p[a]}" for a in attrs if p.get(a) not in (None, ""))
        else:
            text = p.get("text") or n.label or ""
        if str(text).strip():
            out.append((n.id, str(text)))
    return out


async def _remap_pairs_to_field(workspace_id, pairs, fields):
    """Cross-step attribute wiring: re-key a prior step's (node_id, text) pairs so the NEXT step reads
    CHOSEN attribute(s) of those nodes instead of their default text. `fields` may be a single name or
    a list; multiple are combined as "field: value" lines. Empty / 'text' → unchanged."""
    from opencortyx.ingest.semistructured.ingest import extract_prose_context
    if isinstance(fields, str):
        fields = [fields]
    fields = [str(f).strip() for f in (fields or []) if str(f).strip() and str(f).strip() != "text"]
    if not fields or not pairs:
        return pairs
    import json as _json
    pm = {n.id: n for n in await _repo.get_many(workspace_id, [i for i, _ in pairs])}
    out = []
    for i, t in pairs:
        n = pm.get(i)
        if not n:
            out.append((i, t))
            continue
        props = n.properties or {}
        parts = []
        for f in fields:
            v = props.get(f)
            if v in (None, ""):
                continue
            sv = v if isinstance(v, str) else _json.dumps(v)
            parts.append(sv if len(fields) == 1 else f"{f}: {sv}")
        fallback_text = extract_prose_context(props) or t
        out.append((i, "\n".join(parts) if parts else fallback_text))
    return out


_FILTER_KEYS = ("include_keys", "exclude_keys", "min_words", "max_words")


async def _apply_stage_filters(workspace_id, pairs, cfg: dict):
    """Per-stage Include/Exclude keys + word-count bounds (the builder's "Filters" block) applied to
    the pairs a stage is about to read. Both builders have offered these for a while and NOTHING
    consumed them — a stage that said "only node:Summary" or "exclude attr:ssn" ran over everything.

    • include/exclude `node:X` / `entity:X` gate WHICH input nodes the stage runs over
    • bare / `attr:X` entries gate the input node's ATTRIBUTES, so an excluded attribute (ssn, ip)
      never reaches the model even when the node itself is in scope
    • min_words / max_words bound the text a stage is willing to read
    No filter keys in the config → the pairs pass through untouched (no query, no cost)."""
    cfg = cfg or {}
    if not pairs or not any(cfg.get(k) not in (None, "", [], 0) for k in _FILTER_KEYS):
        return pairs
    inc, exc = cfg.get("include_keys"), cfg.get("exclude_keys")
    min_w = int(cfg.get("min_words") or 0)
    max_w = int(cfg.get("max_words") or 0)
    # does include_keys name ATTRIBUTES (not just node types)? then the stage reads those attributes
    inc_list = inc if isinstance(inc, list) else str(inc or "").split(",")
    attr_scoped = any(k.strip() and not k.strip().startswith(("node:", "entity:")) for k in inc_list)
    try:
        rows = await _repo.get_many(workspace_id, [i for i, _ in pairs])
    except Exception as e:                       # noqa: BLE001 — a filter must never sink the stage
        log.warning("document.dag_filter_lookup_failed", error=str(e))
        return pairs
    by_id = {r.id: r for r in rows}
    out = []
    for nid, text in pairs:
        n = by_id.get(nid)
        ntype = n.type if n is not None else ""
        attrs = dict((n.properties or {}) if n is not None else {})
        attrs.setdefault("text", text)
        ok, kept = evaluate_key_filters(ntype, attrs, inc, exc)
        if not ok:
            continue
        txt = text
        if attr_scoped:                          # read exactly the included attributes
            parts = [f"{k}: {v}" for k, v in kept.items()
                     if k != "text" and isinstance(v, (str, int, float, bool)) and str(v).strip()]
            txt = "\n".join(parts) or str(kept.get("text") or "")
        elif "text" in attrs and "text" not in kept:
            continue                             # the stage's own input attribute was excluded
        txt = str(txt or "")
        wc = len(txt.split())                    # bounds apply to what the stage actually reads
        if not wc or (min_w and wc < min_w) or (max_w and wc > max_w):
            continue
        out.append((nid, txt))
    return out


async def _run_dag(workspace_id, source_id, chunk_pairs, stages, collection_id,
                   reporter=None, base_index=0) -> dict:
    """Execute the pipeline as an ordered DAG. Each stage runs over its declared Input Source —
    a previous stage's OUTPUT or the chunks — so extractors can be chained on top of any step.

    `reporter`/`base_index` thread live progress: each runnable stage reports running→completed/failed
    and the loop aborts cooperatively when the run is canceled."""
    totals = {"custom": 0}
    outputs = {"document": chunk_pairs}          # source-key → pairs
    for s in stages:                             # chunking stage ids resolve to the chunks
        if isinstance(s, dict) and _stage_kind(s.get("type")) == "chunk":
            outputs[s.get("id")] = chunk_pairs

    known = await existing_template_names(
        workspace_id, [s.get("type") for s in stages
                 if isinstance(s, dict) and _stage_kind(s.get("type")) == "template"])
    idx = base_index
    for s in stages:
        if not isinstance(s, dict):
            continue
        kind = _stage_kind(s.get("type"))
        if kind in (None, "chunk"):
            continue                             # automatic / no-op stages
        idx += 1
        if reporter and await reporter.is_canceled():
            break                                # cooperative cancel — stop at the stage boundary
        name = s.get("type") or "Stage"
        status = known.get(s.get("type"))
        if status != "active":
            # A saved pipeline can outlive an extractor (a retired built-in, a deleted custom one),
            # or name one that is switched off. Said so on the run, rather than reported as a stage
            # that ran and found nothing.
            why = "extractor no longer exists" if status is None else "extractor is turned off"
            totals.setdefault("skipped_stages", []).append(s.get("type"))
            if reporter:
                await reporter.step(idx, f"{name} — {why}", "skipped")
            continue
        if reporter:
            await reporter.step(idx, name, "running")
        cfg = s.get("config") or {}
        in_pairs = outputs.get(s.get("source"), chunk_pairs)
        # An explicit node+attribute selection wins over the step wiring: the stage was told exactly
        # which nodes to read and through which attributes.
        if cfg.get("input_nodes"):
            picked = await _pairs_for_input_nodes(workspace_id, source_id, cfg["input_nodes"])
            if picked:
                in_pairs = picked
        _in_fields = cfg.get("input_fields")                # cross-step field wiring (multi-select)
        if _in_fields is None:
            _in_fields = cfg.get("input_field")             # back-compat with the single-field form
        in_pairs = await _remap_pairs_to_field(workspace_id, in_pairs, _in_fields)
        in_pairs = await _apply_stage_filters(workspace_id, in_pairs, cfg)   # Include/Exclude + word bounds
        sid = s.get("id")
        try:
            cnt = await run_named_templates_on_chunks(
                workspace_id, [s.get("type")], in_pairs, collection_id)
            totals["custom"] += cnt
            # publish THIS extractor's emitted nodes (via its declared emit edge) so a downstream
            # stage can chain over them — e.g. a temporal detector feeding the date normalizer.
            from opencortyx.ingest.extraction.templates import emitted_pairs_by_name
            outputs[sid] = await emitted_pairs_by_name(workspace_id, s.get("type"), [nid for nid, _ in in_pairs])
            if reporter:
                await reporter.step(idx, name, "completed", cnt)
        except Exception as e:                   # one stage failing never aborts the pipeline
            totals.setdefault("errors", []).append(f"{s.get('type')}: {e}")
            if reporter:
                await reporter.step(idx, name, "failed")
            log.warning("document.dag_stage_failed", stage=str(s.get("type")), error=str(e))
    return totals


def _source_id(workspace_id: uuid.UUID, filename: str) -> uuid.UUID:
    """Stable identity for a named document within an org → re-upload updates, never duplicates."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"{workspace_id}:source:{filename}")


def _chunk_id(source_id: uuid.UUID, ordinal: int) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"{source_id}:chunk:{ordinal}")


_MD_TABLE_RE = re.compile(r"(?:^\|.*\|[ \t]*\n)(?:^\|[ \t:|-]+\|[ \t]*\n)(?:^\|.*\|[ \t]*\n?)+", re.M)
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def document_elements(text: str) -> dict:
    """The non-prose ELEMENTS a document carries: tables and figures.

    `parse_content` returns text, but for the formats that matter that text is MARKDOWN — `.md` files
    natively, and other formats through their layout reader, which renders tables as pipe tables and figures as `![alt](src)`.
    So the elements ARE in there; nothing had ever looked for them, which is why the engine's "Embedded
    Tables & Grids" / "Images, Figures & Charts" switches were inert in both positions.

    This is deliberately a PARSER, not a vision model: it reports what the document literally contains.
    A real layout/vision pipeline would find more (tables in scanned images, uncaptioned figures), and
    when one exists it can fill the same two lists without anything downstream changing.
    """
    body = text or ""
    tables = []
    for m in _MD_TABLE_RE.finditer(body):
        block = m.group(0).strip()
        rows = [r for r in block.splitlines() if r.strip()]
        header = [c.strip() for c in rows[0].strip("|").split("|")] if rows else []
        tables.append({"markdown": block[:4000], "columns": header,
                       "row_count": max(0, len(rows) - 2), "offset": m.start()})
    images = [{"caption": (m.group(1) or "").strip(), "src": (m.group(2) or "").strip(),
               "offset": m.start()} for m in _MD_IMAGE_RE.finditer(body)]
    return {"tables": tables, "images": images}


def _det_element_id(source_id: uuid.UUID, kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"{source_id}:{kind}:{key}")


def _det_section_id(source_id: uuid.UUID, heading: str) -> uuid.UUID:
    """A section is identified by its HEADING within the document, not by position — so an edit that
    shifts a section down the page updates it instead of minting a second one."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"{source_id}:section:{heading.strip().lower()}")


async def _mark_ingested_ok(workspace_id: uuid.UUID, source_id: uuid.UUID) -> None:
    """Stamp __ingested_ok on the Source AFTER extraction finished — the completion half of the
    'unchanged' skip contract. The start-of-ingest upsert rewrites properties WITHOUT this flag,
    so a doc is only ever skippable when its last run truly ran to the end; a mid-way crash/kill
    leaves the flag absent and the next upload re-processes the document."""
    try:
        await _repo.patch_properties(workspace_id, [source_id], {"__ingested_ok": True}, bump_version=False)
    except Exception as e:                        # never let the stamp fail an otherwise-good ingest
        log.warning("document.ingested_ok_stamp_failed", source_id=str(source_id), error=str(e))


def _plan_sig(plan: dict) -> str:
    """Stable fingerprint of the pipeline plan — so re-ingesting the SAME file with a DIFFERENT
    pipeline (e.g. now with Topics) still re-processes instead of being skipped as 'unchanged'."""
    import hashlib
    import json
    return hashlib.sha1(json.dumps(plan or {}, sort_keys=True, default=str).encode()).hexdigest()[:16]


async def _purge_document(workspace_id: uuid.UUID, source_id: uuid.UUID) -> None:
    """Remove a document's chunks and chunk-derived nodes (claims, template extractions, summaries)
    so a changed re-ingest rebuilds cleanly. Shared entities (no source_ref) are preserved."""
    chunks = [n.id for n in await _repo.find(workspace_id, source_refs=[source_id]) if n.id != source_id]
    derived = [n.id for n in await _repo.find(workspace_id, source_refs=chunks)] if chunks else []
    await _repo.delete_nodes(workspace_id, derived + chunks)


async def _drop_chunk_nodes(workspace_id: uuid.UUID, source_id: uuid.UUID, node_type: str) -> int:
    """'Extract only' mode (Step-1 save_chunks=false): AFTER extraction, delete the persisted chunk
    nodes but KEEP everything extracted from them. Provenance is preserved by rewiring to the Source:
      • edges OUT of a chunk (e.g. MENTIONS chunk→entity)  → source becomes the Source
      • edges INTO a chunk  (e.g. EXTRACTED_FROM x→chunk)  → target becomes the Source
      • structural chunk edges (CONTAINS / NEXT)           → dropped with the chunks
      • nodes whose source_ref pointed at a chunk          → repointed to the Source
    Idempotent; only runs when the pipeline explicitly chose it. Returns #chunks removed."""
    structural = ("CONTAINS", "NEXT")
    chunk_ids = [n.id for n in await _repo.find(workspace_id, source_refs=[source_id], exact_types=[node_type])
                 if n.id != source_id]
    if not chunk_ids:
        return 0
    chunks = {str(c) for c in chunk_ids}
    rewired = []
    for edge in await _repo.edges(workspace_id, node_ids=chunk_ids, exclude_types=list(structural)):
        src = source_id if str(edge.source_id) in chunks else edge.source_id
        dst = source_id if str(edge.target_id) in chunks else edge.target_id
        if str(src) != str(dst):
            rewired.append({"source_id": src, "target_id": dst, "type": edge.type, "weight": edge.weight,
                            "properties": edge.properties, "confidence": edge.confidence})
    await _repo.add_edges(workspace_id=workspace_id, edges=rewired)
    for node in await _repo.find(workspace_id, source_refs=chunk_ids):
        node.source_ref = source_id
        await _repo.save(node)
    await _repo.delete_nodes(workspace_id, chunk_ids)
    return len(chunk_ids)


async def _detect_doc_version(workspace_id: uuid.UUID, source_id: uuid.UUID, fingerprint: int):
    """Document version detection — RELOCATED to the Versioning capability
    (capabilities/modules/versioning.py::detect_version) so version matching runs uniformly across
    ALL datasource types, not just documents. Kept as a thin, back-compatible delegator for the
    document path. Gated by the `simhash_versioning` toggle."""
    from opencortyx.core.config import runtime as rc
    if not rc.simhash_versioning():
        return None
    from opencortyx.capabilities.modules.versioning import detect_version
    return await detect_version(workspace_id, source_id, rc.simhash_max_distance())


def _step1_chunk_config(pipeline) -> dict:
    """Pull the Step-1 (chunking) stage's config from a document pipeline DAG (or {} if none)."""
    for s in (pipeline or []):
        if isinstance(s, dict) and _stage_kind(s.get("type")) == "chunk":
            return s.get("config") or {}
    return {}


async def resolve_step1_chunk_config(workspace_id, pipeline, filename: str | None = None) -> dict:
    """The Step-1 reading rules for a run — how the document is split, what the chunk node is, and
    which attributes and edges it carries.

    These rules describe HOW TO READ A SOURCE, which is the schema mapping's job, so they live on the
    bound Unstructured mapping alongside its node/edge rules. The pipeline only references the mapping,
    so editing the mapping changes how documents are read without re-saving any pipeline.

    Two blocks come off the mapping, in this order:
      • `payload.step1` — the NODE/ATTRIBUTE/EDGE rules the author drew on the canvas, translated by
        `document_map_translate`. This is what used to be dropped: only `chunking` was ever read, so a
        mapping declaring a `TextChunk` with a `content` attribute still produced a bare `Chunk`.
        Mappings saved before that translator existed are translated here, on the fly, from `editor`.
      • `payload.editor.chunking` — the reading card (strategy, size, overlap, chunk node name). It is
        applied second, so an explicit reading-card value still wins.

    Anything neither declares falls back to the inline stage config, so pipelines saved before the move
    (and unbound/auto runs) behave exactly as they did. Only the FIRST bound mapping is used: a document
    is split one way, even when several mappings contribute node/edge rules. A `filename` the mapping
    does not describe (see `signature.match_document`) is read with the default rules instead."""
    cfg = _step1_chunk_config(pipeline)
    ids = [str(x) for x in (cfg.get("mapping_ids") or []) if x]
    if not ids and cfg.get("mapping_id"):
        ids = [str(cfg["mapping_id"])]
    if not ids:
        return cfg
    try:
        import uuid as _uuid
        async with V2Session() as db:
            m = await db.get(SchemaMapping, _uuid.UUID(ids[0]))
        if m is None or m.workspace_id != workspace_id:
            return cfg
        from opencortyx.ingest.pipelines import signature as _sig
        if filename and _sig.match_document([{"source_type": m.source_type, "payload": m.payload or {}}],
                                            filename) is None:
            return cfg                         # not this mapping's documents: the default walker's rules
        payload = m.payload or {}
        editor = payload.get("editor") or {}
        step1 = payload.get("step1")
        if step1 is None and editor:
            from opencortyx.ingest.unstructured.translate import translate_document_editor
            step1 = translate_document_editor(editor).get("step1") or {}
        step1 = step1 or {}
        merged = dict(cfg)
        for block in (step1, editor.get("chunking") or {}):
            for k, v in block.items():
                if v is not None and not (k == "node_type" and step1.get("node_type")):
                    merged[k] = v
        if step1.get("node_type"):
            merged["node_type"] = step1["node_type"]
        return merged
    except Exception as e:                     # noqa: BLE001 — never lose an ingest over reading rules
        log.warning("document.reading_rules_resolve_failed", mapping_id=str(ids[0]), error=str(e))
        return cfg


async def document_ignored_reason(workspace_id, pipeline, filename: str):
    """SIGNATURE GATE for documents (see ingest/pipelines/signature.py): the reason this file must be IGNORED,
    or None to ingest it.

    A document has no schema, so a bound Unstructured mapping's signature is the file types it
    declares (`source_type` / `editor.formats`). With auto-accept OFF, a bound mapping is an
    allow-list — a `.md` will not be read through a mapping authored for PDFs. A mapping that
    declares no file types matches everything, so the gate only fires on an explicit declaration."""
    cfg = _step1_chunk_config(pipeline)
    from opencortyx.ingest.pipelines import signature as _sig
    ids = [str(x) for x in (cfg.get("mapping_ids") or []) if x]
    if not ids and cfg.get("mapping_id"):
        ids = [str(cfg["mapping_id"])]
    if not ids:
        return None                            # no bound mapping → nothing to match against
    # Auto-accept ON lets a document the mapping does not describe through to the default walker;
    # OFF makes the bound mapping an allow-list (signature.auto_create_on, same for all paradigms).
    if _sig.auto_create_on(cfg, bound=True):
        return None
    try:
        import uuid as _uuid
        mappings = []
        async with V2Session() as db:
            for mid in ids:
                m = await db.get(SchemaMapping, _uuid.UUID(mid))
                if m is not None and m.workspace_id == workspace_id:
                    mappings.append({"source_type": m.source_type, "payload": m.payload or {}})
        if not mappings:
            return None
        if _sig.match_document(mappings, filename) is None:
            declared = sorted({e for m in mappings for e in _sig._exts_of(m)})
            return (f"{filename} does not match the bound schema mapping"
                    f"{' (' + ', '.join(declared) + ')' if declared else ''} and auto-accept is off")
    except Exception as e:                     # noqa: BLE001 — a gate must never sink an ingest
        log.warning("document.signature_gate_failed", error=str(e), action="ingesting anyway")
    return None


def _compute_attr(expr: str, *, text: str, ordinal: int, offset: int, chunk_size: int) -> str:
    """A `computed` chunk attribute — a tiny fixed vocabulary of derivations, no eval."""
    e = (expr or "").strip().lower()
    words = len((text or "").split())
    table = {
        "word_count": words, "words": words,
        "char_count": len(text or ""), "chars": len(text or ""), "length": len(text or ""),
        "ordinal": ordinal, "index": ordinal,
        "page": offset // max(1, chunk_size) + 1,
    }
    return str(table.get(e, ordinal))


def _regex_attr(pattern: str, text: str) -> str:
    """A `regex` chunk attribute — first match of the pattern against the chunk text ("" if none)."""
    import re
    try:
        m = re.search(pattern or "", text or "")
        return m.group(0) if m else ""
    except Exception:
        return ""


async def _llm_attr(workspace_id, spec: dict, text: str) -> str:
    """An `LLM` chunk attribute — run the attribute's own prompt+model over the chunk. `{text}` in
    the prompt is the chunk. Model 'workspace default' (or empty) → the org's generation model."""
    prompt = (spec.get("prompt") or "").strip()
    if not prompt:
        return ""
    ref = (spec.get("model") or "").strip()
    if ref.lower() in ("", "workspace default", "default"):
        ref = None
    model_spec = await resolve_model(workspace_id, "generation", ref)
    filled = prompt.replace("{text}", text or "")
    try:
        out = await complete(filled, model_spec=model_spec, max_tokens=120)
        return (out or "").strip()
    except Exception as e:
        log.warning("document.chunk_llm_attr_failed", error=str(e))
        return ""


async def _chunk_props(workspace_id, base: dict, attr_specs: list, discovered: dict,
                       text: str, ordinal: int, offset: int, chunk_size: int) -> dict:
    """Build a chunk node's properties: the base (text/ordinal) plus every declared attribute,
    each resolved by its mechanism (verbatim / computed / regex / LLM)."""
    props = dict(base)
    for a in (attr_specs or []):
        if not isinstance(a, dict):
            continue
        name = (a.get("name") or "").strip()
        if not name:
            continue
        mech = (a.get("mechanism") or a.get("mech") or "verbatim").strip().lower()
        if mech == "verbatim":
            # `from` is the SOURCE field when the mapping renames it (file_size → size). Without it a
            # renamed attribute looks itself up under its new name and is always empty.
            src = (a.get("from") or a.get("source") or name)
            val = discovered.get(src)
            if val is None and src != name:
                val = discovered.get(name)
            if val is None:
                val = text if src in ("text", "content", "body") or name in ("text", "content", "body") else ""
            props[name] = val
        elif mech == "computed":
            props[name] = _compute_attr(a.get("expr"), text=text, ordinal=ordinal,
                                        offset=offset, chunk_size=chunk_size)
        elif mech == "regex":
            props[name] = _regex_attr(a.get("pattern") or "", text)
        elif mech in ("llm", "loom"):
            props[name] = await _llm_attr(workspace_id, a, text)
    return props


async def _draw_chunk_edges(workspace_id, chunk_id, props: dict, edge_specs: list, collection_id) -> int:
    """Draw the card's declared edges. A chunk edge PROMOTES one of the chunk's attributes to its
    own node and links to it: `Chunk -[REL]-> Target(label = props[from_attr])`, deduped per
    collection. Edges without a `from_attr` (e.g. MENTIONS→Entity) are the downstream entity
    extractor's job, not chunk-write time, so they're skipped here."""
    from opencortyx.stores.graph.query import normalize_name
    made = 0
    for e in (edge_specs or []):
        if not isinstance(e, dict):
            continue
        rel = (e.get("rel") or e.get("relationship") or "").strip().upper().replace(" ", "_")
        target = (e.get("target") or "").strip()
        from_attr = (e.get("from_attr") or e.get("via") or "").strip()
        if not (rel and target and from_attr):
            continue
        val = props.get(from_attr)
        if not isinstance(val, str) or not val.strip():
            continue
        nid = uuid.uuid5(uuid.NAMESPACE_DNS, f"{workspace_id}:{target}:{normalize_name(val)}")
        await _repo.upsert_node(node_id=nid, workspace_id=workspace_id, type=target, label=val.strip()[:300],
                                properties={"name": val.strip()}, collection_id=collection_id, level=0)
        if await _repo.add_edge(workspace_id=workspace_id, source_id=chunk_id, target_id=nid, type=rel):
            made += 1
    return made


async def ingest_document(workspace_id: uuid.UUID, content: bytes, filename: str,
                          mime_type: str = "text/plain", collection_id=None,
                          chunk_size: int = 2000, overlap: int = 200, plan: dict | None = None,
                          pipeline: list | None = None, reporter=None,
                          defer_extraction: bool = False, replace: bool = False) -> dict:
    """Parse + chunk a document into the v2 substrate, then extract + enrich. Idempotent re-ingest.

    Two execution modes:
      • `pipeline` (ordered stages with per-stage source + config) → real **DAG** execution: each
        stage runs over its declared Input Source (a prior stage's output or the chunks), so
        extractors chain on top of any step.
      • `plan` (flag dict from upload features) → flat per-chunk extraction (backward compatible).

    `defer_extraction` stops after the document is parsed, chunked, embedded and COMMITTED, and
    reports what still owes extraction. The caller then queues that work separately. This is what
    keeps a large document from occupying every worker slot for the duration of its LLM extraction
    while small uploads queue behind it — the document is searchable either way, because retrieval
    runs on chunks and embeddings, not on extracted entities."""
    from opencortyx.ingest.pipelines.defaults import normalize_stages
    from opencortyx.ingest.unstructured.simhash import simhash64, to_str
    # canonical stage vocabulary, whatever the caller sent (inline stages from the browser, a saved
    # pipeline row, a system default): the Step-1 card must be findable as `type: "chunking"`.
    pipeline = normalize_stages(pipeline) if pipeline else pipeline
    # signature gate: a bound mapping is an allow-list when auto-accept is off — refuse BEFORE any
    # parsing or writing, so an ignored document leaves no trace in the graph.
    _ignored = await document_ignored_reason(workspace_id, pipeline, filename)
    if _ignored:
        if reporter is not None:
            await reporter.start(1)
            await reporter.step(1, "Ignored — no matching schema mapping", "completed", 0)
        return {"status": "ignored", "title": filename, "reason": _ignored, "chunks": 0}
    plan = dict(plan or {})
    parsed = parse_content(content, mime_type, filename=filename)
    text = parsed["raw_text"] or ""
    title = (filename or extract_title(text))[:300]
    checksum = parsed["checksum"]
    fingerprint = simhash64(text)                  # 64-bit SimHash for near-duplicate version detection

    step1 = await resolve_step1_chunk_config(workspace_id, pipeline, filename)
    # Layout unless a mapping or the stage asks for windows: it is the reading that keeps
    # the document's own structure — titles, sections, tables, lists and figures as node types of
    # their own. Sliding windows cut text into equal pieces and keep none of it.
    strategy = step1.get("strategy") or "layout"
    c_size = int(step1.get("chunk_size") or chunk_size)
    c_over = int(step1.get("overlap") or overlap)
    node_type = (step1.get("node_type") or "Chunk").strip() or "Chunk"
    from opencortyx.ingest.pipelines import signature as _sig
    save_chunks = not _sig.extract_only_on(step1)  # "Extract only": chunks feed extraction, then are dropped
    attr_specs = step1.get("chunk_attrs") or []
    edge_specs = step1.get("edges") or []
    # The TIERS a bound mapping declares (document_map_translate). Absent → the flat shape this always
    # had: the asset stays a `Source` and owns its chunks by CONTAINS.
    root_type = (step1.get("root_type") or "").strip()
    section_type = (step1.get("section_type") or "").strip()
    # Mapping-declared reading rules that used to be saved and ignored entirely.
    chunk_filters = step1.get("chunk_filters") or {}
    edge_attrs = step1.get("edge_attrs") or {}
    elements_cfg = step1.get("elements") or {}
    identity_cfg = step1.get("identity") or {}
    table_type = (step1.get("table_type") or "TableEntity").strip()     # "Table" is a catalogued asset
    image_type = (step1.get("image_type") or "Figure").strip()
    # the edge the MAPPING declared for each element; the literal is only the historical default
    table_edge = (step1.get("table_edge") or "CONTAINS_TABLE").strip()
    image_edge = (step1.get("image_edge") or "CONTAINS_IMAGE").strip()
    section_edge = (step1.get("section_edge") or "HAS_SECTION").strip()
    chunk_edge = (step1.get("chunk_edge") or "CONTAINS").strip()
    chunk_recs = chunk_document(text, strategy, chunk_size=c_size, overlap=c_over)
    # FILTER RULES — "skip empty chunks", "ignore header/footer", "skip pages matching …". Applied to
    # the split before anything is written, so a filtered chunk never becomes a node at all.
    if chunk_filters:
        _pats = [re.compile(p, re.I) for p in (chunk_filters.get("skip_patterns") or [])
                 if str(p).strip()]

        def _keep(rec) -> bool:
            t = (rec.get("text") or "").strip()
            if chunk_filters.get("skip_empty") and not t:
                return False
            if chunk_filters.get("skip_header_footer") and len(t) < 80 and chr(10) not in t:
                return False        # a lone short line with no body is page furniture, not content
            return not any(p.search(t) for p in _pats)
        chunk_recs = [r for r in chunk_recs if _keep(r)]
    has_headings = any((r.get("attrs") or {}).get("heading") for r in chunk_recs)
    build_sections = bool(section_type and has_headings)

    if identity_cfg.get("fields"):
        _idvals = {"file_name": filename, "filename": filename, "title": title,
                   "checksum": checksum, "mime": mime_type,
                   "language": parsed["language"], "word_count": str(parsed["word_count"]),
                   "creation_date": "", "created_at": ""}
        _key = "|".join(f"{f}={_idvals.get(f, '')}" for f in identity_cfg["fields"])
        if identity_cfg.get("strategy") == "CREATE_NEW":
            _key += f"|checksum={checksum}"
        source_id = _source_id(workspace_id, _key)
    else:
        source_id = _source_id(workspace_id, filename or title)

    # progress: total = parse/chunk step + one step per runnable pipeline stage (flat path → +1)
    if reporter is not None:
        if pipeline:
            runnable = sum(1 for s in pipeline if isinstance(s, dict)
                           and _stage_kind(s.get("type")) not in (None, "chunk"))
            await reporter.start(1 + runnable)
        else:
            await reporter.start(2)
        await reporter.set_source(str(source_id))

    sig = _plan_sig({"pipeline": pipeline} if pipeline else plan)
    pairs = []
    existing = await _repo.get(workspace_id, source_id)
    version = 1
    if existing is not None:
        ex_props = existing.properties or {}
        if (not replace and ex_props.get("__checksum") == checksum
                and ex_props.get("__plan_sig") == sig and ex_props.get("__ingested_ok")
                and await _repo.find(workspace_id, source_refs=[source_id], limit=1)):
            # "Unchanged" also requires the content to still BE there: a document whose chunks
            # were removed kept its completion stamp and every re-upload was skipped, leaving an
            # asset with no knowledge and no way back. `replace` re-reads it on request.
            return {"status": "unchanged", "source_id": str(source_id), "title": title,
                    "version": int(ex_props.get("__version", 1)),
                    "chunks": 0, "rels": 0}
        version = int(ex_props.get("__version", 1)) + 1   # content or plan changed → rebuild
        await _purge_document(workspace_id, source_id)

    src_props = {"filename": filename, "mime": mime_type, "checksum": checksum,
                 "language": parsed["language"], "word_count": parsed["word_count"],
                 "native": True, "__checksum": checksum, "__version": version, "__plan_sig": sig,
                 "__simhash": to_str(fingerprint)}
    if root_type and root_type != "Source":
        src_props["__base_type"] = "Source"
    await _repo.upsert_node(node_id=source_id, workspace_id=workspace_id, type=(root_type or "Source"), label=title,
        properties=src_props, collection_id=collection_id, level=0)
    if root_type and root_type != "Source":
        from opencortyx.ontology.extractors import register_emit_types
        async with V2Session() as db:
            await register_emit_types(db, workspace_id, {"nodes": [{"type": root_type}]}, source="document_mapping")
            await db.commit()

    prev, offset = None, 0
    sections: dict = {}                # heading → section node id (one per distinct heading)

    layout_reader: dict = {}
    if strategy == "layout":
        from opencortyx.ingest.formats.unstructured.layout import LayoutEngine
        # In a thread: layout analysis is seconds of CPU per page, and run inline it froze this
        # worker's event loop — every other coroutine on it (progress writes, heartbeats) stalled.
        assembler = await asyncio.to_thread(
            LayoutEngine.convert, content, filename=filename, doc_id=str(source_id))
        layout_reader = {"engine": assembler.engine, "fallback_reason": assembler.fallback_reason}
        if assembler.engine == "fallback":
            log.warning("document.layout_fallback", filename=filename,
                        reason=assembler.fallback_reason)
        await _record_document_metadata(workspace_id, source_id, assembler, filename, src_props)
        ast_nodes = assembler.nodes
        created_nodes = {}
        # READING UNITS: sections, not elements. Each heading starts a unit and the content under it
        # joins until the next heading, so downstream stages read what a person would. Fed element
        # by element, a summary stage "summarised" three-word headings and the model invented their
        # content (a travel policy's lodging limits from its title alone).
        units: list = []                                  # [node_id, [texts]] in reading order
        # Written as ONE batch after the loop: node by node it was ~17 graph statements per element
        # (the node, its date link, its CONTAINS and NEXT edges, a degree update per endpoint), which
        # made a 150-element document 2,500 sequential round trips and most of its ingest time. Ids
        # are deterministic, so nothing in the loop needs the write's result.
        node_specs: list = []
        edge_specs_out: list = []
        from opencortyx.core.config import runtime as _rc
        unit_cap = _rc.reading_unit_max_chars()           # Settings > Chunking
        for i, ast_node in enumerate(ast_nodes):
            nid = uuid.uuid5(source_id, ast_node.id)
            props = {
                "text": ast_node.text,
                "label": ast_node.label,
                "page_number": ast_node.metadata.page_number,
                "bounding_box": ast_node.metadata.bounding_box.model_dump(),
                "__source": "document",
                "ordinal": i,
            }
            if ast_node.level is not None:
                props["level"] = ast_node.level
            if ast_node.grid:
                props["grid"] = ast_node.grid
                props["row_count"] = len(ast_node.grid)
            if ast_node.asset_reference:
                props["asset_reference"] = ast_node.asset_reference
            props[READING_UNIT_KEY] = True

            # Layout `table`/`picture` elements take the mapping's element types (the same
            # `table_type`/`image_type` the non-layout path uses). Written as the reader names them, a
            # table in a Word file became a `Table` — the catalogue's type for a database table —
            # and was listed among the workspace's assets.
            kind = _LAYOUT_ELEMENT_KINDS.get(ast_node.label)
            ntype = table_type if kind == "table" else image_type if kind == "image" else ast_node.type
            node_specs.append({"node_id": nid, "type": ntype,
                               "label": _element_label(ast_node.text) or f"{ntype} #{i}",
                               "properties": props, "source_ref": source_id,
                               "collection_id": collection_id, "level": 0})
            created_nodes[ast_node.id] = nid

            # Hierarchical edge: parent -> node
            # A top-level element's parent is the document root, whose AST id is the Source id as
            # a string — not a key in `created_nodes`. Looking it up returned None and every
            # top-level heading was written with no CONTAINS edge from its Source at all.
            parent_nid = created_nodes.get(ast_node.parent_id) or source_id
            edge_specs_out.append({"source_id": parent_nid, "target_id": nid, "type": "CONTAINS"})

            # Sequential NEXT edge in reading order
            if prev:
                edge_specs_out.append({"source_id": prev, "target_id": nid, "type": "NEXT"})
            prev = nid

            if ast_node.text:
                # A heading (or the lead-in) opens a unit, and so does a unit grown past the cap: a
                # plain-text RFC has no headings a reader recognises, and 227 KB became one unit.
                if ast_node.level is not None or not units \
                        or sum(len(t) for t in units[-1][1]) >= unit_cap:
                    units.append([nid, [ast_node.text]])
                else:
                    units[-1][1].append(ast_node.text)
        # Nodes first, so every edge's endpoints exist when the edges are written.
        await _repo.upsert_many(workspace_id, node_specs)
        # The elements are this upload's own: nothing else writes them while it runs, so their degrees
        # go in bulk. The Source is not in the list; other writers link to it.
        await _repo.add_edges(workspace_id=workspace_id, edges=edge_specs_out,
                              owned=[s["node_id"] for s in node_specs])
        pairs.extend((uid, "\n\n".join(texts)) for uid, texts in units)
    else:
        for i, rec in enumerate(chunk_recs):
            ct = rec["text"]
            discovered = rec.get("attrs") or {}
            props = await _chunk_props(workspace_id, {"text": ct, "ordinal": i}, attr_specs,
                                       discovered, ct, i, offset, c_size)
            props[READING_UNIT_KEY] = True
            # The SECTION this chunk belongs to — the heading the chunker reported for it. One node
            # per distinct heading, so an over-long section's several chunks share their section.
            parent_id = source_id
            if build_sections:
                heading = str(discovered.get("heading") or "").strip()
                if heading:
                    sid = sections.get(heading)
                    if sid is None:
                        snode = await _repo.upsert_node(node_id=_det_section_id(source_id, heading), workspace_id=workspace_id,
                            type=section_type, label=heading[:300],
                            properties={"heading": heading, "level": discovered.get("level"),
                                        "__source": "document", READING_UNIT_KEY: True},
                            source_ref=source_id, collection_id=collection_id, level=0)
                        sid = sections[heading] = snode.id
                        await _repo.add_edge(workspace_id=workspace_id, source_id=source_id,
                                             target_id=sid, type=section_edge)
                    parent_id = sid
            node = await _repo.upsert_node(node_id=_chunk_id(source_id, i), workspace_id=workspace_id, type=node_type,
                label=f"{node_type} #{i}", properties=props,
                source_ref=source_id, collection_id=collection_id, level=0)
            # The edge the mapping declared for "this chunk belongs to that" — CONTAINS when nothing
            # declared one, which is the shape every existing pipeline already produces.
            _cet = (chunk_edge if parent_id != source_id or step1.get("chunk_edge") else "CONTAINS")
            # EDGE ATTRIBUTE rules put a property on the RELATIONSHIP (HAS_CHUNK.page_num) — readable
            # only here, while the edge is being drawn, from the chunk's own resolved properties.
            _eprops = {a["prop"]: props.get(a["from_field"])
                       for a in (edge_attrs.get(_cet) or [])
                       if props.get(a["from_field"]) not in (None, "")}
            await _repo.add_edge(workspace_id=workspace_id, source_id=parent_id, target_id=node.id,
                                 type=_cet, properties=_eprops)
            if prev:
                # reading order — the walker's own structural edge, deliberately kept (Group NEXT)
                await _repo.add_edge(workspace_id=workspace_id, source_id=prev, target_id=node.id, type="NEXT")
            prev = node.id
            if edge_specs:
                await _draw_chunk_edges(workspace_id, node.id, props, edge_specs, collection_id)
            pairs.append((node.id, ct))
            offset += len(ct)

    _els = document_elements(text) if elements_cfg else {"tables": [], "images": []}
    if (elements_cfg.get("tables") or {}).get("enabled"):
        for ti, tb in enumerate(_els["tables"]):
            tnode = await _repo.upsert_node(node_id=_det_element_id(source_id, "table", str(ti)), workspace_id=workspace_id,
                type=table_type, label=f"{table_type} #{ti}",
                properties={"text": tb["markdown"], "columns": tb["columns"],
                            "row_count": tb["row_count"], "__source": "document"},
                source_ref=source_id, collection_id=collection_id, level=0)
            await _repo.add_edge(workspace_id=workspace_id, source_id=source_id,
                                 target_id=tnode.id, type=table_edge)
    if (elements_cfg.get("images") or {}).get("enabled"):
        for ii, im in enumerate(_els["images"]):
            label = im["caption"] or im["src"].rsplit("/", 1)[-1] or f"{image_type} #{ii}"
            inode = await _repo.upsert_node(node_id=_det_element_id(source_id, "image", im["src"] or str(ii)),
                workspace_id=workspace_id, type=image_type, label=label[:200],
                properties={"text": im["caption"] or label, "caption": im["caption"],
                            "src": im["src"], "__source": "document"},
                source_ref=source_id, collection_id=collection_id, level=0)
            await _repo.add_edge(workspace_id=workspace_id, source_id=source_id,
                                 target_id=inode.id, type=image_edge)

    if reporter is not None:
        await reporter.step(1, "Parse & chunk", "completed", len(pairs))

    version_info = None

    if defer_extraction:
        # NOT stamped complete here: the deferred half stamps it when it finishes. Stamped at
        # hand-off, a deferred run that died (or was purged) left the document "unchanged" to every
        # later upload of the same file, and its extraction or pipeline stages never ran again.
        return {**_reader_fields(layout_reader), "status": "ok", "source_id": str(source_id), "title": title, "version": version,
                "chunks": len(pairs), "doc_version": version_info,
                "extraction": "deferred", "pending_chunks": len(pairs),
                "mode": "dag" if pipeline else "flat",
                # what the deferred pipeline run needs to finish the job exactly as inline would
                "node_type": node_type, "save_chunks": save_chunks}

    # ── DAG mode: stages chain over each other's output (Modular Pipeline Builder) ──
    if pipeline:
        dag = await _run_dag(workspace_id, source_id, pairs, pipeline, collection_id,
                             reporter=reporter, base_index=1)
        if not save_chunks:                        # 'extract only' → keep extracted nodes, drop chunks
            dag["chunks_dropped"] = await _drop_chunk_nodes(workspace_id, source_id, node_type)
        await _mark_ingested_ok(workspace_id, source_id)
        return {**_reader_fields(layout_reader), "status": "ok", "source_id": str(source_id), "title": title,
                "version": version, "chunks": 0 if not save_chunks else len(pairs),
                "doc_version": version_info, **dag}

    # ── flat mode: per-chunk extraction driven by feature flags ──
    if reporter is not None:
        await reporter.step(2, "Extraction & enrichment", "running")
    totals = {"chunks": len(pairs), "rels": 0}

    # Nothing is extracted by default: the extractors that run are the ones the caller names.
    names = list(dict.fromkeys(plan.get("templates") or []))
    if names:
        bd: dict = {}
        try:
            totals["graph"] = await run_named_templates_on_chunks(
                workspace_id, names, pairs, collection_id,
                extra_ctx=None,
                breakdown=bd)
        except Exception as e:
            log.warning("document.unified_extraction_failed", error=str(e))
        totals["rels"] = int(bd.get("edges") or 0)
        totals["node_types"] = bd.get("node_types") or {}
        totals["edge_types"] = bd.get("edge_types") or {}

    # Asset profile sections — surfaced in the result so failures aren't silent
    from opencortyx.core.config import runtime as rc
    # global toggle: skip AI asset overviews when off (Settings → Ingestion)
    if plan.get("asset_sections", True) and rc.ai_overviews():
        try:
            from opencortyx.ingest.extraction.enrich import build_asset_sections
            totals["asset_sections"] = await build_asset_sections(source_id, workspace_id)
        except Exception as e:
            totals["asset_sections_error"] = str(e)
            log.warning("document.asset_sections_failed", error=str(e))

    if reporter is not None:
        await reporter.step(2, "Extraction & enrichment", "completed", totals.get("graph"))

    if not save_chunks:                            # 'extract only' → keep extracted nodes, drop chunks
        totals["chunks_dropped"] = await _drop_chunk_nodes(workspace_id, source_id, node_type)
        totals["chunks"] = 0
    await _mark_ingested_ok(workspace_id, source_id)
    return {**_reader_fields(layout_reader), "status": "ok", "source_id": str(source_id), "title": title, "version": version,
            "doc_version": version_info, **totals}


async def run_pipeline_over_source(workspace_id: uuid.UUID, source_id: uuid.UUID, stages: list,
                                   chunks: int, collection_id=None, reporter=None,
                                   save_chunks: bool = True, node_type: str = "Chunk") -> dict:
    """The deferred half of a PIPELINE document ingest: run its stages over chunks already committed.

    The chunks are addressed by their deterministic ids (`_chunk_id(source, i)`), in reading order,
    so the stages see exactly the pairs the inline run would have handed them — no re-parse, and no
    guessing which of a document's nodes were its reading units."""
    ids = [_chunk_id(source_id, i) for i in range(int(chunks or 0))]
    by_id = {n.id: n for n in await _repo.get_many(workspace_id, ids)}
    pairs = [(cid, str((by_id[cid].properties or {}).get("text") or ""))
             for cid in ids if cid in by_id]
    dag = await _run_dag(workspace_id, source_id, pairs, stages, collection_id,
                         reporter=reporter, base_index=1)
    if not save_chunks:                            # 'extract only' → keep extracted nodes, drop chunks
        dag["chunks_dropped"] = await _drop_chunk_nodes(workspace_id, source_id, node_type)
    await _mark_ingested_ok(workspace_id, source_id)
    return dag


async def extract_over_source(workspace_id: uuid.UUID, source_id: uuid.UUID, *,
                              plan: dict | None = None, collection_id=None,
                              reporter=None) -> dict:
    """Run per-chunk extraction over a document whose chunks ALREADY exist.

    The deferred half of `ingest_document(defer_extraction=True)`. It re-reads the committed chunks
    instead of re-parsing the file, so the expensive LLM work can run on its own queue, minutes or
    hours later, without the original bytes being kept around.

    Idempotent: extraction upserts by deterministic id, so running this twice re-derives the same
    nodes rather than duplicating them — which is what makes the task safe to retry.
    """

    plan = plan or {}
    rows = [n for n in await _repo.find(workspace_id, source_refs=[source_id], order_by="created_at")
            if n.id != source_id]
    # Only real text chunks feed extraction; a document's derived profile nodes are not inputs.
    pairs = [(n.id, (n.properties or {}).get("text") or "")
             for n in rows if (n.properties or {}).get("text")]
    totals = {"chunks": len(pairs), "rels": 0, "graph": 0}
    if not pairs:
        return {**totals, "status": "no_chunks"}

    names = list(dict.fromkeys(plan.get("templates") or []))
    if not names:
        return {**totals, "status": "nothing_to_extract"}

    bd: dict = {}
    try:
        totals["graph"] = await run_named_templates_on_chunks(
            workspace_id, names, pairs, collection_id,
            extra_ctx=None, breakdown=bd)
    except Exception as e:                                # noqa: BLE001
        log.warning("document.deferred_extraction_failed", source_id=str(source_id), error=str(e))
        return {**totals, "status": "error", "error": str(e)[:300]}

    totals["rels"] = int(bd.get("edges") or 0)
    totals["node_types"] = bd.get("node_types") or {}
    totals["edge_types"] = bd.get("edge_types") or {}
    if bd.get("chunks_capped"):
        totals["chunks_capped"] = bd["chunks_capped"]
    await _mark_ingested_ok(workspace_id, source_id)
    return {**totals, "status": "ok"}


#: Layout element labels the mapping retypes: a table takes `table_type`, a figure `image_type`.
_LAYOUT_ELEMENT_KINDS = {"table": "table", "picture": "image", "chart": "image"}


def _element_label(text: str, limit: int = 80) -> str:
    """A layout element's label: its own words, cut at a word boundary — never "Text #3"."""
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0] or t[:limit]
    return cut + "…"


#: Where a document's references to other documents wait for the resolution engine to link them.
REFERENCES_KEY = "__references"


async def _record_document_metadata(workspace_id, source_id, assembler, filename: str, src_props: dict) -> None:
    """Store what the document says about itself on its Source: its title, identifiers, publication
    date and header fields, and the documents it refers to (`REFERENCES_KEY`), which the resolution
    engine links once both are in the graph. A field never replaces one the Source already has."""
    from opencortyx.ingest.formats.unstructured.layout.metadata import document_metadata
    meta = document_metadata([n.text for n in assembler.nodes], [n.label for n in assembler.nodes], filename)
    values = {k: v for k, v in meta["fields"].items() if k not in src_props}
    for key in ("title", "identifiers", "published"):
        if meta[key] and key not in src_props:
            values[key] = meta[key]
    if meta["references"]:
        values[REFERENCES_KEY] = meta["references"]
    if values:
        src_props.update(values)
        await _repo.patch_properties(workspace_id, [source_id], values, bump_version=False)


def _reader_fields(layout_reader: dict) -> dict:
    """Which engine read a layout document, as flat scalars the run row keeps.

    Scalars because every ingest path reduces its result to them before `ProgressReporter.finish`
    stores it; a nested dict would be dropped on the semi-structured paths and survive on this one.
    """
    if not layout_reader:
        return {}
    out = {"reader": layout_reader.get("engine") or ""}
    if layout_reader.get("fallback_reason"):
        out["reader_note"] = str(layout_reader["fallback_reason"])[:300]
    return out
