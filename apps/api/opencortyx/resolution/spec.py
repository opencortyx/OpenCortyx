"""How a node type is resolved: which attributes identify it, which are compared, which are ignored.

A spec lives on the node type in the organization's ontology (`properties_schema["resolution"]`),
so there is one setting per type whichever extractor or mapping produced the nodes. The Extractor
edits it: an emitted node's `id` fields are its identity, and its optional `resolve` block names
the attributes to compare or ignore.

Beside the attributes, a spec says HOW a match is scored and gated:

  * scorer     "exact"   the identity values must be equal after normalising (score 1.0)
               "similar" identity values are compared by text similarity (0–1), so "Acme Corp" and
                         "Acme Corporation" can match
  * gate       a match scoring at least `auto` is applied; one scoring at least `review` waits in
               Review → Conflicts for a person; anything lower is not a match
  * relations  which outcomes may be applied: SAME (link), SUPPORTS, EXTENDS (add a missing value),
               CONTRADICTS (flag a differing value)

A type without identity fields is not resolved as a record. That is the safe default: two nodes
are only ever called the same because a person said which attributes make them the same.

A spec can also declare REFERENCES, the foreign keys of the type: "`customer_id` of an Order names
the `id` of a Customer, linked PLACED_BY". The engine links each node to the node its value names,
in whichever file and whichever order the two arrive. A mapping's join edges are saved as these.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

from sqlalchemy.orm.attributes import flag_modified

KEY = "resolution"
NODE_KINDS = ("node", "entity")
RELATIONS = ("SAME", "SUPPORTS", "EXTENDS", "CONTRADICTS")
SCORERS = ("exact", "similar")
#: The dates the substrate stamps on every write (`repo.AUDIT_DATE_KEYS`): when a node was written,
#: not anything its source said, so two records ingested on different days never "disagree" on them.
STAMPED = ("created_date", "modified_date")


def _names(value) -> tuple[str, ...]:
    """Field names from a list or a comma-separated string, trimmed, order kept, duplicates dropped."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    return tuple(dict.fromkeys(str(v).strip() for v in items if str(v or "").strip()))


@dataclass(frozen=True)
class Reference:
    """`attribute` of this type names `target_attribute` of `target_type`; linked as `relation`, from
    this node to the one it names, or the other way round when `reverse` (a mapping's join edge drawn
    into the record that holds the key). `source` is the mapping that declared it, so saving that
    mapping again replaces its own rules and no one else's."""
    attribute: str
    target_type: str
    target_attribute: str
    relation: str
    reverse: bool = False
    source: str = ""

    def to_dict(self) -> dict:
        return {"attribute": self.attribute, "target_type": self.target_type,
                "target_attribute": self.target_attribute, "relation": self.relation,
                "reverse": self.reverse, "source": self.source}

    @classmethod
    def parse(cls, raw) -> "Reference | None":
        if not isinstance(raw, dict):
            return None
        parts = [str(raw.get(k) or "").strip() for k in ("attribute", "target_type", "target_attribute", "relation")]
        if not all(parts[:2]):
            return None
        attribute, target_type, target_attribute, relation = parts
        rel = re.sub(r"[^A-Za-z0-9]+", "_", relation or "REFERENCES").strip("_").upper() or "REFERENCES"
        return cls(attribute, target_type, target_attribute or "id", rel, bool(raw.get("reverse")),
                   str(raw.get("source") or ""))


@dataclass(frozen=True)
class ResolutionSpec:
    identify_by: tuple[str, ...] = ()   # attributes whose values together say "the same thing"
    compare: tuple[str, ...] = ()       # attributes to compare on a match; empty means all of them
    ignore: tuple[str, ...] = ()        # attributes never compared (timestamps, notes, …)
    enabled: bool = True
    relations: tuple[str, ...] = RELATIONS  # outcomes that may be applied
    scorer: str = "exact"               # exact | similar
    auto: float = 0.95                  # applied at or above this score
    review: float = 0.80                # sent for review at or above this score
    references: tuple = ()              # Reference: this type's foreign keys
    header_references: bool = True      # documents: link the references their header names

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.identify_by)

    @property
    def links(self) -> bool:
        """Whether this spec declares anything the engine links by."""
        return self.enabled and bool(self.references)

    def compared(self, attrs) -> list[str]:
        """The attributes to compare out of `attrs`: the chosen ones (or all), minus identity,
        ignored, stamped and platform-internal ones."""
        chosen = self.compare or tuple(attrs)
        skip = set(self.identify_by) | set(self.ignore) | set(STAMPED)
        return [a for a in chosen if a not in skip and not str(a).startswith("__")]

    def to_dict(self) -> dict:
        return {"identify_by": list(self.identify_by), "compare": list(self.compare),
                "ignore": list(self.ignore), "enabled": self.enabled, "relations": list(self.relations),
                "scorer": self.scorer, "auto": self.auto, "review": self.review,
                "references": [r.to_dict() for r in self.references],
                "header_references": self.header_references}

    @classmethod
    def parse(cls, raw) -> "ResolutionSpec | None":
        if not isinstance(raw, dict):
            return None
        relations = tuple(r for r in _names(raw.get("relations")) if r in RELATIONS)
        auto = _unit(raw.get("auto"), 0.95)
        refs = tuple(dict.fromkeys(r for r in (Reference.parse(x) for x in (raw.get("references") or [])) if r))
        return cls(identify_by=_names(raw.get("identify_by")), compare=_names(raw.get("compare")),
                   ignore=_names(raw.get("ignore")), enabled=bool(raw.get("enabled", True)),
                   relations=relations if "relations" in raw else RELATIONS,
                   scorer=raw.get("scorer") if raw.get("scorer") in SCORERS else "exact",
                   auto=auto, review=min(_unit(raw.get("review"), 0.80), auto), references=refs,
                   header_references=bool(raw.get("header_references", True)))


