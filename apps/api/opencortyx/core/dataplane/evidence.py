"""The evidence ledger: what backs each fact, down to the chunk, row or record, at a version.

Every world-layer node, edge or property an extractor writes carries one or more pointers:

    Pointer(asset_id, unit_kind="chunk"|"row"|"record", unit_key, version_ref,
            quote, extractor, run_id, confidence, valid_from, valid_to, property)

When two sources disagree both pointers are kept: the resolution engine decides which value wins,
and the ledger still says where the other came from. A citation reads the ledger first
(core/substrate/provenance.py); opening one reads the unit from stores/data at its pinned version.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace as _replace
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import delete, func, select, tuple_

from opencortyx.core.db import V2Session
from opencortyx.domain.data import UNIT_KINDS, Evidence

#: A quote longer than this is cut: it is there to show and to rank sources, not to store the text.
QUOTE_MAX = 1000
#: Pointers written per statement.
_BATCH = 500


def value_hash(value) -> str:
    """A stable hash of a value, so a citation on a live source can tell "changed since extraction"."""
    raw = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Pointer:
    asset_id: str
    unit_kind: str
    unit_key: str
    version_ref: Optional[str] = None
    quote: Optional[str] = None
    extractor: Optional[str] = None
    run_id: Optional[str] = None
    confidence: Optional[float] = None
    property: Optional[str] = None
    value_hash: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.unit_kind not in UNIT_KINDS:
            raise ValueError(f"unit_kind must be one of {UNIT_KINDS}, got {self.unit_kind!r}")
        self.unit_key = str(self.unit_key)
        if self.quote and len(self.quote) > QUOTE_MAX:
            self.quote = self.quote[:QUOTE_MAX]

    @classmethod
    def coerce(cls, value) -> "Pointer":
        if isinstance(value, Pointer):
            return value
        d = dict(value or {})
        known = {k: d.pop(k) for k in list(d) if k in cls.__dataclass_fields__ and k != "extra"}
        return cls(**known, extra=d)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra", None)
        return {k: v for k, v in d.items() if v is not None}


def _uuid(v):
    if v in (None, ""):
        return None
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def _row(workspace_id, target_id, target_kind: str, p: Pointer) -> dict:
    return {"id": uuid.uuid4(), "workspace_id": _uuid(workspace_id), "target_id": _uuid(target_id),
            "target_kind": target_kind, "property": p.property, "asset_id": _uuid(p.asset_id),
            "unit_kind": p.unit_kind, "unit_key": p.unit_key[:300], "version_ref": p.version_ref,
            "quote": p.quote, "value_hash": p.value_hash, "extractor": p.extractor,
            "run_id": _uuid(p.run_id), "confidence": p.confidence,
            "valid_from": p.valid_from, "valid_to": p.valid_to}


async def record(workspace_id, items: Iterable[tuple], target_kind: str = "node") -> int:
    """Write pointers: `items` is (target_id, [Pointer | dict, ...]) pairs. A pointer already in the
    ledger for the same target, property, asset and unit is replaced, so a re-run never duplicates.
    Returns how many rows were written."""
    rows = []
    for target_id, pointers in items:
        for p in pointers or []:
            rows.append(_row(workspace_id, target_id, target_kind, Pointer.coerce(p)))
    if not rows:
        return 0
    ws = _uuid(workspace_id)
    async with V2Session() as db:
        for i in range(0, len(rows), _BATCH):
            chunk = rows[i:i + _BATCH]
            keys = [(r["target_id"], r["asset_id"], r["unit_key"], r["property"] or "") for r in chunk]
            await db.execute(delete(Evidence).where(
                Evidence.workspace_id == ws,
                tuple_(Evidence.target_id, Evidence.asset_id, Evidence.unit_key,
                       func.coalesce(Evidence.property, "")).in_(keys)))
            await db.execute(Evidence.__table__.insert(), chunk)
        await db.commit()
    return len(rows)


async def for_targets(workspace_id, target_ids: Iterable) -> dict[str, list[dict]]:
    """{target_id: [pointer dicts, newest first]}."""
    ids = [_uuid(t) for t in target_ids if t]
    if not ids:
        return {}
    async with V2Session() as db:
        rows = (await db.execute(select(Evidence).where(
            Evidence.workspace_id == _uuid(workspace_id), Evidence.target_id.in_(ids))
            .order_by(Evidence.observed_at.desc()))).scalars().all()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r.target_id), []).append({
            "asset_id": str(r.asset_id), "unit_kind": r.unit_kind, "unit_key": r.unit_key,
            "version_ref": r.version_ref, "quote": r.quote, "property": r.property,
            "extractor": r.extractor, "confidence": r.confidence, "value_hash": r.value_hash,
            "observed_at": r.observed_at.isoformat() if r.observed_at else None,
            "target_kind": r.target_kind})
    return out


async def drop_for_asset(workspace_id, asset_id, keep_version: Optional[str] = None) -> list[str]:
    """Remove an asset's pointers (all, or all but `keep_version`'s - a re-ingest of changed content).
    Returns the targets that lost evidence, for the caller to check whether any has none left."""
    ws, aid = _uuid(workspace_id), _uuid(asset_id)
    q = select(Evidence.target_id).where(Evidence.workspace_id == ws, Evidence.asset_id == aid)
    d = delete(Evidence).where(Evidence.workspace_id == ws, Evidence.asset_id == aid)
    if keep_version is not None:
        q = q.where(Evidence.version_ref.is_distinct_from(str(keep_version)))
        d = d.where(Evidence.version_ref.is_distinct_from(str(keep_version)))
    async with V2Session() as db:
        targets = {str(t) for t in (await db.execute(q)).scalars().all()}
        await db.execute(d)
        await db.commit()
    return sorted(targets)


async def without_evidence(workspace_id, target_ids: Iterable) -> list[str]:
    """Which of `target_ids` have no pointer left - an entity whose every source is gone is stale."""
    ids = [str(t) for t in target_ids if t]
    if not ids:
        return []
    have = await for_targets(workspace_id, ids)
    return [t for t in ids if t not in have]


async def assets_of(workspace_id, target_ids: Iterable) -> dict[str, set[str]]:
    """{target_id: {asset_id, ...}} - which assets support each node (FROM_ASSET)."""
    return {t: {p["asset_id"] for p in ps} for t, ps in (await for_targets(workspace_id, target_ids)).items()}


# ── the evidence scope: which unit the code that is writing is reading ──────────────────────────
#
#     async with evidence.collect(workspace_id):          # one per ingest job: batches the ledger
#         for chunk in chunks:
#             with evidence.unit(asset_id, "chunk", chunk_id, quote=..., extractor=...):
#                 ...extract and write nodes and edges as before...
#
# Every NodeRepo write inside `unit` records a pointer to that unit, so extractors that write through
# NodeRepo carry evidence without passing it around. Outside any `unit` nothing is recorded.

_unit: ContextVar[Optional[Pointer]] = ContextVar("evidence_unit", default=None)
_collector: ContextVar[Optional["Collector"]] = ContextVar("evidence_collector", default=None)

#: Written knowledge that is not world knowledge, so carries no evidence: the calendar.
_NOT_WORLD = {"Date"}
#: Pointers buffered before a collector writes them out.
_FLUSH_AT = 2000


def edge_id(source_id, edge_type: str, target_id) -> uuid.UUID:
    """The ledger's id for an edge: edges are keyed by their ends and type, not by an id of their own."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"edge:{source_id}:{edge_type}:{target_id}")


