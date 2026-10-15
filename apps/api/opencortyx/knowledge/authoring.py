"""Authoring layer — user-defined types, extractors and relationships.

Lets any user add their own knowledge — custom types, glossary terms, business rules, synonyms,
relationships, arbitrary nodes/edges — through one uniform surface. Every write goes through
NodeRepo, is embedded shortly after saving, and carries provenance (`__source`, `__author`,
`__validated`, `__created`).

Custom entity types registered here are read by the extractor on the next ingest.
"""
import logging
import uuid
from datetime import datetime, timezone

from opencortyx.core.substrate import NodeRepo
from opencortyx.core.substrate.repo import AUDIT_DATE_KEYS
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

logger = logging.getLogger(__name__)

_repo = NodeRepo()


def _prov(author) -> dict:
    return {"__source": "curation", "__author": (str(author) if author else None),
            "__validated": True, "__created": datetime.now(timezone.utc).isoformat()}


#: Marks a `text` the platform derived from the node's own fields (see `self_text`), as opposed to
#: one a person wrote — only a derived text may be regenerated when the fields change.
_DERIVED_TEXT = "__text_derived"


def self_text(type_: str, label: str, properties: dict) -> str:
    """A SELF-DESCRIBING sentence for an authored node — the same shape the walkers write.

    An authored node used to be embedded on a string that was never STORED. Retrieval packages
    `properties.text`, so the node had a vector but nothing to show: it could win a top-k slot and then
    hand the generator an empty block. That is precisely the case the contentless-node filter now
    excludes, so an Aadhaar added by hand was searchable, retrieved, and invisible in the same breath.

    Building it once and persisting it means the VECTOR and the PACKAGED TEXT are the same string.
    Those two silently diverging is worse than either being wrong: the node matches a query for
    content the reader never sees.

    Type + label first (naming what a thing IS carries most of the signal for a short record), then
    its scalar attributes, so "Aadhaar 4321 8765 9012. number=4321 8765 9012; issued=2019-04-02"
    answers "what identity documents do I have and what are the numbers".
    """
    head = f"{type_} {label}".strip() if label and str(label) != str(type_) else str(type_ or label or "")
    pairs = []
    for k, v in (properties or {}).items():
        if str(k).startswith("__") or k in ("text", "embedding") or k in AUDIT_DATE_KEYS:
            continue
        if isinstance(v, (str, int, float, bool)) and str(v).strip():
            pairs.append(f"{k}={v}")
    body = "; ".join(pairs)
    return ". ".join(p for p in (head, body) if p)[:4000]


def _enqueue_embed(workspace_id, node_id) -> None:
    """Embed the node in the background so the write returns at once. Best-effort: a broker hiccup
    must not fail the write, and the next edit or re-index embeds it again."""
    try:
        from opencortyx.tasks.graph import v2_embed_node
        v2_embed_node.delay(str(workspace_id), str(node_id))
    except Exception as e:                                # noqa: BLE001 - never break the write
        log.warning("authoring.embed_enqueue_failed", node_id=str(node_id), error=str(e))


async def _embed_now(workspace_id, node_ids) -> None:
    from opencortyx.core.inference.embedding import embed_batch
    await _repo.index_embeddings(workspace_id, await _repo.get_many(workspace_id, node_ids), embed_batch)


# ── raw node/edge CRUD ───────────────────────────────────────────────────
async def create_node(workspace_id, *, type, label="", properties=None, collection_id=None,
                      author=None, node_id=None, confidence=1.0, embed: str = "async") -> uuid.UUID:
    """Create a node and return its id. `embed` is "async" (embed in the background, the default),
    "sync" (embed before returning, when the caller needs it searchable at once) or "off"."""
    props = {**(properties or {}), **_prov(author)}
    # A caller-supplied `text` is what the node says; otherwise derive one, so the node is never
    # searchable-but-blank.
    if not str(props.get("text") or "").strip():
        props["text"] = self_text(type, label, props)
        props[_DERIVED_TEXT] = True
    node = await _repo.upsert_node(node_id=node_id, workspace_id=workspace_id, type=type, label=label or type,
                                   properties=props, collection_id=collection_id, confidence=confidence,
                                   replace_properties=True)
    if embed == "sync":
        await _embed_now(workspace_id, [node.id])
    elif embed == "async":
        _enqueue_embed(workspace_id, node.id)
    return node.id


