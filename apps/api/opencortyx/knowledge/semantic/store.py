"""The semantic layer, kept where the platform already keeps meaning - no store of its own.

    type_registry    each entity type and relationship type. An entity type's identity key is its
                     resolution spec's `identify_by`; a relationship is a reference rule on the type
                     that holds it (resolution/spec.py), which is also how the resolution engine links
                     materialized nodes across files
    schema_mapping   where each type lives: a mapping's `knowledge` section names the assets, the
                     record path, the field each property is read from, the key, and whether the
                     type's records are materialized as graph nodes. Written by the Detector (status
                     `auto`, or `draft` while it is unsure) or by a person in the Mapper

    payload["knowledge"] = {
        "entities": [{"type", "keys", "kind", "path", "assets": [{"id", "name", "fields"}],
                      "properties": [{"name", "data_type", "role", "value_set", "distinct", "words"}],
                      "label": the property that names a record,
                      "materialize", "status", "confidence", "rows"}],
        "relationships": [{"name", "from_type", "from_field", "to_type", "to_field",
                           "origin": "declared" | "inferred", "coverage", "status"}],
        "curated": bool,          # a person changed it: detection never overwrites it
        "version": int,
    }
    payload["history"] = [{"version", "at", "knowledge"}, ...]      the last HISTORY versions

`load` reads it back as one Layer: every type with all its bindings across mappings.
"""
from __future__ import annotations

import dataclasses
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

#: Applied without review at this confidence or above; below it a type or link waits in the Mapper.
AUTO_ACCEPT = 0.9
#: Earlier versions of a mapping's knowledge kept for review and restore.
HISTORY = 20
#: The reference rules the Detector writes are its own; a person's mappings keep theirs.
DETECTOR = "detector"
DETECTED = "detected"                # schema_mapping.source_type of a mapping the Detector wrote
_LIVE = ("active", "auto")           # mapping statuses whose knowledge is in use


@dataclass
class Property:
    name: str
    data_type: str = "text"          # text | number | date
    role: str = "text"               # key | reference | category | measure | time | text | prose
    value_set: dict = field(default_factory=dict)
    distinct: int = 0
    description: str = ""
    words: float = 0.0               # median words per value: prose is free text, text a short value


@dataclass
class Binding:
    asset_id: str
    asset_name: str
    kind: str                        # structured | semistructured
    path: str = ""                   # "" (the row), "$.K" (an object), "$.K[*]" (each element of a list)
    fields: dict = field(default_factory=dict)        # property -> the field it is read from


@dataclass
class ObjectType:
    name: str
    keys: list = field(default_factory=list)          # the identifying properties; [] for an event table
    kind: str = "entity"             # entity | event
    status: str = "suggested"        # accepted | suggested | rejected
    confidence: float = 0.0
    materialize: bool = False        # records as graph nodes (the mapping's rules), or read from Delta
    label: str = ""                  # the property that names a record ("name", "title"), if one does
    description: str = ""
    rows: int = 0
    properties: list = field(default_factory=list)    # [Property]
    bindings: list = field(default_factory=list)      # [Binding]

    @property
    def id(self) -> str:
        return self.name

    @property
    def key(self) -> str:
        return self.keys[0] if self.keys else ""

    def prop(self, name: str) -> Optional[Property]:
        return next((p for p in self.properties if p.name == name), None)


@dataclass
class LinkType:
    name: str
    source: str                      # the type holding the reference
    source_prop: str
    target: str                      # the type whose key it names
    target_prop: str
    coverage: float = 0.0            # share of the reference's values found among the target's keys
    status: str = "suggested"
    origin: str = "inferred"         # declared (a person or a database said so) | inferred (from values)

    @property
    def id(self) -> str:
        return f"{self.source}.{self.source_prop}->{self.target}.{self.target_prop}"


@dataclass
class Layer:
    types: list = field(default_factory=list)
    links: list = field(default_factory=list)

    def accepted(self) -> "Layer":
        types = [t for t in self.types if t.status == "accepted"]
        names = {t.name for t in types}
        return Layer(types=types, links=[lk for lk in self.links if lk.status == "accepted"
                                         and lk.source in names and lk.target in names])

    def type(self, name: str) -> Optional[ObjectType]:
        return next((t for t in self.types if t.name == name), None)


# ── reading ────────────────────────────────────────────────────────────────────────────────────

