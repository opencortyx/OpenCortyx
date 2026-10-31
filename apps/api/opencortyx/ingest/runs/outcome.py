"""(B) What an ingestion actually produced — the answer to "did that work?".

A run today reports a percentage, a list of steps and a `result` dict whose keys differ per path:
the document flow records `source_id` and `chunks`, the semi-structured flow whatever scalars its
walker happened to return, the tabular flow another set again. That is fine as a worker's own
bookkeeping and useless as an answer to a person, who asked one question — *what is in my system
now that was not there ten seconds ago?*

This module answers it as a READ-SIDE PROJECTION. Nothing about how an ingest reports is changed,
no path has to agree on a schema, and no migration is involved: the projection reads the run row
and then asks the graph what the run left behind. Adding a fourth ingest paradigm tomorrow gets a
correct outcome for free, because the graph is the thing every path writes to.

It is also the honest place to put the next step. A user who has just watched a file land is
exactly the person able to say "no, `body` isn't a label, it's the text" — and exactly the person
who has no idea the Mapper exists. `next_steps` names the one or two things worth doing about THIS
result, with the id needed to do them, so refinement is offered on a concrete outcome instead of
waiting behind a menu item.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from opencortyx.core.observability import get_logger

log = get_logger(__name__)

#: Types that are the plumbing of an ingest rather than a finding. A user who imported contracts
#: wants to hear "42 Vendors", not "1 Source, 128 Chunks" — the chunks are how it works, not what
#: it found. Counted and reported separately rather than hidden, because "128 chunks and nothing
#: else" is itself the answer when an extraction produced nothing.
STRUCTURAL_TYPES = ("Source", "Chunk", "Document", "Table", "Attribute", "Date")

#: A run's nodes are identified by `source_ref` where the path recorded one. Where it did not,
#: they are found by creation time instead, widened by this much to cover the clock skew between
#: the API that stamped `started_at` and the worker that wrote the nodes.
CLOCK_SKEW = timedelta(seconds=30)

#: The edge an extracted node draws to the text it was read from (see ingest.conformance).
LINEAGE_EDGE = "EXTRACTED_FROM"

MAX_TYPES = 12
MAX_EXAMPLES = 5
#: Nodes counted per run, and how many provenance levels below the asset are followed.
NODE_CAP = 5_000
MAX_DEPTH = 6


@dataclass
class TypeCount:
    type: str
    count: int
    examples: list[str] = field(default_factory=list)


@dataclass
class NextStep:
    """One thing worth doing about this result, and everything needed to do it."""

    action: str                  # stable token the UI routes on: refine_mapping | ask | …
    label: str                   # the button
    reason: str                  # why it is being offered, in terms of what just happened
    href: str = ""               # where it goes, when it is a link
    payload: dict = field(default_factory=dict)


@dataclass
class IngestOutcome:
    run_id: str
    status: str
    asset: str
    kind: str
    found: list[TypeCount] = field(default_factory=list)
    structural: list[TypeCount] = field(default_factory=list)
    total_nodes: int = 0
    duration_seconds: float | None = None
    error: str = ""
    next_steps: list[NextStep] = field(default_factory=list)
    #: What the worker itself recorded. Kept verbatim beside the projection so a support question
    #: can still be answered from the run row without re-running anything.
    reported: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: The run is done but work it queued is not: a document's entity/claim extraction runs on its
    #: own queue after the chunks land. Callers keep polling while this is true.
    pending: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


async def ingest_outcome(workspace_id: uuid.UUID, run_id) -> IngestOutcome | None:
    """Project one run into what it produced. None when the run is not this org's.

    Read-only and safe to poll: the UI calls this while a run is still going, so a run that is
    `running` returns what exists SO FAR rather than refusing to answer.
    """
    from opencortyx.domain.control import IngestRun
    from opencortyx.core.db import V2Session

    async with V2Session() as db:
        run = await db.get(IngestRun, _as_uuid(run_id))
        if run is None or run.workspace_id != workspace_id:
            return None
        snapshot = {"id": str(run.id), "status": run.status, "asset": run.asset or "",
                    "kind": run.kind or "", "source_ref": run.source_ref,
                    "result": dict(run.result or {}), "error": run.error or "",
                    "started_at": run.started_at, "created_at": run.created_at,
                    "finished_at": run.finished_at}

    outcome = IngestOutcome(
        run_id=snapshot["id"], status=snapshot["status"], asset=snapshot["asset"],
        kind=snapshot["kind"], error=snapshot["error"], reported=snapshot["result"],
        duration_seconds=_duration(snapshot["started_at"], snapshot["finished_at"]))

    found, structural, notes = await _counts(workspace_id, snapshot)
    outcome.found = found
    outcome.structural = structural
    outcome.notes = notes
    outcome.total_nodes = sum(t.count for t in found) + sum(t.count for t in structural)
    reported = snapshot["result"] or {}
    if outcome.status == "completed" and reported.get("rows") == 0 and "rows_available" in reported:
        outcome.notes.append("the table has columns but no rows — its columns were recorded, and "
                             "rows appear when a version with data is uploaded")
    dup = int((snapshot["result"] or {}).get("duplicate_ids") or 0)
    if dup:
        seen = ", ".join((snapshot["result"] or {}).get("duplicate_id_examples") or [])
        outcome.notes.append(
            f"{dup} row(s) repeated an identity value an earlier row already had"
            + (f" ({seen})" if seen else "") + " and were merged into it — if they are different "
            "records, choose another identity column in the Mapper")
    from opencortyx.tasks.maintenance import knowledge_pending
    deferred = (snapshot["result"] or {}).get("extraction") == "deferred"
    reading = outcome.status == "completed" and knowledge_pending(workspace_id, snapshot["id"],
                                                                  snapshot.get("source_ref"))
    outcome.pending = outcome.status == "completed" and (deferred or reading)
    if reading:
        outcome.notes.append("its records go into the graph once detection has read the file, "
                             "usually within a few minutes — they appear here as they land")
    elif outcome.pending:
        outcome.notes.append("entities and claims are still being extracted in the background — "
                             "they appear here as they land")
    elif (snapshot["result"] or {}).get("extraction") == "failed":
        outcome.notes.append("extraction did not finish: "
                             + str((snapshot["result"] or {}).get("extraction_error") or "unknown error"))
    outcome.next_steps = _next_steps(outcome, snapshot)
    return outcome


async def _counts(workspace_id, snapshot: dict) -> tuple[list[TypeCount], list[TypeCount], list[str]]:
    """Ask the graph what this run left behind, split into findings and plumbing."""
    from opencortyx.stores.graph import get_graph_store
    from opencortyx.stores.graph.models import NodeQuery

    source_ref = snapshot.get("source_ref")
    since = snapshot.get("started_at") or snapshot.get("created_at")
    notes: list[str] = []

    if source_ref and _as_uuid_or_none(source_ref) is None:
        # A SQL run records its SOURCE KEY ("sqlite:qa_shop.sqlite"), not a node id — the same two
        # forms `progress.purge_asset` reads. Treated as an id it matched nothing, and every
        # database ingest reported that it had produced nothing.
        query = NodeQuery(source_key=str(source_ref), limit=NODE_CAP)
    elif source_ref:
        query = NodeQuery(source_refs=[source_ref], limit=NODE_CAP)
    elif snapshot.get("status") == "failed":
        # A failed run that never named its asset has nothing attributable: counting by time would
        # report every other run's writes as this one's (a malformed file "found" 4 records).
        return [], [], ["this run failed before it wrote anything it could name"]
    elif since:
        # No source_ref: the path never called `set_source`. Creation time is the only handle
        # left, and it over-counts when two ingests overlap — said out loud rather than presented
        # as an exact figure.
        query = NodeQuery(created_after=since - CLOCK_SKEW, limit=NODE_CAP)
        notes.append("this run did not record an asset id, so these counts are everything "
                     "written since it started and may include another run's work")
    else:
        return [], [], ["this run recorded neither an asset id nor a start time, so what it "
                        "produced cannot be attributed"]

    try:
        store = get_graph_store()
        nodes = list(await store.find_nodes(workspace_id, query))
        if source_ref:
            # Below the direct children too: provenance is a tree. A JSON record's carrier and line
            # items point at the RECORD, and a document's entities at their chunk, so counting only
            # what names the asset reported a nested file as its top-level rows alone.
            seen = {str(source_ref)} | {str(getattr(n, "id", "")) for n in nodes}
            frontier = [str(n.id) for n in nodes if getattr(n, "id", None)]
            for _ in range(MAX_DEPTH):
                if not frontier or len(nodes) >= NODE_CAP:
                    break
                below = await store.find_nodes(workspace_id, NodeQuery(source_refs=frontier,
                                                                 limit=NODE_CAP - len(nodes)))
                frontier = []
                for n in below:
                    if str(n.id) not in seen:
                        seen.add(str(n.id))
                        frontier.append(str(n.id))
                        nodes.append(n)
            # And across extraction lineage. A resolved entity is SHARED — one node for every
            # document that names it — so it has no provenance parent to walk down to; it points at
            # the chunks it was read from with EXTRACTED_FROM instead. Without this a document's
            # outcome listed its claims and never the entities they are about.
            from opencortyx.stores.graph.models import EdgeQuery
            # And the records published from it (Extract knowledge): each points at the files it
            # was read from (record -FROM_ASSET-> asset) and sits in no file's tree
            if len(nodes) < NODE_CAP:
                published = await store.find_edges(workspace_id, EdgeQuery(
                    target_ids=[source_ref], types=["FROM_ASSET"], limit=NODE_CAP - len(nodes)))
                fresh = list(dict.fromkeys(str(e.source_id) for e in published if str(e.source_id) not in seen))
                for n in (await store.get_nodes(workspace_id, fresh) if fresh else []):
                    seen.add(str(n.id))
                    nodes.append(n)
            anchors = [n.id for n in nodes if getattr(n, "id", None)]
            if anchors and len(nodes) < NODE_CAP:
                lineage = await store.find_edges(workspace_id, EdgeQuery(
                    target_ids=anchors, types=[LINEAGE_EDGE], limit=NODE_CAP))
                extra = [e.source_id for e in lineage if str(e.source_id) not in seen]
                if extra:
                    for n in await store.get_nodes(workspace_id, list(dict.fromkeys(extra))[:NODE_CAP - len(nodes)]):
                        if str(n.id) not in seen:
                            seen.add(str(n.id))
                            nodes.append(n)
    except Exception as exc:                        # noqa: BLE001 — a run is still a run
        log.warning("outcome.graph_unreachable", run=snapshot["id"],
                    error=f"{type(exc).__name__}: {exc}")
        return [], [], ["the graph could not be read just now, so only what the worker reported "
                        "is shown"]

    counted: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    for node in nodes:
        node_type = _display_type(node)
        counted[node_type] = counted.get(node_type, 0) + 1
        labels = examples.setdefault(node_type, [])
        if node.label and len(labels) < MAX_EXAMPLES:
            labels.append(str(node.label)[:80])

    findings: list[TypeCount] = []
    plumbing: list[TypeCount] = []
    for node_type, count in sorted(counted.items(), key=lambda kv: -kv[1]):
        row = TypeCount(type=node_type, count=count, examples=examples[node_type])
        (plumbing if node_type in STRUCTURAL_TYPES else findings).append(row)

    if len(nodes) >= NODE_CAP:
        notes.append("counts are capped at the first 5,000 nodes of this run")
    return findings[:MAX_TYPES], plumbing[:MAX_TYPES], notes


def _next_steps(outcome: IngestOutcome, snapshot: dict) -> list[NextStep]:
    """The one or two things worth doing about THIS result.

    Ordered by what the outcome says is wrong, not by a fixed menu — the point is that a user is
    offered refinement at the moment they can judge whether it is needed, which is the moment they
    are looking at what came out.
    """
    steps: list[NextStep] = []
    source_ref = snapshot.get("source_ref")

    if outcome.status == "failed":
        return [NextStep(action="retry", label="Try again",
                         reason=outcome.error or "the run did not finish",
                         payload={"run_id": outcome.run_id})]

    if outcome.status not in ("completed", "duplicate"):
        return []
    if outcome.pending:
        # Nothing to judge yet: "tell it what to read" would be advice about a file still being read.
        return ([NextStep(action="open_asset", label="See it in the catalogue",
                          reason="its text is already searchable while extraction finishes",
                          href=f"/dashboard/catalogue/{source_ref}")] if source_ref else [])

    if outcome.status == "duplicate":
        return [NextStep(action="open_asset", label="Open what is already there",
                         reason="this exact file had already been ingested, so nothing changed",
                         href=f"/dashboard/catalogue/{source_ref}" if source_ref else
                              "/dashboard/catalogue"),
                NextStep(action="replace", label="Replace — read it again",
                         reason="re-reads this file and rebuilds what came from it, even though an "
                                "identical copy is already in — use it when that copy's knowledge "
                                "is missing or incomplete",
                         payload={"run_id": outcome.run_id})]

    if (snapshot.get("result") or {}).get("rows") == 0 and "rows_available" in (snapshot.get("result") or {}):
        pass                                        # an empty table: nothing to refine, said in notes
    elif not outcome.found and outcome.structural:
        steps.append(NextStep(
            action="refine_mapping", label="Tell it what to read",
            reason=f"{sum(t.count for t in outcome.structural)} node(s) were created but nothing "
                   f"was recognised as an entity — usually a field being kept as a label when it "
                   f"is really the text",
            href="/dashboard/mapper",
            payload={"run_id": outcome.run_id, "source_ref": str(source_ref or "")}))
    elif not outcome.found and not outcome.structural:
        steps.append(NextStep(
            action="refine_mapping", label="Check how this file is read",
            reason="the run completed but produced nothing",
            href="/dashboard/mapper",
            payload={"run_id": outcome.run_id, "source_ref": str(source_ref or "")}))
    else:
        top = outcome.found[0]
        steps.append(NextStep(
            action="ask", label="Ask a question about this",
            reason=f"{top.count} {top.type}(s) are in the graph and can be asked about now",
            href="/dashboard/chat",
            payload={"suggested": f"What do we know about {top.examples[0]}?"
                                  if top.examples else f"List every {top.type}."}))
        steps.append(NextStep(
            action="extract_more", label="Pull out something else",
            reason="describe anything else worth recording from this content and it will be "
                   "drafted for you",
            href="/dashboard/custom-assets",
            payload={"source_ref": str(source_ref or "")}))

    if source_ref:
        steps.append(NextStep(action="open_asset", label="See it in the catalogue",
                              reason="the full contents of what was ingested",
                              href=f"/dashboard/catalogue/{source_ref}"))
    return steps


def _duration(started: datetime | None, finished: datetime | None) -> float | None:
    if not (started and finished):
        return None
    return round((finished - started).total_seconds(), 1)


def _as_uuid(value) -> uuid.UUID:
    """Raises ValueError on a malformed id, so the caller can answer 422 rather than 404.

    They are different answers: 404 says "no such run", which sends someone looking for a run
    that never existed under a string that could not name one in the first place.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{value!r} is not a run id") from exc


__all__ = ["CLOCK_SKEW", "STRUCTURAL_TYPES", "IngestOutcome", "NextStep", "TypeCount",
           "ingest_outcome"]


def _as_uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _display_type(node) -> str:
    """The type a person would call this node. A reader that could not name a record's type stores
    it as the generic `KeyNode` and keeps what it IS under `key` ("Product") — reporting "3
    KeyNodes" for a products file told the user nothing they could recognise."""
    from opencortyx.ingest.structured.ingest import KEYNODE
    key = (getattr(node, "properties", None) or {}).get("key")
    if node.type == KEYNODE and isinstance(key, str) and key.strip():
        return key.strip()
    return node.type or "Unknown"
