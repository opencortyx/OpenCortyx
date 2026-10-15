"""Trust layer — turns retrieved nodes into a grounded, cited package, or ABSTAINS.

This is the no-hallucination guarantee: if nothing is grounded above threshold, we refuse
rather than guess. Every included node carries a citation back to its source.
"""
from opencortyx.core.config import ABSTAIN_SCORE_THRESHOLD, MIN_NODE_CONFIDENCE, settings


def _relevance(n) -> float:
    """The 0-1 signal the abstain decision should use: the native retrieval score (cosine), and the
    reranker score when present — whichever is HIGHER. NOT the tiny RRF fusion score.

    Critically we take the MAX, not the rerank alone: a reranker (especially the LLM fallback used when
    the cross-encoder isn't installed) may miscalibrate and score a clearly-relevant passage near 0.
    Rerank is allowed to PROMOTE relevance for ordering, but must never bury a confidently-retrieved
    passage below the abstain floor — that would silently refuse to answer a question whose answer was
    actually retrieved (the exact failure we hit on "Who is <known entity>?")."""
    native = float(n.meta.get("relevance", n.score) or 0.0)
    r = n.meta.get("rerank")
    if r is not None:
        return max(float(r), native)
    return native


def filter_and_decide(scored_nodes: list, threshold: float | None = None):
    """Drop low-confidence nodes; decide whether to abstain. Returns (kept, abstain, best).
    `threshold` (from the caller) overrides the default abstain score."""
    thr = threshold if threshold is not None else ABSTAIN_SCORE_THRESHOLD
    kept = [n for n in scored_nodes if n.confidence >= MIN_NODE_CONFIDENCE]
    best = max((_relevance(n) for n in kept), default=0.0)
    abstain = best < thr or not kept
    return kept, abstain, best


_GROUND_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by", "is", "was", "were",
    "are", "be", "been", "has", "have", "had", "it", "its", "this", "that", "there", "their", "they",
    "as", "at", "from", "was", "did", "does", "do", "he", "she", "his", "her", "them", "which",
}


def _lexically_grounded(sentence: str, blocks: list, need: float = 0.6) -> bool:
    """Is this sentence's CONTENT literally present in some context block?

    Cosine alone is the wrong instrument for structured context. A graph node's text is
    `Payment PAY9001. payment_id=PAY9001; method=Credit Card; status=Completed` — a key=value dump with
    almost no sentence shape — so "John Doe paid with a Credit Card" scored under the 0.55 grounding
    floor even though the answer is quoting the context verbatim. The whole answer then counted as
    ungrounded and the assistant abstained on data it had read correctly.

    Token containment is the honest complement: if most of a sentence's content words appear in the
    context, the sentence is supported by it — which is what grounding means. This can only ADD
    evidence, never remove it, so a hallucination cosine would have caught is still caught.

    Checked per-block FIRST, then against the blocks COMBINED. The per-block test alone was too strict
    for a graph answer, which is composed ACROSS nodes by construction: "John Doe ordered a Keyboard
    and two Laptop Mice" takes the customer from one node and the items from another, and NEITHER node
    contains most of that sentence. The better the graph does its job of spreading facts over
    connected nodes, the more certainly a correct answer failed this test — so a true answer was
    discarded precisely because it used the graph properly.

    The combined pass is not a licence to hallucinate. The tokens must still all be PRESENT in the
    context the model was actually handed — it cannot introduce a value that is nowhere in the
    evidence — and per-block cosine still runs first as the semantic check. What it stops is
    penalising an answer for joining two facts that were both given to it.
    """
    import re as _re
    toks = [t for t in _re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,}", (sentence or "").lower())
            if t not in _GROUND_STOP]
    if len(toks) < 2:
        return False
    for b in blocks:
        low = (b or "").lower()
        hit = sum(1 for t in toks if t in low)
        if hit / len(toks) >= need:
            return True
    # composed-across-blocks: the union of everything the model was shown
    combined = " ".join((b or "") for b in blocks).lower()
    if combined:
        hit = sum(1 for t in toks if t in combined)
        if hit / len(toks) >= need:
            return True
    return False