async def update_node(workspace_id, node_id, *, label=None, properties=None, collection_id=None, author=None,
                      embed: str = "async") -> bool:
    """Update a node in place; its embeddings are refreshed like create_node's."""
    node = await _repo.get(workspace_id, node_id)
    if node is None:
        return False
    if label is not None:
        node.label = label
    if collection_id is not None:
        node.collection_id = _uuid(collection_id)
    before = node.properties or {}
    if properties is not None:
        node.properties = {**before, **properties,
                           "__updated": datetime.now(timezone.utc).isoformat(),
                           "__author": str(author) if author else before.get("__author")}
    sent = str((properties or {}).get("text") or "").strip()
    # The text is re-derived unless the user WROTE it: an edit form sends every attribute back,
    # including a derived `text`, and keeping that returned copy froze the node's text at the values
    # it was created with (category=Logistics after the category became Freight).
    old_text = str(before.get("text") or "").strip()
    was_derived = bool(before.get(_DERIVED_TEXT)) or old_text == self_text(
        node.type, node.label, {k: v for k, v in before.items() if k != "text"}).strip()
    user_wrote = bool(sent) and not (was_derived and sent == old_text)
    if user_wrote:
        node.properties = {k: v for k, v in (node.properties or {}).items() if k != _DERIVED_TEXT}
    else:
        derived = self_text(node.type, node.label,
                            {k: v for k, v in (node.properties or {}).items() if k != "text"})
        if derived.strip():
            node.properties = {**(node.properties or {}), "text": derived, _DERIVED_TEXT: True}
    await _repo.save(node)
    if node.type != "Date":
        await _repo.link_date(workspace_id=workspace_id, node_id=node.id, created=False)
    if embed == "sync":
        await _embed_now(workspace_id, [node.id])
    elif embed == "async":
        _enqueue_embed(workspace_id, node.id)
    return True


async def delete_node(workspace_id, node_id) -> bool:
    return bool(await _repo.delete_nodes(workspace_id, [node_id]))


async def create_edge(workspace_id, *, source_id, target_id, type, properties=None, author=None) -> dict:
    """Create an edge when both endpoints exist."""
    s_node, t_node = await _repo.get(workspace_id, source_id), await _repo.get(workspace_id, target_id)
    if not s_node or not t_node:
        return {"status": "endpoint_not_found"}

    await _repo.add_edge(workspace_id=workspace_id, source_id=s_node.id, target_id=t_node.id, type=type,
                         properties={**(properties or {}), **_prov(author)})
    return {"status": "ok"}


async def delete_edge(workspace_id, edge_id) -> bool:
    edges = await _repo.edges(workspace_id, ids=[edge_id])
    return bool(edges) and bool(await _repo.delete_edges(workspace_id, edges))


def _meta_props(*, kind=None, scope=None, status=None, owner=None, source=None,
                valid_from=None, valid_until=None, examples=None) -> dict:
    p = {}
    if kind: p["kind"] = kind
    if scope: p["scope"] = scope
    if status: p["status"] = status
    if owner: p["owner"] = owner
    if source: p["source"] = source
    if valid_from: p["valid_from"] = valid_from
    if valid_until: p["valid_until"] = valid_until
    if examples:
        p["examples"] = examples if isinstance(examples, list) else [examples]
    return p


def _merge_targets(targets, legacy_single) -> list:
    """Accept either a list of node ids or the legacy single id; dedupe, drop blanks."""
    out, seen = [], set()
    for t in list(targets or []) + ([legacy_single] if legacy_single else []):
        s = str(t).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


async def _resolve_target(workspace_id, t):
    """A target as a node id — given as an id or, the way a person would name it, by label — or None
    when it names nothing, so the caller can report it instead of failing the whole write."""
    raw = str(t).strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        pass
    found = await _repo.find(workspace_id, label=raw, limit=1)
    return found[0].id if found else None


async def _attach(workspace_id, nid, targets, edge_type, author, *, reverse=False) -> tuple[list, list]:
    """Edge nid↔each target node. reverse=True links target→nid (used by HAS_SYNONYM).

    Returns `(statuses, unresolved)` so the caller can say how many targets were actually linked
    rather than implying every one of them was.
    """
    out, unresolved = [], []
    for t in targets:
        tid = await _resolve_target(workspace_id, t)
        if tid is None:
            unresolved.append(str(t))
            continue
        if reverse:
            r = await create_edge(workspace_id, source_id=tid, target_id=nid, type=edge_type, author=author)
        else:
            r = await create_edge(workspace_id, source_id=nid, target_id=tid, type=edge_type, author=author)
        if r.get("status") == "ok":
            out.append(r.get("status"))
        else:                                      # a well-formed id that names no node: not linked
            unresolved.append(str(t))
    return out, unresolved


