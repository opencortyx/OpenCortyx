"""Contradiction Detection & Resolution — one engine, four stages, in one file.

WHAT CHANGED AND WHY
The previous engine grouped statements by a property it expected to be called `subject`, over a
hardcoded list of node types, and asked an LLM to judge each group. On a workspace whose schema
mapping names that field anything else — `company_name`, `party`, `account` — the grouping silently
returned nothing: no error, no warning, no detection. The entity-grouping alternative filtered
holders by `type == "entity"`, a literal the rest of the codebase had already abandoned (see
`ingest/extraction._is_identity`), so it matched nothing for mapping-declared types either.

The fix is not a better prompt. It is candidate generation that needs no field names at all:
a written statement is compared against its own semantic neighbourhood, the statements hanging off
the same entity, and — only if a tenant declares one — an exact key. Field names, where they are
used, are configuration, never literals in this file.

THE BOUNDARY THAT MATTERS (measured, not assumed)
"Reliable Post-Retrieval Assembly for Agent Memory" (arXiv 2606.01435) shows that systems which let
one model both identify evidence and decide the answer score 54% single-hop / 7% multi-hop on
MemoryAgentBench, while separating the two reaches 82-93% / 27-41% — and that most of the gain comes
from the separation itself, not from the freshness rule. So here:
    a model may only say "these two statements are about the same thing and disagree"
    code alone decides who wins, what becomes historical, and what a human must approve

`localized maintenance is more cost-efficient than global reorganization` (arXiv 2606.24775, 12
memory systems over 11 datasets) is the other half: this runs per written statement against a
handful of retrieved neighbours, not as a sweep over the workspace.

Closing a validity window instead of deleting is Graphiti/Zep's bitemporal model: the superseded
statement stays queryable ("where were they in January?") and stops being served as current.

THE LAYERS
Read top to bottom; each layer is a banner in this file and does exactly one job.

  LAYER 0  CONFIGURATION      Settings — every knob, one card in the UI
  LAYER 1  ASSERTION          StatementView: text / temporal / provenance / confidence / values,
                              all through TENANT field names. RESERVED declares the system-owned
                              attributes that any node or edge may carry.
  LAYER 2  CANDIDATE GEN      "which assertions should be compared?" — semantic ∪ linked ∪ key
  LAYER 3  CONSISTENCY        3a RULES    — numbers, units, dates, booleans: decided by code
                              3b SEMANTIC — duplicate → NLI → LLM
                              3c GRAPH    — deliberately absent; see the note at that banner
  LAYER 4  DECISION / JUDGE   the cascade → Verdict, tagged CLEAR / UNCERTAIN / COMPLEX
  LAYER 5  CONFIDENCE GATE    accept / review / human — policy alone, never the model
  LAYER 6  FINAL DECISION     support | contradiction (evolve · supersede) | unknown
  LAYER 7  MEMORY             reinforcement · consolidation · temporal decay (close, never delete)
  LAYER 8  KNOWLEDGE STATE    the writes, and `history_of()` — the read that makes them answerable

WHY RULES RUN FIRST (LAYER 3a)
The judge this deployment serves agrees with labelled pairs about 6 times in 10. A rule tier does not
improve the model; it REMOVES work from it. "90 days vs 120 days", "EUR 100 vs USD 100",
"2024-01-15 vs 2024-06-01" are decidable by comparing extracted values — with certainty, at zero
inference cost. Only prose reaches the model. And the asymmetry that governs every rule here:

    a FALSE contradiction is worse than a missed one — it closes a validity window,
    so an unsure rule DECLINES and the next tier runs

Every layer is injected through `Deps`, so the whole pipeline is testable without a database, an
embedding server or an LLM — which is what `tests/test_contradiction_engine.py` does.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Optional

from opencortyx.core.substrate.document import STORED_KEY as STORED_TEXT_KEY
from opencortyx.capabilities.base import Capability, ConfigField
from opencortyx.core.config import settings
from opencortyx.core.substrate import NodeRepo

__all__ = ["Settings", "StatementView", "Verdict", "Outcome", "Deps", "GraphDeps", "RESERVED",
           "judge_by_rules", "gate", "history_of",
           "resolve_statement", "resolve_many", "sweep", "ContradictionDetection"]

# ── edges this engine writes (the review queue and retrieval already read these) ──────────────────
E_CONTRADICTS = "CONTRADICTS"
E_EVOLVED_FROM = "EVOLVED_FROM"
E_SUPERSEDES = "SUPERSEDES"
E_SUPPORTS = "SUPPORTS"                 # LAYER 6/7: corroboration — the agree branch


# ── THE RESERVED ATTRIBUTE CONTRACT ───────────────────────────────────────────────────────────────
class RESERVED:
    """System-owned attributes that any node or edge the platform writes may carry.

    "No hardcoded node/edge attributes" has meant, from day one, that TENANT SCHEMA is never written
    into code: a workspace names its own fields and this engine reads them through configuration. That
    rule is not in tension with this class, because these are not tenant fields — they are the
    platform's own bookkeeping, and they are declared HERE, in one place, instead of appearing as
    string literals scattered across the engine, the review queue and retrieval.

    Two properties make them safe to put on everything:
      • they are namespaced (`__`-prefixed, or `resolution_`/`valid_` for the ones retrieval and the
        review queue already query by name), so a tenant field can never collide with one;
      • they are all OPTIONAL and absent-means-unknown. A node that has never been compared carries
        none of them, and every reader treats missing as "no opinion" rather than as zero.

    BELIEF and SALIENCE are kept apart deliberately, and it is the load-bearing decision of LAYER 7:
    `superseded`/`__confidence` answer "do we believe this", `__access_count` answers "does anyone
    need this". A fact can be true and never read (an office address), or read constantly and wrong.
    Collapsing them into one number makes "not current, but exactly what you asked for" — the answer
    to "where did they travel BEFORE?" — impossible to express.
    """

    # ── temporal validity (LAYER 1 reads, LAYER 7 writes) ──
    VALID_FROM = "valid_from"           # when this became true; absent = unknown, never epoch
    VALID_UNTIL = "valid_until"         # bitemporal close; the statement stays, stops being current

    # ── belief (LAYER 6/7) ──
    SUPERSEDED = "superseded"           # "true" (applied) | "proposed" (awaiting review)
    SUPERSEDED_BY = "superseded_by"
    ABOUT = "resolution_about"          # what the pair disagreed about
    PRIORITY = "resolution_priority"    # LAYER 5: "review" | "human" — how a proposal is queued
    CONFIDENCE = "__confidence"         # mirrored onto the node's confidence by patch_props
    SUPPORT_COUNT = "__support_count"   # independent corroborations seen (LAYER 7)

    # ── salience (LAYER 7 — ranking only, NEVER a filter) ──
    ACCESS_COUNT = "__access_count"
    LAST_ACCESSED = "__last_accessed"

    # ── edge properties, written on every edge this engine creates ──
    E_METHOD = "method"                 # rule | duplicate | nli | llm — which tier decided
    E_CONFIDENCE = "confidence"
    E_LANE = "lane"                     # clear | uncertain | complex (LAYER 4)
    E_AT = "at"

    #: What a pending proposal writes on its loser — removed together when it is rejected or withdrawn.
    PROPOSAL_KEYS = (SUPERSEDED, SUPERSEDED_BY, ABOUT, PRIORITY)

    #: Never comparable as tenant values (LAYER 3a skips these when diffing two statements).
    SYSTEM_KEYS = frozenset({
        VALID_FROM, VALID_UNTIL, SUPERSEDED, SUPERSEDED_BY, ABOUT, PRIORITY, CONFIDENCE,
        SUPPORT_COUNT, ACCESS_COUNT, LAST_ACCESSED,
    })
    #: Where and when a node was written, not what it asserts: the repository's date stamps and the
    #: document reader's position fields. As compared values they made every pair of sibling
    #: paragraphs a "certain" rule contradiction (`ordinal: 1 vs 3`), auto-applied as SUPERSEDES.
    PROVENANCE_KEYS = frozenset({
        "created_date", "modified_date", "ordinal", "page_number", "chunk_index", "label", "Label",
        "row_count", "grid",
    })


# Back-compat aliases: these names are read by the review queue and retrieval today.
MARK_SUPERSEDED = RESERVED.SUPERSEDED
MARK_SUPERSEDED_BY = RESERVED.SUPERSEDED_BY
MARK_ABOUT = RESERVED.ABOUT
MARK_VALID_UNTIL = RESERVED.VALID_UNTIL


_DEFAULTS: dict = {
    "memory_resolution": True,
    "contradiction_use_rules": True,
    "contradiction_use_llm": True,
    "contradiction_support": True,
    "contradiction_support_step": 0.1,
    "contradiction_support_cap": 0.95,
    "contradiction_text_fields": ["text", "statement", "summary", "title", "value"],
    "contradiction_time_fields": ["valid_from", "effective_date", "as_of", "ts", "date", "timestamp"],
    "contradiction_reason_fields": ["reason", "rationale", "because", "note"],
    "contradiction_key_fields": [],
    "contradiction_types": [],
    "contradiction_similarity": 0.82,
    "contradiction_candidates": 8,
    "contradiction_hub_degree": 25,
    "contradiction_auto_apply": 0.75,
    "contradiction_judge_model": "",
    "resolution_mode": "latest_wins",
    "resolution_review": False,
    "resolution_trust_boost": 0.5,
}


def _as_list(v, fallback: list) -> list:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():                 # tolerate "a, b" typed into the UI
        return [p.strip() for p in v.split(",") if p.strip()]
    return list(fallback)


@dataclass
class Settings:
    enabled: bool = True
    use_rules: bool = True
    use_llm: bool = True
    support: bool = True
    support_step: float = _DEFAULTS["contradiction_support_step"]
    support_cap: float = _DEFAULTS["contradiction_support_cap"]
    text_fields: list = field(default_factory=lambda: list(_DEFAULTS["contradiction_text_fields"]))
    time_fields: list = field(default_factory=lambda: list(_DEFAULTS["contradiction_time_fields"]))
    reason_fields: list = field(default_factory=lambda: list(_DEFAULTS["contradiction_reason_fields"]))
    key_fields: list = field(default_factory=list)
    types: list = field(default_factory=list)
    similarity: float = _DEFAULTS["contradiction_similarity"]
    candidates: int = _DEFAULTS["contradiction_candidates"]
    hub_degree: int = _DEFAULTS["contradiction_hub_degree"]
    auto_apply: float = _DEFAULTS["contradiction_auto_apply"]
    judge_model: str = ""
    mode: str = "latest_wins"
    review: bool = False
    trust_boost: float = 0.5

    @classmethod
    def load(cls) -> "Settings":
        """Workspace settings, defaults filled in. Never raises: a config store that is unreachable
        must not stop a write from being checked — it falls back to the shipped defaults."""
        try:
            from opencortyx.core.config import runtime as rc
            get = rc.get
        except Exception:                                  # noqa: BLE001 — import-time/config failure
            def get(_k):
                return None

        def val(key):
            v = get(key)
            return _DEFAULTS[key] if v is None else v

        return cls(
            enabled=bool(val("memory_resolution")),
            use_rules=bool(val("contradiction_use_rules")),
            use_llm=bool(val("contradiction_use_llm")),
            support=bool(val("contradiction_support")),
            support_step=float(val("contradiction_support_step")),
            support_cap=float(val("contradiction_support_cap")),
            text_fields=_as_list(val("contradiction_text_fields"), _DEFAULTS["contradiction_text_fields"]),
            time_fields=_as_list(val("contradiction_time_fields"), _DEFAULTS["contradiction_time_fields"]),
            reason_fields=_as_list(val("contradiction_reason_fields"), _DEFAULTS["contradiction_reason_fields"]),
            key_fields=_as_list(val("contradiction_key_fields"), []),
            types=_as_list(val("contradiction_types"), []),
            similarity=float(val("contradiction_similarity")),
            candidates=int(val("contradiction_candidates")),
            hub_degree=int(val("contradiction_hub_degree")),
            auto_apply=float(val("contradiction_auto_apply")),
            judge_model=str(val("contradiction_judge_model") or ""),
            mode=str(val("resolution_mode") or "latest_wins"),
            review=bool(val("resolution_review")),
            trust_boost=float(val("resolution_trust_boost")),
        )


# ══ LAYER 1 — THE ASSERTION ══════════════════════════════════════════════════════════════════════
_ISO_LIKE = re.compile(r"^\s*(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")


def parse_time(value: Any) -> Optional[datetime]:
    """A timestamp from whatever the tenant stored, or None when there isn't one.

    None is a first-class answer, not a zero: `valid_from` is frequently absent, and treating a
    missing date as 1970 would make every undated statement lose every recency comparison — silently
    retiring facts because nobody filled in a field."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)):                    # epoch seconds (ms tolerated)
        try:
            v = float(value)
            return datetime.utcfromtimestamp(v / 1000.0 if v > 1e11 else v)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:                                                   # full ISO, with or without a zone
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    m = _ISO_LIKE.match(text)                              # "2026", "2026-03", "2026-03-11 …"
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))
        except ValueError:
            return None
    return None


