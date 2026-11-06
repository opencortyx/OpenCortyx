"""Record resolution for one node type of one asset: pure decisions, no database.

For each node of the asset:
  * no complete identity (an identity attribute is missing)  → left alone
  * identity not seen before                                 → it becomes the canonical node
  * identity matches a node                                  → a SAME decision, scored and gated

A wrong link does more harm than a missing one, so a match is applied only when nothing speaks
against it. In order:

  1. apart        a person said these two are different (a split, a "keep" in Review, a label):
                  never matched again - the next-best candidate is tried instead
  2. must agree   a `distinguish` field both records hold, with different values: DISTINCT,
                  recorded, never joined and never reviewed
  3. score        ≥ auto: may be applied; ≥ review: a person decides; lower: not a match
  4. disagree     `conflict_review` and a compared field differs: a person decides
  5. corroborate  "similar" and the identity is only similar, or "always": a person decides
                  unless at least one compared field holds the same value in both

An applied SAME joins the two in one cluster (engine.join); each compared attribute is then
  * SUPPORTS     the same value (after normalising)
  * EXTENDS      the canonical node does not have it yet (the master, rebuilt from its members,
                 will carry it - resolution/master.py)
  * CONTRADICTS  a different value → both kept, flagged for review, never overwritten

The spec's `relations` limit which of these are applied. Nodes are visited in a fixed order, so the
same input always gives the same decisions. `spec` must be `spec.effective()` - its gates resolved.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from opencortyx.resolution.normalize import identity_block, identity_blocks, identity_key, identity_text, norm, similarity
from opencortyx.resolution.spec import ResolutionSpec

SAME, SUPPORTS, EXTENDS, CONTRADICTS, DISTINCT = "SAME", "SUPPORTS", "EXTENDS", "CONTRADICTS", "DISTINCT"
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/resolution")


@dataclass
class Plan:
    keys: list = field(default_factory=list)        # (key, node_id) to add to the identity index
    same: list = field(default_factory=list)        # (node_id, canonical_id) to join as one cluster
    extend: dict = field(default_factory=dict)      # canonical_id → {attribute: value} the cluster gains
    decisions: list = field(default_factory=list)   # rows for the decision ledger
    unidentified: int = 0

    def summary(self) -> dict:
        count = {r: 0 for r in (SAME, SUPPORTS, EXTENDS, CONTRADICTS, DISTINCT)}
        review = 0
        for d in self.decisions:
            count[d["relation"]] = count.get(d["relation"], 0) + 1
            review += bool(d["needs_review"])
        return {"same": len(self.same), "supports": count[SUPPORTS], "extends": count[EXTENDS],
                "contradicts": count[CONTRADICTS], "distinct": count[DISTINCT], "review": review,
                "unidentified": self.unidentified}


def decision_id(workspace_id, relation, subject, target, attribute=None) -> uuid.UUID:
    return uuid.uuid5(_NS, f"{workspace_id}:{relation}:{subject}:{target}:{attribute or ''}")


def _decision(workspace_id, source_id, node_type, relation, subject, target, *, attribute=None, evidence=None,
              score=1.0, method="exact", review=False):
    return {"id": decision_id(workspace_id, relation, subject, target, attribute),
            "workspace_id": workspace_id, "source_id": source_id, "node_type": node_type, "relation": relation,
            "subject": subject, "target": target, "attribute": attribute, "confidence": round(score, 4),
            "method": method, "evidence": evidence or {}, "needs_review": review}


def _identity(node, spec: ResolutionSpec) -> list:
    props = node.properties or {}
    return [node.label if f == "label" else props.get(f) for f in spec.identify_by]


def key_of(node, spec: ResolutionSpec) -> str | None:
    return identity_key(_identity(node, spec))


def block_of(node, spec: ResolutionSpec) -> str | None:
    return identity_block(_identity(node, spec))


def blocks_of(node, spec: ResolutionSpec) -> list[str]:
    """Every block a node is filed under (see normalize.identity_blocks)."""
    return identity_blocks(_identity(node, spec))


def pair(a, b) -> frozenset:
    return frozenset((str(a), str(b)))


def compare(spec: ResolutionSpec, node, other, *, label_matched: bool, added: dict | None = None) -> list:
    """[(attribute, relation, evidence)] for the compared attributes of `node` against `other`
    (`added`: what this plan already gave `other`'s cluster, which counts as there)."""
    props = node.properties or {}
    theirs = dict(other.properties or {}) if other is not None else {}
    theirs.update(added or {})
    # fields a writer generated (a search rendering, a stamped date) say nothing about the source
    generated = set(props.get("__generated") or []) | set(theirs.get("__generated") or [])
    # what identified them was scored by the match: "hostel" joined "Hostels" by its name, and
    # the name (the label, or the field holding it) is not a second disagreement to review
    matched = set(spec.identify_by) | ({a for a in props if props[a] == node.label} if label_matched else set())
    out = []
    for attr in spec.compared(sorted((set(props) | set(theirs)) - generated - matched)):
        mine_v, other_v = norm(props.get(attr)), norm(theirs.get(attr))
        if mine_v is None:
            continue
        if other_v is None:
            out.append((attr, EXTENDS, {"value": props[attr]}))
        elif mine_v == other_v:
            out.append((attr, SUPPORTS, {"value": props[attr]}))
        else:
            out.append((attr, CONTRADICTS, {"value": props[attr], "other": theirs[attr]}))
    return out


def must_differ(spec: ResolutionSpec, node, other) -> list:
    """The `distinguish` fields both hold with different values: [(attribute, mine, theirs)]."""
    if not spec.distinguish or other is None:
        return []
    mine, theirs = node.properties or {}, other.properties or {}
    out = []
    for attr in spec.distinguish:
        a = node.label if attr == "label" else mine.get(attr)
        b = other.label if attr == "label" else theirs.get(attr)
        if norm(a) is not None and norm(b) is not None and norm(a) != norm(b):
            out.append((attr, a, b))
    return out


def decide(workspace_id, source_id, node_type: str, spec: ResolutionSpec, nodes, existing: dict,
           known: dict, pool: dict | None = None, apart: set | None = None) -> Plan:
    """`nodes`: this asset's nodes of `node_type`. `existing`: {key: node id} of nodes outside this
    asset that already hold the identity. `pool`: {block: [node id]} of nodes outside the asset
    sharing a block (the "similar" scorer). `known`: {node id: node} for all of those. `apart`:
    pairs (`pair(a, b)`) a person said are different."""
    plan = Plan()
    apart = apart or set()
    similar = spec.scorer == "similar"
    canonical = dict(existing)                      # key → canonical node id
    blocks = {b: list(ids) for b, ids in (pool or {}).items()}    # block → candidate node ids
    known = {**known, **{n.id: n for n in nodes}}
    for node in sorted(nodes, key=lambda n: (str(n.created_at or ""), str(n.id))):
        key = key_of(node, spec)
        if key is None:
            plan.unidentified += 1
            continue
        plan.keys.append((key, node.id))
        target, score = canonical.get(key), 1.0
        if target is not None and pair(node.id, target) in apart:
            target = None
        mine_blocks = blocks_of(node, spec) if similar else []
        if similar:
            plan.keys.extend((b, node.id) for b in mine_blocks)
            if target is None:
                mine = identity_text(_identity(node, spec))
                seen, scored = set(), []
                for b in mine_blocks:
                    for c in blocks.get(b, []):
                        if c in seen or c == node.id or c not in known or pair(node.id, c) in apart:
                            continue
                        seen.add(c)
                        scored.append((similarity(mine, identity_text(_identity(known[c], spec)) or ""), c))
                if scored:
                    score, target = max(scored, key=lambda s: (s[0], str(s[1])))
                    if score < spec.review:
                        target = None
            for b in mine_blocks:
                blocks.setdefault(b, []).append(node.id)
        if target is None or target == node.id or SAME not in spec.relations:
            canonical.setdefault(key, node.id)
            continue
        other = known.get(target)
        props = node.properties or {}
        evidence = {"identity": {f: (node.label if f == "label" else props.get(f)) for f in spec.identify_by}}
        method = "exact" if score >= 1.0 else "similar"

        differ = must_differ(spec, node, other)
        if differ:
            canonical.setdefault(key, node.id)
            plan.decisions.append(_decision(workspace_id, source_id, node_type, DISTINCT, node.id, target,
                                            evidence={**evidence, "differ": [{"attribute": a, "value": x, "other": y}
                                                                             for a, x, y in differ]},
                                            score=score, method=method))
            continue

        rels = compare(spec, node, other, label_matched="label" in spec.identify_by,
                       added=plan.extend.get(target))
        reasons = []
        if score < spec.auto:
            reasons.append("similar, below the automatic threshold")
        if spec.conflict_review and any(r == CONTRADICTS for _, r, _ in rels):
            reasons.append("disagrees on " + ", ".join(a for a, r, _ in rels if r == CONTRADICTS))
        needs_second = spec.corroborate == "always" or (spec.corroborate == "similar" and score < 1.0)
        if needs_second and not any(r == SUPPORTS for _, r, _ in rels):
            reasons.append("no second field agrees")
        if reasons:                                 # a possible match: a person decides
            canonical.setdefault(key, node.id)
            plan.decisions.append(_decision(workspace_id, source_id, node_type, SAME, node.id, target,
                                            evidence={**evidence, "reasons": reasons}, score=score,
                                            method=method, review=True))
            continue

        canonical.setdefault(key, target)
        plan.same.append((node.id, target))
        plan.decisions.append(_decision(workspace_id, source_id, node_type, SAME, node.id, target,
                                        evidence=evidence, score=score, method=method))
        for attr, relation, ev in rels:
            if relation not in spec.relations:
                continue
            if relation == EXTENDS:
                plan.extend.setdefault(target, {})[attr] = props[attr]
            plan.decisions.append(_decision(workspace_id, source_id, node_type, relation, node.id, target,
                                            attribute=attr, evidence=ev, score=score, method=method,
                                            review=relation == CONTRADICTS))
    return plan
