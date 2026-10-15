"""L1 — Entity resolution: bind a mention to a canonical node, once, with its evidence.

    "Microsoft"  ─┬─ exact label      ─┐
                  ├─ alias property   ─┤
                  ├─ fuzzy/trigram    ─┼─► canonical node + confidence + matched_by[]
                  ├─ keyword          ─┤
                  └─ dense            ─┘

WHY THIS IS A SEPARATE STAGE AND NOT "THE FUZZY RETRIEVER"

Three reasons, all of which the previous engine got wrong by not having it:

  1. RESOLVE ONCE. Without a resolution step, every downstream surface re-guesses what "Nate"
     refers to, and they can disagree — one retrieves the person, another a turn that mentions
     the name. Graph expansion then seeds from whichever guess happened to rank highest.

  2. EVIDENCE OF THE BINDING. A mention matched by an exact label and a mention matched at 0.36
     trigram similarity are not equally certain, and a system that cannot tell them apart will
     confidently answer about the wrong entity. `matched_by` and `confidence` travel with the
     resolution so later stages can discount a weak binding.

  3. AMBIGUITY IS A RESULT, NOT AN ERROR. When two candidates are close, both are returned in
     `alternatives`. Silently picking one is how a query about one company retrieves another with
     a similar description.

NO GAZETTEER, NO TYPE FILTER. Resolution searches whatever the graph holds. Restricting to a
"Person/Company/Country" type list would fail on the first corpus that names its types otherwise,
and the previous engine's entity retriever did exactly that — it asked for `types=["entity"]`
against nodes stored as `Entity` and returned nothing for years' worth of queries.
"""
from __future__ import annotations

import uuid
from typing import Optional

from opencortyx.core.substrate import NodeRepo
from opencortyx.retrieval.contracts import (
    Candidate, EntityMention, Evidence, ResolvedEntity,
)
from opencortyx.retrieval.l1_candidates.lexical import document_filter, similar_names
from opencortyx.stores.vector import VectorFilter

_METHOD_CONFIDENCE = {
    "exact": 0.98,
    "alias": 0.92,
    "keyword": 0.68,
    "dense": 0.60,
    "fuzzy": 0.45,
}

#: Below this, a binding is reported but not acted on — the mention stays a lexical signal.
MIN_BINDING_CONFIDENCE = 0.40

#: Two candidates within this of each other are ambiguous, and BOTH are surfaced.
AMBIGUITY_MARGIN = 0.08

#: The attribute surfaces that carry NAMES; matching a mention against prose binds common nouns to sentences.
_NAME_ATTRS = ("label", "name")

#: A name longer than this is a sentence, whatever surface it is stored on.
_MAX_NAME_CHARS = 80

_MIN_DENSE_SIM = 0.60

_CANDIDATES = 25