def _norm_text(s: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. The collapse is load-bearing: without it
    "acme is in  delhi" and "acme is in delhi" were different strings, so the same statement typed
    with a stray double space missed the exact-match duplicate check."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower())).strip()


@dataclass
class StatementView:
    """One node, read through the tenant's own field names."""
    id: str
    type: str
    text: str
    time: Optional[datetime] = None
    reason: str = ""
    keys: dict = field(default_factory=dict)
    trust: float = 0.0                     # confidence + the validated/user boost (LAYER 6 winner policy)
    confidence: float = 0.0                # the node's OWN confidence, unboosted (LAYER 7 reinforcement)
    score: float = 0.0                     # similarity that surfaced this candidate (0 = not semantic)
    created_at: Optional[datetime] = None
    superseded: bool = False
    #: LAYER 3a input: the statement's own scalar properties, system attributes and the text/time/
    #: reason fields removed — what is left is the tenant's structured data, comparable by code.
    values: dict = field(default_factory=dict)
    origin: str = "self"
    #: Independence check for LAYER 7: the same evidence ingested twice is not corroboration.
    source_ref: str = ""
    support_count: int = 0                 # independent corroborations already counted (LAYER 7)
    #: A walked RECORD (a JSON row), not a written statement: see `decide`.
    record: bool = False

    @property
    def time_known(self) -> bool:
        return self.time is not None

    @classmethod
    def of(cls, node: Any, cfg: Settings) -> Optional["StatementView"]:
        """Build a view, or None when the node carries no assertion to compare.

        `node` is either an ORM Node or the plain dict shape the SDK/tests use — both are read the
        same way, so nothing in this engine depends on the persistence layer."""
        def prop(name, default=None):
            if isinstance(node, dict):
                return (node.get("properties") or {}).get(name, default)
            return (getattr(node, "properties", None) or {}).get(name, default)

        def attr(name, default=None):
            if isinstance(node, dict):
                return node.get(name, default)
            return getattr(node, name, default)

        if prop(READING_UNIT_KEY) or attr("type") in _reading_unit_types():
            # A passage of a document is not an assertion: the claims extracted from it are, and they
            # are judged. Judged as statements, every chunk of a 200-chunk PDF was compared against
            # its neighbours with an LLM call each — on every upload, including an unchanged one.
            return None

        if prop(IDENTITY_KEY):
            # A resolved entity is a NAME, not an assertion: two names are the same thing (entity
            # resolution merges them into aliases) or different things — never a contradiction.
            # Judged as statements, "vendor register" was proposed to be retired in favour of "Failure".
            return None

        text = ""
        for f in cfg.text_fields:                          # tenant order wins: first non-empty
            v = prop(f)
            if isinstance(v, str) and v.strip():
                text = v.strip()
                break
        if not text:
            # The document the substrate stores for every node (a row's or a record's fields as
            # text) before the bare label: judged on labels alone, two table rows read as
            # "Product #0" vs "Product #1" and a model called that a contradiction.
            stored = prop(STORED_TEXT_KEY)
            text = str(stored).strip() if isinstance(stored, str) and stored.strip() else ""
        if not text:
            label = attr("label") or ""
            text = str(label).strip()
        if not text:
            return None

        when = None
        for f in cfg.time_fields:
            when = parse_time(prop(f))
            if when is not None:
                break

        reason = ""
        for f in cfg.reason_fields:
            v = prop(f)
            if isinstance(v, str) and v.strip():
                reason = v.strip()
                break

        keys = {}
        for f in cfg.key_fields:
            v = prop(f)
            if isinstance(v, str) and v.strip():
                keys[f] = v.strip()

        own_confidence = float(attr("confidence") or 0.0)
        trust = own_confidence
        if prop("__validated") or prop("__source") == "user":
            trust += cfg.trust_boost

        consumed = set(cfg.text_fields) | set(cfg.time_fields) | set(cfg.reason_fields)
        raw = (node.get("properties") if isinstance(node, dict) else getattr(node, "properties", None))
        values = {}
        for k, v in (raw or {}).items():
            if (k in RESERVED.SYSTEM_KEYS or k in RESERVED.PROVENANCE_KEYS or k in consumed
                    or str(k).startswith("__")):
                continue
            if isinstance(v, (str, int, float, bool)) and str(v).strip():
                values[str(k)] = v

        return cls(
            id=str(attr("id") or ""), type=str(attr("type") or ""), text=text, time=when,
            reason=reason, keys=keys, trust=trust, confidence=own_confidence,
            created_at=parse_time(attr("created_at")),
            superseded=str(prop(MARK_SUPERSEDED) or "").lower() == "true",
            values=values,
            source_ref=str(attr("source_ref") or ""),
            support_count=int(prop(RESERVED.SUPPORT_COUNT) or 0),
            record=(str(prop("__source") or "") in _RECORD_SOURCES or bool(prop("__source_key"))),
        )


# ══ VERDICT + OUTCOME — what LAYERS 4-6 pass along ═══════════════════════════════════════════════
@dataclass
class Verdict:
    relation: str            # "contradiction" | "duplicate" | "compatible" | "unrelated" | "unknown"
    confidence: float = 0.0
    method: str = ""         # rule | duplicate | nli | llm — which tier decided, for the audit trail
    detail: str = ""
    lane: str = "uncertain"


@dataclass
class Outcome:
    kind: str                # "support" | "consolidate" | "evolve" | "supersede" | "propose" | "none"
    winner: Optional[StatementView] = None
    loser: Optional[StatementView] = None
    verdict: Optional[Verdict] = None
    about: str = ""
    priority: str = ""       # LAYER 5, on a proposal only: "review" | "human"

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "about": self.about,
             "winner": self.winner.id if self.winner else None,
             "loser": self.loser.id if self.loser else None,
             "method": self.verdict.method if self.verdict else "",
             "lane": self.verdict.lane if self.verdict else "",
             "confidence": round(self.verdict.confidence, 3) if self.verdict else 0.0}
        if self.priority:
            d["priority"] = self.priority
        return d


