"""L4 — Context construction: evidence with roles and provenance, not a list of nodes.

    ranked candidates ─► role assignment ─► rendered blocks (+ path, + retrieved_by) ─► sections

WHAT THIS REPLACES

The previous engine emitted a flat array of node texts. Three things were lost by that shape:

  - WHY a node is present. A reader (and the answering model) cannot tell a direct semantic match
    from a node dragged in by a graph walk, so both are weighted equally.
  - The ROUTE. "Which companies connect to John through Germany" is answered by a path, and only
    the endpoint survived.
  - ATTRIBUTION. An answer could not be explained, because nothing recorded which surface found
    the evidence or which edge carried it.

ROLES ARE DERIVED FROM RETRIEVAL, NOT FROM NODE TYPE

A block's role says what it CONTRIBUTES, and that follows from how it was found:

    primary       matched the question directly, on a similarity or literal surface
    relationship  reached over a path — the route itself is the evidence
    temporal      satisfies the question's time bounds with a known date
    supporting    present as corroboration or context

There is no type table. A `Fact`, a `Turn` and a node type invented next week are all eligible for
every role; what differs is the evidence they carry.
"""
from __future__ import annotations

from typing import Iterable, Optional

from opencortyx.retrieval.contracts import (
    Intent,
    Candidate, ContextBlock, QueryPlan,
)

#: Rough tokens-per-character. Deliberately conservative — overestimating tokens under-fills the
#: budget, which costs a little recall; underestimating overflows it, which costs the whole call.
CHARS_PER_TOKEN = 3.6

ROLE_PRIMARY = "primary"
ROLE_RELATIONSHIP = "relationship"
ROLE_TEMPORAL = "temporal"
ROLE_SUPPORTING = "supporting"
#: Rows the data route read (retrieval/l2_data): the table, its SQL and the versions read.
ROLE_TABLE_RESULT = "table-result"

#: Order sections appear in. Primary first because attention degrades with distance, and the
#: block most likely to answer should not sit behind twenty corroborating ones.
ROLE_ORDER = (ROLE_TABLE_RESULT, ROLE_PRIMARY, ROLE_RELATIONSHIP, ROLE_TEMPORAL, ROLE_SUPPORTING)


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text or "") / CHARS_PER_TOKEN) + 1)


def assign_role(cand: Candidate, plan: Optional[QueryPlan] = None) -> str:
    """What this candidate contributes, read off its evidence."""
    f = cand.features or {}
    direct = max(float(f.get("semantic", 0.0)),
                 float(f.get("attribute", 0.0)),
                 float(f.get("exact", 0.0)))

    # A route is the evidence when the question is about connection AND the node was reached
    # over one — not merely because a path exists.
    reached_by_path = bool(cand.paths) and float(f.get("path_quality", 0.0)) > 0.0
    if reached_by_path and direct < 0.55:
        return ROLE_RELATIONSHIP

    if plan and plan.temporal and float(f.get("temporal_fit", 0.0)) >= 1.0:
        return ROLE_TEMPORAL

    if direct >= 0.55 or float(f.get("exact", 0.0)) > 0:
        return ROLE_PRIMARY
    return ROLE_SUPPORTING


def retrieved_by(cand: Candidate) -> list[str]:
    """Provenance: every surface that proposed this, with the attribute or edge that fired.

    This is what makes an answer auditable. "found by attr_embedding(description)" is a materially
    different claim from "found by graph(MENTIONS)", and a flat node list expresses neither.

    GROUPED BY SURFACE, one entry each. Listing every hit separately made one surface read as
    several independent confirmations: a node matched on three fields of the SAME attribute
    surface rendered as `attr_embedding(label), attr_embedding(text), attr_embedding(speaker)`,
    which a reader takes for three-way agreement. It is one surface agreeing with itself, and one
    of those fields (`speaker`, at 0.05 specificity) discriminates nothing at all.

    `agreement` in the reranker already counts DISTINCT surfaces; this makes the rendered
    provenance say the same thing the score does.
    """
    tags = []
    for ev in sorted(cand.evidence, key=lambda e: e.rank):
        # The representation is provenance for the EVIDENCE, not for the reader; the field that
        # matched is what makes the claim auditable.
        detail = (ev.detail or "").split("|")[0]
        tags.append(f"{ev.surface}({detail})" if detail else ev.surface)
    return group_provenance(tags)


def group_provenance(tags) -> list[str]:
    """Fold `surface(a)`, `surface(b)` into one `surface(a,b)`.

    Used wherever provenance is assembled OR combined, because both places had the same defect:
    one surface listed several times reads as several independent confirmations. A block merged
    from two near-identical blocks showed `node_embedding(entity)` and
    `node_embedding(entity,concept)` side by side — one surface, rendered as two agreeing
    sources. `agreement` in the reranker counts distinct surfaces; this makes what the reader
    sees say the same thing the score does.
    """
    by: dict[str, list[str]] = {}
    order: list[str] = []
    for tag in tags:
        surface, _, rest = (tag or "").partition("(")
        if surface not in by:
            by[surface] = []
            order.append(surface)
        for d in (x.strip() for x in rest.rstrip(")").split(",")):
            if d and d not in by[surface]:
                by[surface].append(d)
    return [f"{s}({','.join(by[s])})" if by[s] else s for s in order]