class EntityResolver:
    """Binds mentions to canonical nodes. Results are cached per instance, per query."""

    def __init__(self, embed=None, repo: Optional[NodeRepo] = None):
        self._embed = embed
        self._repo = repo or NodeRepo()
        self._cache: dict[str, ResolvedEntity] = {}

    async def resolve(self, workspace_id: uuid.UUID, mention: EntityMention,
                      collection_ids: Optional[list] = None) -> ResolvedEntity:
        key = mention.text.lower().strip()
        if key in self._cache:
            return self._cache[key]

        scored: dict[str, dict] = {}
        labels: dict[str, tuple] = {}

        def record(node, method, score):
            e = scored.setdefault(str(node.id), {})
            e[method] = max(e.get(method, 0.0), float(score))
            labels[str(node.id)] = (node.type or "", node.label or "")

        async def nodes_where(**conditions):
            rows = await self._repo.vectors.scroll(document_filter(workspace_id, collection_ids, **conditions),
                                                   limit=_CANDIDATES, fields=["node_id"])
            return await self._repo.get_many(workspace_id, [r["node_id"] for r in rows])

        # Exact: the label, a `name`, or an alias equal to the mention.
        for node in await nodes_where(names=[key]):
            props = node.properties or {}
            exact = (node.label or "").strip().lower() == key or str(props.get("name") or "").strip().lower() == key
            record(node, "exact" if exact else "alias", 1.0)

        # The mention inside a longer name: an alias containing it, or a label containing it.
        for node in await nodes_where(names_phrase=mention.text):
            label = (node.label or "").lower()
            if key in label:
                record(node, "keyword", min(1.0, len(key) / max(len(label), 1)))
            else:
                record(node, "alias", 1.0)

        qvec = None
        if self._embed is not None:
            try:
                qvec = list(await self._embed(mention.text) or []) or None
            except Exception:
                qvec = None             # no embedder: no dense evidence, and fuzzy matches shared words only

        async def mention_vector(_text):
            return qvec

        for node, sim in await similar_names(self._repo, mention_vector if qvec else None, workspace_id, mention.text,
                                             collection_ids=collection_ids, k=_CANDIDATES):
            record(node, "fuzzy", sim)

        if qvec:
            flt = VectorFilter(workspace_id=workspace_id, attrs=list(_NAME_ATTRS), exclude_superseded=True,
                               collection_ids=collection_ids or None, include_unscoped=True)
            hits = [h for h in await self._repo.vectors.search_dense(qvec, flt, 10)
                    if h.score >= _MIN_DENSE_SIM and len(h.text.split(": ", 1)[-1]) <= _MAX_NAME_CHARS]
            for node in await self._repo.get_many(workspace_id, [h.node_id for h in hits]):
                record(node, "dense", max(h.score for h in hits if h.node_id == node.id))

        if not scored:
            out = ResolvedEntity(mention=mention, confidence=0.0, matched_by=[])
            self._cache[key] = out
            return out

        # The strongest single method sets the floor, corroboration adds a little; methods are not
        # summed, since three weak signals do not equal one exact match.
        ranked = []
        for nid, methods in scored.items():
            best_method = max(methods.items(), key=lambda kv: _METHOD_CONFIDENCE.get(kv[0], 0.3) * kv[1])
            base = _METHOD_CONFIDENCE.get(best_method[0], 0.3) * best_method[1]
            ranked.append((min(1.0, base + 0.03 * (len(methods) - 1)), nid, sorted(methods)))
        ranked.sort(reverse=True)

        top_conf, top_id, top_methods = ranked[0]
        alternatives = [(uuid.UUID(nid), labels[nid][1], conf)
                        for conf, nid, _ in ranked[1:4]
                        if top_conf - conf <= AMBIGUITY_MARGIN]

        out = ResolvedEntity(
            mention=mention,
            node_id=uuid.UUID(top_id) if top_conf >= MIN_BINDING_CONFIDENCE else None,
            label=labels[top_id][1],
            confidence=round(top_conf, 4),
            matched_by=top_methods,
            alternatives=alternatives,
        )
        self._cache[key] = out
        return out

    async def resolve_all(self, workspace_id: uuid.UUID, mentions: list[EntityMention],
                          collection_ids: Optional[list] = None) -> list[ResolvedEntity]:
        return [await self.resolve(workspace_id, m, collection_ids) for m in (mentions or [])]

    @staticmethod
    def to_candidates(resolved: list[ResolvedEntity]) -> list[Candidate]:
        """Resolved entities as first-class candidates, so a resolution is itself retrievable
        evidence and can seed graph expansion."""
        out: list[Candidate] = []
        for rank, r in enumerate(resolved):
            if not r.resolved:
                continue
            c = Candidate(node_id=r.node_id, label=r.label, text=r.label)
            c.add(Evidence(surface="entity", rank=rank, raw_score=r.confidence,
                           scale="binding", detail="+".join(r.matched_by),
                           matched_text=r.mention.text))
            out.append(c)
        return out