def _entity_type(e: dict, paradigm: str, live: bool) -> ObjectType:
    t = ObjectType(name=str(e.get("type") or ""), keys=list(e.get("keys") or []), kind=e.get("kind") or "entity",
                   status=(e.get("status") or "accepted") if live else "suggested",
                   confidence=float(e.get("confidence") or 0.0), materialize=bool(e.get("materialize")),
                   label=str(e.get("label") or ""),
                   description=e.get("description") or "", rows=int(e.get("rows") or 0),
                   properties=[Property(**{k: v for k, v in p.items() if k in Property.__dataclass_fields__})
                               for p in (e.get("properties") or [])])
    path = str(e.get("path") or "").strip()
    path = "" if path.lower() in ("rows", "row", "$") else path
    t.bindings = [Binding(asset_id=str(a.get("id")), asset_name=str(a.get("name") or ""), kind=paradigm,
                          path=path, fields=dict(a.get("fields") or {}))
                  for a in (e.get("assets") or []) if a.get("id")]
    return t


def _merge(into: ObjectType, t: ObjectType) -> None:
    into.bindings += t.bindings
    into.rows += t.rows
    into.keys = into.keys or t.keys
    into.materialize = into.materialize or t.materialize
    into.label = into.label or t.label
    into.confidence = min(into.confidence, t.confidence) if into.confidence and t.confidence else (into.confidence or t.confidence)
    if t.status == "accepted" or into.status == "accepted":
        into.status = "accepted"
    for p in t.properties:
        cur = into.prop(p.name)
        if cur is None:
            into.properties.append(p)
        else:
            for v, n in p.value_set.items():
                cur.value_set[v] = cur.value_set.get(v, 0) + n
            cur.distinct = max(cur.distinct, p.distinct)


def layer_of(mappings) -> Layer:
    """The Layer the `knowledge` sections of `mappings` (SchemaMapping rows) describe."""
    layer = Layer()
    by_name: dict = {}
    seen_links: set = set()
    for m in mappings:
        k = (m.payload or {}).get("knowledge") or {}
        live = m.status in _LIVE
        for e in k.get("entities") or []:
            t = _entity_type(e, m.paradigm, live)
            if not t.name or t.status == "rejected":
                continue
            if t.name in by_name:
                _merge(by_name[t.name], t)
            else:
                by_name[t.name] = t
                layer.types.append(t)
        for r in k.get("relationships") or []:
            lk = LinkType(name=str(r.get("name") or ""), source=str(r.get("from_type") or ""),
                          source_prop=str(r.get("from_field") or ""), target=str(r.get("to_type") or ""),
                          target_prop=str(r.get("to_field") or ""), coverage=float(r.get("coverage") or 0.0),
                          status=(r.get("status") or "accepted") if live else "suggested",
                          origin=r.get("origin") or "inferred")
            if lk.source and lk.target and lk.status != "rejected" and lk.id not in seen_links:
                seen_links.add(lk.id)
                layer.links.append(lk)
    layer.types.sort(key=lambda t: t.name)
    return layer


async def mappings(workspace_id, *, include_paused: bool = False) -> list:
    from sqlalchemy import select

    from opencortyx.core.db import V2Session
    from opencortyx.domain.control import SchemaMapping
    async with V2Session() as db:
        rows = (await db.execute(select(SchemaMapping).where(
            SchemaMapping.workspace_id == workspace_id))).scalars().all()
    return [m for m in rows if (m.payload or {}).get("knowledge")
            and (include_paused or m.status not in ("paused", "disabled"))]


async def load(workspace_id) -> Layer:
    return layer_of(await mappings(workspace_id))


# ── writing what the Detector found ────────────────────────────────────────────────────────────

def _entity_dict(t: ObjectType, path: str, bindings: list) -> dict:
    return {"type": t.name, "keys": list(t.keys), "kind": t.kind, "path": path,
            "assets": [{"id": b.asset_id, "name": b.asset_name, "fields": b.fields} for b in bindings],
            "properties": [asdict(p) for p in t.properties], "materialize": t.materialize, "label": t.label,
            "status": t.status, "confidence": t.confidence, "rows": t.rows, "description": t.description}


def _keep_choices(entity: dict, before: dict) -> dict:
    """What a person chose on the detected type - publish it to the graph - kept when the Detector
    rewrites the structure; everything else is the Detector's."""
    if before:
        entity["materialize"] = bool(entity.get("materialize") or before.get("materialize"))
    return entity


def _link_dict(lk: LinkType) -> dict:
    return {"name": lk.name, "from_type": lk.source, "from_field": lk.source_prop, "to_type": lk.target,
            "to_field": lk.target_prop, "origin": lk.origin, "coverage": lk.coverage, "status": lk.status}


def with_version(payload: dict, knowledge: dict) -> dict:
    """`payload` with `knowledge` as its current version, the previous one kept in its history."""
    payload = dict(payload or {})
    old = payload.get("knowledge")
    if old == knowledge:
        return payload
    version = int((old or {}).get("version") or 0) + 1
    if old:
        hist = list(payload.get("history") or [])
        hist.insert(0, {"version": old.get("version") or version - 1,
                        "at": datetime.now(timezone.utc).isoformat(), "knowledge": old})
        payload["history"] = hist[:HISTORY]
    payload["knowledge"] = {**knowledge, "version": version}
    return payload


