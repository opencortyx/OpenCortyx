"""Experiment flags: the baseline is what runs when nothing is set.

The engine as it stands scores 85.67% @8k / 82.66% @5k on the 692-question LoCoMo suite, and
that is the number every change is measured against. So changes land as FLAGS, never as edits:

    WFV2_EXP=fact_contentfulness,subquery_exec python benchmarks/eval_recall.py ...

With no flag set, every guarded path takes its baseline branch and the run must reproduce the
frozen result exactly. That property is what makes a measured delta mean anything - if the
baseline drifts under the experiment, the comparison is measuring two changes at once.

Rules this module enforces:

* An UNKNOWN flag name raises. A typo that silently ran the baseline would produce a run that
  looks like "the fix does nothing", which is the same failure shape as the absent-measurement
  bugs this engine has already paid for twice (an unregistered fusion surface defaulting to
  weight 1.0; an unmeasured cosine scoring 0.0). Silence must never read as a verdict.
* Every flag is declared with what it does and which measured finding motivates it, so a run
  recorded six weeks from now still says why it existed.
* Flags are read ONCE per process and echoed by `describe()`, so a result file can state the
  condition it was produced under instead of relying on someone's memory of the command line.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Set

#: name -> why it exists. Adding a flag here is the only way to make it settable.
REGISTRY: Dict[str, str] = {
    "pool_composition":
        "TESTED, NEGATIVE - kept for the record. Oversample the net 3x and admit by measured "
        "informativeness. On 20 never-retrieved golds it moved Turn share 32.5% -> 32.9% and "
        "put ZERO of them in the pool. The premise was wrong: those turns are not crowded out, "
        "they are never MATCHED. 87% share no content term with their question at all.",
    "distilled_only":
        "TESTED, NEGATIVE - kept for the record. IMPLEMENTATION REVERTED: setting this flag now "
        "does nothing; re-add the guarded filter after fusion.merge in orchestrator._l1 to retest. "
        "Hypothesis: raw Turns take 76.1% of packed blocks and 84.3% of the TOKEN budget while the "
        "whole distilled graph (Fact/Claim/TurnAttributes/Entity) gets 9%, and a Fact costs 13.6 "
        "tok/block against a Turn's 34.5 while still COVERING its Turn for provenance - so spending "
        "the budget on the distilled layer should buy more evidence per token. It does not. Dropping "
        "raw walker content from the fused pool (structurally, by absent `__template`/`__by`/"
        "`__identity` - no type named) on the 74-question multi-hop suite, 292 evidence turns: "
        "turn-identity 75.00% -> 23.97% (-51.03), PROVENANCE 77.40% -> 44.86% (-32.53). The "
        "distilled layer does NOT substitute for the Turns it was derived from. Two further "
        "observations: Turns re-entered anyway via L2 graph expansion (still 70.4% of packed tokens "
        "with the filter ON), so the pool was degraded for nothing; and the token share was a red "
        "herring - Turns dominate the budget because they are what MATCHES. Independently "
        "reproduces `pool_composition` by a different mechanism: the pool is VOCABULARY-limited, "
        "not space-limited. Measured on the same graph: 95.4% of MISSED evidence turns share zero "
        "content terms with their question (mean overlap 0.05) vs 66.8% of covered ones (0.44).",

    "rrf_relative_rank":
        "TESTED 2026-09-04, NEGATIVE - hypothesis refuted in its correct form, do not retry. 74 "
        "multi-hop questions, 292 evidence turns. Alone: turn 75.00 -> 73.29, prov 77.40 -> 75.34 "
        "(-2.05). WITH bm25 drawn deep (--bm25-k 400), which is the form the hypothesis actually "
        "requires: turn -> 72.26, prov -> 74.32 (-3.08). Worse than either half. THE HYPOTHESIS was "
        "that RRF penalises a surface for searching deeper, since w/(k+rank) reads position as an "
        "absolute integer while the probe measured bm25 reaching 96.2% of evidence at k=400 against "
        "node_embedding's 56.0%. Normalising rank by the surface's own depth does not recover it. "
        "FIRST RUN ALSO HAD AN ERROR worth knowing: at DEFAULT config bm25 draws SHALLOWER than "
        "dense (120 candidates/question vs 230 - dense queries several representations, each "
        "drawing its own k), so the first arm boosted the weaker surface. Both arms are now "
        "measured and both are negative. WHAT THIS LEAVES: fusion is not where the loss is either. "
        "Pool at k=400 holds 1,137 distinct candidates containing 100% of evidence; 256 blocks fit "
        "in 8,000 tokens; 77.4% of gold survives that 22.5% cut - which is 3.4x better than chance, "
        "so ranking is WORKING, not broken. Getting 94% through a 22.5% cut is a hard ranking "
        "problem, not a defect to fix.",

    "fact_contentfulness":
        "Facts take 40% of the pool. Vacuous ones ('Joanna is OK', 'Joanna is excited') hold "
        "slots that a real evidence Turn would otherwise occupy.",
    "fact_turn_merge":
        "A Fact and its source Turn (median 67% word overlap) occupy two pool slots and two "
        "block budgets to say one thing. Collapse to one candidate with both provenances.",

    "query_expansion":
        "TESTED, NEUTRAL AT 3.4x COST - kept for the record, do not ship. Retrieve with an "
        "ANSWER's vocabulary: 87% of the 122 never-retrieved gold turns share ZERO content terms "
        "with their question. Three variants on the full 692: uncapped -0.73 (pool +58%, 17 "
        "questions better / 24 worse); capped but wired before widen() -2.39 (INVALID - it "
        "suppressed the widen safety net, a control-flow change masquerading as a result); "
        "capped and correctly wired -0.10 (7 better / 9 worse, 2.1s -> 7.1s). It does exactly "
        "what it was built for - Open-domain +1.54, Temporal +0.83 - and gives the same back in "
        "Multi-Hop and Single-Hop. The diagnosis holds; bridging the gap PER QUERY with a small "
        "local model does not pay. That is the argument for building the bridge once at write "
        "time instead.",

    "attr_targeted_own":
        "Score a candidate on the attributes it ACTUALLY CARRIES, not only on whether an "
        "attribute surface happened to return it. Measured on 'What nickname does Nate use for "
        "Joanna?': the gold turn carries `nickname_used: Jo`, its attribute surface ranked 496 "
        "of 72,191 (beaten by 70 rows of `nickname_used: Nate|Joanna` at ranks 5-20), so no "
        "attr_embedding evidence existed, so BOTH `attribute` and `attr_targeted` were ABSENT "
        "and the turn ranked 640. The node held the answer and the reranker never looked at it.",

    # ---- workstream 2: Multi-Hop (27 winnable turns; budget-insensitive) -------------------
    "subquery_exec":
        "decompose() currently only EMBEDS its sub-queries. Execute them as real retrievals and "
        "union the evidence; hop 2 conditioned on hop 1's retrieved answer, not on the original "
        "question - otherwise it is one query with extra embeddings.",

    # ---- workstream 3: Temporal (22 winnable turns; budget-insensitive) --------------------
    "temporal_pushdown":
        "CLASS B. A date constraint is a PREDICATE on a relevance-ranked search, not a retriever "
        "of its own. Run as its own surface it returns in-window turns from unrelated "
        "conversations (measured: 17 candidates, all from conv-49, for a conv-43 question). "
        "Pushed into bm25's WHERE clause it narrows 5,882 turns to the 23 in the window, all 3 "
        "gold turns among them. This is what every filtered-ANN store does; we were doing the "
        "opposite.",

    "aspect_diversity":
        "CLASS A. Relevance scores each passage alone, so a question whose answer spans several "
        "turns ('What recipes has Joanna made?' - seven recipes, seven turns) gives every turn a "
        "middling score and none reaches the top 30 as a group. Relevance cannot express 'these "
        "together are complete'. Selects for aspect COVERAGE instead (xQuAD / IA-Select): once a "
        "candidate is chosen, prefer candidates adding a NEW item over ones repeating a covered "
        "one. Subsumes list AND count questions - a count is a list whose items get counted.",

    "aspect_diversity_on":
        "TESTED, NEGATIVE (2026-08-28) - 282 multi-hop questions, paired, rank curve on both "
        "arms. prov@5 -1.92, prov@20 -0.68, prov@50 -2.38, turn@50 -7.58; median provenance rank "
        "WORSENED 41 -> 45; @all identical by construction. On the 154 questions it fires on: "
        "prov@5 -3.29, prov@50 -4.07. On the 39 of those already failing: prov@5 -1.95, "
        "prov@20 -1.30, and ZERO movement at @50 or @all. "
        "IT TOOK TWO INVALID RUNS TO GET A REAL NUMBER, and both failure modes are worth "
        "remembering. (1) SILENT NO-OP: diversify_aspects only reorders the list and never writes "
        "a score, while BOTH of L4's ordering paths re-derive order from the block score "
        "(`group_by_role` sorts each role by score; `_coverage_ordered` scores blocks itself). "
        "44 of 44 questions came back byte-identical while applies() fired on 55% of the suite - "
        "the third `evidence_ops` bug exactly. Fixed by carrying an ORDERED selection to L4 and "
        "writing block scores rank-preserving, the same mechanism `_operator_selected` uses. "
        "(2) BLIND METRIC: with that fixed the run STILL returned 83/83 identical, because "
        "provenance scores MEMBERSHIP over a 300-480 block window and a reordering mechanism "
        "moves gold WITHIN it. Verifying that a mechanism runs is not sufficient - the metric "
        "must be able to register the effect. eval_provenance.py now records per-evidence RANKS "
        "so recall@k is computable offline for any k from one run. "
        "WHY IT FAILS: these questions are missing 34% of their required evidence, and diversity "
        "can only reorder what was retrieved. Preferring a block that adds a new term over one "
        "that repeats a covered one trades a strong hit for a weak novel one, which is a losing "
        "trade at ranks 5-50 when the missing set members are not in the pool at all. "
        "ORIGINAL RATIONALE FOLLOWS. "
        "CLASS A, implemented in l3_rerank/aspects.py. Greedy selection for aspect COVERAGE "
        "(xQuAD / IA-Select) on SET and COUNT questions only. Gated on `Cardinality`, which L0 "
        "has always decided and nothing has ever read.",

    "temporal_prefilter":
        "FIXES A BUG: MetadataSurface applies its LIMIT before the date filter. With only a "
        "temporal constraint the SQL clause list is just the org scope, so it pulls an arbitrary "
        "k rows out of 410,000 UNORDERED and only then asks which fall in the window. Measured "
        "on 'Where was John between August 11 and 15 2023?': the surface returned 1 candidate, "
        "while 23 turns fall in that window and all 3 gold turns are among them. This pushes a "
        "coarse month/year prefilter into SQL so the LIMIT applies to rows that already match, "
        "then refines exactly in Python as before.",

    "temporal_index":
        "Dates are matched as text. Every one of the 58 first-pass audit turns carried a "
        "date_time property, and LoCoMo answers date questions through the SENT time plus a "
        "relative word ('yesterday', 'see you tomorrow') - the evidence turn often contains no "
        "date text at all. ExpansionContext already parses the constraint and has no index to "
        "spend it against.",

    # ---- workstream: Multi-Hop completeness (measured on the 1,540 run) --------------------
    "evidence_ops":
        "TESTED 2026-09-04/05, NEGATIVE - kept for the record. IMPLEMENTATION REVERTED: setting "
        "this flag now does nothing; the modules (l2_graph/evidence_ops.py, evidence_algebra.py) "
        "and the four orchestrator hooks were deleted. Re-add a guarded selector after "
        "structural() to retest. Hypothesis: selection by COMPUTATION rather than ranking - "
        "ASSEMBLE (entity, predicate, object, time) rows from the fused pool, then INTERSECT for "
        "comparison questions or SET for list questions, returning the source rows. Aimed at the "
        "75 of 85 evidence turns still missed on multi-hop (57 set/list, 15 intersection, 3 "
        "count), on the reasoning that six prior interventions failed by REORDERING a pool whose "
        "median missed evidence sits at rank 579/1,072. WHAT ACTUALLY HAPPENED, in order, because "
        "each step looked like the previous one's fix: (1) the operator selected 0 rows on a "
        "graph holding 2,319 Facts - `_assemble` read `predicate` while the cards declare "
        "`relation`; (2) with that fixed it selected mostly parse artefacts, because the Fact "
        "extractor writes pronoun subjects ('I', 'the speaker') and inverts subject/object; (3) "
        "SQL-repairing 25 pronoun subjects and 24 inversions on a 51-turn sample got it to cover "
        "gold on both test questions - the mechanism works when the data is clean, which is a "
        "statement about EXTRACTION, not about retrieval; (4) widening it to all node types, "
        "edges, edge types and stored embeddings made it WORSE, not better: on 'What are Joanna's "
        "hobbies' the property-based one-hop selector covered both gold turns and the graph walk "
        "covered neither. Every capability added broadened the draw and made the answer harder to "
        "locate. Two endpoint bugs found and fixed along the way (edge rows naming only the node "
        "REACHED, so they read 'joanna EXTRACTED_FROM Joanna'; and direction taken from the WALK "
        "rather than from e.source_id/e.target_id, inverting every upstream edge) changed the "
        "output's correctness and moved coverage zero. THE REASON IT WAS DROPPED is that it "
        "targets the wrong stage: benchmarks/surface_ceiling.py shows retrieval already reaches "
        "96.2% (bm25) / 95.2% (fuzzy) of gold evidence at k=400 and 99.7% / 98.3% at k=1500, so "
        "the draw is not what loses the evidence - fusion, ranking and packing are. An operator "
        "that computes a better SET from the pool cannot help when the pool is already nearly "
        "complete and the loss is downstream of it.",

    "no_canonical_dedup":
        "Restores the pre-2026-09-05 baseline by SKIPPING the source-turn collapse in L4. The "
        "collapse is now DEFAULT because it is the largest validated gain on this engine: "
        "68.92% -> 78.38% answer accuracy on the 74-question multi-hop suite at a SMALLER budget "
        "(6,000 vs 8,000), 71.86% on the 199-question mixed suite, measured on eval_answer's "
        "`correct AND grounded` column. Diagnosis behind it (benchmarks/stage_trace.py, "
        "benchmarks/context_recall.py): with the one-hop rule the engine's own pool, its ranking "
        "and its block builder all reach 100.00% of gold turns, and L4 optimize alone loses 5.84 "
        "points - so the packer is the only lossy stage and the fix is to stop spending its fixed "
        "budget on four representations of one turn. Set this flag to measure against the old "
        "behaviour; do not set it in production.",

    "canonical_dedup_mid":
        "The MIDDLE survivor for the source-turn collapse: the cheapest member that still clears "
        "an information floor (12 tokens), chosen structurally so no node type is named. Both "
        "extremes are measured and both fail - keeping the source Turn is readable but ~35 tok so "
        "the packer fits few (77.03% correct at 8k), keeping the cheapest is ~10 tok and "
        "unanswerable (59.46% correct despite the HIGHEST provenance, 80.82%). The floor selects "
        "the compact self-contained representation - on this corpus a subject/relation/object "
        "triple at ~14 tok. Target arithmetic: holding 95% of 484 evidence nodes inside 6,000 "
        "tokens requires ~13 tok/node against today's 45, which is exactly where the triple sits. "
        "JUDGE THIS ON eval_answer's `correct AND grounded` COLUMN, never on provenance recall - "
        "provenance is a membership measure and the cheap arm proved it moves opposite to "
        "usefulness.",

    "canonical_dedup_cheap":
        "TESTED 2026-09-05, NEGATIVE ON ANSWERING - do not ship. It has the HIGHEST "
        "provenance of any arm and the LOWEST answer accuracy: 74-question multi-hop, budget "
        "8000 - baseline 68.92% correct / 75.34% prov; keep-source 77.03% correct / 76.71% "
        "prov; KEEP-CHEAPEST 59.46% correct / 80.82% prov. Covering more distinct turns with "
        "~10-token attribute bags raises the membership metric and removes the text the model "
        "answers from; turn-identity collapsing to 43.84% was the visible tell. The packer "
        "probe that motivated this (keep-cheapest 83.6% vs keep-source 65.6% gold retained) "
        "measured gold COVERAGE, which does not predict answering. Original rationale follows. "
        "Same collapse as `canonical_dedup` but keeping the CHEAPEST member of each source-turn "
        "group instead of the source node. Measured 2026-09-05 through the real packer (cps=400, "
        "budget 8000, 20 questions, gold handed to the packer 96.7%): no dedup 316 blocks/q at "
        "25.3 tok/block -> 67.2%; keep-source 231 blocks/q at 34.6 tok/block -> 65.6%; "
        "KEEP-CHEAPEST 327 blocks/q at 24.5 tok/block -> 83.6%. Keeping the Turn makes every "
        "block FATTER, so the packer fits fewer and the collapse costs more slots than the "
        "redundancy it removes - which is why `canonical_dedup` measured only +1.37 end-to-end. "
        "Found by benchmarks/stage_trace.py, which showed the draw and the reranker lose nothing "
        "(L3 rank -0.00) while optimizer.optimize destroys 27 points in one step at cps=400. The "
        "role floors were NOT the cause - removing them changes 41 gold to 40. Block COST is. "
        "OPEN RISK: the cheapest member is often a ~10-token attribute bag, which preserves the "
        "provenance METRIC while removing the text a reader needs; validate on eval_answer's "
        "grounded column, not on provenance recall alone.",

    "canonical_dedup":
        "Collapse context blocks that are REPRESENTATIONS OF THE SAME EVIDENCE before packing, "
        "keeping the source node. Measured on pool composition first (benchmarks/pool_dedup.py, "
        "74-question multi-hop suite, union of every surface at k=400): the pool costs 18,163 "
        "tokens/question and holds 99.0% of the gold evidence; collapsed to one node per SOURCE "
        "turn it costs 13,658 tokens keeping the source itself (75.2%) or 8,869 keeping the "
        "cheapest member (48.8%) - at an unchanged 99.0% in every variant. Against an 8,000-token "
        "budget that is the difference between dropping 56% of the pool and dropping 10-41% of "
        "it. WHY THIS AND NOT A BETTER SELECTOR: three structural selectors were measured the "
        "same day and all lost to plain RRF - core-set graph density -2.41 (density collapsed to "
        "'the Turn layer' because degree proxies node type), max-coverage -13.06 (the element set "
        "saturates in ~20 picks), and the top-400 pool cut caps the ceiling at 75.9%, below the "
        "engine's own baseline. They were all competing over which 56% of a REDUNDANT pool to "
        "discard. The same turn appears as Turn + Claim + Fact + TurnAttributes, each paying its "
        "own token charge for one fact. Provenance edges are derived from the cards (from/to in "
        "{source,emitted}), never named in code, so re-authoring a card cannot silently drop a "
        "type. The SOURCE is kept rather than the cheapest member because the cheapest is the "
        "least informative - a 10-token attribute bag preserves the provenance METRIC while "
        "removing the text a reader needs, and membership is not answerability.",

    "multi_query":
        "TESTED 2026-09-04, MARGINAL - first actual run of this flag. 74-question multi-hop suite, 292 evidence turns, budget 8000, unscoped: turn-identity 75.00% -> 76.03% (+1.03), provenance 77.40% -> 77.74% (+0.34). Directionally right and the cheapest positive result on record, but inside noise for provenance. Read alongside the surface probe run the same day (benchmarks/surface_ceiling.py): bm25 ALONE reaches 63.6% of evidence at k=100 and uniquely contributes 68 of 292 turns, against node_embedding 32.0% - so widening the lexical surface with more phrasings is aimed at the right surface, it just does not reach far enough to move the number. NOTE a 26-question partial of this same run read -2.53; do not read partials for small effects. Original rationale follows. "
        "The 3 generated framings reach ONLY dense search, at HYPO_K=25. Two measurements say "
        "that is the wrong place for them: BM25 is where the unreachable evidence lives "
        "(\"probing 20 unreachable gold turns, BM25 found 16 at ranks 246-1305 while dense found "
        "3 at depth 4000\") and it is handed exactly ONE phrasing; and the root failure is "
        "vocabulary - 87% of never-retrieved gold turns share ZERO content terms with their "
        "question, which a lexical index cannot bridge from a single phrasing. So this runs every "
        "framing through bm25 at FULL depth plus the attribute surface, pooling into the same RRF "
        "fusion. Distinct from `query_expansion` (which measured -0.10 overall but Open-domain "
        "+1.54): that one only deepened DENSE retrieval with the same vectors.",

    "adaptive_truncation":
        "TESTED, STRONGLY NEGATIVE ON RECALL - stopped at 98/282 multi-hop questions because the "
        "effect was unambiguous: turn 80.43 -> 27.52, provenance 91.52 -> 49.61. It did exactly "
        "what it was built to do - 141.7 blocks and 2,486 tokens per question against ~365 and "
        "~7,932 - so ~69% of the window was saved to lose ~42 points of provenance. Not a bug and "
        "not a no-op: the trade is real and the exchange rate is ruinous ON THIS METRIC. "
        "WHY IT FAILS HERE, specifically: the multi-hop answer routinely spans 3-4 turns (mean "
        "3.13 required evidence turns, 3.67 on a failing question), and 57 of the 60 provenance "
        "failures are PARTIAL - some evidence found, the rest missing. A confidence cut keeps the "
        "single best block and discards the tail, and for a set question the tail IS the missing "
        "members. It works against the dominant failure mode rather than on it. "
        "WHAT THIS NUMBER IS NOT: evidence about answer quality. Provenance rewards packing more; "
        "a reader rewards packing less noise. The upside this mechanism exists for is invisible "
        "to a retrieval metric, which is the same divergence that cost `digest_sets` 8.71 points. "
        "If revisited it needs a far gentler tau and eval_answer.py to judge the other side - "
        "tau=0.70 is not defensible on a corpus whose answers span several turns. "
        "ORIGINAL RATIONALE FOLLOWS. "
        "SCORE-ADAPTIVE CONTEXT TRUNCATION. Keep only blocks scoring within TAU=0.70 of the best "
        "block instead of filling the 8,000-token window with whatever fits. Motivated by the "
        "packed-precision measurement: of ~365 packed blocks per question, 4.3 are gold, using "
        "105 of 7,932 tokens - 1.19% evidence. A reader LLM pays attention over all of it. "
        "RELATIVE to the top block, never an absolute 0.70: `final_score` is a weighted feature "
        "sum whose range moves with the question, and this engine has already paid for treating "
        "a score as comparable across contexts. Gated on score SPREAD >= 0.05 for the same "
        "reason rank-normalisation is - 64 near-identical cosines were once amplified into "
        "0.97/0.95/0.94 of pure noise, so a relative cut over a flat distribution discards by "
        "floating-point accident. "
        "EXPECT THIS TO LOSE RECALL. Provenance rewards packing MORE (every extra block is "
        "another chance to contain the gold node) while a reader rewards packing LESS noise; "
        "the two objectives point opposite ways here. That divergence is exactly what cost "
        "`digest_sets` 8.71 points, and a retrieval metric structurally cannot see the upside. "
        "The recall delta is therefore the COST side of this trade, and it must be read against "
        "packed precision and tokens spent - never alone. The verdict needs eval_answer.py.",

    "hop2_lexical":
        "TESTED, NEUTRAL - kept for the record, do not ship as-is. 282 multi-hop questions, "
        "paired against a SAME-DAY baseline: turn 80.43 -> 80.66, provenance 91.52 -> 91.74. "
        "Both +0.23, which is +2 evidence turns of 884, with 272 of 282 questions unchanged "
        "(6 better / 4 worse on turn). The mechanism was VERIFIED RUNNING before the number was "
        "trusted: it fired on 23 of 25 sampled questions, pooled 220 candidates it alone found, "
        "and 85 of those reached the PACKED WINDOW - so the packer saw it and this is a verdict "
        "on the idea, not another silent no-op. It fails for the reason the other six "
        "retrieve-more mechanisms failed: the packed window is 1.19% evidence, and hop2 spent "
        "~3.4 blocks per question on topically-adjacent non-evidence ('Woohoo Melanie! I passed "
        "the adoption agency interviews' for 'What did Caroline research?'). The premise that "
        "distinguishes it from `prf_set` HELD - phrase matching cannot drift, and the spans it "
        "picked were real entities (Samantha, Josh and Mark, Perseid, Amy Ellis Nutt). Being "
        "unable to drift is simply not the same as being evidence. Worth re-testing ONLY "
        "answer-side (eval_answer.py) or after extraction quality improves - a retrieval metric "
        "cannot see whether an adjacent block helps a reader. "
        "ORIGINAL RATIONALE FOLLOWS. "
        "A SECOND LEXICAL PASS on entity spans the first pass discovered, matched as exact "
        "PHRASES. Targets the one population ranking cannot reach: 23% of missed evidence is "
        "NOT_RETRIEVED, and BM25 is where the unreachable gold lives (probing 20 unreachable gold "
        "turns, BM25 found 16 at ranks 246-1305 while dense found 3 at depth 4000). "
        "DISTINCT FROM `prf_set` (-3.17), which is the reason to expect a different result: that "
        "one embedded the whole top passage and re-queried BY VECTOR, so the second pass drifted "
        "toward everything resembling the first pass. This matches discrete spans by adjacency - "
        "a phrase occurs or it does not, and there is no similarity gradient to slide down. It "
        "also attacks the measured embedding wall head-on: 'Jon lost job as a banker' vs 'Gina "
        "lost job at Door Dash' score cosine 0.525 against an unrelated pair's 0.521, but a "
        "literal 'Seattle Marathon' in two sessions is linkable lexically. Spans come from the "
        "same structural detector L0 runs on the question (no gazetteer); a span the question "
        "already named is dropped, and so is one whose LIVE document frequency exceeds 5% of the "
        "scoped corpus. Runs AFTER the widen decision, never before - counting expansion "
        "candidates into the widen total once cost 85.67 -> 83.28 by suppressing the safety net.",

    "entity_facts":
        "Multi-Hop scores 87.67% provenance and only 1% of its questions retrieve NOTHING - 27% "
        "retrieve SOME but not all of 3.13 average evidence turns. It is a completeness problem, "
        "not a finding problem, and ranking cannot assemble a set whose members sit at ranks 39, "
        "55 and 129. Facts already carry subject/predicate/object but the subject was a STRING: "
        "270 Facts said subject='Joanna' with ZERO edges to Entity(Joanna). After the HAS_FACT "
        "backfill (13,631 edges, 99.9% of Facts reachable) this pulls an entity's whole fact set "
        "in one hop. edge_scoring can never choose it: 'has fact' matches no question "
        "semantically, and the relation is now too common for the rarity term to favour.",

}

RETIRED = {
    "adaptive_budget": "packing is not the constraint - 92.2% of pooled Turns are already packaged",

    "neighbor_ctx": "NOT a failure - its target population no longer exists. Built for the "
                    "1,332 dark turns (no Fact, no attribute), 67 of them multi-hop gold. That "
                    "count was taken when claims covered only 3 of 10 conversations. Finishing "
                    "extraction took claim coverage to 5,715/5,882 (97.2%) and left 156 dark "
                    "turns, ZERO of them multi-hop gold. Verified stage-by-stage before "
                    "retiring: there is nothing left for NEXT_TURN expansion to reach.",
    "prf_set": "pseudo-relevance feedback: 84.84% (-3.17), 35 questions worse. Set members do "
               "resemble each other, but the feedback query pulls in everything resembling the "
               "TOP OF THE FIRST PASS, flooding the pool with near-duplicates.",
    "entity_scope": "conversation INFERRED from resolved entities: 82.81% (-5.20). Names recur "
                    "across conversations and duplicate Entity nodes bound to the wrong one, "
                    "excluding the right conversation. Routing it from the harness instead is "
                    "worth +1.92 - see `--scope`.",
    "entity_floor": "L4 budget share for resolved-entity evidence: 88.24% (+0.23) but "
                    "turn-identity FELL 2.6. Reserving budget REALLOCATES, it does not add: 505 "
                    "smaller blocks replaced 407 fuller ones. Metric-gaming, not retrieval.",
    "digest_sets": "synthesized (entity, predicate) list nodes: 81.22% (-8.71) with episode_ctx. "
                   "The mechanism WORKS - digests ranked 1,2,3 on set questions - but a Digest "
                   "links to nothing, so provenance scores it zero while it displaces the Facts "
                   "that do earn credit. Linking them for credit would be metric-gaming; the "
                   "honest evaluation is answer-side (eval_answer.py), not retrieval-side.",
    "episode_ctx": "session-level Episode nodes as a container for the 1,332 dark turns. Measured "
                   "together with digest_sets at -8.71; same displacement problem.",
    "entity_fusion": "SAME_AS across duplicate Entity nodes. Measured only in the -8.71 stack, so "
                     "its own effect is unknown. The underlying DUPLICATE BUG is real (~2.8 "
                     "entity nodes per fact subject) and worth fixing by merging, not aliasing.",
}

_parsed: Optional[Set[str]] = None


def _parse() -> Set[str]:
    raw = os.environ.get("WFV2_EXP", "")
    names = {p.strip() for p in raw.replace(";", ",").split(",") if p.strip()}
    unknown = names - set(REGISTRY)
    if unknown:
        raise ValueError(
            "unknown experiment flag(s): %s\nknown flags: %s"
            % (", ".join(sorted(unknown)), ", ".join(sorted(REGISTRY)))
        )
    return names


def active() -> Set[str]:
    global _parsed
    if _parsed is None:
        _parsed = _parse()
    return _parsed


def on(name: str) -> bool:
    """True when `name` is enabled. Guards every experimental branch."""
    if name not in REGISTRY:
        raise ValueError("undeclared experiment flag: %r" % name)
    return name in active()


def describe() -> str:
    a = sorted(active())
    return "BASELINE (no experiments)" if not a else "EXPERIMENTS: " + ", ".join(a)


def reset_for_tests() -> None:
    global _parsed
    _parsed = None
