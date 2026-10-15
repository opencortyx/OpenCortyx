# ADR-003: Hybrid retrieval over one graph

## Status
Accepted

## Context
Vector search alone misses exact identifiers, relationships spread across several documents,
aggregations, and time-bounded questions. A single retrieval method cannot serve all question
shapes well.

## Decision
Retrieval is a staged workflow (`retrieval`):

1. **Understanding**: classify the question (lookup, semantic, relationship, multi-hop, temporal,
   aggregation), extract entity mentions and constraints, and choose which retrieval surfaces run.
2. **Candidates**: run the chosen surfaces over the same graph: node embeddings, attribute
   embeddings, BM25 full-text, identifiers, fuzzy matching, metadata filters, entity resolution and
   graph traversal.
3. **Fusion and reranking**: combine ranked lists with reciprocal rank fusion, then rerank
   (cross-encoder when installed, otherwise the LLM).
4. **Context**: deduplicate representations of the same evidence, keep sources, and pack blocks
   into the model's context budget.
5. **Answer**: generate with citations, verify sentences against the retrieved context, and
   abstain when grounding is insufficient.

Extractors (declarative templates) build entities, claims, relationships and community summaries
during ingestion, so graph-based surfaces have structure to use.

## Consequences
- Each question type runs only the surfaces that help it, which keeps latency and token cost down.
- Every answer can be traced to the nodes that supported it.
- Quality depends on extraction quality, which depends on the model in use.
