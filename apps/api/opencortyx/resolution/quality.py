"""Is resolution right? Labels a person gave, and the engine scored against them.

Tuning resolution without a labelled set is guessing: a change that finds more duplicates may also
join things that are different, and a wrong link does more harm than a missing one. So:

  LABELS   `resolution_label`: a person's "same" or "different" for a pair of records. They come
           from Review (answering a possible duplicate), from checking automatic merges (`audit`),
           from splitting or merging by hand, and from an imported benchmark. A label keeps what
           each record SAID (label, fields, the asset it came from), not node ids, so it still
           applies after the files are read again. Any node type has them - whatever extractor,
           mapping or reader wrote it.

  SCORE    `evaluate(spec, labels)` runs the real decision function (`records.decide`) over the
           labelled records with the type's rule - or a proposed one - and counts, for the
           labelled pairs:
               true merges       labelled same, joined
               false merges      labelled different, joined        <- the expensive kind
               missed            labelled same, not joined (left apart, or waiting in Review)
               correct apart     labelled different, not joined
           with precision, recall and F1 over automatic joins, how many pairs a person would have
           to review, and the false merges themselves so each can be looked at.

  WRITE-TIME NAMES  A named thing an extractor writes with "Merge duplicates" is ONE node per
           (type, name) from the start (`ingest/extraction/templates.named_node`): two different
           "John Smith"s are joined before resolution runs. The evaluation counts those as false
           merges too ("at write"), because they are; the fix is the type's `name_scope: asset`.

Everything here except the async helpers is pure, so it runs in tests and from the CLI.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Iterable, Optional

from opencortyx.resolution import records
from opencortyx.resolution.normalize import norm
from opencortyx.resolution.spec import ResolutionSpec, effective

_SIG_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/resolution/label")
#: Bookkeeping a snapshot leaves out: it says nothing about which thing a record is.
_SKIP = {"created_date", "modified_date", "text", "__text", "__embedding_text"}
ORIGINS = ("review", "audit", "split", "merge", "manual", "import")


# ── what a record said ────────────────────────────────────────────────────────────────────────

def snapshot(node, asset_id=None) -> dict:
    props = {k: v for k, v in (node.properties or {}).items()
             if k not in _SKIP and (not str(k).startswith("__") or k == "__identity")}
    return {"label": node.label, "type": node.type, "properties": props,
            "asset_id": str(asset_id) if asset_id else (str(node.source_ref) if getattr(node, "source_ref", None) else None)}


def signature(snap: dict) -> str:
    raw = json.dumps({"label": snap.get("label"), "properties": snap.get("properties") or {},
                      "asset_id": snap.get("asset_id")}, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pair_hash(a: dict, b: dict) -> str:
    x, y = sorted((signature(a), signature(b)))
    return hashlib.sha256(f"{x}:{y}".encode()).hexdigest()


def _node(snap: dict, order: int):
    """A record the decision function can read, rebuilt from a snapshot."""
    sig = signature(snap)
    return SimpleNamespace(id=uuid.uuid5(_SIG_NS, sig), type=snap.get("type") or "Thing", label=snap.get("label"),
                           properties=dict(snap.get("properties") or {}),
                           created_at=datetime(2000, 1, 1, tzinfo=timezone.utc).replace(microsecond=order % 1_000_000),
                           confidence=1.0, level=0, source_ref=snap.get("asset_id"))


# ── the score ─────────────────────────────────────────────────────────────────────────────────

def _find(parent: dict, x):
    while parent.get(x, x) != x:
        parent[x] = parent.get(parent[x], parent[x])
        x = parent[x]
    return x


def rule_for(spec: Optional[ResolutionSpec], named: bool) -> Optional[ResolutionSpec]:
    from opencortyx.resolution.engine import named_spec
    if spec is not None and spec.active:
        return spec
    return named_spec() if named else None


def evaluate(spec: Optional[ResolutionSpec], labels: Iterable, *, corroborate: Optional[bool] = None,
             conflict_review: Optional[bool] = None, examples: int = 25) -> dict:
    """Score the decision function on labelled pairs. `labels`: objects or dicts with `a`, `b`
    (snapshots) and `same`. `spec`: the type's rule (None: the named-thing rule when the records are
    named things). Gates left as "default" take `corroborate` / `conflict_review`, or the settings."""
    labels = [l if isinstance(l, dict) else {"a": l.a, "b": l.b, "same": l.same, "id": str(getattr(l, "id", ""))}
              for l in labels]
    out = {"labels": len(labels), "pairs_same": sum(1 for l in labels if l["same"]),
           "pairs_different": sum(1 for l in labels if not l["same"]),
           "true_merges": 0, "false_merges": 0, "false_merges_at_write": 0, "missed": 0,
           "missed_in_review": 0, "correct_apart": 0, "review_pairs": 0,
           "precision": None, "recall": None, "f1": None, "false_merge_examples": [], "missed_examples": [],
           "rule": None}
    if not labels:
        return out
    named = any(((l["a"].get("properties") or {}).get("__identity") == "entity") for l in labels)
    rule = rule_for(spec, named)
    if rule is None:
        out["rule"] = None
        out["note"] = "This type has no identity fields: its records are never matched."
        out["missed"] = out["pairs_same"]
        out["correct_apart"] = out["pairs_different"]
        out["recall"] = 0.0 if out["pairs_same"] else None
        return out
    rule = effective(rule, corroborate=corroborate, conflict_review=conflict_review)
    out["rule"] = rule.to_dict()

    nodes, by_sig = [], {}
    for l in labels:
        for snap in (l["a"], l["b"]):
            sig = signature(snap)
            if sig not in by_sig:
                by_sig[sig] = _node(snap, len(nodes))
                nodes.append(by_sig[sig])
    # Grouped by asset in the order the assets first appear, as the engine sees them: one asset at a
    # time, each against everything resolved before it.
    plan = records.decide(uuid.UUID(int=0), None, nodes[0].type, rule, nodes, {}, {}, {})
    parent: dict = {}
    for a, c in plan.same:
        ra, rc_ = _find(parent, a), _find(parent, c)
        if ra != rc_:
            parent[ra] = rc_
    review = {records.pair(d["subject"], d["target"]) for d in plan.decisions
              if d["relation"] == records.SAME and d["needs_review"]}
    write_time = named and rule.name_scope == "workspace"

    for l in labels:
        a, b = by_sig[signature(l["a"])], by_sig[signature(l["b"])]
        at_write = write_time and norm(a.label) is not None and norm(a.label) == norm(b.label)
        merged = at_write or _find(parent, a.id) == _find(parent, b.id)
        in_review = records.pair(a.id, b.id) in review
        out["review_pairs"] += int(in_review and not merged)
        item = {"label_id": l.get("id"), "a": l["a"].get("label"), "b": l["b"].get("label")}
        if l["same"] and merged:
            out["true_merges"] += 1
        elif l["same"]:
            out["missed"] += 1
            out["missed_in_review"] += int(in_review)
            if len(out["missed_examples"]) < examples:
                out["missed_examples"].append({**item, "in_review": in_review})
        elif merged:
            out["false_merges"] += 1
            out["false_merges_at_write"] += int(at_write)
            if len(out["false_merge_examples"]) < examples:
                out["false_merge_examples"].append({**item, "at_write": at_write})
        else:
            out["correct_apart"] += 1
    tp, fp, fn = out["true_merges"], out["false_merges"], out["missed"]
    out["precision"] = round(tp / (tp + fp), 4) if (tp + fp) else None
    out["recall"] = round(tp / (tp + fn), 4) if (tp + fn) else None
    if out["precision"] is not None and out["recall"] is not None and (out["precision"] + out["recall"]):
        out["f1"] = round(2 * out["precision"] * out["recall"] / (out["precision"] + out["recall"]), 4)
    return out


def with_overrides(spec: Optional[ResolutionSpec], overrides: Optional[dict]) -> Optional[ResolutionSpec]:
    """`spec` with what-if settings applied (the same names `ResolutionSpec.parse` reads)."""
    if not overrides:
        return spec
    base = (spec or ResolutionSpec()).to_dict()
    merged = {**base, **overrides}
    # parse() quietly lowers review to auto; a what-if must say so instead of scoring something else
    if float(merged.get("review", 0.80)) > float(merged.get("auto", 0.95)):
        raise ValueError("the review threshold cannot be above the automatic one")
    return ResolutionSpec.parse(merged)


# ── the ledger of labels ──────────────────────────────────────────────────────────────────────

async def _asset_of(workspace_id, node) -> Optional[str]:
    if getattr(node, "source_ref", None):
        return str(node.source_ref)
    try:
        from opencortyx.core.dataplane import evidence
        assets = (await evidence.assets_of(workspace_id, [node.id])).get(str(node.id)) or set()
        return sorted(assets)[0] if assets else None
    except Exception:                                         # noqa: BLE001
        return None


async def snapshot_node(workspace_id, node) -> dict:
    return snapshot(node, await _asset_of(workspace_id, node))


async def add_label(workspace_id, node_type: str, a: dict, b: dict, same: bool, *, origin: str = "manual",
                    author: Optional[str] = None, note: Optional[str] = None) -> dict:
    """Record (or replace) a person's answer for the pair. Order-free: (a, b) is (b, a)."""
    from sqlalchemy.dialects.postgresql import insert
    from opencortyx.core.db import V2Session
    from opencortyx.domain.resolution import ResolutionLabel as L
    origin = origin if origin in ORIGINS else "manual"
    h = pair_hash(a, b)
    row = {"id": uuid.uuid4(), "workspace_id": uuid.UUID(str(workspace_id)), "node_type": str(node_type)[:80],
           "pair_hash": h, "a": a, "b": b, "same": bool(same), "origin": origin,
           "author": (str(author)[:120] if author else None), "note": (str(note)[:500] if note else None)}
    stmt = insert(L).values(**row)
    stmt = stmt.on_conflict_do_update(index_elements=[L.workspace_id, L.node_type, L.pair_hash],
                                      set_={"same": stmt.excluded.same, "origin": stmt.excluded.origin,
                                            "author": stmt.excluded.author, "note": stmt.excluded.note,
                                            "a": stmt.excluded.a, "b": stmt.excluded.b})
    async with V2Session() as db:
        await db.execute(stmt)
        await db.commit()
    return {"pair_hash": h, "same": bool(same), "origin": origin}


