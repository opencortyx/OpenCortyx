"""L1 — Lexical surfaces: BM25, identifier lookup, fuzzy names, metadata constraints, type-scoped BM25.

Kept apart because they fail in different ways and a plan weights them differently: BM25 is right
for prose and wrong for `REC-890998`, and a trigram match is right for a misspelt name and noise
for a sentence. BM25 scores stay on `scale="bm25"` so fusion never compares them to a cosine.

All of them read each node's keyword document in the vector store: a BM25 sparse vector over its
text, generated context, stored rendering and label, plus its names and scalar properties.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from opencortyx.core.substrate import document as _node_document
from opencortyx.core.inference.embedding import sparse_query
from opencortyx.core.substrate import NodeRepo
from opencortyx.stores.graph import Node
from opencortyx.retrieval.contracts import Candidate, Evidence
from opencortyx.stores.vector import DOCUMENT_ATTR, VectorFilter

_WORD = re.compile(r"[A-Za-z0-9']+")
_IDENTIFIER = re.compile(r"\b([A-Z]{2,}[-_]?\d{2,}[-\w]*|\b\d{6,}\b)")

#: Terms this common carry no discrimination.
_STOP = {
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how", "is", "are", "was",
    "were", "do", "does", "did", "the", "a", "an", "and", "or", "of", "for", "to", "from", "in",
    "on", "at", "by", "with", "about", "that", "this", "these", "those", "it", "its", "be",
    "been", "have", "has", "had", "would", "could", "should", "will", "can", "may", "might",
}

#: A metadata constraint reads at most this many nodes carrying the constrained properties.
_METADATA_SCAN = 5000
_DATE_KEYS = ("date_time", "date", "timestamp", "valid_from", "created_date")


def query_terms(query: str) -> list[str]:
    """Content words, lowercased. Empty means "do not run BM25", never "match everything"."""
    return [w.lower() for w in _WORD.findall(query or "") if w.lower() not in _STOP and len(w) > 1]


def document_filter(workspace_id, collection_ids=None, **extra) -> VectorFilter:
    """Keyword documents of an organization, not superseded; nodes in no collection always count."""
    return VectorFilter(workspace_id=workspace_id, attrs=[DOCUMENT_ATTR], exclude_superseded=True,
                        collection_ids=collection_ids or None, include_unscoped=True, **extra)


def candidate(node: Node) -> Candidate:
    return Candidate(node_id=node.id, node_type=node.type or "", label=node.label or "",
                     text=_node_document.from_row(node.properties, node.label, node.type),
                     properties=node.properties or {})


async def ranked_documents(repo: NodeRepo, workspace_id, query: str, flt: VectorFilter, k: int):
    """(node, BM25 score) for the query's content words, best first."""
    terms = query_terms(query)
    if not terms:
        return []
    vector = await sparse_query(" ".join(terms))
    hits = await repo.vectors.search_sparse(vector, flt, k)
    nodes = {str(n.id): n for n in await repo.get_many(workspace_id, [h.node_id for h in hits])}
    return [(nodes[str(h.node_id)], h.score) for h in hits if str(h.node_id) in nodes]


class BM25Surface:
    """Ranked keyword search. Terms are ORed: an answering passage restates only part of a
    question, and BM25 already rewards covering more of it."""
    surface = "bm25"

    def __init__(self, repo: Optional[NodeRepo] = None):
        self._repo = repo or NodeRepo()

    async def retrieve(self, workspace_id: uuid.UUID, query: str, k: int = 100,
                       collection_ids: Optional[list] = None, temporal=None) -> list[Candidate]:
        terms = query_terms(query)
        out = []
        for rank, (node, score) in enumerate(await ranked_documents(
                self._repo, workspace_id, query, document_filter(workspace_id, collection_ids), k)):
            c = candidate(node)
            c.add(Evidence(surface=self.surface, rank=rank, raw_score=float(score), scale="bm25",
                           detail=f"{len(terms)}terms"))
            out.append(c)
        return out