# ── Dependencies: everything that touches the world, behind one seam ──────────────────────────────
class Deps:
    """What the pipeline needs from outside itself. `GraphDeps` is the production implementation;
    tests pass a fake. Keeping these four methods together is what makes the engine unit-testable
    without a database, an embedding server or a model."""

    async def load(self, workspace_id, node_id) -> Any:                      # -> node or None
        raise NotImplementedError

    async def semantic(self, workspace_id, view: StatementView, cfg: Settings) -> list:   # -> [(node, score)]
        raise NotImplementedError

    async def linked(self, workspace_id, view: StatementView, cfg: Settings) -> list:     # -> [node]
        raise NotImplementedError

    async def by_key(self, workspace_id, view: StatementView, cfg: Settings) -> list:     # -> [node]
        raise NotImplementedError

    async def edge_exists(self, workspace_id, a_id, b_id, type: str) -> bool:
        raise NotImplementedError

    async def evolved_from(self, workspace_id, node_id):                     # -> previous node id or None
        """LAYER 8: one hop back along the value's history. Optional — a Deps that does not
        implement it simply yields a one-entry history rather than failing."""
        return None

    async def write_edge(self, workspace_id, src, dst, type: str, properties: dict) -> None:
        raise NotImplementedError

    async def patch_props(self, workspace_id, node_id, props: dict) -> None:
        raise NotImplementedError

    async def clear_props(self, workspace_id, node_id, keys) -> None:
        """Remove properties from one node. Defaults to blanking them through `patch_props`."""
        await self.patch_props(workspace_id, node_id, {k: None for k in keys})

    async def llm_json(self, workspace_id, prompt: str, system: str, cfg: Settings) -> dict:
        raise NotImplementedError

    async def nli(self, a_text: str, b_text: str) -> Optional[float]:   # contradiction prob, or None
        return None


_ORIGIN_RANK: dict = {"self": 0, "semantic": 1, "linked": 2, "key": 3}


async def find_candidates(workspace_id, view: StatementView, cfg: Settings, deps: Deps) -> list:
    """Statements this one might disagree with — no field names required.

    Three sources, unioned and bounded: the semantic neighbourhood (works on any schema), statements
    attached to the same entity (structural, type-blind), and an exact match on a key the tenant
    declared (precision, optional). A statement without candidates simply has nothing to contradict:
    that is a normal outcome, not a failure.

    Each candidate is stamped with the source that found it (`origin`), because LAYER 3a cannot judge
    without it: a semantic neighbour is merely SIMILAR, while a linked or key candidate is
    structurally about the same subject. Differing values only mean a contradiction in the second
    case — "region=eu-west-1" and "region=ap-south-1" on two different customers is not a conflict."""
    out, seen = [], {view.id}

    def take(v, origin, score=None):
        """Add a candidate, or UPGRADE one already found by a weaker source.

        The upgrade is the load-bearing half. Semantic runs first and dedupes by id, so without it a
        statement that is both semantically similar AND structurally linked kept `origin="semantic"`
        purely because of the order the sources run in — and LAYER 3a then refused to judge it,
        because it only trusts structural evidence of a shared subject. Measured on the real graph:
        two SLA statements on the same entity were judged by the LLM at `lane=complex` when the rule
        tier should have settled them by arithmetic with no model call at all."""
        if v is None or v.superseded:
            return
        if v.id in seen:
            if origin == "semantic":
                return
            for existing in out:
                if existing.id == v.id and _ORIGIN_RANK[origin] > _ORIGIN_RANK[existing.origin]:
                    existing.origin = origin
            return
        v.origin = origin
        if score is not None:
            v.score = float(score or 0.0)
        seen.add(v.id)
        out.append(v)

    for node, score in (await deps.semantic(workspace_id, view, cfg) or []):
        take(StatementView.of(node, cfg), "semantic", score)
    for node in (await deps.linked(workspace_id, view, cfg) or []):
        take(StatementView.of(node, cfg), "linked")
    if cfg.key_fields and view.keys:
        for node in (await deps.by_key(workspace_id, view, cfg) or []):
            take(StatementView.of(node, cfg), "key")
    return out[: max(1, cfg.candidates)]


#: Time units in seconds. The flag is whether the conversion is EXACT — a month is not a fixed number
#: of days, so `3 months` vs `90 days` must not be declared different on a 1.4% rounding artefact.
_TIME_UNITS: dict = {
    "s": (1.0, True), "sec": (1.0, True), "secs": (1.0, True),
    "second": (1.0, True), "seconds": (1.0, True),
    "m": (60.0, True), "min": (60.0, True), "mins": (60.0, True),
    "minute": (60.0, True), "minutes": (60.0, True),
    "h": (3600.0, True), "hr": (3600.0, True), "hrs": (3600.0, True),
    "hour": (3600.0, True), "hours": (3600.0, True),
    "d": (86400.0, True), "day": (86400.0, True), "days": (86400.0, True),
    "w": (604800.0, True), "week": (604800.0, True), "weeks": (604800.0, True),
    "month": (2592000.0, False), "months": (2592000.0, False),   # 30d — approximate
    "quarter": (7776000.0, False), "quarters": (7776000.0, False),
    "year": (31536000.0, False), "years": (31536000.0, False),   # 365d — approximate
    "yr": (31536000.0, False), "yrs": (31536000.0, False),
}

#: Currency is deliberately NOT convertible. "EUR 100" and "USD 100" are different values and no
#: exchange rate belongs in a consistency check — it would make the verdict depend on the date it ran.
_CURRENCY_SYMBOLS: dict = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
_CURRENCY_CODES = frozenset({
    "USD", "EUR", "GBP", "JPY", "INR", "AUD", "CAD", "CHF", "CNY", "SEK", "NZD", "SGD", "AED", "ZAR",
})

_NUM = r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d*\.?\d+"
_QTY_RE = re.compile(rf"^\s*(?P<pre>[^\d\s.+-]{{1,3}})?\s*(?P<num>{_NUM})\s*(?P<post>[^\d\s]{{1,12}})?\s*$")


def _as_bool(v) -> Optional[bool]:
    """Booleans only where they are unambiguous. "1"/"0" are NOT accepted — they are far more often
    numbers, and reading a quantity as a flag is how a rule tier invents a conflict."""
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "yes"):
        return True
    if s in ("false", "no"):
        return False
    return None


def _as_date(v) -> Optional[datetime]:
    """Dates only from strings that LOOK like one. `parse_time` also accepts epoch integers, so
    feeding it a plain `4` would compare two SLA hour counts as if they were timestamps."""
    if isinstance(v, (datetime, date)):
        return parse_time(v)
    if isinstance(v, str) and _ISO_LIKE.match(v.strip()):
        return parse_time(v.strip())
    return None