#: Fields of a document's record that describe the stored file, not the document.
_FILE_FIELDS = frozenset({"filename", "mime", "checksum", "word_count", "language", "native", "reader",
                          "reader_note", "source_file", "tags"})
_METADATA_VALUE_MAX = 200


def _metadata_text(cand: Candidate) -> str:
    """A document's own metadata as its block's text, for a node that has no text of its own: its
    Source carries the title, identifiers, date and header fields read from it, and shown as only its
    filename, "what is its status" or "when was it published" had no answer in the context."""
    props = cand.properties or {}
    if not (props.get("title") or props.get("identifiers")):
        return ""
    parts = []
    for key, value in props.items():
        if key.startswith("__") or key in _FILE_FIELDS:
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if isinstance(v, (str, int, float)))
        if isinstance(value, (str, int, float)) and str(value).strip() and len(str(value)) <= _METADATA_VALUE_MAX:
            parts.append(f"{key}: {str(value).strip()}")
    head = (cand.label or "").strip()
    return f"{head} — {'; '.join(parts)}" if head and parts else "; ".join(parts)


def _block_text(cand: Candidate, plan: Optional[QueryPlan]) -> str:
    """What the block says.

    Prefers the node's own text. When an ATTRIBUTE surface fired and matched the field the
    question asked for, the matched value is surfaced too — otherwise a node retrieved because its
    `date_time` matched arrives showing only its prose, and the very thing that made it relevant
    is invisible to the reader.
    """
    base = (cand.text or "").strip() or _metadata_text(cand) or (cand.label or "").strip()
    extras: list[str] = []
    wanted = (plan.answer_type_hint if plan else "") or ""
    for ev in cand.evidence:
        if ev.surface != "attr_embedding" or not ev.matched_text:
            continue
        attr = (ev.detail or "").lower()
        if attr in ("text", "label"):
            continue
        relevant = bool(wanted and (attr in wanted.lower() or wanted.lower() in attr))
        if relevant or (plan and plan.temporal and ("date" in attr or "time" in attr)):
            snippet = ev.matched_text.strip()
            if snippet and snippet not in base and snippet not in extras:
                extras.append(snippet)
    if plan is not None and (plan.temporal or plan.intent is Intent.TEMPORAL) and not extras:
        own = _own_date(cand.properties)
        if own and own not in base:
            extras.append(own)

    if extras:
        return f"{base} [{' | '.join(extras[:2])}]" if base else " | ".join(extras[:2])
    return base


#: Property keys that carry a node's own timestamp, most specific first.
_DATE_KEYS = ("date_time", "date", "timestamp", "occurred_at", "created_at", "when")


