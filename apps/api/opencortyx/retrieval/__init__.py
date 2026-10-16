"""Retrieval: understanding a question, finding evidence for it, and answering with citations.

One package, because it was two and the split did not describe anything. `retrieve/` held the
product surface — answer, rerank, trust, memory — and `retrieval/` (before the merge) held the staged
engine those were built on, so every change to retrieval touched both and neither could be read
alone.

    l0_understanding/  what is being asked: intent, constraints, entities, vocabulary, the plan
    l1_candidates/     evidence: lexical + embedding + entity resolution, fused
    l2_graph/          expansion, beam, edge scoring, aggregation
    l3_rerank/         features, cross-encoder, learned rerank
    l4_context/        pack what survived into the model's budget
    orchestrator.py    runs L0 to L4

    adapter.py         build_context_v2() — serves the contract the product calls
    answer.py          grounded answer + per-sentence grounding verification
    trust.py           confidence gate, citations, caveats, abstain
    rerank.py          the cross-encoder / LLM cascade
    memory.py          conversation-scoped working memory and scratchpad
    structured.py      the structured-data domain and its retriever
    temporal.py        recency and time-anchor parsing
"""