def _path(p) -> str:
    """A record path as bindings store it: "rows", "row" and "$" are the record itself ("")."""
    p = str(p or "").strip()
    return "" if p.lower() in ("rows", "row", "$") else p


async def save_detected(workspace_id, layer: Layer) -> dict:
    """Write what the Detector found: one mapping per type and record shape (status `auto` when
    confident, `draft` when not), the types and relationship types in the type registry, each type's
    key as its identity, each link as a reference rule. A mapping a person curated is left as it is;
    a detected mapping no longer detected is removed."""
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from opencortyx.core.db import V2Session
    from opencortyx.domain.control import SchemaMapping
    from opencortyx.ontology.extractors import register_emit_types
    from opencortyx.resolution.spec import Reference, ResolutionSpec, get_spec, save_references, save_spec

    written, kept = 0, 0
    async with V2Session() as db:
        existing: dict = {}
        for m in (await db.execute(select(SchemaMapping).where(
                SchemaMapping.workspace_id == workspace_id).order_by(SchemaMapping.updated_at.desc()))).scalars().all():
            if m.name not in existing:
                existing[m.name] = m
            elif m.source_type == DETECTED and not ((m.payload or {}).get("knowledge") or {}).get("curated"):
                # An older detected copy under the same name: kept, it was read first and its stale
                # label ("contact_email") named the type's records instead of the current one.
                await db.delete(m)
        # Record sources a person described: what detection finds there is theirs already, under the
        # names they gave (re-detected under the data's own words it was a second, duplicate type).
        covered = {(str(a.get("id")), _path(e.get("path")))
                   for m in existing.values() if ((m.payload or {}).get("knowledge") or {}).get("curated")
                   for e in (m.payload["knowledge"].get("entities") or []) for a in e.get("assets") or []}
        names: set = set()
        for t in layer.types:
            shapes: dict = {}
            for b in t.bindings:
                shapes.setdefault((b.kind, b.path), []).append(b)
            for (kind, path), bindings in shapes.items():
                name = f"{t.name}: {path or 'rows'}" + (f" ({kind})" if len({k for k, _ in shapes}) > 1 else "")
                if all((str(b.asset_id), _path(b.path)) in covered for b in bindings):
                    kept += 1
                    continue
                names.add(name)
                m = existing.get(name)
                if m is not None and ((m.payload or {}).get("knowledge") or {}).get("curated"):
                    kept += 1
                    continue
                before = next((e for e in (((m.payload or {}).get("knowledge") or {}).get("entities") or [])
                               if e.get("type") == t.name), {}) if m is not None else {}
                knowledge = {"entities": [_keep_choices(_entity_dict(t, path, bindings), before)],
                             "relationships": [_link_dict(lk) for lk in layer.links if lk.source == t.name],
                             "curated": False}
                if m is None:
                    m = SchemaMapping(workspace_id=workspace_id, name=name, paradigm=kind, source_type=DETECTED,
                                      payload={})
                    db.add(m)
                m.payload = with_version(m.payload or {}, {**knowledge, "version": ((m.payload or {}).get(
                    "knowledge") or {}).get("version", 0)})
                flag_modified(m, "payload")
                m.status = "auto" if t.status == "accepted" else "draft"
                m.summary = {"targets": [t.name], "nodes": 1 if knowledge["entities"][0]["materialize"] else 0,
                             "edges": len(knowledge["relationships"])}
                written += 1
        for name, m in existing.items():
            if m.source_type == DETECTED and name not in names and not ((m.payload or {}).get("knowledge") or {}).get("curated"):
                await db.delete(m)

        from opencortyx.domain.ontology import own_type
        for t in layer.types:
            await register_emit_types(db, workspace_id, {"nodes": [{"type": t.name,
                                                                      "attributes": [p.name for p in t.properties]}]},
                                      source="discovered", status="active" if t.status == "accepted" else "candidate")
            row = await own_type(db, workspace_id, t.name, "node")
            if row is not None and row.source == "discovered" and not (row.properties_schema or {}).get(DETECTED):
                # The Detector's own type: a workspace reset removes it with the data it described.
                row.properties_schema = {**(row.properties_schema or {}), DETECTED: True}
                flag_modified(row, "properties_schema")
            if t.keys and t.kind == "entity":
                spec = await get_spec(db, workspace_id, t.name) or ResolutionSpec()
                if not spec.identify_by:
                    await save_spec(db, workspace_id, t.name, dataclasses.replace(spec, identify_by=tuple(t.keys)))
        if layer.links:
            await register_emit_types(db, workspace_id, {"edges": [{"type": lk.name} for lk in layer.links]},
                                      source="discovered")
        refs: dict = {}
        for lk in layer.links:
            if lk.status == "accepted":
                refs.setdefault(lk.source, []).append(Reference(lk.source_prop, lk.target, lk.target_prop, lk.name,
                                                                False, DETECTOR))
        await save_references(db, workspace_id, DETECTOR, refs)
        await db.commit()
    invalidate_views(workspace_id)
    return {"mappings": written, "curated_kept": kept, "types": len(layer.types), "links": len(layer.links)}