def _quantity(v) -> Optional[tuple]:
    """(magnitude, family, unit, exact) or None when this is not a quantity.

    Families never compare across each other: "90" and "90 days" are left undecided rather than
    guessed at, because a bare number may or may not be in the other's unit."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return (float(v), "number", "", True)
    s = str(v).strip()
    if not s:
        return None
    m = _QTY_RE.match(s)
    if not m:
        return None
    try:
        mag = float(m.group("num").replace(",", ""))
    except (TypeError, ValueError):
        return None
    pre = (m.group("pre") or "").strip()
    post = (m.group("post") or "").strip()

    if pre in _CURRENCY_SYMBOLS:
        return (mag, "currency", _CURRENCY_SYMBOLS[pre], True)
    if post in _CURRENCY_SYMBOLS:
        return (mag, "currency", _CURRENCY_SYMBOLS[post], True)
    token = post.upper().strip(".")
    if token in _CURRENCY_CODES:
        return (mag, "currency", token, True)
    if (pre or "").upper() in _CURRENCY_CODES:
        return (mag, "currency", pre.upper(), True)
    if post == "%":
        return (mag, "percent", "%", True)
    unit = _TIME_UNITS.get(post.lower().strip("."))
    if unit:
        return (mag * unit[0], "time", "s", unit[1])
    if not post and not pre:
        return (mag, "number", "", True)
    return None                                            # a unit we do not model — decline


_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")


def _is_identifier(s: str) -> bool:
    """A machine code rather than a word: no whitespace, and carrying a digit or a separator.

    "eu-west-1", "SKU-456", "v2.1", "acct_9" qualify; "Mumbai", "active", "PostgreSQL 16" do not.
    Codes have no synonyms, which is what makes "different code" safe to call a contradiction while
    "different word" is not."""
    s = (s or "").strip()
    if not s or not _IDENT_RE.match(s):
        return False
    return any(c.isdigit() for c in s) or any(c in "._:/-" for c in s)


def _cmp_magnitude(x: float, y: float, exact: bool) -> str:
    """Same / differ / unknown. Approximate units get a dead band: close enough is the same value
    expressed differently, far enough apart is a real difference, and the gap between stays undecided
    instead of being resolved by a constant nobody can justify."""
    hi = max(abs(x), abs(y)) or 1.0
    rel = abs(x - y) / hi
    if rel <= 1e-9:
        return "same"
    if exact:
        return "differ"
    if rel <= 0.02:                                        # 3 months vs 90 days
        return "same"
    if rel >= 0.10:
        return "differ"
    return "unknown"


def _compare_values(x, y) -> str:
    """One property, two statements → "same" | "differ" | "unknown". Order matters: booleans and
    dates are checked before quantities so a flag or a timestamp is never read as a number."""
    bx, by = _as_bool(x), _as_bool(y)
    if bx is not None and by is not None:
        return "same" if bx == by else "differ"
    if (bx is None) != (by is None):
        return "unknown"                                   # one is a flag, the other is not

    dx, dy = _as_date(x), _as_date(y)
    if dx is not None and dy is not None:
        return "same" if dx == dy else "differ"
    if (dx is None) != (dy is None):
        return "unknown"

    qx, qy = _quantity(x), _quantity(y)
    if qx and qy:
        if qx[1] != qy[1]:
            return "unknown"                               # different families — not comparable
        if qx[1] == "currency" and qx[2] != qy[2]:
            return "differ"                                # EUR 100 is not USD 100
        return _cmp_magnitude(qx[0], qy[0], qx[3] and qy[3])
    if bool(qx) != bool(qy):
        return "unknown"

    nx, ny = _norm_text(str(x)), _norm_text(str(y))
    if nx and nx == ny:
        return "same"
    if _is_identifier(str(x)) and _is_identifier(str(y)):
        return "differ"
    return "unknown"


def _subject_established(a: StatementView, b: StatementView) -> bool:
    """May code treat these two as describing the SAME subject?

    Only structural evidence counts. A semantic neighbour is similar, which is not the same thing:
    two customers can both have a `region`, and calling that a contradiction would be a fabricated
    conflict between unrelated records."""
    if a.keys and b.keys and a.keys == b.keys:
        return True
    if a.record and b.record:
        # Two walked records sharing a parent are PEERS — the rows of one file, the items of one
        # order — not two readings of one subject. The reader already gave each its own identity;
        # a record that changed comes back as the same node. Only a declared key (above) can say
        # two records describe one thing.
        return False
    return b.origin in ("linked", "key") or a.origin in ("linked", "key")


#: `__source` values of nodes a structural reader walked out of a file, rather than statements.
#: Table rows carry the reader's `__source_key` instead. Both are platform bookkeeping, not tenant
#: fields: nothing here names a column.
_RECORD_SOURCES = frozenset({"json"})
#: The stamp entity resolution puts on the nodes it mints (see ingest.extraction.is_identity).
IDENTITY_KEY = "__identity"
#: The stamp the document reader puts on its reading units (see ingest.ingest_unstructured).
READING_UNIT_KEY = "__reading_unit"
_READING_UNIT_TYPES = None


def _reading_unit_types() -> frozenset:
    """The reader's own unit types — the default chunk and the layout labels — for units written
    before they were stamped. Not every platform type: a JSON record or a remembered Fact IS a
    statement."""
    global _READING_UNIT_TYPES
    if _READING_UNIT_TYPES is None:
        try:
            from opencortyx.ingest.formats.unstructured.layout.schemas import layout_element_types
            _READING_UNIT_TYPES = frozenset({"Chunk"} | set(layout_element_types()))
        except Exception:                           # noqa: BLE001
            _READING_UNIT_TYPES = frozenset({"Chunk"})
    return _READING_UNIT_TYPES


def judge_by_rules(a: StatementView, b: StatementView, cfg: Settings) -> Optional[Verdict]:
    """LAYER 3a. A certain verdict from the structured values, or None to fall through."""
    # Two records a reader walked out of files are two records: each has the identity the reader gave
    # it, and a record that changed comes back as the SAME node. So they neither contradict nor
    # corroborate one another — judged as statements, sibling rows read alike ("Order OR-1. amount=…")
    # and were "supporting" each other. Records are still judged against real statements.
    if a.record and b.record:
        return Verdict("unrelated", 1.0, "rule", "distinct records", lane="clear")
    if not cfg.use_rules:
        return None

    # Declared keys that DISAGREE settle it without a model: different subject, nothing to compare.
    if a.keys and b.keys:
        shared_keys = set(a.keys) & set(b.keys)
        if shared_keys and any(
                _norm_text(str(a.keys[k])) != _norm_text(str(b.keys[k])) for k in shared_keys):
            return Verdict("unrelated", 1.0, "rule", "different declared subject", lane="clear")

    if not _subject_established(a, b):
        return None
    shared = sorted(set(a.values) & set(b.values))
    if not shared:
        return None
    for k in shared:
        if _compare_values(a.values[k], b.values[k]) == "differ":
            detail = f"{k}: {a.values[k]} vs {b.values[k]}"
            return Verdict("contradiction", 1.0, "rule", detail[:120], lane="clear")
    return None                                            # agreement is LAYER 3b's to declare


# ══ LAYER 3b — CONSISTENCY: THE SEMANTIC ENGINE ═══════════════════════════════════════════════════
def judge_duplicate(a: StatementView, b: StatementView, cfg: Settings) -> Optional[Verdict]:
    """The SAME statement said twice — decided by code, never by a model. Runs first because a repeat
    that reached an LLM would be judged 'compatible' at the cost of a call, every time.

    Exact match only, deliberately. A SimHash tier was tried here and removed: measured on real text,
    one word changed in a 23-word statement is 6 bits and a whole clause added is also 6, while an
    unrelated statement of the same shape is 28. There is no threshold that separates "same statement,
    tidier wording" from "same statement, different number" — and the second is the most common factual
    contradiction there is ("revenue was $4.2B" vs "$4.8B"). A fingerprint that swallows those would
    reintroduce exactly the silent failure this engine exists to remove, so near-duplicates go to the
    judge, which can tell a restatement from a changed value."""
    na, nb = _norm_text(a.text), _norm_text(b.text)
    if not na or not nb:
        return None
    if na == nb:
        return Verdict("duplicate", 1.0, "duplicate", "identical text")
    return None


_LLM_SYSTEM = "You read statements and report what they are about. Answer only with JSON."

_LLM_PROMPT = (
    "A: {a}\n"
    "B: {b}\n\n"
    "For each statement, name what it is ABOUT (the subject) and WHICH property of it the statement "
    "gives (the attribute). Then say whether A and B give different values for that property.\n"
    "Saying the same value in other words (90 days / ninety days) is NOT different.\n\n"
    'Return only: {{"a_subject": "...", "a_attribute": "...", '
    '"b_subject": "...", "b_attribute": "...", "values_differ": true|false}}'
)

_SAME_TARGET = 0.5


def _tokens(text: str) -> set:
    """Meaningful words only — "the", "of", "is" carry no signal about which thing is described."""
    return {w for w in _norm_text(text).split() if len(w) > 2}


def _overlap(x: str, y: str) -> float:
    """How much two short phrases name the same thing, 0..1.

    The short-name fallback is not a nicety: dropping words of 1-2 characters empties the token set
    for real attribute names like "HQ", "ID", "IP" or "AZ", and an empty set scored 0.0 — so every
    statement whose subject or attribute the model named that way was ruled unrelated, silently. When
    either side has no long words left, the normalised strings are compared directly."""
    nx, ny = _norm_text(x), _norm_text(y)
    if not nx or not ny:
        return 0.0
    tx, ty = _tokens(nx), _tokens(ny)
    if not tx or not ty:
        return 1.0 if nx == ny else 0.0
    return len(tx & ty) / min(len(tx), len(ty))


def _same_target(answer: dict) -> tuple:
    """Do the two statements describe the same attribute of the same subject? Decided here, from the
    model's extraction — never asked of the model as a judgement."""
    subj = _overlap(str(answer.get("a_subject") or ""), str(answer.get("b_subject") or ""))
    attr = _overlap(str(answer.get("a_attribute") or ""), str(answer.get("b_attribute") or ""))
    about = str(answer.get("a_attribute") or answer.get("b_attribute") or "")[:60]
    return (subj >= _SAME_TARGET and attr >= _SAME_TARGET), about


