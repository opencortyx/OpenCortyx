"""Running a decomposition: each sub-question through the whole L0-L4 workflow, then one context.

    wave 1:  the question as asked  +  every step that waits on nothing      (side by side)
    bridge:  the model reads each finished step's answer from its context
    wave 2:  the steps that waited, with the answers written in               (side by side)
    ...
    merge:   one context, the final steps' blocks first, then the question's, then the rest

THE QUESTION AS ASKED ALWAYS RUNS. A decomposition is the model's guess at a plan, and a small
model guesses wrong in ways that cannot be checked before searching: a step that asks the wrong
direction ("which RFC obsoletes 9260" for "which RFC 9260 obsoletes") finds a real document that is
not the answer. Keeping the plain search in the merge means a wrong plan costs context space, not
the evidence a single search would have found.

A CHAIN THAT LOSES ITS ANSWER STOPS. When a step's context does not hold its answer, the steps that
needed it are not run with the placeholder left in: "Who wrote #1?" searched literally finds
authors of everything. What was found so far is merged as it is.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from opencortyx.retrieval.contracts import LayerTrace, RetrievalResult
from opencortyx.retrieval.decomposition import model
from opencortyx.retrieval.decomposition.steps import Decomposition

Retrieve = Callable[[str], Awaitable[RetrievalResult]]


def _mark(trace: list, stage: str, t0: float, detail: str = "", n_out: int = 0) -> float:
    now = time.perf_counter()
    trace.append(LayerTrace(layer="D", stage=stage, detail=detail, n_out=n_out,
                            ms=round((now - t0) * 1000, 1)))
    return now


def merge(results: list[RetrievalResult], budget_tokens: int, max_blocks: int = 0) -> tuple[list, int]:
    """Interleave the results' blocks by rank, first list first, each node once, within the budget.

    Round-robin rather than by score: each list was scored against a DIFFERENT question, so a score
    from one says nothing about a block from another. Taking each list's best in turn is what puts
    both compared documents, or both ends of a chain, into the context."""
    lists = [list(r.blocks or []) for r in results]
    seen: set = set()
    out: list = []
    used = 0
    depth = max((len(blocks) for blocks in lists), default=0)
    for rank in range(depth):
        for blocks in lists:
            if rank >= len(blocks):
                continue
            b = blocks[rank]
            if b.node_id in seen:
                continue
            cost = int(getattr(b, "tokens", 0) or 0)
            if out and budget_tokens and used + cost > budget_tokens:
                continue
            seen.add(b.node_id)
            out.append(b)
            used += cost
            if max_blocks and len(out) >= max_blocks:
                return out, used
    return out, used


async def execute(plan: Decomposition, retrieve: Retrieve, complete: model.Complete,
                  budget_tokens: int, max_blocks: int = 0) -> RetrievalResult:
    trace: list[LayerTrace] = []
    t0 = time.perf_counter()
    t0 = _mark(trace, "decompose", t0, n_out=len(plan.steps),
               detail="%s: %s" % (plan.source, " | ".join(
                   "%d. %s" % (s.index, s.text) for s in plan.steps)))

    done: dict[int, RetrievalResult] = {}
    answers: dict[int, str] = {}
    pending = list(plan.steps)
    original: RetrievalResult | None = None
    first = True
    while pending:
        ready = [s for s in pending if all(d in answers for d in s.depends_on)]
        if not ready:
            break
        for s in ready:
            s.asked = s.resolve(answers)
        jobs = [retrieve(s.asked) for s in ready]
        if first:
            jobs.append(retrieve(plan.question))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        if first:
            got = results.pop()
            original = got if isinstance(got, RetrievalResult) else None
            first = False
        for s, res in zip(ready, results):
            pending.remove(s)
            if isinstance(res, RetrievalResult):
                done[s.index] = res
        t0 = _mark(trace, "steps", t0, n_out=len(ready), detail=" | ".join(
            "%d. %s -> %d blocks" % (s.index, s.asked, len(done[s.index].blocks)
                                      if s.index in done else 0) for s in ready))

        # Read the answers the waiting steps need, and only those: a comparison's steps are all
        # final, and reading an answer nobody uses costs a model call for nothing.
        needed = {d for s in pending for d in s.depends_on}
        to_read = [s for s in ready if s.index in needed and s.index in done]
        if to_read:
            read = await asyncio.gather(*[model.answer(s.asked, done[s.index].blocks, complete)
                                          for s in to_read])
            for s, a in zip(to_read, read):
                s.answer = a
                if a:
                    answers[s.index] = a
            t0 = _mark(trace, "bridge", t0, n_out=sum(1 for a in read if a), detail=" | ".join(
                "#%d = %s" % (s.index, s.answer or "(not found)") for s in to_read))

    if pending:
        t0 = _mark(trace, "stopped", t0, detail="not run, an answer they need was not found: "
                   + " | ".join("%d. %s" % (s.index, s.text) for s in pending))

    # Final steps first (what the question asks for), then the question as asked, then the steps
    # that only led there.
    needed_by_others = {d for s in plan.steps for d in s.depends_on}
    final = [done[s.index] for s in plan.steps if s.index in done and s.index not in needed_by_others]
    middle = [done[s.index] for s in plan.steps if s.index in done and s.index in needed_by_others]
    ordered = final + ([original] if original else []) + middle
    if not ordered:
        # Every search raised. Say so by raising, as a single search would have.
        raise RuntimeError("every decomposed search failed")
    blocks, tokens = merge(ordered, budget_tokens, max_blocks)
    _mark(trace, "merge", t0, n_out=len(blocks),
          detail="%d result(s) -> %d blocks, %d tokens" % (len(ordered), len(blocks), tokens))

    base = original or ordered[0]
    return RetrievalResult(
        plan=base.plan, graph=base.graph, blocks=blocks, trace=list(base.trace or []) + trace,
        tokens=tokens, abstained=all(r.abstained for r in ordered) or not blocks,
        paths=base.paths, aggregation=None,
        steps=[{"index": s.index, "question": s.text, "asked": s.asked or "",
                "answer": s.answer, "blocks": len(done[s.index].blocks) if s.index in done else 0,
                "ran": s.index in done} for s in plan.steps])

