"""The workflow: L0 understanding → L1 candidates → L2 graph → L3 rerank → L4 context.

    question
       │
    L0 plan ──────────────► intent, mentions, constraints, time, strategy weights, hop budget
       │                    node/edge TYPE hypotheses, scored against the corpus's own vocabulary
       │
    L1 candidates ────────► embedding (node + per-attribute) · bm25 · identifier · fuzzy · metadata
       │                    └─ RRF fusion, evidence preserved per surface
       │
    L2 graph ─────────────► relation relevance COMPUTED vs the query → beam search → paths
       │
    L3 rerank ────────────► 12 named features → intent-shaped scoring → MMR diversity
       │
    L4 context ───────────► roles + provenance → dedupe/merge → budget
       │
    RetrievalResult

EVERY LAYER REPORTS. `LayerTrace` records what entered, what left, and how long it took, because
the stated objective is to measure each layer independently. A pipeline that reports only its
final number cannot tell a recall problem from a ranking problem — the distinction that took a
full day of forensics to establish on the previous engine.

FAILURE POLICY: A LAYER THAT FAILS DEGRADES, IT DOES NOT EMPTY THE RESULT. If graph expansion
raises, the candidates from L1 still flow to L3. The previous engine had the opposite property in
places — a filter that came back short returned silently, and the symptom was indistinguishable
from "there is no such data".
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from opencortyx.retrieval.contracts import (
    CandidateGraph, ContextBlock, LayerTrace, QueryPlan, RetrievalResult,
)
from opencortyx.retrieval.l0_understanding import expansion
from opencortyx.retrieval.l0_understanding.planner import (
    build_plan, query_representations,
)
from opencortyx.retrieval.l0_understanding.vocabulary import (
    TypeVocabulary, load_vocabulary,
)
from opencortyx.retrieval.l0_understanding import policy as policy_mod
from opencortyx.retrieval.l0_understanding.intent import Intent

from opencortyx.core.substrate import NodeRepo, window_open
from opencortyx.retrieval import experiments
from opencortyx.retrieval.l1_candidates import composition, fusion
from opencortyx.retrieval.l1_candidates import hop2 as hop2_mod
from opencortyx.retrieval.l1_candidates.embedding import EmbeddingRetrieval
from opencortyx.retrieval.l1_candidates.entity_resolver import (
    EntityResolver,
)
from opencortyx.retrieval.l1_candidates.lexical import (
    BM25Surface, FuzzySurface, IdentifierSurface, MetadataSurface, OntologySurface,
)
from opencortyx.retrieval.l2_graph import aggregate as aggregate_mod
from opencortyx.retrieval.l2_graph.beam import BeamConfig, BeamSearch
from opencortyx.retrieval.l2_graph.edge_scoring import EdgeScorer
from opencortyx.retrieval.l2_graph.expansion import ExpansionContext
from opencortyx.retrieval.l2_graph import structural
from opencortyx.retrieval.l3_rerank import aspects, cross_encoder, reranker
from opencortyx.retrieval.l3_rerank.features import annotate
from opencortyx.retrieval.l4_context import builder, optimizer


@dataclass
class WorkflowConfig:
    """Every bound in one place, named for what it protects.

    A constant whose name describes a COST must never also decide RECALL — that conflation is what
    let a cross-encoder's budget silently cap how much evidence could survive.
    """
    candidates_per_surface: int = 120     # recall net per surface
    bm25_k: int = 0
    fusion_cut: int = 0                   # 0 = no cut; fusion produces a full ordering
    rerank_pool: int = 0
    seed_count: int = 12                  # graph seeds taken from fused candidates
    context_blocks: int = 0               # 0 = budget decides, not a count
    budget_tokens: int = 8000
    enable_graph: bool = True
    collection_ids: Optional[list] = None
    scope: str = "knowledge"
    cross_encoder: bool = False
    cross_encoder_pool: int = 30          # a COST bound; never a recall bound
    cross_encoder_model: Optional[str] = None


@dataclass
class _Timer:
    trace: list = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter)

    def mark(self, layer: str, stage: str, n_in: int = 0, n_out: int = 0,
             detail: str = "", **extra) -> None:
        now = time.perf_counter()
        self.trace.append(LayerTrace(layer=layer, stage=stage, detail=detail,
                                     n_in=n_in, n_out=n_out,
                                     ms=round((now - self._t0) * 1000, 1), extra=extra))
        self._t0 = now


async def _default_complete(prompt, max_tokens=200, temperature=0.0, model=None):
    from opencortyx.core.inference.llm import complete as _c
    return await _c(prompt, max_tokens=max_tokens, temperature=temperature, model=model)


_MID_FLOOR_TOKENS = 12


def _as_of(plan) -> str:
    """The moment the question is about: a time it names, else today.

    A statement carries the window it is true in, and one whose window has closed is history, not an
    answer — but a question about the past needs exactly those statements, so a temporal constraint
    moves the moment rather than disabling the check."""
    for constraint in (getattr(plan, "temporal", None) or []):
        for bound in (constraint.end, constraint.start):
            if bound:
                return str(bound)
    return datetime.now(timezone.utc).date().isoformat()


class RetrievalWorkflow:
    """Runs the five layers. Constructed with the embedding callables so it stays testable."""

    def __init__(self, embed, embed_batch, config: Optional[WorkflowConfig] = None,
                 complete=None):
        self.cfg = config or WorkflowConfig()
        self._embed = embed
        self._embed_batch = embed_batch
        self._complete = complete or _default_complete
        self.repo = NodeRepo()
        self.embedding = EmbeddingRetrieval(embed, repo=self.repo)
        self.bm25 = BM25Surface(self.repo)
        self.identifier = IdentifierSurface(self.repo)
        self.fuzzy = FuzzySurface(embed, self.repo)
        self.metadata = MetadataSurface(self.repo)
        self.ontology = OntologySurface(self.repo)
        self.hop2 = hop2_mod.Hop2LexicalSurface(self.repo)
        self.resolver = EntityResolver(embed, self.repo)
        self.edge_scorer = EdgeScorer(embed_batch)

    # ── L0 ───────────────────────────────────────────────────────────────────────────────────
    def understand(self, query: str, t: _Timer,
                   vocab: Optional[TypeVocabulary] = None) -> QueryPlan:
        plan = build_plan(query, vocab)
        t.mark("L0", "plan", n_out=len(plan.strategies),
               detail=f"intent={plan.intent.value} hops={plan.max_hops} "
                      f"mentions={len(plan.mentions)} temporal={len(plan.temporal)} "
                      f"types={len(plan.node_types)}n/{len(plan.edge_types)}e",
               intent=plan.intent.value, notes=plan.notes,
               node_types=plan.node_types, edge_types=plan.edge_types)
        return plan

    async def vocabulary(self, workspace_id: uuid.UUID, t: _Timer) -> Optional[TypeVocabulary]:
        """The corpus's type names, for L0 to score the question against.

        Failure here must not stop the query. Without a vocabulary the plan carries no type
        hypotheses and every other signal is unchanged, which is a measurably weaker plan but a
        working one — the same degrade-do-not-empty rule every other layer follows.
        """
        try:
            vocab = await load_vocabulary(workspace_id)
            t.mark("L0", "vocabulary", n_out=len(vocab.node_types) + len(vocab.edge_types),
                   detail=vocab.summary())
            return vocab
        except Exception as exc:
            t.mark("L0", "vocabulary", detail=f"unavailable: {type(exc).__name__}: {exc}")
            return None

    # ── L1 ───────────────────────────────────────────────────────────────────────────────────
    async def generate(self, workspace_id: uuid.UUID, plan: QueryPlan, t: _Timer,
                       collection_ids=None) -> CandidateGraph:
        reps = query_representations(plan)
        hypo_reps: dict[str, str] = {}
        if experiments.on("query_expansion") or experiments.on("multi_query"):
            if experiments.on("multi_query"):
                hyps = await expansion.rephrasings(plan.raw_query, self._complete)
            else:
                hyps = await expansion.hypotheticals(plan.raw_query, self._complete)
            for i, h in enumerate(hyps):
                hypo_reps[f"hypo{i}"] = h
            t.mark("L0", "expand", n_out=len(hyps), detail=" | ".join(h[:60] for h in hyps))
        qvecs: dict[str, list] = {}
        for name, text in reps.items():
            if not text:
                continue
            try:
                v = await self._embed(text)
                if v:
                    qvecs[name] = v
            except Exception:
                continue
        hvecs: dict[str, list] = {}
        for name, text in hypo_reps.items():
            try:
                v = await self._embed(text)
                if v:
                    hvecs[name] = v
            except Exception:
                continue
        t.mark("L1", "embed_query", n_out=len(qvecs) + len(hvecs),
               detail=",".join(list(qvecs) + list(hvecs)))

        try:
            spec = await self.embedding.load_specificity(workspace_id)
            if spec:
                t.mark("L1", "attr_specificity", n_out=len(spec),
                       detail=" ".join(f"{a}={v:.2f}" for a, v in
                                       sorted(spec.items(), key=lambda kv: -kv[1])[:6]))
        except Exception as exc:
            t.mark("L1", "attr_specificity", detail=f"unavailable: {type(exc).__name__}")

        k = self.cfg.candidates_per_surface
        if experiments.on("pool_composition"):
            k = k * composition.OVERSAMPLE
        lists: list[list] = []
        # Passed per QUERY, not read from shared config: three queries run concurrently, so
        # mutating cfg would leak one query's scope into another.
        cid = collection_ids if collection_ids is not None else self.cfg.collection_ids

        async def run(name: str, coro):
            try:
                out = await coro
            except Exception as exc:                      # a surface that fails must not empty the result
                t.mark("L1", f"surface:{name}", detail=f"FAILED {type(exc).__name__}: {exc}")
                return
            lists.append(out or [])
            t.mark("L1", f"surface:{name}", n_out=len(out or []))

        async def sweep(allowed) -> None:
            """Run the surfaces `allowed` permits. Called twice at most: once for the plan's
            chosen set, and once more for the skipped set if that set underfilled."""
            wants_attr = "attr_embedding" in allowed
            if qvecs and wants_attr:
                await run("embedding", self.embedding.retrieve(workspace_id, qvecs, k=k, collection_ids=cid))
            if "entity" in allowed and plan.mentions:
                await run("entity", self._resolve(workspace_id, plan, t, collection_ids=cid))
            if "bm25" in allowed:
                await run("bm25", self.bm25.retrieve(
                    workspace_id, plan.normalized_query,
                    k=self.cfg.bm25_k or k, collection_ids=cid,
                    temporal=plan.temporal))
            if "identifier" in allowed:
                await run("identifier", self.identifier.retrieve(
                    workspace_id, plan.normalized_query, k=min(k, 50), collection_ids=cid))
            if "fuzzy" in allowed and plan.mentions:
                await run("fuzzy", self.fuzzy.retrieve(
                    workspace_id, [m.text for m in plan.mentions], k=min(k, 50),
                    collection_ids=cid))
            if "metadata" in allowed and (plan.temporal or plan.attributes):
                dlgs = await self._entity_dialogues(workspace_id, plan)
                await run("metadata", self.metadata.retrieve(
                    workspace_id, temporal=plan.temporal, attributes=plan.attributes,
                    k=k * 2, collection_ids=cid, scope_dialogues=dlgs or None))
            if "ontology" in allowed and plan.node_types:
                await run("ontology", self.ontology.retrieve(
                    workspace_id, plan.normalized_query, node_types=plan.node_types,
                    k=min(k, 60), collection_ids=cid))

        async def sweep_multi_query() -> None:
            """Every generated framing through the LEXICAL and ATTRIBUTE surfaces too.

            The variants exist already but only ever reach dense search, shallow. Two measurements
            say that is the wrong place for them:

              * BM25 is where the unreachable evidence lives - "probing 20 unreachable gold turns,
                BM25 found 16 at ranks 246-1305 while dense found 3 at depth 4000" - and it is
                handed exactly ONE phrasing, `plan.normalized_query`.
              * The root failure is vocabulary: 87% of never-retrieved gold turns share ZERO
                content terms with their question. A lexical index cannot bridge that from one
                phrasing, and a rephrasing is precisely a second set of terms to match on.

            So each framing is run at FULL depth against bm25 and the attribute surface, and the
            results pool into the same RRF fusion as everything else."""
            if not hypo_reps:
                return
            texts = list(hypo_reps.values())[:expansion.N_HYPOTHESES]
            coros = []
            if "bm25" in policy.run or "bm25" in policy.skipped:
                for i, txt in enumerate(texts):
                    coros.append(run("mq_bm25_%d" % i, self.bm25.retrieve(
                        workspace_id, txt, k=expansion.HYPO_K * 2, collection_ids=cid,
                        temporal=plan.temporal)))
            if hvecs:
                coros.append(run("mq_attr", self.embedding.retrieve(
                    workspace_id, hvecs, k=expansion.HYPO_K * 2, collection_ids=cid)))
            if coros:
                await asyncio.gather(*coros)

        async def sweep_hop2() -> None:
            """Hop 2: the entities the FIRST PASS found, searched as exact phrases.

            Distinct from `prf_set` (-3.17) in the one way that matters. That mechanism embedded
            the top passage and re-queried by vector, so its second pass drifted toward whatever
            resembled the first. A phrase either occurs in a node or it does not -- there is no
            gradient to drift along, so its failure mode is unavailable here.

            The target is the population ranking cannot touch: 23% of missed evidence never
            enters the pool, and it is BM25 that reaches it (16 of 20 unreachable golds at ranks
            246-1305, against dense finding 3 at depth 4000)."""
            spans = hop2_mod.discover_spans(lists, [m.text for m in plan.mentions])
            if not spans:
                t.mark("L1", "hop2", detail="no unasked entity spans in the top of the pass")
                return
            probe = [s for s, _ in spans[:hop2_mod.MAX_SPANS * 3]]
            try:
                df = await self.hop2.document_frequency(workspace_id, probe, collection_ids=cid)
            except Exception as exc:
                t.mark("L1", "hop2", detail=f"df probe FAILED {type(exc).__name__}: {exc}")
                return
            kept, dropped = [], []
            for span, support in spans:
                if span not in df:
                    continue
                (kept if df[span] <= hop2_mod.DF_CEILING else dropped).append(
                    (span, support, df[span]))
                if len(kept) >= hop2_mod.MAX_SPANS:
                    break
            if not kept:
                t.mark("L1", "hop2", n_in=len(spans),
                       detail="every span above the %.0f%% df ceiling: %s" % (
                           100 * hop2_mod.DF_CEILING,
                           " ".join("%s=%.3f" % (s, d) for s, _, d in dropped[:6])))
                return
            before = sum(len(x) for x in lists)
            await run("hop2_lexical", self.hop2.retrieve(
                workspace_id, [s for s, _, _ in kept], k=hop2_mod.HOP2_K, collection_ids=cid))
            t.mark("L1", "hop2", n_in=len(spans), n_out=sum(len(x) for x in lists) - before,
                   detail="phrases " + " ".join(
                       "%s(x%d,df=%.3f)" % (s, sup, d) for s, sup, d in kept) +
                          (" | dropped " + " ".join(s for s, _, _ in dropped[:4]) if dropped
                           else ""))

        policy = plan.policy
        await sweep(policy.run)
        if policy.skipped:
            t.mark("L1", "policy", n_out=len(policy.run),
                   detail=f"ran {sorted(policy.run)} | skipped {sorted(policy.skipped)}",
                   skipped=policy.skipped, reason=policy.reason)

        if experiments.on("multi_query"):
            before_mq = sum(len(x) for x in lists)
            await sweep_multi_query()
            t.mark("L1", "multi_query", n_in=len(hypo_reps), n_out=sum(len(x) for x in lists),
                   detail="%d framings through bm25 + attribute surface (+%d candidates)"
                          % (len(hypo_reps), sum(len(x) for x in lists) - before_mq))

        found = sum(len(x) for x in lists)
        should_widen, why = policy_mod.widen(policy, found)
        if should_widen:
            # The safety net that makes narrowing safe rather than a recall gamble: being wrong
            # about the intent costs a second pass of latency, never the answer.
            policy.widened = True
            await sweep(set(policy.skipped))
            t.mark("L1", "widen", n_in=found, n_out=sum(len(x) for x in lists), detail=why)

        if experiments.on("hop2_lexical") or (plan and plan.intent in (Intent.MULTI_HOP, Intent.RELATIONSHIP)):
            await sweep_hop2()

        graph = fusion.merge(lists)
        fusion.rrf(graph, weights=plan.strategies)
        if experiments.on("pool_composition"):
            graph, comp = composition.admit(graph, plan)
            t.mark("L1", "compose", n_in=comp["drawn"], n_out=comp["kept"],
                   detail=("target=%(target)d passed_floor=%(passed_floor)d "
                           "deferred=%(deferred)d refilled=%(refilled)d" % comp), **comp)
        report = fusion.surface_report(graph)
        t.mark("L1", "fuse", n_in=sum(len(x) for x in lists), n_out=len(graph),
               detail=" ".join(f"{s}:{r['n']}({r['unique']}u)" for s, r in report.items()),
               surfaces=report)
        return graph

    async def _resolve(self, workspace_id: uuid.UUID, plan: QueryPlan, t: _Timer,
                       collection_ids=None) -> list:
        """Bind every mention to a canonical node, and record the binding ON THE PLAN.

        The resolutions are stored in `plan.entities` as well as returned as candidates, because
        they are two different things to two different consumers: candidates are retrievable
        evidence for fusion, while `plan.entities` is what L2 seeds from and what an answer cites
        when it says which "Nate" it meant.
        """
        resolved = await self.resolver.resolve_all(workspace_id, plan.mentions, collection_ids=collection_ids)
        plan.entities = resolved
        bound = [r for r in resolved if r.resolved]
        ambiguous = [r for r in bound if r.alternatives]
        t.mark("L1", "resolve", n_in=len(plan.mentions), n_out=len(bound),
               detail=" ".join(f"{r.mention.text}->{r.label or '?'}"
                               f"({'+'.join(r.matched_by)},{r.confidence:.2f})"
                               for r in resolved) or "no mentions",
               ambiguous=[r.mention.text for r in ambiguous])
        return self.resolver.to_candidates(resolved)

    # ── L2 ───────────────────────────────────────────────────────────────────────────────────
    async def expand(self, workspace_id: uuid.UUID, plan: QueryPlan, graph: CandidateGraph,
                     t: _Timer) -> CandidateGraph:
        if not self.cfg.enable_graph or not len(graph):
            t.mark("L2", "skipped", detail="graph disabled or no candidates")
            return graph
        if not plan.policy.allows("graph"):
            t.mark("L2", "skipped", n_in=len(graph),
                   detail=f"policy: {plan.policy.skipped.get('graph', 'not implicated')}")
            return graph
        try:
            bound = [r.node_id for r in plan.entities if r.resolved]
            fused = [c.node_id for c in graph.ranked()[: self.cfg.seed_count]]
            seeds, seen = [], set()
            for nid in bound + fused:
                if nid not in seen:
                    seen.add(nid)
                    seeds.append(nid)
            seeds = seeds[: max(self.cfg.seed_count, len(bound))]
            if not seeds:
                t.mark("L2", "skipped", detail="no seeds")
                return graph
            if bound:
                t.mark("L2", "seeds", n_out=len(seeds),
                       detail=f"{len(bound)} resolved entity seed(s) ahead of "
                              f"{len(seeds) - len(bound)} fused")

            from opencortyx.retrieval.l2_graph.edge_scoring import (
                profile_relations_for,
            )
            profiles = await profile_relations_for(workspace_id, [str(s) for s in seeds])
            qvec = await self._embed(plan.normalized_query or plan.raw_query)
            edge_rel = await self.edge_scorer.score_relations(workspace_id, qvec, profiles=profiles)
            t.mark("L2", "edge_scoring", n_in=len(seeds), n_out=len(edge_rel.scores),
                   detail=" ".join(f"{r}={s}" for r, s in edge_rel.top(5)),
                   top=edge_rel.top(8))
            if not edge_rel.scores:
                return graph

            beam = BeamSearch(BeamConfig(max_hops=plan.max_hops))
            notes: list = []
            # What the QUESTION demands of the nodes walked to: its own content words, the time
            # bounds it stated, the field it asked for. Built once and applied per hop.
            ctx = ExpansionContext.from_plan(plan)
            found = await beam.run(workspace_id, seeds, edge_rel,
                                   on_trace=lambda s, d: notes.append(d),
                                   out_edges=graph.edges, ctx=ctx)
            graph.seeds = list(seeds)
            for c in found:
                graph.upsert(c)
            fusion.rrf(graph, weights=plan.strategies)
            aware = []
            if ctx.terms:
                aware.append(f"node({len(ctx.terms)}terms)")
            if ctx.temporal:
                aware.append(f"temporal(-{ctx.rejected_temporal})")
            if ctx.attribute_targets:
                aware.append(f"attr({','.join(sorted(ctx.attribute_targets))})")
            t.mark("L2", "beam", n_in=len(seeds), n_out=len(found),
                   detail=f"hops={plan.max_hops} discovered={len(found)} "
                          f"edges={len(graph.edges)}"
                          + (f" aware={'+'.join(aware)}" if aware else " aware=edge-only"),
                   hops=notes, out_of_range=ctx.rejected_temporal)
        except Exception as exc:
            t.mark("L2", "failed", detail=f"{type(exc).__name__}: {exc}")
        return graph

    async def structural(self, workspace_id: uuid.UUID, graph: CandidateGraph,
                         t: _Timer, plan: Optional[QueryPlan] = None,
                         collection_ids=None, scope_dialogues=None) -> CandidateGraph:
        """Edges `edge_scoring` cannot choose, applied deterministically. Flag-gated, off by
        default: with no flag set this is a no-op and the baseline reproduces exactly."""
        if not len(graph):
            return graph
        try:
            n = await structural.topological_expansion(workspace_id, graph, plan=plan, collection_ids=collection_ids)
            if n:
                t.mark("L2", "topological_expansion", n_out=n, detail=f"one-hop topology from seeds ({n} nodes)")
        except Exception as exc:
            t.mark("L2", "topological_expansion_failed", detail=f"{type(exc).__name__}: {exc}")

        if scope_dialogues:
            keep, drop = {}, 0
            allow = set(scope_dialogues)
            for nid, c in graph.candidates.items():
                d = (c.properties or {}).get("dialogue_id")
                if d and d not in allow:
                    drop += 1
                    continue
                keep[nid] = c
            if drop and keep:
                graph.candidates = keep
                t.mark("L2", "scope", n_in=drop + len(keep), n_out=len(keep),
                       detail="restricted to %s (dropped %d)" % (",".join(sorted(allow)), drop))
        fusion.rrf(graph)
        return graph

    def _apply_validity(self, graph: CandidateGraph, plan, t) -> CandidateGraph:
        """Drop statements whose validity window does not contain the moment the question is about.

        Applied AFTER expansion, for the reason `_apply_scope` is: a statement retired last year
        still hangs off the entity it describes, so filtering only the seed search lets graph
        expansion pull it straight back in as a neighbour — which is how a closed window ends up
        cited as current truth.
        """
        as_of = _as_of(plan)
        keep = {nid: c for nid, c in graph.candidates.items() if window_open(c.properties, as_of)}
        closed = len(graph.candidates) - len(keep)
        if not closed:
            return graph
        graph.candidates = keep
        graph.seeds = [s for s in graph.seeds if s in keep]
        graph.edges = [e for e in graph.edges if e.from_id in keep and e.to_id in keep]
        t.mark("L2", "validity", n_in=len(keep) + closed, n_out=len(keep),
               detail="as of %s; dropped %d statement(s) whose window had closed" % (as_of, closed))
        return graph

    # ── L3 ───────────────────────────────────────────────────────────────────────────────────
    async def rank(self, plan: QueryPlan, graph: CandidateGraph, t: _Timer) -> list:
        pool = fusion.cut(graph, self.cfg.rerank_pool)
        # The GRAPH is passed, not just the pool: `connectivity` is a property of a candidate's
        # position among the others, and it is the one signal a flat list cannot produce.
        annotate(pool, plan, graph=graph)
        ranked = reranker.rerank(pool, plan)

        if experiments.on("aspect_diversity_on") and aspects.applies(plan):
            before = [str(c.node_id) for c in ranked[:30]]
            ranked = aspects.diversify_aspects(ranked, plan)
            moved = sum(1 for a, b in zip(before, [str(c.node_id) for c in ranked[:30]]) if a != b)
            self._aspect_selected = [str(c.node_id) for c in ranked[:aspects.DEPTH]]
            t.mark("L3", "aspects", n_in=len(ranked), n_out=moved,
                   detail="cardinality=%s reordered %d of the top 30, %d carried to L4"
                          % (plan.cardinality.value, moved, len(self._aspect_selected)))

        if self.cfg.cross_encoder:
            ce = await cross_encoder.score(
                plan.normalized_query or plan.raw_query, ranked,
                pool=self.cfg.cross_encoder_pool, model_id=self.cfg.cross_encoder_model)
            n = cross_encoder.apply(ce, ranked)
            if n:
                ranked = reranker.rerank(ranked, plan)
            t.mark("L3", "cross_encoder", n_in=min(len(ranked), self.cfg.cross_encoder_pool),
                   n_out=n, detail=(f"scored {n} in {ce.ms}ms" if n else
                                    f"unavailable: {ce.error} — ranking on the other features"))

        dangling = len(graph.dangling_edges())
        t.mark("L3", "rerank", n_in=len(graph), n_out=len(ranked),
               detail=(f"pool={len(pool)} best={ranked[0].rerank_score:.3f} "
                       f"edges={len(graph.edges)}(-{dangling} outside)" if ranked else "empty"))
        return ranked

    async def _entity_dialogues(self, workspace_id: uuid.UUID, plan) -> list:
        """Which conversations the resolved entities belong to. [] when nothing resolved."""
        ids = [r.node_id for r in (plan.entities or []) if getattr(r, "node_id", None)]
        if not ids:
            return []
        try:
            nodes = await self.repo.get_many(workspace_id, ids)
        except Exception:
            return []
        return sorted({str(n.properties["dialogue_id"]) for n in nodes if (n.properties or {}).get("dialogue_id")})

    # ── L4 ───────────────────────────────────────────────────────────────────────────────────

    async def _canonical_dedup(self, workspace_id, blocks: list, t: _Timer,
                               keep: str = "source") -> list:
        """Collapse blocks that are REPRESENTATIONS OF THE SAME EVIDENCE, keeping the source.

        Measured 2026-09-05 (benchmarks/pool_dedup.py, 74-question multi-hop suite): the union
        pool costs 18,163 tokens/question and holds 99.0% of the gold. Collapsing it to one node
        per SOURCE turn costs 13,658 tokens keeping the source itself - 75.2% of the budget for
        exactly the same 99.0%. The pool was never badly ordered; the same turn simply appears as
        Turn + Claim + Fact + TurnAttributes, each paying its own token charge for one fact. That
        is why three structural selectors (core-set density, max-coverage, top-400 pooling) all
        lost to plain RRF: they were choosing which 56% of a redundant pool to discard.

        WHICH EDGES MEAN "derived from" IS DECLARED, NEVER NAMED HERE. A card's emit says `from`/
        `to` as `source` (the node it ran over) and `emitted` (what it produced); any card-declared
        edge between those two ends is a provenance edge, whichever way it points. That admits
        CLAIM_FROM, HAS_FACT, EXTRACTED_FROM, MENTIONS, HAS_ATTRIBUTES and HAS_TEMPORAL_REF without
        this code knowing any of their names, and correctly EXCLUDES RELATES_TO, whose card
        declares entity->entity. A hardcoded pair here would silently drop every Fact the day a
        card is re-authored.

        The SOURCE is kept rather than the cheapest member: the cheapest is the least informative
        (a 10-token attribute bag), which preserves the provenance METRIC while removing the text
        a reader needs. Membership is not answerability.
        """
        if not blocks or workspace_id is None:
            return blocks
        src_is_source = await provenance_edge_types()
        if not src_is_source:
            return blocks
        edges = await self.repo.edges(workspace_id, node_ids=[b.node_id for b in blocks], types=list(src_is_source))
        owner: dict = {}
        for edge in edges:
            etype, sid, tid = edge.type, str(edge.source_id), str(edge.target_id)
            if src_is_source.get(etype):
                src, emitted = sid, tid
            else:
                src, emitted = tid, sid
            owner[emitted] = src
        groups: dict = {}
        for b in blocks:
            nid = str(b.node_id)
            groups.setdefault(owner.get(nid, nid), []).append(b)
        kept, dropped, saved = [], 0, 0
        for src, members in groups.items():
            if len(members) == 1:
                kept.append(members[0])
                continue
            if keep == "mid":
                ok = [m for m in members if (m.tokens or 0) >= _MID_FLOOR_TOKENS]
                rep = min(ok, key=lambda m: (m.tokens or 1)) if ok else                     max(members, key=lambda m: (m.tokens or 0))
            elif keep == "cheapest":
                rep = min(members, key=lambda m: (m.tokens or 1))
            else:
                rep = next((m for m in members if str(m.node_id) == src), None)
                if rep is None:
                    rep = max(members, key=lambda m: (m.tokens or 0))
            # the survivor inherits the best score in its group, so collapsing never demotes
            rep.score = max((m.score or 0.0) for m in members)
            kept.append(rep)
            dropped += len(members) - 1
            saved += sum((m.tokens or 0) for m in members if m is not rep)
        t.mark("L4", "canonical_dedup", n_in=len(blocks), n_out=len(kept),
               detail="keep=%s; collapsed %d duplicate representations, freeing %d tokens"
                      % (keep, dropped, saved))
        return kept

    @staticmethod
    def _resolved_ids(plan: QueryPlan) -> set:
        """The nodes L1 established the question NAMES, as strings."""
        return {str(r.node_id) for r in (plan.entities or [])
                if getattr(r, "resolved", False) and r.node_id}

    async def contextualize(self, plan: QueryPlan, ranked: list,
                            t: _Timer, workspace_id=None) -> tuple[list, int]:
        pinned = self._resolved_ids(plan)
        if self.cfg.context_blocks:
            kept = reranker.diversify(ranked, self.cfg.context_blocks)
            if pinned:
                have = {str(c.node_id) for c in kept}
                missing = [c for c in ranked if str(c.node_id) in pinned and str(c.node_id) not in have]
                if missing:
                    # in at the head, and the tail gives up the same number of slots
                    kept = missing + kept[: max(0, len(kept) - len(missing))]
                    t.mark("L4", "pin_resolved", n_in=len(pinned), n_out=len(missing),
                           detail="%d resolved seed(s) restored after diversify" % len(missing))
            ranked = kept
            t.mark("L4", "diversify", n_out=len(ranked))
        blocks = builder.build(ranked, plan)
        t.mark("L4", "build", n_in=len(ranked), n_out=len(blocks))
        asel = getattr(self, "_aspect_selected", None)
        if asel:
            best = max((b.score or 0.0) for b in blocks) if blocks else 1.0
            order = {nid: i for i, nid in enumerate(asel)}
            step = max(best, 1.0) * (optimizer.COVERAGE_BONUS + 0.05)
            base = best * (1.0 + optimizer.COVERAGE_BONUS) + 1.0
            n = 0
            for b in blocks:
                i = order.get(str(b.node_id))
                if i is not None:
                    b.score = base + (len(asel) - i) * step
                    n += 1
            t.mark("L4", "aspect_blocks", n_out=n,
                   detail="%d coverage-selected blocks promoted above all, step=%.3f" % (n, step))
            self._aspect_selected = []

        keep = "source"
        if experiments.on("canonical_dedup_cheap"):
            keep = "cheapest"
        elif experiments.on("canonical_dedup_mid"):
            keep = "mid"
        if not experiments.on("no_canonical_dedup"):
            blocks = await self._canonical_dedup(workspace_id, blocks, t, keep=keep)
        if pinned and blocks:
            top = max((b.score or 0.0) for b in blocks)
            n_pin = 0
            for b in blocks:
                if str(b.node_id) in pinned:
                    b.score = top + 1.0
                    n_pin += 1
            if n_pin:
                t.mark("L4", "promote_resolved", n_out=n_pin,
                       detail="%d resolved seed block(s) placed above every other block" % n_pin)
        selected = optimizer.optimize(blocks, self.cfg.budget_tokens, plan=plan)
        density = optimizer.evidence_density(selected)
        coverage = optimizer.evidence_coverage(selected, plan)
        t.mark("L4", "optimize", n_in=len(blocks), n_out=len(selected),
               detail=f"{density['tokens']}tok {density['tokens_per_block']}tok/block "
                      f"{density['distinct_sources']}src "
                      f"cover={coverage['covered']}/{coverage['of']} "
                      f"({coverage['term_coverage']:.0%}) spread={coverage['source_spread']}",
               density=density, coverage=coverage)
        return selected, density["tokens"]

    # ── run ──────────────────────────────────────────────────────────────────────────────────
    # ── aggregation ──────────────────────────────────────────────────────────────────────────
    async def aggregate(self, workspace_id: uuid.UUID, plan: QueryPlan,
                        vocab, t: _Timer):
        """Compute a counting question instead of retrieving a sample of it.

        Returns the result, or None when this is not an aggregation or the corpus cannot ground
        it. Grounding nothing must fall back to ordinary retrieval: a zero from a query that
        matched no rows reads identically to a zero that is the true answer, and only one of
        those is honest.
        """
        spec = aggregate_mod.parse(plan, vocab)
        if spec is None:
            return None
        if not spec.grounded:
            t.mark("L2", "aggregate", detail=f"not grounded: {spec.why} — falling back to retrieval")
            return None
        try:
            result = await aggregate_mod.execute(workspace_id, spec, scope=self.cfg.scope)
        except Exception as exc:
            t.mark("L2", "aggregate", detail=f"FAILED {type(exc).__name__}: {exc}")
            return None
        if not result.usable:
            t.mark("L2", "aggregate", detail=f"inconclusive: {result.note} — falling back")
            return None
        t.mark("L2", "aggregate", n_out=result.groups_total or 1,
               detail=f"{spec.describe()} -> "
                      f"{result.scalar if result.scalar is not None else f'{result.groups_total} group(s)'}"
                      f" in {result.ms}ms")
        return result

    _MEMORY_FLAG = "__memory"          # core.substrate.CONVERSATION_FLAG - stamped by session capture

    def _apply_scope(self, graph: CandidateGraph, t) -> CandidateGraph:
        """Keep only the corpus `cfg.scope` names.

        Applied AFTER expansion, deliberately. Scoping only the seed search leaves the hole the
        scope contract warns about: app-memory turns hang off the same shared hubs the corpus does
        (a Date node, an Entity), so a clean seed set pulls them straight back in as neighbours.
        Filtering the assembled graph closes seeds, expansion and structural in one place.

        Until now the v2 engine read no scope at all - `retrieval.adapter._UNSUPPORTED`
        said so plainly - so `knowledge` and `conversations` returned the SAME nodes. Measured on
        this corpus: 10 of 160 packed blocks for one question were the agent's own captured
        queries, occupying budget that gold evidence needed.

        The flag is written by capture and carried on every candidate, so this is a row predicate,
        not a type or id list: nothing here names a node type.
        """
        scope = (self.cfg.scope or "all").strip().lower()
        if scope == "all" or not graph.candidates:
            return graph
        want_memory = scope == "conversations"
        keep, dropped = {}, 0
        for nid, c in graph.candidates.items():
            is_mem = str((c.properties or {}).get(self._MEMORY_FLAG, "")).lower() == "true"
            if is_mem == want_memory:
                keep[nid] = c
            else:
                dropped += 1
        if not dropped:
            return graph
        graph.candidates = keep
        graph.seeds = [s for s in graph.seeds if s in keep]
        # An edge whose endpoint was dropped is no longer a traversal between two live nodes.
        graph.edges = [e for e in graph.edges
                       if e.from_id in keep and e.to_id in keep]
        t.mark("L2", "scope", n_in=len(keep) + dropped, n_out=len(keep),
               detail="scope=%s; dropped %d %s node(s)"
                      % (scope, dropped, "ingested" if want_memory else "app-memory"))
        return graph

    async def run(self, workspace_id: uuid.UUID, query: str,
                  scope_dialogues: Optional[list] = None,
                  collection_ids: Optional[list] = None) -> RetrievalResult:
        """`scope_dialogues` is ROUTING metadata, not an answer hint: which memory to search.

        Every published LoCoMo number is produced per-conversation - systems ingest one
        conversation as one user's memory and query that memory ("top-20 relevant memories
        retrieved for each user"). This corpus was ingested into a SINGLE collection, so every
        query searched all ten conversations and 29% of the packed window held turns that could
        not be evidence. Passing the conversation is what makes the task the same one the
        published numbers measure.

        Distinct from `entity_scope`, which INFERRED the conversation from resolved entities and
        regressed 6 points: names recur across conversations and duplicate Entity nodes made the
        inference bind to the wrong one, excluding the right conversation entirely."""
        t = _Timer()
        vocab = await self.vocabulary(workspace_id, t)
        plan = self.understand(query, t, vocab)

        agg = await self.aggregate(workspace_id, plan, vocab, t)
        if agg is not None:
            # The answer is a computation over everything, not a ranking of a few things. Adding
            # a retrieved sample alongside it would invite the model to count the sample.
            block = ContextBlock(node_id=uuid.uuid4(), role="aggregation", text=agg.render(),
                                 node_type=agg.spec.target_type,
                                 retrieved_by=["aggregation"], score=1.0)
            block.tokens = max(1, len(block.text) // 4)
            t.mark("L4", "aggregate_block", n_out=1, detail=agg.spec.describe())
            return RetrievalResult(plan=plan, graph=CandidateGraph(), blocks=[block],
                                   trace=t.trace, tokens=block.tokens, aggregation=agg)

        graph = await self.generate(workspace_id, plan, t, collection_ids=collection_ids)
        if collection_ids:
            t.mark("L1", "scope", detail="collection-scoped: %s" % ",".join(
                str(c)[:8] for c in collection_ids))
        graph = await self.expand(workspace_id, plan, graph, t)
        graph = await self.structural(workspace_id, graph, t, plan=plan,
                                      collection_ids=collection_ids,
                                      scope_dialogues=scope_dialogues)
        graph = self._apply_scope(graph, t)
        graph = self._apply_validity(graph, plan, t)
        ranked = await self.rank(plan, graph, t)

        abstain = reranker.should_abstain(ranked)
        if abstain:
            t.mark("L3", "abstain", detail="no candidate resembles the question")

        blocks, tokens = await self.contextualize(plan, ranked, t, workspace_id=workspace_id)

        # Routes as first-class results. For a relationship or multi-hop question the connection
        # IS the answer, and a list of endpoints does not contain it.
        top_paths = reranker.rank_paths(graph, plan, limit=10)
        if top_paths:
            t.mark("L4", "paths", n_out=len(top_paths),
                   detail=" | ".join("->".join(s.edge_type for s in p.steps) + f"={sc:.3f}"
                                     for sc, p, _ in top_paths[:3]))

        return RetrievalResult(plan=plan, graph=graph, blocks=blocks, trace=t.trace,
                               tokens=tokens, abstained=abstain, paths=top_paths)


_provenance_cache: dict = {}


async def provenance_edge_types() -> dict[str, bool]:
    """Edge types that mean "derived from", per extraction card, and whether the edge's source end
    is the provenance source. Read from the extraction templates, cached for a minute."""
    import time as _time
    cached = _provenance_cache.get("types")
    if cached and _time.monotonic() - cached[0] < 60:
        return cached[1]
    from sqlalchemy import text as _sql
    from opencortyx.core.db import V2ReadSession
    async with V2ReadSession() as db:
        rows = (await db.execute(_sql(
            "select distinct e->>'type', e->>'from' "
            "  from opencortyx_v2.extraction_template t, "
            "       jsonb_array_elements(coalesce(t.emit->'edges', '[]'::jsonb)) e "
            " where e->>'type' is not null "
            "   and (e->>'from') in ('source','emitted') "
            "   and (e->>'to')   in ('source','emitted') "
            "   and (e->>'from') <> (e->>'to')"))).all()
    types = {r[0]: (r[1] == "source") for r in rows if r[0]}
    _provenance_cache["types"] = (_time.monotonic(), types)
    return types


def format_trace(result: RetrievalResult) -> str:
    """Per-layer report. This is the artefact that makes each layer independently measurable."""
    lines = []
    for tr in result.trace:
        lines.append(f"  {tr.layer:<3} {tr.stage:<18} in={tr.n_in:<5} out={tr.n_out:<5} "
                     f"{tr.ms:>7.1f}ms  {tr.detail}")
    return "\n".join(lines)