async def label_nodes(workspace_id, a_node, b_node, same: bool, **kw) -> dict:
    a = await snapshot_node(workspace_id, a_node)
    b = await snapshot_node(workspace_id, b_node)
    return await add_label(workspace_id, a_node.type, a, b, same, **kw)


async def load_labels(workspace_id, node_type: Optional[str] = None, *, limit: int = 100_000) -> list:
    from sqlalchemy import select
    from opencortyx.core.db import V2Session
    from opencortyx.domain.resolution import ResolutionLabel as L
    q = select(L).where(L.workspace_id == uuid.UUID(str(workspace_id)))
    if node_type:
        q = q.where(L.node_type == node_type)
    async with V2Session() as db:
        return list((await db.execute(q.order_by(L.created_at.desc()).limit(limit))).scalars().all())


async def summary(workspace_id) -> list[dict]:
    """[{node_type, labels, same, different, by origin}] for every labelled type."""
    from sqlalchemy import Integer, cast, func, select
    from opencortyx.core.db import V2Session
    from opencortyx.domain.resolution import ResolutionLabel as L
    async with V2Session() as db:
        rows = (await db.execute(select(L.node_type, L.origin, func.count(), func.sum(cast(L.same, Integer)))
                                 .where(L.workspace_id == uuid.UUID(str(workspace_id)))
                                 .group_by(L.node_type, L.origin))).all()
    out: dict = {}
    for t, origin, n, same in rows:
        e = out.setdefault(t, {"node_type": t, "labels": 0, "same": 0, "different": 0, "by_origin": {}})
        e["labels"] += int(n)
        e["same"] += int(same or 0)
        e["different"] += int(n) - int(same or 0)
        e["by_origin"][origin] = int(n)
    return sorted(out.values(), key=lambda e: -e["labels"])


