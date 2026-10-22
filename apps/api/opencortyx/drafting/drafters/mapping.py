"""(C) Derive a schema mapping from a sample, and say why it reads the source that way.

The derivation itself is NOT new. `schema.build_structure` has always turned one record into a
structure, `schema.scout_structure` has always refined its roles with one model call, and
`tabular.preview_file` has always typed a table's columns and guessed their roles. All of it ran
inside a worker, during ingestion, behind the `auto_accept` toggle — so the system was already
deciding how to read your file, and the only way to see the decision was to go and find the draft
card it had left on a pipeline afterwards.

These drafters move that work in front of the ingest and attach the reasons to it. The output is
the same compiled payload the mapping store already holds, so a draft that is saved is
indistinguishable from a hand-authored mapping and the builder UI renders it without changes
(`mapping_migrate.with_editor` derives the editor half on read).

A document's draft describes the structure its reading strategy will ACTUALLY build, never more:
a sliding-window reading produces a Document and its Chunks and nothing else, so a draft of one
must not promise Sections. When the sample has headings the drafter proposes the `layout`
strategy instead, which is the one that keeps them.
"""
from __future__ import annotations

import asyncio
import re

from opencortyx.drafting.llm import ask_json, sample_block
from opencortyx.drafting.models import Confidence, Draft, DraftKind, DraftRequest, Evidence
from opencortyx.drafting.port import Drafter
from opencortyx.drafting.sampling import THIN_RECORDS
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

SAVE_ENDPOINT = "/api/v2/schema-mappings"

#: Column roles `tabular.guess_role` emits that mean "this column is a key, not a value".
_KEY_ROLES = ("identity", "fk")
_FK_SUFFIX = re.compile(r"_id$", re.I)
#: A node type is spelled out by the ontology and by every later query, so a model-proposed one
#: must be a single capitalised word or it is discarded rather than sanitised.
_TYPE_NAME = re.compile(r"[A-Z][A-Za-z0-9]{1,39}")
#: Pages a document structure preview reads. A long document costs time per page; a preview needs the
#: document's shape, not all of it. Ingestion reads every page.
PREVIEW_PAGES = 5