class IdentifierSurface:
    """Literal lookup for codes and references in the question: BM25 stems them and dense search
    has no notion of an exact token, so a unique key needs its own surface."""
    surface = "identifier"

    def __init__(self, repo: Optional[NodeRepo] = None):
        self._repo = repo or NodeRepo()

    async def retrieve(self, workspace_id: uuid.UUID, query: str, k: int = 50,
                       collection_ids: Optional[list] = None) -> list[Candidate]:
        ids = [m.group(1) for m in _IDENTIFIER.finditer(query or "")]
        out: list[Candidate] = []
        for token in ids[:5]:
            rows = await self._repo.vectors.scroll(document_filter(workspace_id, collection_ids, phrase=token),
                                                   limit=k, fields=["node_id"])
            nodes = await self._repo.get_many(workspace_id, [r["node_id"] for r in rows])
            for rank, node in enumerate(nodes):
                c = candidate(node)
                c.add(Evidence(surface=self.surface, rank=rank, raw_score=1.0, scale="exact",
                               detail=token, matched_text=token))
                out.append(c)
        return out


def _trigrams(text: str) -> set[str]:
    grams: set[str] = set()
    for word in re.findall(r"\w+", (text or "").lower()):
        padded = f"  {word} "
        grams.update(padded[i:i + 3] for i in range(len(padded) - 2))
    return grams


def trigram_similarity(a: str, b: str) -> float:
    """Shared trigrams over all trigrams of both strings, as PostgreSQL's pg_trgm computes it."""
    ga, gb = _trigrams(a), _trigrams(b)
    union = len(ga | gb)
    return len(ga & gb) / union if union else 0.0


async def similar_names(repo: NodeRepo, embed, workspace_id, mention: str, *, collection_ids=None,
                        k: int = 25, threshold: float = 0.3) -> list[tuple[Node, float]]:
    """Nodes whose label is spelt like `mention`, as (node, trigram similarity).

    Candidates are nodes sharing a word with the mention plus the nearest label embeddings, which
    together catch shortened names and misspellings; trigram similarity then decides."""
    ids: dict[str, None] = {}
    for word in {w for w in re.findall(r"\w+", mention.lower()) if len(w) > 2}:
        rows = await repo.vectors.scroll(document_filter(workspace_id, collection_ids, names_phrase=word),
                                         limit=k * 4, fields=["node_id"])
        ids.update((r["node_id"], None) for r in rows)
    if embed is not None:
        vector = await embed(mention)
        if vector:
            flt = VectorFilter(workspace_id=workspace_id, attrs=["label", "name"], exclude_superseded=True,
                               collection_ids=collection_ids or None, include_unscoped=True)
            ids.update((str(h.node_id), None) for h in await repo.vectors.search_dense(vector, flt, k))
    scored = [(n, trigram_similarity(n.label or "", mention)) for n in await repo.get_many(workspace_id, list(ids))]
    scored = [(n, s) for n, s in scored if s >= threshold]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:k]


class FuzzySurface:
    """Trigram similarity over labels, driven by the question's MENTIONS only: run over whole
    questions it matches much of the corpus on common substrings."""
    surface = "fuzzy"

    def __init__(self, embed=None, repo: Optional[NodeRepo] = None):
        self._embed = embed
        self._repo = repo or NodeRepo()

    async def retrieve(self, workspace_id: uuid.UUID, mentions: list[str], k: int = 50, threshold: float = 0.35,
                       collection_ids: Optional[list] = None) -> list[Candidate]:
        out: list[Candidate] = []
        for term in [m.strip() for m in (mentions or []) if m and len(m.strip()) > 2][:6]:
            matches = await similar_names(self._repo, self._embed, workspace_id, term, collection_ids=collection_ids,
                                          k=k, threshold=threshold)
            for rank, (node, sim) in enumerate(matches):
                c = candidate(node)
                c.add(Evidence(surface=self.surface, rank=rank, raw_score=float(sim), scale="ratio",
                               detail=term, matched_text=node.label or ""))
                out.append(c)
        return out