def current() -> Optional[Pointer]:
    return _unit.get()


@contextlib.contextmanager
def unit(asset_id, unit_kind: str, unit_key, **fields):
    """The chunk, row or record the code inside is reading."""
    token = _unit.set(Pointer(asset_id=str(asset_id), unit_kind=unit_kind, unit_key=str(unit_key), **fields))
    try:
        yield _unit.get()
    finally:
        _unit.reset(token)


class Collector:
    def __init__(self, workspace_id, from_asset: bool):
        self.workspace_id = workspace_id
        self.from_asset = from_asset
        self.nodes: list[tuple] = []
        self.edges: list[tuple] = []
        self.written = 0

    async def flush(self) -> None:
        nodes, edges, self.nodes, self.edges = self.nodes, self.edges, [], []
        token = _unit.set(None)                       # what the flush writes is not extraction
        try:
            self.written += await record(self.workspace_id, nodes, "node")
            self.written += await record(self.workspace_id, edges, "edge")
            if self.from_asset and nodes:
                # One FROM_ASSET edge per supporting asset - the graph's own view of provenance, so a
                # fact reaches its document without a chunk node in between.
                pairs = {(str(t), ps[0].asset_id) for t, ps in nodes if ps and str(t) != ps[0].asset_id}
                if pairs:
                    from opencortyx.core.substrate import NodeRepo
                    await NodeRepo().add_edges(workspace_id=self.workspace_id, edges=[
                        {"source_id": t, "target_id": a, "type": "FROM_ASSET"} for t, a in sorted(pairs)])
        finally:
            _unit.reset(token)


@contextlib.asynccontextmanager
async def collect(workspace_id, *, from_asset: Optional[bool] = None):
    """Batch the pointers of everything written inside into a few ledger writes. `from_asset`
    (default: when the workspace's data lives in Delta) also links each node to its assets."""
    if from_asset is None:
        from opencortyx.core.dataplane.mode import uses_delta
        from_asset = await uses_delta(workspace_id)
    c = Collector(workspace_id, bool(from_asset))
    token = _collector.set(c)
    try:
        yield c
    finally:
        _collector.reset(token)
        await c.flush()


async def note(workspace_id, target_id, *, node_type: Optional[str] = None, target_kind: str = "node",
               property: Optional[str] = None) -> None:
    """Called by NodeRepo after a write: record the active unit as the write's evidence."""
    p = _unit.get()
    if p is None or not target_id:
        return
    if target_kind == "node" and node_type in _NOT_WORLD:
        return
    from opencortyx.domain.ontology import ASSET_TYPES
    if target_kind == "node" and node_type in ASSET_TYPES:
        return
    ptr = _replace(p, property=property) if property is not None else p
    c = _collector.get()
    if c is not None and str(c.workspace_id) == str(workspace_id):
        (c.nodes if target_kind == "node" else c.edges).append((target_id, [ptr]))
        if len(c.nodes) + len(c.edges) >= _FLUSH_AT:
            await c.flush()
        return
    try:
        await record(workspace_id, [(target_id, [ptr])], target_kind)
    except Exception:                                        # noqa: BLE001 - the ledger never fails a write
        pass


async def flush() -> None:
    """Write out what the active collector holds - before something reads the ledger mid-job."""
    c = _collector.get()
    if c is not None:
        await c.flush()