async def resolve_assets(workspace_id, knowledge: dict) -> dict:
    """`knowledge` with each entity's assets named by id as well: a person may name an asset by its
    name in the Mapper. An asset that does not exist keeps no id (validation reports it)."""
    from opencortyx.core.dataplane import assets
    refs = await assets.refs(workspace_id)
    by_name = {r.name: r.asset_id for r in refs}
    ids = {r.asset_id for r in refs}
    out = dict(knowledge or {})
    ents = []
    for e in out.get("entities") or []:
        e = dict(e)
        e["assets"] = [{**a, "id": a.get("id") if a.get("id") in ids else by_name.get(a.get("name") or "", "")}
                       for a in (e.get("assets") or [])]
        ents.append(e)
    out["entities"] = ents
    return out


def invalidate_views(workspace_id) -> None:
    try:
        from opencortyx.retrieval.l2_data.schema import invalidate
        invalidate(workspace_id)
    except Exception:                                         # noqa: BLE001 - nothing cached yet
        pass


async def choose(workspace_id, *, asset_ids=(), materialize: bool | None = None) -> list[str]:
    """Record a person's choice on every type bound to `asset_ids` (Add data: "also add these records
    to the knowledge graph"). Returns the types changed. A choice, so detection keeps it."""
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from opencortyx.core.db import V2Session
    from opencortyx.domain.control import SchemaMapping
    wanted = {str(a) for a in asset_ids}
    changed: list = []
    async with V2Session() as db:
        for m in (await db.execute(select(SchemaMapping).where(SchemaMapping.workspace_id == workspace_id))).scalars():
            k = (m.payload or {}).get("knowledge") or {}
            ents = []
            hit = False
            for e in k.get("entities") or []:
                # a mapping a person curated keeps its own choice; records without an identifier (events)
                # are rows, never nodes
                if materialize is not None and {str(a.get("id")) for a in e.get("assets") or []} & wanted                         and bool(e.get("materialize")) != materialize and not k.get("curated")                         and (e.get("keys") or not materialize):
                    e = {**e, "materialize": materialize}
                    hit = True
                    changed.append(str(e.get("type")))
                ents.append(e)
            if hit:
                m.payload = with_version(m.payload or {}, {**k, "entities": ents})
                flag_modified(m, "payload")
        await db.commit()
    if changed:
        invalidate_views(workspace_id)
    return sorted(set(changed))


def redetected(mapping, layer: Layer) -> dict | None:
    """The knowledge the Detector now proposes for ONE mapping - its assets at its record path, read
    across the whole workspace so links to other files are kept - with the person's choices kept.
    None when the Detector no longer finds a type there."""
    k = (mapping.payload or {}).get("knowledge") or {}
    ents = k.get("entities") or []
    assets = {str(a.get("id")) for e in ents for a in e.get("assets") or [] if a.get("id")}
    path = str((ents[0].get("path") if ents else "") or "")
    path = "" if path.lower() in ("rows", "row", "$") else path
    before = {e.get("type"): e for e in ents}
    out, names = [], set()
    for t in layer.types:
        bound = [b for b in t.bindings if b.asset_id in assets and b.path == path]
        if bound:
            prev = before.get(t.name) or (next(iter(before.values())) if len(before) == 1 else {})
            out.append(_keep_choices(_entity_dict(t, path, bound), prev))
            names.add(t.name)
    if not out:
        return None
    return {"entities": out, "relationships": [_link_dict(lk) for lk in layer.links if lk.source in names],
            "curated": False}


def publish_changed(before: dict, after: dict) -> bool:
    """Whether what a mapping puts in the graph changed between two versions of its knowledge: which
    types are published, and for each published type its name, identifier, name field, record path
    and status - or the links. Renaming a published type, or naming its records by another field,
    changes nodes already written."""
    def published(k: dict):
        ents = sorted((str(e.get("type")), tuple(e.get("keys") or []), str(e.get("label") or ""),
                       str(e.get("path") or ""), str(e.get("status") or ""))
                      for e in (k or {}).get("entities") or [] if e.get("materialize"))
        links = sorted((str(r.get("from_type")), str(r.get("from_field")), str(r.get("to_type")),
                        str(r.get("to_field")), str(r.get("status") or ""))
                       for r in (k or {}).get("relationships") or [])
        return ents, links
    return published(before) != published(after)