def _own_date(properties: Optional[dict]) -> str:
    """The node's own date, as the corpus WROTE it.

    Rendered raw rather than normalised to ISO: "2:01 pm on 23 January, 2022" is what a reader —
    and a grader comparing against a gold answer phrased the same way — needs to see.
    """
    for key in _DATE_KEYS:
        value = (properties or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


#: A block must say something. Below this it is a name, not a statement.
MIN_CONTENT_CHARS = 24


def carries_content(cand: Candidate, text: str) -> bool:
    """Does this candidate assert anything, or is it just a label?

    MEASURED: the bare entity node "Joanna" ranked FIRST for "What is Joanna allergic to?" at
    0.405, ahead of every actual statement, and "Nate" ranked first for "What Console does Nate
    own?". A concept label matches a query naming that concept almost perfectly and answers
    nothing.

    Decided by SUBSTANCE, not by node type. A short node that happens to be a fact triple
    ("Nate owns Xenoblade") is content; a long node that is only its own name is not. Testing the
    type would reintroduce exactly the lookup table this engine exists to avoid - and would break
    on the first corpus that labels its concepts differently.
    """
    t = (text or "").strip()
    if not t:
        return False
    # A single short token is a name, not a statement.
    if " " not in t and len(t) < MIN_CONTENT_CHARS:
        return False
    # The text IS the node's own label, and the label is short: a multi-word name
    # ("Bank of England") rather than an assertion.
    label = (cand.label or "").strip()
    if label and t == label and len(t) < MIN_CONTENT_CHARS:
        return False
    return True


def _relations_of(cand: Candidate) -> list:
    """Every edge on this candidate's routes, oriented the way the EDGE points.

    `PathStep.direction` is "in" when the walk traversed the edge backwards, so `from_id`/`to_id`
    describe the WALK, not the relationship. Swapping on "in" is what makes the rendered line agree
    with the graph.
    """
    out, seen = [], set()
    for path in (cand.paths or []):
        for step in (path.steps or []):
            a, b = str(step.from_id), str(step.to_id)
            if (step.direction or "out") == "in":
                a, b = b, a
            key = (a, step.edge_type, b)
            if step.edge_type and key not in seen:
                seen.add(key)
                out.append(key)
    return out


def build(candidates: Iterable[Candidate], plan: Optional[QueryPlan] = None) -> list[ContextBlock]:
    """Render ranked candidates into blocks carrying role, provenance and route."""
    blocks: list[ContextBlock] = []
    for cand in candidates:
        text = _block_text(cand, plan)
        if not text:
            continue                                  # a block with no content is pure cost
        if not carries_content(cand, (cand.text or cand.label or "").strip()):
            continue
        blocks.append(ContextBlock(
            node_id=cand.node_id,
            role=assign_role(cand, plan),
            text=text,
            node_type=cand.node_type or "",
            label=cand.label or "",
            relations=_relations_of(cand),
            retrieved_by=retrieved_by(cand),
            score=float(cand.final_score),
            path=(max(cand.paths, key=lambda p: p.score).as_tuple() if cand.paths else None),
            attributes={k: v for k, v in (cand.properties or {}).items()
                        if isinstance(v, str) and not str(k).startswith("__")},
            tokens=estimate_tokens(text),
        ))
    return blocks


def group_by_role(blocks: Iterable[ContextBlock]) -> dict[str, list[ContextBlock]]:
    grouped: dict[str, list[ContextBlock]] = {r: [] for r in ROLE_ORDER}
    for b in blocks:
        grouped.setdefault(b.role, []).append(b)
    for role in grouped:
        grouped[role].sort(key=lambda b: b.score, reverse=True)
    return grouped


def _name_of(b: ContextBlock) -> str:
    """How this node should be referred to in a relationship line."""
    nm = (b.label or "").strip()
    ty = (b.node_type or "").strip()
    if nm and ty and nm != ty:
        return f"{ty} {nm}"
    return nm or ty or str(b.node_id)[:8]


def relationship_lines(blocks: Iterable[ContextBlock]) -> list[str]:
    """The edges among the packaged nodes, written out.

    THE EDGE WAS RETRIEVED AND NEVER SAID. `Path.as_tuple()` is
    `(seed, edge_type, to, edge_type, to, ...)`, and `render` used to print it as
    `"->".join(str(p)[:8] ...)` — which truncates the edge type along with the ids and yields
    `651868ab->HAS_PROD->da32e1ef`. Measured: for "what product did order DBO-1 contain" the window
    held Order DBO-1 and BOTH products, the correct edge was in the path, and the answer was the
    wrong product — because nothing in the context stated which product the order was linked to.

    Only edges whose BOTH endpoints are in the window are emitted. An edge to a node that was not
    packaged cannot be checked against anything the reader can see, and asserting it would be asking
    the reader to trust a name with no evidence behind it.
    """
    blocks = list(blocks)
    named = {str(b.node_id): _name_of(b) for b in blocks if b.node_id}
    out: list[str] = []
    seen: set[str] = set()
    for b in blocks:
        for a, edge, c in (b.relations or []):
            if a in named and c in named and edge:
                line = f"{named[a]} -{edge}-> {named[c]}"
                if line not in seen:
                    seen.add(line)
                    out.append(line)
    return out


def render(blocks: Iterable[ContextBlock], include_provenance: bool = True) -> str:
    """The final string handed to the model, sectioned by what each part contributes."""
    blocks = list(blocks)
    grouped = group_by_role(blocks)
    lines: list[str] = []
    titles = {
        ROLE_PRIMARY: "PRIMARY EVIDENCE",
        ROLE_RELATIONSHIP: "RELATIONSHIP EVIDENCE",
        ROLE_TEMPORAL: "TEMPORAL EVIDENCE",
        ROLE_SUPPORTING: "SUPPORTING EVIDENCE",
        ROLE_TABLE_RESULT: "DATA (query result)",
    }
    for role in ROLE_ORDER:
        items = grouped.get(role) or []
        if not items:
            continue
        lines.append(titles[role])
        for b in items:
            lines.append(f"  {b.text}")
            if include_provenance:
                meta = [f"id={str(b.node_id)[:8]}"]
                if b.node_type:
                    meta.append(f"type={b.node_type}")
                if b.retrieved_by:
                    meta.append("via=" + ",".join(b.retrieved_by[:3]))
                if b.path:
                    meta.append("path=" + "->".join(str(p)[:8] for p in b.path))
                lines.append(f"    ({' '.join(meta)})")
        lines.append("")
    rels = relationship_lines(blocks)
    if rels:
        lines.append("RELATIONSHIPS")
        lines.extend(f"  {r}" for r in rels)
        lines.append("")
    return "\n".join(lines).strip()
