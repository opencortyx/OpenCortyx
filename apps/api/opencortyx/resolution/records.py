"""Record resolution for one node type of one asset: pure decisions, no database.

For each node of the asset:
  * no complete identity (an identity attribute is missing)  → left alone
  * identity not seen before                                 → it becomes the canonical node
  * identity matches a node                                  → a SAME decision, scored and gated:
        score ≥ auto     linked `SAME_AS` and its attributes classified
        score ≥ review   waits for a person (Review → Conflicts); nothing is linked yet
        lower            not a match: the node becomes canonical for its own identity

With the "exact" scorer a match is an equal normalised identity (score 1.0). With "similar", the
candidates are the nodes sharing the identity's block, scored by text similarity; the best wins.

For every applied SAME pair, each compared attribute is one of:
  * SUPPORTS     the same value (after normalising)
  * EXTENDS      the canonical node does not have it yet → it is added to the canonical node
  * CONTRADICTS  a different value → both kept, flagged for review, never overwritten

The spec's `relations` limit which of these are applied. Nodes are visited in a fixed order, so the
same input always gives the same decisions.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from opencortyx.resolution.normalize import identity_block, identity_key, identity_text, norm, similarity
from opencortyx.resolution.spec import ResolutionSpec

SAME, SUPPORTS, EXTENDS, CONTRADICTS = "SAME", "SUPPORTS", "EXTENDS", "CONTRADICTS"
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/resolution")


@dataclass
class Plan:
    keys: list = field(default_factory=list)        # (key, node_id) to add to the identity index
    same: list = field(default_factory=list)        # (node_id, canonical_id) to link SAME_AS
    extend: dict = field(default_factory=dict)      # canonical_id → {attribute: value} to add
    decisions: list = field(default_factory=list)   # rows for the decision ledger
    unidentified: int = 0

    def summary(self) -> dict:
        count = {r: 0 for r in (SAME, SUPPORTS, EXTENDS, CONTRADICTS)}
        review = 0
        for d in self.decisions:
            count[d["relation"]] += 1
            review += bool(d["needs_review"])
        return {"same": count[SAME], "supports": count[SUPPORTS], "extends": count[EXTENDS],
                "contradicts": count[CONTRADICTS], "review": review, "unidentified": self.unidentified}


def _decision(workspace_id, source_id, node_type, relation, subject, target, *, attribute=None, evidence=None,
              score=1.0, method="exact", review=False):
    return {"id": uuid.uuid5(_NS, f"{workspace_id}:{relation}:{subject}:{target}:{attribute or ''}"),
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


def decide(workspace_id, source_id, node_type: str, spec: ResolutionSpec, nodes, existing: dict,
           known: dict, pool: dict | None = None) -> Plan:
    """`nodes`: this asset's nodes of `node_type`. `existing`: {key: node id} of nodes outside this
    asset that already hold the identity. `pool`: {block: [node id]} of nodes outside the asset
    sharing a block (the "similar" scorer). `known`: {node id: node} for all of those."""
    plan = Plan()
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
        if similar:
            block, mine = block_of(node, spec), identity_text(_identity(node, spec))
            plan.keys.append((block, node.id))
            if target is None:
                scored = [(similarity(mine, identity_text(_identity(known[c], spec)) or ""), c)
                          for c in blocks.get(block, []) if c in known and c != node.id]
                if scored:
                    score, target = max(scored, key=lambda s: (s[0], str(s[1])))
                    if score < spec.review:
                        target = None
            blocks.setdefault(block, []).append(node.id)
        if target is None or target == node.id or SAME not in spec.relations:
            canonical.setdefault(key, node.id)
            continue
        props = node.properties or {}
        evidence = {"identity": {f: (node.label if f == "label" else props.get(f)) for f in spec.identify_by}}
        method = "exact" if score >= 1.0 else "similar"
        if score < spec.auto:                       # a possible match: a person decides
            canonical.setdefault(key, node.id)
            plan.decisions.append(_decision(workspace_id, source_id, node_type, SAME, node.id, target,
                                            evidence=evidence, score=score, method=method, review=True))
            continue
        canonical.setdefault(key, target)
        plan.same.append((node.id, target))
        plan.decisions.append(_decision(workspace_id, source_id, node_type, SAME, node.id, target,
                                        evidence=evidence, score=score, method=method))
        theirs = dict((known.get(target).properties or {}) if known.get(target) else {})
        theirs.update(plan.extend.get(target, {}))  # what this plan already added counts as there
        # fields a writer generated (a search rendering, a stamped date) say nothing about the source
        generated = set(props.get("__generated") or []) | set(theirs.get("__generated") or [])
        for attr in spec.compared(sorted((set(props) | set(theirs)) - generated)):
            mine_v, other = norm(props.get(attr)), norm(theirs.get(attr))
            if mine_v is None:
                continue
            if other is None:
                relation, ev = EXTENDS, {"value": props[attr]}
            elif mine_v == other:
                relation, ev = SUPPORTS, {"value": props[attr]}
            else:
                relation, ev = CONTRADICTS, {"value": props[attr], "other": theirs[attr]}
            if relation not in spec.relations:
                continue
            if relation == EXTENDS:
                plan.extend.setdefault(target, {})[attr] = props[attr]
            plan.decisions.append(_decision(workspace_id, source_id, node_type, relation, node.id, target,
                                            attribute=attr, evidence=ev, score=score, method=method,
                                            review=relation == CONTRADICTS))
    return plan