def _unit(value, default: float) -> float:
    """A score threshold in 0..1, or the default when it is missing or not a number."""
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


def references_from_mapping(payload: dict, source: str) -> dict[str, list[Reference]]:
    """{node type: [Reference]} from a mapping's declared join edges (`structures[].edge_rules`, whose
    `self` side is the record being read and `join` side names another type by one of its fields)."""
    out: dict[str, list[Reference]] = {}
    for structure in (payload or {}).get("structures") or []:
        for rule in (structure or {}).get("edge_rules") or []:
            frm, to, rel = rule.get("from") or {}, rule.get("to") or {}, rule.get("type")
            if to.get("kind") == "join" and (frm.get("kind") or "self") == "self" and frm.get("type"):
                holder, side, reverse = frm["type"], to, False
            elif frm.get("kind") == "join" and (to.get("kind") or "self") == "self" and to.get("type"):
                holder, side, reverse = to["type"], frm, True    # drawn from the named node into the record
            else:
                continue
            ref = Reference.parse({"attribute": side.get("field"), "target_type": side.get("type"),
                                   "target_attribute": side.get("on") or "id", "relation": rel,
                                   "reverse": reverse, "source": source})
            if ref and ref not in out.setdefault(holder, []):
                out[holder].append(ref)
    return out


async def save_references(db, workspace_id, source: str, by_type: dict[str, list[Reference]]) -> list[str]:
    """Make `source`'s reference rules the ones in `by_type`: rules it declared before and no longer
    does are removed, other mappings' rules are kept. Returns the types whose rules changed. The
    caller commits."""
    from opencortyx.domain.ontology import TypeRegistry, visible_types
    rows = await visible_types(db, workspace_id, TypeRegistry.kind.in_(NODE_KINDS))
    names = {r.name for r in rows if any(x.source == source for x in
                                         (ResolutionSpec.parse((r.properties_schema or {}).get(KEY)) or ResolutionSpec()).references)}
    changed = []
    for type_name in sorted(names | set(by_type)):
        spec = await get_spec(db, workspace_id, type_name) or ResolutionSpec()
        kept = tuple(r for r in spec.references if r.source != source)
        new = kept + tuple(r for r in by_type.get(type_name, []) if r not in kept)
        if new != spec.references:
            await save_spec(db, workspace_id, type_name, replace(spec, references=new))
            changed.append(type_name)
    return changed


def specs_from_emit(emit) -> dict[str, ResolutionSpec]:
    """{node type: spec} from an extractor's emit: `id` is the identity, `resolve` the rest."""
    out = {}
    for n in ((emit or {}).get("nodes") or []):
        if not isinstance(n, dict) or not n.get("type"):
            continue
        extra = n.get("resolve") if isinstance(n.get("resolve"), dict) else {}
        spec = ResolutionSpec.parse({**extra, "identify_by": extra.get("identify_by") or n.get("id")})
        if spec.identify_by or extra:
            out[str(n["type"])] = spec
    return out


async def load_specs(db, workspace_id) -> dict[str, ResolutionSpec]:
    """{node type: spec} for every type this organization resolves."""
    from opencortyx.domain.ontology import TypeRegistry, visible_types
    rows = await visible_types(db, workspace_id, TypeRegistry.kind.in_(NODE_KINDS))
    out = {}
    for r in rows:
        spec = ResolutionSpec.parse((r.properties_schema or {}).get(KEY))
        if spec is not None and (spec.active or spec.links or not spec.header_references):
            out[r.name] = spec
    return out


async def get_spec(db, workspace_id, type_name: str) -> ResolutionSpec | None:
    from opencortyx.domain.ontology import find_type
    for kind in NODE_KINDS:
        row = await find_type(db, workspace_id, type_name, kind)
        if row is not None:
            return ResolutionSpec.parse((row.properties_schema or {}).get(KEY))
    return None


async def save_spec(db, workspace_id, type_name: str, spec: ResolutionSpec) -> None:
    """Store `spec` on the organization's own row for the type (a built-in is copied first), creating
    the type when the organization does not have it yet. The caller commits."""
    from opencortyx.domain.ontology import TypeRegistry, own_type
    row = None
    for kind in NODE_KINDS:
        row = await own_type(db, workspace_id, type_name, kind)
        if row is not None:
            break
    if row is None:
        row = TypeRegistry(workspace_id=workspace_id, name=type_name, kind="node", description=type_name,
                           properties_schema={}, source="user", status="active")
        db.add(row)
    ps = dict(row.properties_schema or {})
    ps[KEY] = spec.to_dict()
    row.properties_schema = ps
    flag_modified(row, "properties_schema")


async def save_from_emit(db, workspace_id, emit) -> int:
    """Store the specs an extractor's emit declares. Returns how many types were set."""
    specs = specs_from_emit(emit)
    for type_name, spec in specs.items():
        await save_spec(db, workspace_id, type_name, spec)
    return len(specs)