class SemistructuredMappingDrafter(Drafter):
    """One structure covering the sampled records, with every role justified against the data."""

    name = "mapping.semistructured"
    kind = DraftKind.MAPPING
    description = "Reads a JSON/XML/YAML sample and derives the structure it will be walked as"

    def supports(self, req: DraftRequest) -> bool:
        from opencortyx.ingest.formats import catalog as formats
        return req.sample.route == formats.SEMISTRUCTURED and bool(req.sample.records)

    async def draft(self, req: DraftRequest) -> Draft:
        from opencortyx.ingest.semistructured import schema as ss

        sample = req.sample
        structure, warnings = _widest_structure(sample.records)
        if sample.truncated:
            warnings.append("derived from the start of the file; later records may differ in shape")

        confidence = Confidence.HIGH
        if len(sample.records) < THIN_RECORDS:
            confidence = Confidence.LOW
            warnings.append(f"only {len(sample.records)} record(s) to read; an optional key that "
                            f"appears later will be missing from this mapping")

        if req.use_model:
            from opencortyx.core.inference.registry import resolve_model
            spec = await resolve_model(req.workspace_id, "utility", req.model_ref)
            confidence, note = await _scout(sample.records, structure, confidence, spec)
            if note:
                warnings.append(note)

        body = {
            "structures": [structure],
            # The same default the Universal JSON pipeline ships with: an unrecognised shape is
            # accepted and recorded rather than parked, because a parked record is invisible.
            "auto_accept": True,
            "rules": [],
            "id_patterns": [],
            "sample": sample.records[0] if sample.records else {},
        }
        # Named like the other drafters' mappings — "<file> → <root type>" — so two JSON files do not
        # both save as the structure builder's generic "JSON Keys Classification".
        stem = (sample.filename or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        root = next((k.get("node_type") for k in structure.get("keys") or []
                     if k.get("kind") == "node" and not k.get("path")), None)
        name = (f"{stem} → {root}" if stem and root
                else str(stem or structure.get("name") or "Derived mapping"))
        structure["name"] = name
        nodes = [k for k in structure.get("keys") or [] if k.get("kind") == "node"]
        attrs = [k for k in structure.get("keys") or [] if k.get("kind") != "node"]
        readable = ss.text_paths(structure) + ss.document_paths(structure)
        text_note = f" ({len(readable)} long prose)" if readable else ""
        return Draft(
            kind=DraftKind.MAPPING,
            name=name,
            payload=_mapping_spec(name, "semistructured", sample.format, body),
            drafter=self.name,
            confidence=confidence,
            summary=(f"{len(nodes)} entity type(s) and {len(attrs)} attribute(s){text_note} "
                     f"from {len(sample.records)} sampled record(s)"),
            evidence=_record_evidence(structure),
            warnings=warnings,
            save_endpoint=SAVE_ENDPOINT,
        )


class StructuredMappingDrafter(Drafter):
    """Columns to one node type per table, plus an edge for every foreign key that names its target.

    Roles come from `tabular.guess_role`, the same function the structured builder shows — so the
    draft agrees with what a user would see opening the file in the builder by hand, rather than
    offering a second opinion about the same columns.
    """

    name = "mapping.structured"
    kind = DraftKind.MAPPING
    description = "Reads a CSV/Parquet/Excel sample and derives the table's node and edge rules"

    def supports(self, req: DraftRequest) -> bool:
        from opencortyx.ingest.formats import catalog as formats
        return req.sample.route == formats.STRUCTURED and bool(req.sample.columns)

    async def draft(self, req: DraftRequest) -> Draft:
        from opencortyx.ingest.structured.translate import _node_from_table
        from opencortyx.ingest.formats.structured.tabular import table_name_of

        sample = req.sample
        table = table_name_of(sample.filename or "table")
        node_type = _node_from_table(table)
        columns = [c for c in sample.records if isinstance(c, dict) and c.get("name")]

        warnings: list[str] = []
        is_renamed = False
        default_type = node_type
        if req.use_model:
            named, note = await _name_the_entity(req, table, columns)
            if note:
                warnings.append(note)
            elif named and named != node_type:
                node_type = named
                is_renamed = True

        attributes: list[dict] = []
        root_basis = (
            f"the columns describe a {node_type}; {default_type} was only the file's name"
            if is_renamed
            else f"one {node_type} entity node per table row ({len(columns)} columns)"
        )
        evidence: list[Evidence] = [Evidence(
            claim=f"{table} becomes a {node_type} node",
            basis=root_basis,
            path=table,
        )]
        foreign_keys: list[str] = []
        for col in columns:
            name = str(col["name"])
            role = str(col.get("role") or "dimension")
            if role == "ignore":
                continue
            attributes.append({"property": name, "column": f"{table}.{name}"})
            evidence.append(Evidence(claim=f"{name} → {_role_phrase(role)}",
                                     basis=_column_basis(col, role), path=f"{table}.{name}"))
            if role == "fk":
                foreign_keys.append(name)

        fallback = [{"property": "id",
                     "column": f"{table}.{columns[0]['name']}" if columns else table}]
        nodes: list[dict] = [{"nodeType": node_type, "attributes": attributes or fallback}]
        edges: list[dict] = []
        for col in foreign_keys:
            target = _fk_target_type(col)
            if not target or target == node_type:
                continue
            nodes.append({"nodeType": target,
                          "attributes": [{"property": "id", "column": f"{table}.{col}"}]})
            edge_type = f"HAS_{target.upper()}"
            edges.append({"sourceRef": 0, "targetRef": len(nodes) - 1, "edgeType": edge_type})
            evidence.append(Evidence(claim=f"{table}.{col} becomes a {target} node",
                                     basis=f"referenced {target} entity via foreign key",
                                     path=f"{table}.{col}"))
            evidence.append(Evidence(claim=f"{node_type} -{edge_type}-> {target}",
                                     basis=f"{col} is shaped like a foreign key to {target}",
                                     path=f"{table}.{col}"))

        if sample.truncated:
            warnings.append("column types were read from a sample; a rare value may widen one")
        if not edges:
            warnings.append("no foreign keys were recognised, so these rows stand alone in the "
                            "graph — add relationships in the Mapper if they reference another table")

        name = f"{table} → {node_type}"
        body = {"table_mappings": {"nodes": nodes, "edges": edges}, "table": table}
        return Draft(
            kind=DraftKind.MAPPING,
            name=name,
            payload=_mapping_spec(name, "structured", sample.format, body),
            drafter=self.name,
            confidence=Confidence.HIGH if columns else Confidence.LOW,
            summary=(f"one {node_type} per row, {len(attributes)} attribute(s), "
                     f"{len(edges)} relationship(s)"),
            evidence=evidence,
            warnings=warnings,
            save_endpoint=SAVE_ENDPOINT,
        )


class DocumentMappingDrafter(Drafter):
    """Document files (PDF, Markdown, TXT, DOCX) to Document, Section, and Chunk graph nodes."""

    name = "mapping.document"
    kind = DraftKind.MAPPING
    description = "Reads a document sample and derives its Document, Section, and Chunk graph structure"

    def supports(self, req: DraftRequest) -> bool:
        from opencortyx.ingest.formats import catalog as formats
        return req.sample.route == formats.DOCUMENT and bool((req.sample.text or "").strip())

    async def draft(self, req: DraftRequest) -> Draft:
        from opencortyx.ingest.walkers.unstructured import UnstructuredWalker

        sample = req.sample
        # Always layout: the reading that keeps a document's structure (titles, sections, tables,
        # lists, figures as node types of their own) and the one ingestion uses by default. This was
        # chosen only when the extracted TEXT had Markdown `#` headings — which a PDF's text seldom
        # has — so most PDFs were previewed, and then read, as sliding-window chunks. The layout reader reads
        # the FILE, where that structure is; the text is used only for a sample with no file bytes.
        strategy = "layout"
        # In a thread: layout reading is CPU-bound and would stall the API's event loop.
        info = await asyncio.to_thread(
            UnstructuredWalker.introspect, sample.document or sample.text or "",
            filename=sample.filename, mime=sample.format, strategy=strategy,
            max_pages=PREVIEW_PAGES)

        root = info["root"]
        table = info["table"]
        sections = info.get("sections") or []
        chunks = info.get("chunks") or {}

        headings = [n for n in (info.get("ast_nodes") or [])
                    if n.get("type") in ("Title", "SectionHeader", "Heading")]
        engine = info.get("engine") or ""
        evidence: list[Evidence] = [Evidence(
            claim=(f"read by layout ({engine + ' reader' if engine and engine != 'fallback' else 'built-in structural parser'}): "
                   f"{info.get('node_count', 0)} element(s) of {len(info.get('types') or [])} kind(s), "
                   f"{len(headings)} heading(s)"),
            basis=((f"first heading {str(headings[0].get('text') or '').strip()[:60]!r}" if headings
                    else "no headings found, so its elements hang directly from the document")
                   + (f"; its reader failed: {info.get('fallback_reason')}"
                      if engine == "fallback" else "")
                   + f"; previewed up to {PREVIEW_PAGES} page(s)"),
            path="document")]
        # 1. Document Root Node derived from walker
        evidence.append(Evidence(
            claim=f"document becomes a {root['nodeLabel']} node",
            basis=f"root entity for {info['filename']} ({info['char_count']:,} chars, {info['word_count']:,} words)",
            path="document",
        ))
        for a in root["attributes"]:
            evidence.append(Evidence(
                claim=f"document.{a['name']} → {_role_phrase(a['role'])}",
                basis=a["basis"],
                path=f"document.{a['name']}",
            ))

        node_rules = [{"nodeType": root["nodeType"], "nodeLabel": root["nodeLabel"], "elementSource": root["elementSource"]}]
        edge_rules = []
        attribute_rules = [
            {"attachToNode": root["nodeLabel"], "propertyName": a["name"], "sourceField": a["name"]}
            for a in root["attributes"]
        ]

        # 2. Layout Dynamic Nodes OR Structural Sections/Chunks
        if info.get("strategy") == "layout":
            dynamic_nodes = info.get("dynamic_nodes", [])
            for dn in dynamic_nodes:
                node_rules.append({"nodeType": dn["nodeType"], "nodeLabel": dn["nodeLabel"], "elementSource": dn["elementSource"]})
                evidence.append(Evidence(
                    claim=f"document.{dn['elementSource']}[] becomes a {dn['nodeLabel']} node",
                    basis=f"{dn['count']} {dn['nodeLabel']} element(s) discovered via layout parsing",
                    path=f"document.{dn['elementSource']}",
                ))
                for a in dn["attributes"]:
                    evidence.append(Evidence(
                        claim=f"document.{dn['elementSource']}[].{a['name']} → {_role_phrase(a['role'])}",
                        basis=a["basis"],
                        path=f"document.{dn['elementSource']}[].{a['name']}",
                    ))
                    attribute_rules.append({"attachToNode": dn["nodeLabel"], "propertyName": a["name"], "sourceField": a["name"]})

            for er in info.get("edge_rules", []):
                edge_rules.append(er)
                evidence.append(Evidence(
                    claim=f"{er['fromNode']} -{er['edgeLabel']}-> {er['toNode']}",
                    basis=f"{'hierarchical containment' if er['edgeLabel'] == 'CONTAINS' else 'sequential reading flow'} edge",
                    path="document",
                ))
        else:
            # 2. Structural Section Nodes discovered by walker
            if sections:
                sec = sections[0]
                node_rules.append({"nodeType": sec["nodeType"], "nodeLabel": sec["nodeLabel"], "elementSource": sec["elementSource"]})
                edge_rules.append({"fromNode": sec["edgeFrom"], "toNode": sec["nodeLabel"], "edgeLabel": sec["edgeLabel"]})
                evidence.append(Evidence(
                    claim="document.sections[] becomes a Section node",
                    basis=f"{sec['count']} unique heading section(s) discovered in document",
                    path="document.sections",
                ))
                evidence.append(Evidence(
                    claim=f"{sec['edgeFrom']} -{sec['edgeLabel']}-> {sec['nodeLabel']}",
                    basis="document contains structural sections",
                    path="document.sections",
                ))
                for a in sec["attributes"]:
                    evidence.append(Evidence(
                        claim=f"document.sections[].{a['name']} → {_role_phrase(a['role'])}",
                        basis=a["basis"],
                        path=f"document.sections[].{a['name']}",
                    ))
                    attribute_rules.append({"attachToNode": sec["nodeLabel"], "propertyName": a["name"], "sourceField": a["name"]})

            # 3. Content Chunk Nodes derived by walker
            node_rules.append({"nodeType": chunks["nodeType"], "nodeLabel": chunks["nodeLabel"], "elementSource": chunks["elementSource"]})
            chunk_parent_path = "document.sections[].chunks" if sections else "document.chunks"
            edge_rules.append({"fromNode": chunks["edgeFrom"], "toNode": chunks["nodeLabel"], "edgeLabel": chunks["edgeLabel"]})

            evidence.append(Evidence(
                claim=f"{chunk_parent_path}[] becomes a Chunk node",
                basis=f"split into {chunks['count']} passage chunk(s) via {chunks['strategy']}",
                path=chunk_parent_path,
            ))
            evidence.append(Evidence(
                claim=f"{chunks['edgeFrom']} -{chunks['edgeLabel']}-> {chunks['nodeLabel']}",
                basis=f"content split into passages under {chunks['edgeFrom'].lower()}",
                path=chunk_parent_path,
            ))
            for a in chunks["attributes"]:
                evidence.append(Evidence(
                    claim=f"{chunk_parent_path}[].{a['name']} → {_role_phrase(a['role'])}",
                    basis=a["basis"],
                    path=f"{chunk_parent_path}[].{a['name']}",
                ))
                attribute_rules.append({"attachToNode": chunks["nodeLabel"], "propertyName": a["name"], "sourceField": a["name"]})

        editor = {
            "nodeRules": node_rules,
            "edgeRules": edge_rules,
            "attributeRules": attribute_rules,
            "chunking": {
                "strategy": info.get("strategy") or "sliding_window",
                "chunk_size": 2000,
                "overlap": 200,
                "node_type": "Chunk",
                "root_type": root["nodeLabel"],
            },
        }
        body = {"editor": editor}
        name = f"{table} → Document Graph"

        warnings: list[str] = []
        if sample.truncated:
            warnings.append("sampled the first 8,000 characters; full document chunking will apply at ingest")

        return Draft(
            kind=DraftKind.MAPPING,
            name=name,
            payload=_mapping_spec(name, "unstructured", sample.format, body),
            drafter=self.name,
            confidence=Confidence.HIGH,
            summary=(f"root Document, {info.get('node_count', 0)} layout element(s) of "
                     f"{len(info.get('types') or [])} kind(s) ({info['char_count']:,} chars)"
                     if strategy == "layout" else
                     f"root Document, {chunks.get('count', 0)} chunk(s) "
                     f"({info['char_count']:,} chars prose)"),
            evidence=evidence,
            warnings=warnings,
            save_endpoint=SAVE_ENDPOINT,
        )


def _mapping_spec(name: str, paradigm: str, source_type: str, body: dict) -> dict:
    """Wrap a compiled mapping body in the exact request `POST /schema-mappings` accepts.

    `Draft.payload` is a request body, not a fragment: a client that has to know which envelope
    goes around which kind is a client that will one day wrap a structured body in the
    semi-structured envelope. Status is `active` because a saved draft is one the user has just
    reviewed and chosen — `draft` would mean it sits there not reading anything, which is not what
    "use this" means.
    """
    return {"name": name, "paradigm": paradigm, "source_type": source_type or "",
            "payload": body, "status": "active"}


async def _scout(records: list, structure: dict, confidence: Confidence,
                 model_spec: dict | None) -> tuple[Confidence, str]:
    """Let the model review the measured roles in place. Returns the confidence and one note.

    A model that CHANGES a measured role is worth saying out loud: the measurement is checkable
    against the sample and the model's opinion is not, so the draft drops to MEDIUM and names the
    keys it disagreed about, rather than presenting both kinds of decision as equally solid.
    """
    from opencortyx.ingest.semistructured import schema as ss

    if not model_spec:
        return confidence, "no model is connected, so the key roles are the measured ones only"
    before = {k.get("path"): k.get("role") for k in structure.get("keys") or []}
    try:
        await ss.scout_structure(records, structure, model_spec)
    except Exception as exc:                          # noqa: BLE001 — measured roles already stand
        log.warning("authoring.scout_failed", error=f"{type(exc).__name__}: {exc}")
        return confidence, "the model could not review the key roles; kept the measured ones"

    changed = [str(k.get("path")) for k in structure.get("keys") or []
               if k.get("path") in before and before[k["path"]] != k.get("role")]
    if not changed:
        return confidence, ""
    note = (f"the model read {len(changed)} key(s) differently from the measurement: "
            f"{', '.join(changed[:5])}")
    return Confidence.worst(confidence, Confidence.MEDIUM), note


def _widest_structure(records: list) -> tuple[dict, list[str]]:
    """Build a structure from each sampled record and keep the one covering the most paths.

    Records of one shape routinely differ: optional keys, nulls, a field that only appears once
    something has happened. `build_structure` reads ONE record, so reading only the first produces
    a mapping that silently drops every key the first record happened not to carry. Taking the
    widest is the cheap correct answer; the disagreement is reported rather than smoothed over,
    because a file whose records differ a lot is usually a file holding two different things.
    """
    from opencortyx.ingest.semistructured.schema import build_structure

    built = []
    for record in records:
        try:
            built.append(build_structure(record))
        except Exception as exc:                      # noqa: BLE001 — one bad record is not fatal
            log.warning("authoring.build_structure_failed", error=f"{type(exc).__name__}: {exc}")
    if not built:
        return ({"name": "Unreadable", "keys": [], "paths": []},
                ["no record in the sample could be read as a structure"])

    widest = max(built, key=lambda s: len(s.get("paths") or []))
    union = {p for s in built for p in (s.get("paths") or [])}
    missing = sorted(union - set(widest.get("paths") or []))
    notes = []
    if missing:
        notes.append(f"records disagree on {len(missing)} key(s) ({', '.join(missing[:5])}) — "
                     f"this file may hold two different shapes")
    return widest, notes


def _record_evidence(structure: dict) -> list[Evidence]:
    """The structure's own decisions as reviewable claims, attribute roles first."""
    attrs: list[Evidence] = []
    nodes: list[Evidence] = []
    for key in structure.get("keys") or []:
        path = str(key.get("path") or "")
        if key.get("kind") == "node":
            nodes.append(Evidence(
                claim=f"{path or 'the record'} becomes a {key.get('node_type')} node",
                basis=f"{key.get('type')} branch with {len(key.get('attrs') or [])} scalar field(s)",
                path=path))
        else:
            attrs.append(Evidence(claim=f"{path} → {_role_phrase(str(key.get('role') or ''))}",
                                  basis=_attribute_basis(key), path=path))
    return attrs + nodes


def _attribute_basis(key: dict) -> str:
    """Why an attribute got its role, in terms a reader can check against the sample value."""
    described = str(key.get("description") or "").strip()
    if described:
        return described
    sample = str(key.get("sample") or "")
    type_name = str(key.get("type") or "value")
    if key.get("role") == "text":
        return f"{type_name} of {len(sample)}+ characters: {sample[:60]!r}"
    return f"{type_name}: {sample[:60]!r}" if sample else f"{type_name} value"


def _column_basis(col: dict, role: str) -> str:
    declared = str(col.get("type") or col.get("raw_type") or "untyped")
    if role in _KEY_ROLES:
        return f"{declared} column, named like a key"
    return f"{declared} column"


_ROLE_PHRASES = {
    "text": "prose that gets read and extracted from",
    "date": "the record's date",
    "id": "an identifier",
    "metadata": "a label kept on the node",
    "skip": "ignored",
    "identity": "the row's identity",
    "fk": "a link to another table",
    "measure": "a number to aggregate",
    "dimension": "a label kept on the node",
}


def _role_phrase(role: str) -> str:
    return _ROLE_PHRASES.get(role, role or "a label kept on the node")


def _fk_target_type(column: str) -> str:
    """`customer_id` → `Customer`. Empty when the column does not name what it points at."""
    stem = _FK_SUFFIX.sub("", column or "").strip("_ ")
    word = stem.rsplit("_", 1)[-1] if stem else ""
    if word.lower() in ("", "id", "parent", "ref", "uuid", "key"):
        return ""
    if word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word[:1].upper() + word[1:]


async def _name_the_entity(req: DraftRequest, table: str, columns: list) -> tuple[str, str]:
    """Ask what the rows ARE, because a filename is a poor entity name. `(node_type, warning)`."""
    listing = ", ".join(f"{c.get('name')} ({c.get('type')})" for c in columns[:40])
    prompt = (f"A table named {table!r} has these columns:\n{listing}\n\n"
              f"Sample rows:\n{sample_block(req.sample.rows[:3])}\n\n"
              f"What single real-world entity does ONE ROW represent? Answer with one capitalised "
              f'singular noun, no explanation. Return ONLY JSON: {{"entity": "Invoice"}}')
    parsed, note = await ask_json(req.workspace_id, prompt, model_ref=req.model_ref, max_tokens=60)
    if note:
        return "", note
    candidate = str(parsed.get("entity") or "").strip()
    return (candidate, "") if _TYPE_NAME.fullmatch(candidate) else ("", "")


__all__ = ["DocumentMappingDrafter", "SemistructuredMappingDrafter", "StructuredMappingDrafter"]