def _truthy(v) -> bool:
    """Models answer booleans as booleans, as "true"/"false", and as "yes"/"no"."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1")


# ══ LAYER 4 — DECISION / JUDGE ═══════════════════════════════════════════════════════════════════
async def judge_pair(workspace_id, a: StatementView, b: StatementView, cfg: Settings, deps: Deps) -> Verdict:
    """LAYER 4. The cascade, cheapest and most certain first. Each tier may decline (returns None or
    `unknown`) and the next one runs; when the last tier available declines, nothing is written — an
    undecided pair is left alone by design.

        3a RULES  →  3b DUPLICATE  →  3b NLI  →  3b LLM

    Rules lead because they are both the cheapest and the only tier that is CERTAIN. Every tier above
    the LLM also runs when the LLM is switched off, which is what makes that toggle safe: turning the
    model off degrades coverage, it does not disable detection."""
    ruled = judge_by_rules(a, b, cfg)
    if ruled is not None:
        return ruled

    dup = judge_duplicate(a, b, cfg)
    if dup is not None:
        return dup

    prob = await deps.nli(a.text, b.text)
    if prob is not None and prob >= 0.80:                  # confident model, no LLM call needed
        return Verdict("contradiction", float(prob), "nli", "entailment model", lane="clear")
    if prob is not None and prob <= 0.20:
        return Verdict("compatible", 1.0 - float(prob), "nli", "entailment model", lane="clear")

    if not cfg.use_llm:                                    # the toggle: tiers 1-3 still did their job
        return Verdict("unknown", 0.0, "nli" if prob is not None else "none", "llm disabled")

    lane = "uncertain" if _subject_established(a, b) else "complex"

    try:
        data = await deps.llm_json(workspace_id, _LLM_PROMPT.format(a=a.text[:600], b=b.text[:600]),
                                   _LLM_SYSTEM, cfg) or {}
    except Exception as e:                                 # noqa: BLE001 — a judge outage is not a verdict
        return Verdict("unknown", 0.0, "llm", f"judge unavailable: {type(e).__name__}", lane=lane)
    if not isinstance(data, dict) or not ({"a_subject", "b_subject", "values_differ"} & set(data)):
        return Verdict("unknown", 0.0, "llm", "unparseable answer", lane=lane)
    same, about = _same_target(data)

    conf = b.score or a.score or 0.6
    if not same:
        return Verdict("unrelated", conf, "llm", about, lane=lane)
    return Verdict("contradiction" if _truthy(data.get("values_differ")) else "compatible",
                   conf, "llm", about, lane=lane)


# ══ LAYER 5 — THE CONFIDENCE GATE ═════════════════════════════════════════════════════════════════
def gate(verdict: Verdict, cfg: Settings) -> str:
    """How much authority this verdict has: "accept" | "review" | "human".

    Three destinations, from the lane and the confidence together — never from the model's opinion of
    itself. The lane is what makes this more than a threshold: a COMPLEX verdict is one where nothing
    outside the model says the two statements are even related, so it is never auto-applied no matter
    how confident it claims to be. That is the case where a wrong answer invents a conflict between
    unrelated records, and no number the model reports can rule it out.

    A note on the "medium → re-check with rules" lane in the original design: rules do not run here,
    because they already ran FIRST, in LAYER 3a. Re-running them would re-derive a decline. What a
    medium verdict needs is a person, so the difference is recorded as the proposal's priority and the
    review queue orders by it.

    The lane decides WHERE a below-threshold verdict goes; it does not veto auto-apply. An earlier
    draft refused to auto-apply anything in the COMPLEX lane, which sounds prudent and is wrong:
    almost every candidate is found by semantic search, so it would have turned the engine into a
    queue that resolves nothing. Similarity is real evidence measured outside the model, and it is
    already what `confidence` carries — so one threshold governs acceptance, exactly as before."""
    if cfg.review:
        return "human"                                     # the workspace demands approval for all
    if verdict.confidence >= cfg.auto_apply:
        return "accept"
    return "human" if verdict.lane == "complex" else "review"


# ══ LAYER 6 — THE FINAL DECISION ══════════════════════════════════════════════════════════════════
def is_evolution(a: StatementView, b: StatementView) -> bool:
    """A disagreement that is really a change over time: either side gives a reason, or both are
    dated and the dates differ. Evolutions keep BOTH statements."""
    if a.reason or b.reason:
        return True
    return a.time_known and b.time_known and a.time != b.time


def pick_winner(a: StatementView, b: StatementView, cfg: Settings) -> tuple:
    """(winner, loser) under the workspace's policy.

    A KNOWN time always beats an unknown one, whichever mode is set: the alternative is letting an
    undated statement win on a tie-break it never earned. When neither side has any signal the order
    is stable by id, so re-running never flips a decision."""
    def by_time():
        if a.time_known and b.time_known and a.time != b.time:
            return (a, b) if a.time > b.time else (b, a)
        if a.time_known != b.time_known:
            return (a, b) if a.time_known else (b, a)
        return None

    def by_trust():
        if a.trust != b.trust:
            return (a, b) if a.trust > b.trust else (b, a)
        return None

    order = (by_trust, by_time) if cfg.mode == "trust_weighted" else (by_time, by_trust)
    for rule in order:
        got = rule()
        if got:
            return got
    if a.created_at and b.created_at and a.created_at != b.created_at:
        return (a, b) if a.created_at > b.created_at else (b, a)
    return (a, b) if a.id >= b.id else (b, a)


def decide(a: StatementView, b: StatementView, verdict: Verdict, cfg: Settings) -> Outcome:
    """Verdict + policy → what actually happens. This function is the reason the model is never
    asked who wins: everything below is inspectable, repeatable and settable in the UI."""
    if verdict.relation == "duplicate":
        return (Outcome("consolidate", a, b, verdict, "duplicate") if cfg.support
                else Outcome("none", verdict=verdict, about="duplicate"))
    if verdict.relation == "compatible":
        return (Outcome("support", a, b, verdict, verdict.detail or "agrees") if cfg.support
                else Outcome("none", verdict=verdict, about="compatible"))
    if verdict.relation != "contradiction":
        return Outcome("none", verdict=verdict, about=verdict.relation or "undecided")

    winner, loser = pick_winner(a, b, cfg)
    about = (winner.keys or loser.keys or {})
    about = next(iter(about.values()), "") if about else winner.text[:60]
    if is_evolution(a, b):
        return Outcome("evolve", winner, loser, verdict, about)
    decision = gate(verdict, cfg)
    if decision == "accept" and (a.record or b.record):
        # Records the walker wrote as DISTINCT nodes are distinct by construction: a change to one
        # arrives as an update to the same id-keyed node, not as a second node. Whichever tier says
        # one sibling row "supersedes" another (two line items of a shipment, "linked" by their
        # parent and differing in `sku`), it is at most a question for a person, never grounds to
        # hide the row automatically.
        decision = "review"
    if decision == "accept":
        return Outcome("supersede", winner, loser, verdict, about)
    return Outcome("propose", winner, loser, verdict, about, priority=decision)


# ══ LAYERS 7 + 8 — MEMORY, then THE KNOWLEDGE STATE ══════════════════════════════════════════════
def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


async def _reinforce(workspace_id, out: Outcome, deps: Deps, a: StatementView, b: StatementView,
                     cfg: Settings, props: dict) -> dict:
    """LAYER 7 — REINFORCEMENT + CONSOLIDATION. Two statements agree: record it, and raise belief.

    Three safeguards, each of which exists because the naive version is wrong:

    IDEMPOTENT. If the pair already has a SUPPORTS edge, this returns without touching confidence.
    Re-running the engine — a sweep, the peer's own write, a curator's edit — must not compound a
    raise that was already counted.

    INDEPENDENT. The same evidence ingested twice is NOT corroboration. Two statements sharing a
    `source_ref` get the edge (they really do agree) but no confidence raise, because nothing new was
    learned. Without this, re-ingesting one document would inflate every fact in it.

    BOUNDED. `cur + step * (1 - cur)` approaches the cap asymptotically and never reaches 1.0.
    Certainty is not something repetition can manufacture, and a fact at 1.0 would outrank a
    human-validated one."""
    if await deps.edge_exists(workspace_id, a.id, b.id, E_SUPPORTS) or \
       await deps.edge_exists(workspace_id, b.id, a.id, E_SUPPORTS):
        return {**out.to_dict(), "reinforced": False, "why": "already counted"}
    await deps.write_edge(workspace_id, a.id, b.id, E_SUPPORTS, props)
    if a.source_ref and a.source_ref == b.source_ref:
        return {**out.to_dict(), "reinforced": False, "why": "same source — not independent"}

    raised = []
    for v in (a, b):
        cur = max(0.0, min(1.0, float(v.confidence or 0.0)))
        new = min(cfg.support_cap, cur + cfg.support_step * (1.0 - cur))
        if new <= cur:
            continue
        await deps.patch_props(workspace_id, v.id, {
            RESERVED.CONFIDENCE: round(new, 4),
            RESERVED.SUPPORT_COUNT: int(v.support_count or 0) + 1,
        })
        raised.append({"node": v.id, "from": round(cur, 4), "to": round(new, 4)})
    return {**out.to_dict(), "reinforced": True, "raised": raised}


async def apply_outcome(workspace_id, out: Outcome, deps: Deps, a: StatementView, b: StatementView,
                        cfg: Optional[Settings] = None) -> dict:
    """LAYER 8 — write the decision into the knowledge state.

    CONTRADICTS always records that a disagreeing pair was judged; the rest depends on the outcome. A
    superseded statement is never deleted — its validity window is closed, which is what keeps
    'what did we believe in March?' answerable."""
    if out.kind == "none":
        return out.to_dict()
    cfg = cfg or Settings.load()

    props = {RESERVED.E_METHOD: out.verdict.method if out.verdict else "",
             RESERVED.E_CONFIDENCE: round(out.verdict.confidence, 3) if out.verdict else 0.0,
             RESERVED.E_LANE: out.verdict.lane if out.verdict else "",
             RESERVED.E_AT: _now_iso()}

    if out.kind in ("support", "consolidate"):
        return await _reinforce(workspace_id, out, deps, a, b, cfg, props)

    await deps.write_edge(workspace_id, a.id, b.id, E_CONTRADICTS, props)

    winner, loser = out.winner, out.loser
    close_at = (winner.time or datetime.utcnow()).replace(microsecond=0).isoformat()

    if out.kind == "evolve":
        await deps.write_edge(workspace_id, winner.id, loser.id, E_EVOLVED_FROM, props)
        await deps.patch_props(workspace_id, loser.id, {MARK_VALID_UNTIL: close_at, MARK_ABOUT: out.about})
    elif out.kind == "supersede":
        await deps.write_edge(workspace_id, winner.id, loser.id, E_SUPERSEDES, props)
        await deps.patch_props(workspace_id, loser.id, {
            MARK_SUPERSEDED: "true", MARK_SUPERSEDED_BY: winner.id, MARK_ABOUT: out.about,
            MARK_VALID_UNTIL: close_at, "__confidence": 0})
    elif out.kind == "propose":
        await deps.patch_props(workspace_id, loser.id, {
            RESERVED.SUPERSEDED: "proposed", RESERVED.SUPERSEDED_BY: winner.id,
            RESERVED.ABOUT: out.about, RESERVED.PRIORITY: out.priority or "review"})
    return out.to_dict()


async def history_of(workspace_id, node_id, *, deps: Optional[Deps] = None, limit: int = 50) -> list:
    """LAYER 8 (read) — the ordered history of one value, newest first.

    This is the half of bitemporality that writing `valid_until` alone does not give you. Every
    `evolve` writes `winner --EVOLVED_FROM--> loser`, so the versions of a value are already a chain
    in the graph; this walks it. "Where are they now?" is the head, "where were they before?" is one
    hop back — an exact graph answer, not a ranking guess, which is what makes a superseded statement
    still worth keeping.

    Returns [{id, text, valid_from, valid_until, current}], starting from `node_id` and following
    EVOLVED_FROM backwards in time. `limit` bounds a cycle as well as a long chain."""
    deps = deps or GraphDeps()
    cfg = Settings.load()
    out, seen, cur = [], set(), str(node_id)
    while cur and cur not in seen and len(out) < limit:
        seen.add(cur)
        node = await deps.load(workspace_id, cur)
        if node is None:
            break
        view = StatementView.of(node, cfg)
        props = (node.get("properties") if isinstance(node, dict)
                 else getattr(node, "properties", None)) or {}
        out.append({
            "id": cur,
            "text": view.text if view else "",
            "valid_from": props.get(RESERVED.VALID_FROM) or (view.time.isoformat() if view and view.time else None),
            "valid_until": props.get(RESERVED.VALID_UNTIL),
            "current": not props.get(RESERVED.VALID_UNTIL) and str(
                props.get(RESERVED.SUPERSEDED) or "").lower() != "true",
        })
        nxt = await deps.evolved_from(workspace_id, cur) if hasattr(deps, "evolved_from") else None
        cur = str(nxt) if nxt else ""
    return out


# ══ ENTRY POINTS ═════════════════════════════════════════════════════════════════════════════════
async def resolve_statement(workspace_id, node_id, *, deps: Optional[Deps] = None,
                            cfg: Optional[Settings] = None) -> dict:
    """Check ONE written statement against its neighbourhood. This is the write-path hook: ingest,
    an agent's `remember`, a session distillation and a curator's edit all call it the same way."""
    cfg = cfg or Settings.load()
    if not cfg.enabled:
        return {"skipped": "disabled"}
    deps = deps or GraphDeps()
    node = await deps.load(workspace_id, node_id)
    if node is None:
        return {"skipped": "not found"}
    view = StatementView.of(node, cfg)
    if view is None:
        # not a statement (any more): a claim proposal left on it from before has nothing to stand on
        withdrawn = await _recheck_proposal(workspace_id, node, None, cfg, deps)
        return {"skipped": "not a statement", "withdrawn": withdrawn}
    if view.superseded:
        return {"skipped": "superseded"}
    withdrawn = await _recheck_proposal(workspace_id, node, view, cfg, deps)

    candidates = await find_candidates(workspace_id, view, cfg, deps)
    outcomes, verdicts, judged = [], [], 0
    for other in candidates:
        if await deps.edge_exists(workspace_id, view.id, other.id, E_CONTRADICTS) or \
           await deps.edge_exists(workspace_id, other.id, view.id, E_CONTRADICTS) or \
           await deps.edge_exists(workspace_id, view.id, other.id, E_SUPPORTS) or \
           await deps.edge_exists(workspace_id, other.id, view.id, E_SUPPORTS):
            continue
        verdict = await judge_pair(workspace_id, view, other, cfg, deps)
        judged += 1
        verdicts.append({"peer": other.id, "relation": verdict.relation, "method": verdict.method,
                         "lane": verdict.lane, "confidence": round(verdict.confidence, 3)})
        out = decide(view, other, verdict, cfg)
        if out.kind != "none":
            outcomes.append(await apply_outcome(workspace_id, out, deps, view, other, cfg))
    # `verdicts` is the audit trail: a pair judged 'duplicate' or 'compatible' writes nothing, and
    # without this the caller could not tell that from having found no candidate at all.
    return {"node": str(node_id), "candidates": len(candidates), "judged": judged,
            "outcomes": outcomes, "verdicts": verdicts, "withdrawn": withdrawn}


async def _recheck_proposal(workspace_id, node, view: Optional[StatementView], cfg: Settings,
                            deps: Deps) -> bool:
    """A pending proposal is only as good as the latest judgment of its pair. The statement was just
    rewritten (a re-ingest, an edit), so when the CERTAIN tier now says the pair does not conflict,
    the proposal is withdrawn instead of waiting in the queue for a person to reject a stale claim.
    Only rules are consulted: they are free and certain, and a model's second opinion is not grounds
    to overturn its first. Version proposals belong to versioning and are left alone."""
    props = (node.get("properties") if isinstance(node, dict) else getattr(node, "properties", None)) or {}
    if str(props.get(RESERVED.SUPERSEDED) or "") != "proposed" or props.get("resolution_kind"):
        return False
    node_id = view.id if view is not None else _node_id(node)
    if view is not None:
        other_id = props.get(RESERVED.SUPERSEDED_BY)
        other = await deps.load(workspace_id, other_id) if other_id else None
        other_view = StatementView.of(other, cfg) if other is not None else None
        if other_view is not None:
            ruled = judge_by_rules(view, other_view, cfg)
            if ruled is None or ruled.relation == "contradiction":
                return False
    await deps.clear_props(workspace_id, node_id, RESERVED.PROPOSAL_KEYS)
    return True


def _node_id(node) -> str:
    return str(node.get("id") if isinstance(node, dict) else getattr(node, "id", ""))


async def resolve_many(workspace_id, node_ids, *, deps: Optional[Deps] = None,
                       cfg: Optional[Settings] = None) -> dict:
    cfg = cfg or Settings.load()
    if not cfg.enabled:
        return {"skipped": "disabled"}
    deps = deps or GraphDeps()
    done, applied = 0, 0
    for nid in (node_ids or []):
        r = await resolve_statement(workspace_id, nid, deps=deps, cfg=cfg)
        done += 1
        applied += len(r.get("outcomes") or [])
    return {"checked": done, "applied": applied}


async def sweep(workspace_id, *, limit: int = 200, deps: Optional[Deps] = None,
                cfg: Optional[Settings] = None, since=None) -> dict:
    """A bounded pass over recent statements — for a datasource that produced no single unit (a SQL
    schema ingest) and for the manual re-run. Deliberately bounded and deliberately not the default
    path: per-write checking is both cheaper and more accurate than periodic reorganisation.

    `since` scopes the pass to what was written after that moment — an ingest checks ITS writes.
    Unscoped, every SQL ingest re-judged the workspace's 200 newest nodes, one model call per pair."""
    cfg = cfg or Settings.load()
    if not cfg.enabled:
        return {"skipped": "disabled"}
    deps = deps or GraphDeps()
    ids = await deps.recent(workspace_id, cfg, limit, since=since) if hasattr(deps, "recent") else []
    return await resolve_many(workspace_id, ids, deps=deps, cfg=cfg)


