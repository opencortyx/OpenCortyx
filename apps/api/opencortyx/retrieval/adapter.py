"""Serve `build_context`'s contract from the L0-L4 engine.

    build_context_v2(query, ctx) -> the SAME dict `retrieve.planner.build_context` returns

WHY AN ADAPTER RATHER THAN A REWRITE

`POST /api/v2/answer` -> `retrieve.answer.answer_query` -> `retrieve.planner.build_context`, and
that dict is read by the router, the abstain capability, session capture and the chat UI
(`context`, `citations`, `confidence`, `retrievers`, `stage_trace`, `tokens_used`, `abstained`,
`caveats`). Holding the contract still means the migration is one seam, provable by diffing the
packed context against a reference captured beforehand - rather than a change whose blast radius
is every consumer at once.

Everything measured this session (94.20% provenance on 1,540) lives in `retrieval`,
which the product has never called. This is what makes that work reach a user.

UNSUPPORTED OPTIONS ARE REPORTED, NEVER SILENTLY DROPPED

`RetrieveContext` carries per-query controls the Chat UI sets. Some map onto the v2 engine, some
have no equivalent yet. An option that is accepted and ignored is the worst outcome: the control
appears to work, the user trusts it, and nothing changes. So anything unmappable is listed in
`result["unsupported"]` and named in the stage trace - visible, not swallowed.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from opencortyx.core.contracts import RetrieveContext
from opencortyx.core.observability import get_logger
from opencortyx.core.inference.embedding import embed as _emb, embed_batch as _embb
from opencortyx.retrieval.orchestrator import (
    RetrievalWorkflow, WorkflowConfig,
)

log = get_logger(__name__)

#: Built once. The workflow holds no per-query state - scope and budget are passed per call, which
#: is why concurrent requests cannot leak into one another.
_WF: Optional[RetrievalWorkflow] = None

#: Controls with no v2 equivalent yet. Named here so adding one is a deliberate act, and so the
#: caller is told rather than silently disappointed.
_UNSUPPORTED = {
    "as_of": "bitemporal valid-time filtering is not implemented in the v2 engine",
    "as_of_ingest": "bitemporal transaction-time filtering is not implemented in the v2 engine",
    "rrf_k": "v2 fusion uses its own RRF constant; per-query override not plumbed",
    "retrievers": "v2 selects surfaces per question via its policy; explicit lists are not honoured",
    "rerank": "v2 ranks with one weighted feature model; per-query rerank stages are not honoured",
    "abstain_threshold": "v2 abstains on its own feature-based gate; the threshold is not plumbed",
    "fast": "v2 has no separate fast path; ranking is linear and already sub-second",
}


def _workflow(cfg: WorkflowConfig) -> RetrievalWorkflow:
    """Reuse the workflow only when EVERY field that changes its behaviour matches.

    The key covered `candidates_per_surface` and `budget_tokens` but NOT `context_blocks` (the
    caller's `k`, applied as an L4 diversify cut). So the first request after a restart pinned that
    value for the whole process: a request carrying k=5 made every later request return 5 blocks
    even when it passed no k at all (measured: 5 blocks / 304 tokens where the same query returns
    173 / 6,000), and conversely a first request without k made every later `k` silently ignored.
    Which behaviour you got depended on which request happened to arrive first.

    WorkflowConfig is a dataclass, so comparing it whole removes the CLASS of bug rather than this
    one instance: a field added later is covered without anyone remembering to extend a key.
    """
    from opencortyx.core.config import RETRIEVAL_WORKFLOW_CACHE
    if not RETRIEVAL_WORKFLOW_CACHE:          # off by default - build fresh, no shared state
        return RetrievalWorkflow(_emb, _embb, cfg)
    global _WF
    if _WF is None or _WF.cfg != cfg:
        _WF = RetrievalWorkflow(_emb, _embb, cfg)
    return _WF


def _serialise(blocks, prov: dict | None = None) -> tuple[str, int]:
    """One JSON object per block, newline joined — the `context` the model, the API and MCP clients read.

    Each block says where it came from, in the context itself and not only in `citations`, because
    `context` is what an API or MCP caller hands to its own model: `asset` / `asset_id` for the main
    source (ranked for the question), `also_in` when a shared entity lives in more assets, or
    `origin` when the node belongs to no asset (curation, template, memory). Kept to a few tokens a
    block; the full ranked list with evidence snippets is in `citations`.
    """
    prov = prov or {}
    out, used = [], 0
    for b in blocks:
        obj = {"node_id": str(b.node_id), "type": b.node_type, "text": b.text or ""}
        p = prov.get(str(b.node_id)) or {}
        if p.get("asset_id"):
            obj["asset"] = p.get("asset_label")
            obj["asset_id"] = p["asset_id"]
            if (p.get("source_count") or 0) > 1:
                obj["also_in"] = f"{p['source_count'] - 1}{'+' if p.get('source_count_capped') else ''} more assets"
        elif p:
            obj["origin"] = p.get("origin") or "unknown"
        s = json.dumps(obj)
        out.append(s)
        used += max(1, len(s) // 4)
    from opencortyx.retrieval.l4_context.builder import relationship_lines
    rels = relationship_lines(blocks)
    if rels:
        rel_s = json.dumps({"node_id": "relationships", "type": "RELATIONSHIP",
                            "text": "Relationships between the nodes above: " + "; ".join(rels)})
        out.append(rel_s)
        used += max(1, len(rel_s) // 4)
    return "\n".join(out), used


def _citations(blocks, prov: dict | None = None) -> list:
    """One citation per packed block: what it is, how well it scored, its evidence text, and where it
    came from.

    Where it came from is `asset_id` (the main asset, ranked for the question — see
    `core.substrate.provenance`), `sources` (the top few assets, each with the chunk that says it),
    `source_count`, and `origin` when there is no asset at all (curation, template, memory). Every
    citation has an `asset_id` or an `origin`; a shared entity in many assets lists them ranked.

    `source_ref` and `label` used to ride along here and are gone. `source_ref` was a verbatim copy
    of `node_id`, and `label` was `text` truncated to 80 chars - so each citation carried the same
    id twice and the same sentence twice. Nothing read either: the chat UI uses `node_id` alone.
    Across 178 citations that was dead weight in every answer payload.

    `text` is a 240-char display snippet for the chat's source list. It is NOT the evidence: grounding
    (`answer._grounding_blocks`) reads each node's full text from `context`, because checking an
    answer against the snippet rejected every fact that sat past the first 240 chars of a chunk.
    """
    prov = prov or {}
    out = []
    for b in blocks:
        p = prov.get(str(b.node_id)) or {}
        out.append({"node_id": str(b.node_id),
                    "type": b.node_type,
                    "score": round(float(b.score or 0.0), 4),
                    "text": (b.text or "")[:240],
                    "asset_id": p.get("asset_id"),
                    "asset_label": p.get("asset_label"),
                    "asset_type": p.get("asset_type"),
                    "origin": p.get("origin") or "unknown",
                    "sources": p.get("sources") or [],
                    "source_count": p.get("source_count") or 0,
                    "source_count_capped": bool(p.get("source_count_capped"))})
    return out


async def _provenance(workspace_id, blocks, query: str) -> dict:
    """Provenance for the packed blocks — best-effort: a failure here never costs the answer."""
    try:
        from opencortyx.core.substrate.provenance import provenance
        return await provenance(workspace_id, [b.node_id for b in blocks], query=query)
    except Exception as exc:                               # noqa: BLE001 — citations still go out
        log.warning("retrieval.provenance_failed", error=str(exc)[:200])
        return {}


def _note_salience(workspace_id, node_ids) -> None:
    """Record that these statements were USED — off the request, and never able to break it.

    Fire-and-forget by design. This is bookkeeping for ranking, so it must not add a write to the
    latency of a query, and a failure to record it must not cost the caller their answer. Scheduled
    as a task rather than awaited for the first reason; wrapped for the second.

    The nodes passed are the PACKED blocks, not the candidate pool: "did anyone need this" is a
    question about what reached the answer. Counting candidates would mark a statement useful merely
    for resembling a question, which is the opposite of what salience means.
    """
    ids = [str(n) for n in (node_ids or []) if n]
    if not ids:
        return

    async def _run():
        try:
            from opencortyx.core.substrate import NodeRepo
            await NodeRepo().note_access(workspace_id=workspace_id, node_ids=ids)
        except Exception:                                  # noqa: BLE001 — telemetry, never the answer
            pass

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:                                   # no loop (sync caller) — simply skip
        pass


def _trace(result) -> list:
    rows = []
    for m in (result.trace or []):
        rows.append({"stage": "%s.%s" % (getattr(m, "layer", "?"), getattr(m, "stage", "?")),
                     "detail": getattr(m, "detail", "") or "",
                     "n_in": getattr(m, "n_in", 0), "n_out": getattr(m, "n_out", 0),
                     "ms": round(float(getattr(m, "ms", 0.0) or 0.0), 1)})
    return rows


async def build_context_v2(query: str, ctx: RetrieveContext, k: int = 0) -> dict:
    """Retrieve with the L0-L4 engine, answer with v1's package contract."""
    from opencortyx.core.config import runtime as rc

    # Report only what the CALLER chose. `profile` defaults to "docs_qa" and would otherwise be
    # flagged on every single request, which trains the reader to ignore the field.
    _DEFAULTS = {"scope": "knowledge"}
    unsupported = {}
    for name, why in _UNSUPPORTED.items():
        val = getattr(ctx, name, None)
        if val in (None, False, "") or val == _DEFAULTS.get(name):
            continue
        unsupported[name] = why

    from opencortyx.core.config import CONTEXT_MAX_TOKENS, LLM_MAX_MODEL_LEN
    _ceiling = min(CONTEXT_MAX_TOKENS, max(500, LLM_MAX_MODEL_LEN - 2500))
    budget = int(rc.get("context_max_tokens") or _ceiling)   # rc.get takes ONE arg
    _asked = getattr(ctx, "max_tokens", None)
    if _asked:
        budget = min(int(_asked), budget)
    budget = max(500, min(budget, _ceiling))
    cps = int(rc.retrieve_pool_k() or 400) if hasattr(rc, "retrieve_pool_k") else 400
    cfg = WorkflowConfig(candidates_per_surface=max(cps, 25), budget_tokens=budget,
                         scope=(getattr(ctx, "scope", None) or "knowledge"))
    # `k` is the number of blocks the caller wants back, which v2 expresses as a diversify cut.
    want_k = int(ctx.k or k or 0)
    if want_k:
        cfg.context_blocks = want_k
    wf = _workflow(cfg)

    decompose = getattr(ctx, "decompose", None)
    res = await wf.run(ctx.workspace_id, query,
                       collection_ids=list(ctx.collection_ids) if ctx.collection_ids else None,
                       decompose=rc.query_decomposition() if decompose is None
                       else ("full" if decompose else "off"))

    prov = await _provenance(ctx.workspace_id, res.blocks, query) if res.blocks else {}
    context_str, used = _serialise(res.blocks, prov)
    cites = _citations(res.blocks, prov)
    best = max((float(b.score or 0.0) for b in res.blocks), default=0.0)
    surfaces = sorted({s.split("(")[0] for b in res.blocks for s in (b.retrieved_by or [])})
    trace = _trace(res)
    if unsupported:
        trace.append({"stage": "adapter.unsupported",
                      "detail": "; ".join("%s: %s" % (n, w) for n, w in unsupported.items()),
                      "n_in": 0, "n_out": 0, "ms": 0.0})

    if res.abstained or not res.blocks:
        return {"abstained": True,
                "reason": "no candidate resembles the question",
                "context": "", "citations": [], "confidence": round(best, 3),
                "retrievers": surfaces, "stage_trace": trace,
                "engine": "retrieval",
                "unsupported": unsupported or None}

    _note_salience(getattr(ctx, "workspace_id", None), [b.node_id for b in res.blocks])

    return {"abstained": False,
            "context": context_str,
            "citations": cites,
            "caveats": [],
            "tokens_used": res.tokens or used,
            "confidence": round(best, 3),
            "retrievers": surfaces,
            "reranked": ["v2_linear"],
            "temporal": None,
            "stage_trace": trace,
            "engine": "retrieval",
            "unsupported": unsupported or None,
            "paths": [{"steps": [s.edge_type for s in p.steps], "score": round(float(sc), 4)}
                      for sc, p, _ in (res.paths or [])[:5]],
            **({"decomposition": res.steps} if getattr(res, "steps", None) else {}),
            **({"candidate_node_ids": [str(b.node_id) for b in res.blocks]}
               if getattr(ctx, "explain_retriever", False) else {})}


SURFACES = [
    {"name": "attr_embedding", "label": "Attribute vectors",
     "desc": "a vector per ATTRIBUTE (76,606 surfaces) so a field nobody anticipated stays searchable"},
    {"name": "bm25", "label": "Keyword (BM25)",
     "desc": "Postgres full-text - exact terms, IDs, codes, dates"},
    {"name": "entity", "label": "Entity resolution",
     "desc": "binds a named mention to a canonical node, and seeds graph expansion from it"},
    {"name": "fuzzy", "label": "N-gram / fuzzy", "desc": "trigram matching for typos and variants"},
    {"name": "identifier", "label": "Identifier", "desc": "exact lookup on IDs and codes"},
    {"name": "metadata", "label": "Metadata / temporal",
     "desc": "date-range and property filters, applied as predicates rather than keywords"},
    {"name": "ontology", "label": "Type-scoped lexical",
     "desc": "ranked WITHIN the types the question implicates, so a rare type is not drowned"},
    {"name": "graph", "label": "Graph beam search",
     "desc": "query-directed walk scoring relations against the question; returns PATHS as evidence"},
]