def _compare(value, op: str, target) -> bool:
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        try:
            number = float(str(value))
        except ValueError:
            return False
        return {"eq": number == target, "ne": number != target, "gt": number > target,
                "gte": number >= target, "lt": number < target, "lte": number <= target}.get(op, number == target)
    equal = str(value).strip().lower() == str(target).strip().lower()
    return not equal if op == "ne" else equal


class MetadataSurface:
    """Structured constraints applied as filters, not as ranking: a date bound or an attribute
    comparison rejects a node rather than hoping the right one outranks it. Attribute names are
    whatever the corpus's properties are called."""
    surface = "metadata"

    def __init__(self, repo: Optional[NodeRepo] = None):
        self._repo = repo or NodeRepo()

    async def retrieve(self, workspace_id: uuid.UUID, temporal=None, attributes=None, k: int = 200,
                       collection_ids: Optional[list] = None, scope_dialogues: Optional[list] = None) -> list[Candidate]:
        """`scope_dialogues` confines the constraint to the conversations the question is about."""
        attributes = list(attributes or [])
        if not attributes and not temporal:
            return []
        wanted = [a.attr for a in attributes] or list(_DATE_KEYS)
        rows = await self._repo.vectors.scroll(document_filter(workspace_id, collection_ids, has_props=wanted),
                                               limit=_METADATA_SCAN, fields=["node_id", "props"])
        from opencortyx.retrieval.l0_understanding.constraints import to_iso, within
        allowed = {str(d) for d in scope_dialogues} if scope_dialogues else None
        keep: list[str] = []
        for row in rows:
            props = row.get("props") or {}
            if allowed is not None and str(props.get("dialogue_id")) not in allowed:
                continue
            if not all(props.get(a.attr) not in (None, "") and _compare(props[a.attr], a.op, a.value)
                       for a in attributes):
                continue
            if temporal:
                iso = next((to_iso(str(props[key])) for key in _DATE_KEYS if props.get(key)), None)
                if not all(within(tc, iso) for tc in temporal):
                    continue
            keep.append(row["node_id"])
            if len(keep) >= k:
                break
        out = []
        for rank, node in enumerate(await self._repo.get_many(workspace_id, keep)):
            c = candidate(node)
            c.add(Evidence(surface=self.surface, rank=rank, raw_score=1.0, scale="constant",
                           detail="constraint-match"))
            out.append(c)
        return out


class OntologySurface:
    """BM25 ranked WITHIN each type the question implicated, so a rare type the question named is
    not drowned by an abundant one. `node_types` is empty unless the question argued for a type,
    and other surfaces still search everything, so a wrong hypothesis removes nothing."""
    surface = "ontology"

    def __init__(self, repo: Optional[NodeRepo] = None):
        self._repo = repo or NodeRepo()

    async def retrieve(self, workspace_id: uuid.UUID, query: str, node_types: Optional[dict] = None, k: int = 50,
                       collection_ids: Optional[list] = None) -> list[Candidate]:
        weights = node_types or {}
        if not weights or not query_terms(query):
            return []
        per_type = max(1, k // len(weights))
        pooled = []
        for node_type in weights:
            flt = document_filter(workspace_id, collection_ids, types=[node_type])
            pooled += await ranked_documents(self._repo, workspace_id, query, flt, per_type)
        pooled.sort(key=lambda pair: pair[1], reverse=True)
        out = []
        for rank, (node, score) in enumerate(pooled[:k]):
            c = candidate(node)
            c.add(Evidence(surface=self.surface, rank=rank,
                           raw_score=float(score) * float(weights.get(node.type, 1.0)),
                           scale="bm25", detail=node.type))
            out.append(c)
        return out