# ══ PRODUCTION DEPENDENCIES ══════════════════════════════════════════════════════════════════════
class GraphDeps(Deps):
    """The real graph, embeddings, entailment model and LLM. Imports are local so the module stays
    importable (and unit-testable) in an environment with none of them."""

    async def load(self, workspace_id, node_id):
        return await NodeRepo().get(uuid.UUID(str(workspace_id)), uuid.UUID(str(node_id)))

    async def semantic(self, workspace_id, view: StatementView, cfg: Settings) -> list:
        """Nearest statements by meaning, queried with the node's canonical document, the same text its
        own surfaces are embedded from, so like is compared with like."""
        from opencortyx.core.substrate import document as node_document
        from opencortyx.core.inference.embedding import embed
        repo = NodeRepo()
        node = await repo.get(uuid.UUID(str(workspace_id)), uuid.UUID(view.id))
        text = node_document.from_row(node.properties, node.label, node.type) if node else view.text
        try:
            vec = await embed(text or view.text)
        except Exception:                                  # noqa: BLE001 — no embedder → other sources
            return []
        if not vec:
            return []
        types = list(cfg.types) or ([view.type] if view.type else None)
        rows = await repo.vector_search(workspace_id=uuid.UUID(str(workspace_id)), query_vec=vec, types=types,
                                        k=max(2, cfg.candidates * 2))
        return [(n, s) for n, s in (rows or []) if str(n.id) != view.id and float(s or 0.0) >= cfg.similarity]

    async def linked(self, workspace_id, view: StatementView, cfg: Settings) -> list:
        """Statements hanging off the same entity as this one — structural, so it needs no field
        names and no type literals. The holder is whatever the statement is attached to.

        HUBS ARE NOT SUBJECTS. Every node also hangs off shared scaffolding (a Date, a collection),
        so two defences keep "attached to the same thing" meaningful, neither needing a type literal:

        1. Platform scaffolding (`__source: "system"`, such as Date nodes) is never a subject.
        2. A holder connecting more than `hub_degree` statements describes a category, not a subject.
        """
        repo, out = NodeRepo(), []
        org = uuid.UUID(str(workspace_id))
        types = set(cfg.types) or ({view.type} if view.type else set())
        holders = await repo.neighbors(org, [uuid.UUID(view.id)], direction="both", limit=max(4, cfg.candidates))
        holder_ids = [n.id for _e, n in holders
                      if str((n.properties or {}).get("__source") or "") != "system"
                      and int(n.degree or 0) <= cfg.hub_degree][: cfg.candidates]
        if holder_ids:
            for _e, n in await repo.neighbors(org, holder_ids, direction="both", limit=max(8, cfg.candidates * 4)):
                if str(n.id) != view.id and (not types or n.type in types):
                    out.append(n)
        return out[: cfg.candidates]

    async def by_key(self, workspace_id, view: StatementView, cfg: Settings) -> list:
        repo, out = NodeRepo(), []
        for f, v in (view.keys or {}).items():
            found = await repo.find(uuid.UUID(str(workspace_id)), exact_types=[view.type] if view.type else None,
                                     props={f: str(v)})
            out.extend(n for n in found if str(n.id) != view.id)
        return out[: cfg.candidates * max(1, len(view.keys or {}))]

    async def recent(self, workspace_id, cfg: Settings, limit: int, since=None) -> list:
        rows = await NodeRepo().find(uuid.UUID(str(workspace_id)), exact_types=list(cfg.types) or None,
                                     props_not={MARK_SUPERSEDED: "true"}, created_after=since,
                                     order_by="-created_at", limit=limit)
        return [str(n.id) for n in rows]

    async def edge_exists(self, workspace_id, a_id, b_id, type: str) -> bool:
        found = await NodeRepo().edges(uuid.UUID(str(workspace_id)), source_ids=[uuid.UUID(str(a_id))],
                                       target_ids=[uuid.UUID(str(b_id))], types=[type], limit=1)
        return bool(found)

    async def evolved_from(self, workspace_id, node_id):
        """The statement this one replaced, or None at the end of the chain."""
        found = await NodeRepo().edges(uuid.UUID(str(workspace_id)), source_ids=[uuid.UUID(str(node_id))],
                                       types=[E_EVOLVED_FROM], limit=1)
        return str(found[0].target_id) if found else None

    async def write_edge(self, workspace_id, src, dst, type: str, properties: dict) -> None:
        from opencortyx.knowledge import authoring
        await authoring.create_edge(workspace_id, source_id=uuid.UUID(str(src)),
                                    target_id=uuid.UUID(str(dst)), type=type, properties=properties)

    async def patch_props(self, workspace_id, node_id, props: dict) -> None:
        """Merge properties on one node. A merge, not a replace: the statement's own content must
        survive being marked."""
        repo = NodeRepo()
        node = await repo.get(uuid.UUID(str(workspace_id)), uuid.UUID(str(node_id)))
        if node is None:
            return
        node.properties = {**(node.properties or {}), **props}
        if "__confidence" in props:
            node.confidence = float(props["__confidence"])
        await repo.save(node)

    async def llm_json(self, workspace_id, prompt: str, system: str, cfg: Settings) -> dict:
        """The judge, through the MODEL REGISTRY — the `utility` role, with a per-workspace override.
        Nothing here pins a model: swapping it in Settings → Models swaps it here."""
        import json as _json
        from opencortyx.core.inference.llm import complete
        from opencortyx.core.inference.registry import resolve_model
        spec = await resolve_model(workspace_id, "utility", cfg.judge_model or None)
        raw = await complete(prompt, system=system, max_tokens=160, json_mode=True,
                             temperature=0.0, model_spec=spec)
        try:
            return _json.loads(raw)
        except Exception:                                  # noqa: BLE001 — salvage the first object
            m = re.search(r"\{.*\}", str(raw or ""), re.S)
            return _json.loads(m.group(0)) if m else {}

    async def nli(self, a_text: str, b_text: str) -> Optional[float]:
        """Contradiction probability from a cross-encoder NLI model, or None when unavailable.

        Same posture as the reranker's cross-encoder: if the library or the model isn't there, this
        tier simply doesn't exist and the cascade carries on. Configure with CONTRADICTION_NLI_MODEL
        (e.g. cross-encoder/nli-deberta-v3-base); unset means off."""
        mid = settings.CONTRADICTION_NLI_MODEL.strip()
        if not mid:
            return None
        model = _nli_model(mid)
        if model is None:
            return None
        try:
            import numpy as _np
            scores = model.predict([(a_text[:512], b_text[:512]), (b_text[:512], a_text[:512])])
            arr = _np.asarray(scores, dtype=float).reshape(2, -1)
            if arr.shape[1] < 3:                           # not a 3-way NLI head — don't guess
                return None
            exp = _np.exp(arr - arr.max(axis=1, keepdims=True))
            probs = exp / exp.sum(axis=1, keepdims=True)
            return float(max(probs[0][0], probs[1][0]))    # label 0 = contradiction
        except Exception:                                  # noqa: BLE001
            return None


