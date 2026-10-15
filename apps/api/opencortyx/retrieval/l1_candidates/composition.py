"""L1 — pool composition: spend candidate slots on candidates that can serve as evidence.

THE MEASUREMENT THIS EXISTS FOR

The bucket audit of the 138 baseline misses found 128 winnable turns, and **122 of them never
entered the candidate pool at all** — only 4 were pooled and buried. So the loss is candidate
generation, not ranking and not packing. Re-running 25 of those questions and counting what the
pool was made of:

    node type   share of pool   packaged / pooled
    Fact             40.1%           65.2%
    Turn             25.2%           92.2%
    Claim            18.9%           26.2%
    KeyNode           8.2%            0.1%
    Entity            6.5%            9.6%

Two facts decide the design. Ninety-two percent of the Turns that reach the pool are packaged, so
packing is nearly free — the context builder is not the constraint. And a third of the pool is
spent on material that produces 7.6% of the packaged context; KeyNode spends 1,121 slots across
25 questions to place ONE block.

WHY THIS IS NOT A TYPE QUOTA

The obvious fix is a per-type budget: cap KeyNode, cap Claim, let Turn run free. That is
`_TYPE_ORDER = {"Turn": 1, ...}` rebuilt, the exact construct this engine bans, and it fails the
same way — it encodes one corpus's type names as if they were a property of retrieval. A corpus
that calls its turns `Message`, or that keeps its evidence in `Fact`, gets silently demoted.

So admission is decided by a MEASURED property of each candidate instead: `informativeness`, the
share of a candidate's own content that the question did not already supply. It never asks what a
node is. A bare `KeyNode` holding a key, an `Entity` whose whole text is "Joanna", and a `Claim`
that restates its source turn all score low for the same reason — they add nothing beyond the
words already in the question — while a turn carrying an answer scores high. The type table falls
out of the measurement rather than being asserted ahead of it.

HOW IT KEEPS RECALL

Filtering alone would only shrink the pool, and a smaller pool cannot contain more turns. The
surfaces are therefore asked for OVERSAMPLE times as many candidates first, and admission spends
that larger draw on candidates that can carry an answer. The pool ends up about the size it was;
what changes is what is in it.

Nothing is discarded for being uninformative alone: candidates that fail the floor are DEFERRED,
and if the admitted set underfills the target they are taken back in fused-rank order. A floor
that could empty the pool would be a recall gamble, and this engine has already paid for one of
those (`rerank_pool = 400`).
"""
from __future__ import annotations

from typing import Optional

from opencortyx.retrieval.contracts import CandidateGraph, QueryPlan

#: How many times the baseline net to draw before admission. Three is enough to change the mix
#: substantially (Turns are a quarter of the baseline pool) without making L1 I/O dominate.
OVERSAMPLE = 3

MIN_INFORMATIVENESS = 0.30


def admit(graph: CandidateGraph, plan: Optional[QueryPlan],
          target: Optional[int] = None, floor: float = MIN_INFORMATIVENESS) -> tuple:
    """Return (pruned graph, stats). Preserves candidate identity, evidence and fused scores.

    `target` defaults to the size the baseline draw would have produced, so the pool this returns
    is comparable to the baseline pool and the experiment measures COMPOSITION rather than size.
    """
    from opencortyx.retrieval.l3_rerank.features import informativeness

    ordered = graph.ranked()
    if target is None:
        target = max(1, round(len(ordered) / OVERSAMPLE))

    admitted, deferred = [], []
    for cand in ordered:
        score = informativeness(cand, plan)
        cand.features["informative"] = score
        (admitted if score >= floor else deferred).append(cand)

    kept = admitted[:target]
    if len(kept) < target:
        # Underfilled: take the deferred back in fused order rather than return a thin pool.
        kept = kept + deferred[:target - len(kept)]

    out = CandidateGraph()
    for cand in kept:
        out.candidates[cand.node_id] = cand
    out.edges = list(graph.edges)
    out.seeds = list(graph.seeds)

    stats = {
        "drawn": len(ordered),
        "target": target,
        "kept": len(out.candidates),
        "passed_floor": len(admitted),
        "deferred": len(deferred),
        "refilled": max(0, len(kept) - min(len(admitted), target)),
    }
    return out, stats