_SUPPORT_STOP = _GROUND_STOP | {
    "under", "over", "into", "about", "also", "than", "then", "what", "who", "whom", "whose", "when",
    "where", "why", "how", "not", "but", "can", "could", "would", "should", "will", "all", "any",
}


def supporting_citations(answer_text: str, citations: list, cited_ids=()) -> set:
    """The citations the answer actually rests on, out of everything retrieval packed.

    Retrieval hands the model a dozen blocks; an answer usually uses two or three. Listing all of
    them as "sources" told the reader that "Vendor key-person dependency" supported an answer about
    a settlement-mismatch risk. So the answer's content words are covered greedily: take the block
    that states the most of them, then the block that states the most of what is LEFT, and stop when
    no block adds at least two more. That is the smallest set of blocks that between them say what
    the answer says — the project record, the risk record, the lead's record — and nothing that only
    resembles the question. Blocks the model cited by marker are always kept.

    Returns node ids. Empty when nothing could be attributed (a paraphrase that shares no words);
    the caller then shows everything rather than claim no source.
    """
    import re as _re

    def words(s):
        return {w for w in _re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) >= 3 and w not in _SUPPORT_STOP}

    remaining = words(_re.sub(r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]", " ", answer_text or ""))
    keep = {str(i) for i in cited_ids or ()}
    for c in citations or []:
        if str(c.get("node_id")) in keep:
            remaining -= words(c.get("text"))
    pool = [(str(c.get("node_id")), words(c.get("text"))) for c in citations or []
            if str(c.get("node_id")) not in keep]
    while remaining and pool:
        best = max(pool, key=lambda p: len(p[1] & remaining))
        gain = best[1] & remaining
        if len(gain) < 2:
            break
        keep.add(best[0])
        remaining -= gain
        pool.remove(best)
    return keep


_MAX_BLOCK_CHARS = settings.GROUNDING_MAX_BLOCK_CHARS


def _fit_blocks(blocks: list) -> list:
    """Cut oversized blocks into embedder-sized pieces, preserving all of their content.

    Splitting rather than truncating matters: a caller that hands over one very large block (a whole
    packaged context, say) would otherwise have everything past the window silently dropped, and a
    correct answer grounded in that tail would be rejected as a fabrication.
    """
    out = []
    for b in blocks:
        b = b or ""
        if not b.strip():
            continue
        if len(b) <= _MAX_BLOCK_CHARS:
            out.append(b)
        else:
            out.extend(b[i:i + _MAX_BLOCK_CHARS] for i in range(0, len(b), _MAX_BLOCK_CHARS))
    return out


#: How many context blocks are embedded per call in `verify_grounding`. See the comment inside it:
#: this bounds the peak, and the peak used to be the whole context.
GROUNDING_EMBED_BATCH = settings.GROUNDING_EMBED_BATCH
GROUNDING_MAX_BLOCKS = settings.GROUNDING_MAX_BLOCKS


_CITATION_MARK = None
_NUMBER = None


def _numbers(text: str) -> set:
    """The numbers a text states, normalised so formatting is not a disagreement: `1,000` = `1000`,
    `07` = `7`, `60.0` = `60`. Citation markers (`[15]`, `[1, 2]`) are not claims and are dropped."""
    import re
    global _CITATION_MARK, _NUMBER
    if _NUMBER is None:
        _CITATION_MARK = re.compile(r"\[\s*\d+(?:\s*,\s*\d+)*\s*\]")
        _NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
    out = set()
    for raw in _NUMBER.findall(_CITATION_MARK.sub(" ", text or "")):
        tok = raw.replace(",", "").rstrip(".")
        try:
            val = float(tok)
        except ValueError:
            continue
        out.add(str(int(val)) if val.is_integer() else repr(val))
    return out


def unsupported_numbers(sentence: str, context_numbers: set) -> set:
    """Numbers a sentence asserts that no retrieved evidence contains. A sentence about the right
    topic with an invented figure ("45 days of annual leave") is similar enough by cosine to pass as
    grounded; a number is a checkable fact, so it is checked."""
    return _numbers(sentence) - context_numbers


async def verify_grounding(answer_text: str, context_blocks: list, threshold: float = 0.55):
    """Phase 3 — real citation verification. Embed each answer sentence and each context block;
    a sentence is grounded when its max cosine similarity to some block ≥ threshold, OR when its
    content words are literally present in one (see `_lexically_grounded`).
    Returns (grounded: bool, unsupported: list[str])."""
    import re
    from opencortyx.core.inference.embedding import embed_batch

    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", answer_text or "") if len(s.strip()) > 12]
    if not sents and (answer_text or "").strip():
        # A short answer ("24 days") has no sentence long enough to judge on its own, and was
        # reported ungrounded for that alone. The whole answer is the claim, so it is what is checked.
        sents = [answer_text.strip()]
    if not sents or not context_blocks:
        return (False, sents)
    context_blocks = list(context_blocks or [])[:GROUNDING_MAX_BLOCKS]
    _combined = " ".join((b or "") for b in context_blocks)[:_MAX_BLOCK_CHARS]
    _fitted = _fit_blocks(context_blocks)
    _cands = _fitted + ([_combined] if len(_fitted) > 1 else [])
    block_vecs = []
    for _i in range(0, len(_cands), GROUNDING_EMBED_BATCH):
        block_vecs.extend(v for v in await embed_batch(_cands[_i:_i + GROUNDING_EMBED_BATCH]) if v)
    if not block_vecs:
        return (False, sents)

    def _cos(a, b):
        num = sum(x * y for x, y in zip(a, b))
        da = sum(x * x for x in a) ** 0.5
        db_ = sum(y * y for y in b) ** 0.5
        return num / (da * db_) if da and db_ else 0.0

    unsupported = []
    context_numbers = _numbers(" ".join(b or "" for b in context_blocks))
    # Use embed_batch for answer sentences to avoid N sequential HTTP calls which causes massive latency
    sent_vecs = await embed_batch(sents)
    for s, sv in zip(sents, sent_vecs):
        best = max((_cos(sv, bv) for bv in block_vecs), default=0.0) if sv else 0.0
        if unsupported_numbers(s, context_numbers) or (
                best < threshold and not _lexically_grounded(s, context_blocks)):
            unsupported.append(s)

    return (len(unsupported) == 0, unsupported)


async def verify_grounding_judge(query: str, answer_text: str, context: str, model_spec=None) -> tuple[bool, list]:
    """LLM-judge grounding ('entailment' mode): ask a capable model which answer sentences are NOT
    directly supported by the context. Catches wrong-but-on-topic claims that cosine similarity misses.
    One model call (not per sentence). Falls back to cosine verification on any error."""
    import re
    import json as _json
    from opencortyx.core.inference.llm import complete
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", answer_text or "") if len(s.strip()) > 12]
    if not sents:
        return (True, [])
    prompt = ("You are a strict grounding checker. Given CONTEXT and an ANSWER, return any ANSWER "
              "sentences that are NOT directly supported by the context (invented, wrong, or not stated "
              "there).\n\n"
              f"CONTEXT:\n{context[:6000]}\n\nANSWER:\n{answer_text}\n\n"
              'Return ONLY JSON: {"unsupported": ["<verbatim sentence>", ...]}')
    try:
        raw = await complete(prompt, system="Return only valid JSON.", max_tokens=400,
                             temperature=0.0, json_mode=True, model_spec=model_spec)
        obj = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        unsup = [u for u in (obj.get("unsupported") or []) if isinstance(u, str) and u.strip()]
        return (len(unsup) == 0, unsup)
    except Exception:
        return await verify_grounding(answer_text, context.split("\n\n"))