_nli_cache: dict = {}


def _nli_model(model_id: str):
    if model_id in _nli_cache:
        return _nli_cache[model_id]
    try:
        from sentence_transformers import CrossEncoder
        _nli_cache[model_id] = CrossEncoder(model_id)
    except Exception:                                      # noqa: BLE001 — absent library or model
        _nli_cache[model_id] = None
    return _nli_cache[model_id]


# ══ THE CAPABILITY — one card in Settings → Capabilities ═════════════════════════════════════════
class ContradictionDetection(Capability):
    name = "contradiction"
    title = "Contradiction Detection & Resolution"
    group = "Knowledge Accuracy"
    description = ("When a new statement disagrees with one you already have, detect it and decide "
                   "what happens: a change over time keeps both (the older one stops being current), "
                   "a flat contradiction retires the loser, and anything uncertain waits for you in "
                   "the review queue. Works on any schema — statements are matched by meaning and by "
                   "what they are attached to, not by a field name.")
    ui_route = "/dashboard/settings?tab=capabilities"
    enable_key = "memory_resolution"

    def config_schema(self):
        """The card, laid out as the pipeline runs.

        Order is the argument: a reader going down this card walks the same path a statement does —
        is it checked at all, how is a disagreement decided, what counts as the same thing, where does
        your data keep it, what happens to the loser, and what happens when they agree instead. The
        sections are declared here rather than in the web app, so the card teaches the engine and the
        UI needs no knowledge of this capability.
        """
        S1 = "Detection"
        S2 = "How a disagreement is decided"
        S3 = "What counts as the same thing"
        S4 = "Where your data keeps things"
        S5 = "What happens to the one that loses"
        S6 = "When two statements agree"
        return [
            ConfigField("memory_resolution", "Detect contradictions", "bool", default=True,
                        section=S1,
                        section_help="Check each new statement against the ones it most resembles and "
                                     "the ones attached to the same thing.",
                        help="Off = nothing is compared, and nothing is ever retired."),

            ConfigField("contradiction_use_rules", "Check values by rule first", "bool", default=True,
                        section=S2,
                        section_help="Three tiers, cheapest and most certain first. Each one may "
                                     "decline, and the next takes over.",
                        help="Compare structured values — numbers, units, money, dates, yes/no, "
                             "codes like eu-west-1 — by arithmetic before any model is asked. "
                             "'90 days' vs '120 days' is then certain, and free. Rules never guess: "
                             "anything they cannot settle falls through to the judge below."),
            ConfigField("contradiction_use_llm", "Use the LLM judge", "bool", default=True,
                        section=S2,
                        help="On: pairs the cheap tiers cannot settle go to the model from your Model "
                             "Registry (the 'utility' role). Off: only rules, duplicate detection and "
                             "the entailment model run — fully deterministic, cheaper, and still "
                             "catches repeats and every value conflict rules can see."),
            ConfigField("contradiction_judge_model", "Judge model (optional)", "text", default="",
                        section=S2,
                        help="A model name or id from Settings → Models to use instead of the "
                             "workspace's default utility model. Blank = the default."),

            ConfigField("contradiction_similarity", "How alike counts as the same thing", "float",
                        default=0.82, min=0.5, max=0.99, section=S3,
                        section_help="Two statements are only compared if there is reason to think "
                                     "they are about the same subject.",
                        help="Minimum similarity for a statement to be considered a candidate. "
                             "Higher = fewer, closer comparisons."),
            ConfigField("contradiction_candidates", "Comparisons per statement", "int", default=8,
                        min=1, max=50, section=S3,
                        help="How many neighbours each new statement is checked against. This is the "
                             "main cost control."),
            ConfigField("contradiction_hub_degree", "When a link is too busy to mean anything", "int",
                        default=25, min=2, max=1000, section=S3,
                        help="Statements attached to the same thing get compared. But some things "
                             "everything is attached to — a date, a category — and those say nothing "
                             "about two statements being about the same subject. A connector busier "
                             "than this is treated as a category and ignored."),
            ConfigField("contradiction_key_fields", "Exact-match keys (optional)", "json", default=[],
                        section=S3,
                        help="Property names whose equal values mean 'the same subject' — e.g. "
                             "customer_id. Adds precision; not required for detection to work."),
            ConfigField("contradiction_types", "Limit to these node types (optional)", "json",
                        default=[], section=S3,
                        help="Blank = compare a statement against others of its own type. List types "
                             "to widen or pin the comparison."),

            ConfigField("contradiction_text_fields", "Where the statement text lives", "json",
                        default=_DEFAULTS["contradiction_text_fields"], section=S4,
                        section_help="This engine reads YOUR field names. Nothing here is fixed, so "
                                     "detection works on a schema it has never seen.",
                        help="Property names to read a statement from, in order. The first one "
                             "present wins; the node's label is the last resort."),
            ConfigField("contradiction_time_fields", "Where the date lives", "json",
                        default=_DEFAULTS["contradiction_time_fields"], section=S4,
                        help="Property names that carry when a statement became true. None present is "
                             "fine: the date is treated as unknown, and a dated statement is never "
                             "beaten by an undated one on recency."),
            ConfigField("contradiction_reason_fields", "Where a reason lives", "json",
                        default=_DEFAULTS["contradiction_reason_fields"], section=S4,
                        help="Property names that explain WHY something changed. A stated reason makes "
                             "a disagreement an evolution, so both statements are kept."),

            ConfigField("resolution_mode", "Who wins", "select", default="latest_wins",
                        options=["latest_wins", "trust_weighted"], section=S5,
                        section_help="Nothing is ever deleted. A statement that stops being current "
                                     "keeps its place in history, so 'what did we believe in March?' "
                                     "still has an answer.",
                        help="'latest_wins' prefers the newer statement; 'trust_weighted' prefers the "
                             "human-validated or higher-confidence one. A dated statement always beats "
                             "an undated one."),
            ConfigField("resolution_trust_boost", "Trust boost (validated sources)", "float",
                        default=0.5, min=0.0, max=2.0, section=S5,
                        help="Extra weight for a human-validated or user-entered statement when "
                             "'trust_weighted' picks the winner."),
            ConfigField("contradiction_auto_apply", "Confidence needed to apply", "float",
                        default=0.75, min=0.0, max=1.0, section=S5,
                        help="Below this, a contradiction is proposed for review instead of applied. "
                             "Anything the rules settled is certain and always clears this."),
            ConfigField("resolution_review", "Review before applying", "bool", default=False,
                        section=S5,
                        help="On: nothing is retired until you approve it in the review queue. "
                             "Evolutions keep both statements either way."),

            ConfigField("contradiction_support", "Record agreement too", "bool", default=True,
                        section=S6,
                        section_help="Two statements agreeing is evidence the fact is true — and the "
                                     "comparison has already been paid for.",
                        help="Record it and raise their confidence a little. Repeats of the SAME "
                             "source do not count: only independent sources corroborate, or "
                             "re-reading one document would inflate every fact in it."),
            ConfigField("contradiction_support_step", "How much agreement adds", "float", default=0.1,
                        min=0.0, max=0.5, section=S6,
                        help="Each independent corroboration closes this fraction of the remaining gap "
                             "to the cap, so confidence rises quickly at first and then barely — "
                             "repetition can never manufacture certainty."),
            ConfigField("contradiction_support_cap", "Most that agreement can reach", "float",
                        default=0.95, min=0.5, max=1.0, section=S6,
                        help="Corroboration alone never takes a statement above this, so a "
                             "human-validated fact always outranks a much-repeated one."),
        ]

    # No ingest hook of its own: an asset's statements are checked by `resolution.resolve_asset`,
    # after its extractors and embeddings, so every statement is compared with the vectors it needs.


async def _statements_of_source(workspace_id, source_ref, cfg: Settings, max_depth: int = 3) -> list:
    """The nodes this source produced: its chunks/records and what was derived from them."""
    derived = await NodeRepo().descendants(uuid.UUID(str(workspace_id)), [uuid.UUID(str(source_ref))],
                                           max_depth=max_depth)
    return [str(n.id) for n in derived]