async def evaluate_type(workspace_id, node_type: str, overrides: Optional[dict] = None) -> dict:
    """The type's rule now, and with `overrides`, scored on its labels."""
    from opencortyx.core.db import V2Session
    from opencortyx.resolution.spec import get_spec
    async with V2Session() as db:
        spec = await get_spec(db, uuid.UUID(str(workspace_id)), node_type)
    labels = await load_labels(workspace_id, node_type)
    current = evaluate(spec, labels)
    out = {"node_type": node_type, "current": current}
    if overrides:
        out["proposed"] = evaluate(with_overrides(spec, overrides), labels)
        out["overrides"] = overrides
    return out


async def audit_sample(workspace_id, node_type: Optional[str] = None, n: int = 20) -> list[dict]:
    """Automatic merges nobody has checked yet, at random: the place false merges hide."""
    from sqlalchemy import func, select
    from opencortyx.core.db import V2Session
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.domain.resolution import ResolutionDecision as D, ResolutionLabel as L
    ws = uuid.UUID(str(workspace_id))
    q = select(D).where(D.workspace_id == ws, D.relation == records.SAME, D.needs_review.is_(False),
                        D.attribute.is_(None), D.target.is_not(None))
    if node_type:
        q = q.where(D.node_type == node_type)
    async with V2Session() as db:
        rows = (await db.execute(q.order_by(func.random()).limit(max(1, min(n, 200)) * 3))).scalars().all()
        rows = [d for d in rows if not (d.evidence or {}).get("decision") and not (d.evidence or {}).get("by_person")]
        labelled = set((await db.execute(select(L.pair_hash).where(L.workspace_id == ws))).scalars().all())
    repo = NodeRepo()
    nodes = {x.id: x for x in await repo.get_many(ws, list({i for d in rows for i in (d.subject, d.target)}))}
    out = []
    for d in rows:
        a, b = nodes.get(d.subject), nodes.get(d.target)
        if a is None or b is None:
            continue
        sa, sb = await snapshot_node(ws, a), await snapshot_node(ws, b)
        if pair_hash(sa, sb) in labelled:
            continue
        out.append({"decision_id": str(d.id), "node_type": d.node_type, "score": d.confidence,
                    "method": d.method, "a": {"id": str(a.id), **sa}, "b": {"id": str(b.id), **sb}})
        if len(out) >= n:
            break
    return out


__all__ = ["ORIGINS", "add_label", "audit_sample", "evaluate", "evaluate_type", "label_nodes", "load_labels",
           "pair_hash", "signature", "snapshot", "snapshot_node", "summary", "with_overrides"]