async def add_glossary(workspace_id, *, term, definition, applies_to=None, targets=None,
                       scope=None, status=None, owner=None, source=None,
                       valid_from=None, valid_until=None, examples=None,
                       collection_id=None, author=None) -> dict:
    tgts = _merge_targets(targets, applies_to)
    props = {"term": term, "definition": definition,
             **_meta_props(scope=scope, status=status, owner=owner, source=source,
                           valid_from=valid_from, valid_until=valid_until, examples=examples)}
    nid = await create_node(workspace_id, type="Term", label=term, properties=props,
                            collection_id=_uuid(collection_id) if collection_id else None, author=author)
    linked, unresolved = await _attach(workspace_id, nid, tgts, "DEFINES", author)
    # `targets` reports what was LINKED, not what was asked for: a caller that named
    # three things and got one edge needs to know which two matched nothing.
    return {"id": str(nid), "targets": len(linked),
            **({"unresolved_targets": unresolved} if unresolved else {})}


async def add_rule(workspace_id, *, title, content, applies_to=None, targets=None, severity="info",
                   kind=None, scope=None, status=None, owner=None, source=None,
                   valid_from=None, valid_until=None, collection_id=None, author=None) -> dict:
    tgts = _merge_targets(targets, applies_to)
    props = {"title": title, "content": content, "text": content,
             "context_type": "BUSINESS_RULE", "severity": severity,
             **_meta_props(kind=kind, scope=scope, status=status, owner=owner, source=source,
                           valid_from=valid_from, valid_until=valid_until)}
    nid = await create_node(workspace_id, type="ContextRule", label=title, properties=props,
                            collection_id=_uuid(collection_id) if collection_id else None, author=author)
    linked, unresolved = await _attach(workspace_id, nid, tgts, "APPLIES_TO", author)
    # `targets` reports what was LINKED, not what was asked for: a caller that named
    # three things and got one edge needs to know which two matched nothing.
    return {"id": str(nid), "targets": len(linked),
            **({"unresolved_targets": unresolved} if unresolved else {})}


async def add_synonym(workspace_id, *, term, target_id=None, targets=None, kind=None,
                      scope=None, status=None, owner=None, source=None, author=None) -> dict:
    tgts = _merge_targets(targets, target_id)
    props = {"term": term, "target_id": (tgts[0] if tgts else None),
             **_meta_props(kind=kind, scope=scope, status=status, owner=owner, source=source)}
    nid = await create_node(workspace_id, type="Synonym", label=term, properties=props, author=author)
    linked, unresolved = await _attach(workspace_id, nid, tgts, "HAS_SYNONYM", author, reverse=True)
    # `targets` reports what was LINKED, not what was asked for: a caller that named
    # three things and got one edge needs to know which two matched nothing.
    return {"id": str(nid), "targets": len(linked),
            **({"unresolved_targets": unresolved} if unresolved else {})}


# ── org/collection-level business context (always-on framing for the assistant) ──
async def add_business_context(workspace_id, *, title, content, scope="org", collection_id=None,
                               always_on=True, kind="business_context", owner=None,
                               source=None, author=None) -> dict:
    props = {"title": title, "content": content, "text": content,
             "context_type": "BUSINESS_CONTEXT", "kind": kind, "scope": scope,
             "always_on": bool(always_on), **_meta_props(owner=owner, source=source)}
    nid = await create_node(workspace_id, type="BusinessContext", label=title, properties=props,
                            collection_id=_uuid(collection_id) if collection_id else None, author=author)
    return {"id": str(nid)}


async def list_business_context(workspace_id, *, only_always_on=False) -> list:
    out = []
    for n in await _repo.find(workspace_id, exact_types=["BusinessContext"]):
        p = n.properties or {}
        if only_always_on and not p.get("always_on"):
            continue
        out.append({"id": str(n.id), "title": p.get("title") or n.label,
                    "content": p.get("content") or "", "scope": p.get("scope") or "org",
                    "always_on": bool(p.get("always_on", True)),
                    "collection_id": str(n.collection_id) if n.collection_id else None})
    return out


async def add_relationship(workspace_id, *, source_id, target_id, relation, author=None) -> dict:
    return await create_edge(workspace_id, source_id=_uuid(source_id), target_id=_uuid(target_id),
                             type="RELATES_TO", properties={"relation": relation}, author=author)


# ── search (autocomplete for linking) ────────────────────────────────────
async def search_nodes(workspace_id, *, q, types=None, limit=20) -> list:
    """Nodes whose label contains `q`, newest first, so a just-created term or rule is at the top of
    the picker it feeds.

    A hidden node (removed from its source) is not offered: this surface is what users and agents
    link to."""
    from opencortyx.stores.graph.query import HIDDEN_STATUSES, node_status
    rows = await _repo.find(workspace_id, exact_types=list(types) if types else None, label_contains=q or None,
                            order_by="-created_at", limit=limit * 2 if limit else None)
    rows = [n for n in rows if node_status(n) not in HIDDEN_STATUSES][:limit]
    return [{"id": str(n.id), "label": n.label, "type": n.type} for n in rows]


def _uuid(v):
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
