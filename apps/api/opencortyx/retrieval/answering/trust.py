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



# ── citations: each sentence against the sources it cites ───────────────────────────────────────
#
# `verify_grounding` asks "is this sentence supported by SOMETHING in the context?". That is not what
# a reader is told: a sentence marked [2] says source 2 supports it. So each sentence is checked
# against the blocks it cites, ONE AT A TIME; several cited blocks together count only when the
# graph links them (the answer may join a customer's record to their order's, not to some other
# order's). A sentence whose citation does not support it is re-attributed to the block that does,
# when one does, and the citation is reported as wrong. Checks, strongest available first:
#
#   numbers    every number the sentence states is in the block(s) supporting it - not merely
#              somewhere in the context ("$50,000 limit in Berlin" fails on Munich's 50,000)
#   nli        the workspace's verification model (core/inference/nli.py): the block entails the
#              sentence and does not contradict it - catches "did not approve" vs "approved"
#   words      without a verification model: most of the sentence's content words are in the block
#   similarity ...or the sentence is close to the block by embedding
#
# A sentence with no content words of its own ("Yes.", a bare list marker) is not judged.

_SENT_SPLIT = None
_MARKS = None


def _sentence_split(text: str) -> list[str]:
    """Sentences, with bullets and numbered list items as sentences of their own."""
    import re
    global _SENT_SPLIT
    if _SENT_SPLIT is None:
        _SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+\s*(?:[-*•]\s+|\d+[.)]\s+)?|\n+")
    out = []
    for part in _SENT_SPLIT.split(text or ""):
        part = (part or "").strip()
        if part:
            out.append(part)
    return out


def _marks(sentence: str) -> tuple[str, list[int]]:
    """(the sentence without citation markers, the citation numbers it carries)."""
    import re
    global _MARKS
    if _MARKS is None:
        _MARKS = re.compile(r"\[\s*(\d+(?:\s*,\s*\d+)*)\s*\]")
    nums = []
    for m in _MARKS.finditer(sentence or ""):
        nums.extend(int(x) for x in re.findall(r"\d+", m.group(1)))
    clean = _MARKS.sub(" ", sentence or "")
    return re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"[ \t]{2,}", " ", clean)).strip(), list(dict.fromkeys(nums))


def _content_words(sentence: str) -> list[str]:
    """The words a sentence says something with: lower case, no stop words, and no full stop or
    hyphen left on the end ("LIC-1007." is the id LIC-1007)."""
    import re
    words = (t.rstrip(".-") for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,}", (sentence or "").lower()))
    return [t for t in words if len(t) > 2 and t not in _GROUND_STOP]


def _words_in(sentence: str, text: str, need: float = 0.6) -> bool:
    toks = _content_words(sentence)
    if len(toks) < 2:
        return False
    low = (text or "").lower()
    return sum(1 for t in toks if t in low) / len(toks) >= need


def _names_in(sentence: str) -> set:
    """The words a sentence NAMES things by: capitalised words that do not start it ("Berlin",
    "DBO-1", "Acme"). A supporting source must contain each of them - most words matching is not
    enough when the one that differs is the city or the customer. Scripts without case have none,
    and fall back to numbers and the other checks."""
    import re
    words = re.findall(r"[^\W\d_][\w.-]*", sentence or "")
    out = set()
    for i, w in enumerate(words):
        if i == 0:
            continue
        if any(ch.isupper() for ch in w[:1]) and len(w) > 1:
            out.add(w.lower().rstrip(".-"))
    return out


#: Characters of a block the verification model reads at once (about its 512-token window).
_NLI_WINDOW = 1200


def _windows_for(sentence: str, text: str, keep: int = 3) -> list[str]:
    """What the verification model reads `sentence` against in a block: the block's `keep`
    sentences that share the most content words with it, ONE AT A TIME, and those together.

    One sentence at a time is the point. Read against a whole passage, "Yes, sublicences are
    permitted" was not flagged as contradicting "Sublicensing is not permitted under any licence
    type", because a dozen other sentences diluted it; read against that sentence alone, it is."""
    import re
    text = text or ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if len(p.strip()) > 3]
    if len(parts) <= 1:
        return [text[:_NLI_WINDOW]]
    toks = _content_words(sentence)

    def overlap(p: str) -> int:
        low = p.lower()
        return sum(1 for t in toks if t in low or (len(t) > 6 and t[:6] in low))
    best = sorted(parts, key=lambda p: -overlap(p))[:keep]
    together = " ".join(p for p in parts if p in best)[:_NLI_WINDOW]
    return list(dict.fromkeys([*best, together]))


def _contradicted(score) -> bool:
    """A block contradicts a sentence when some part of it does and NO part of it states it: one
    unrelated sentence the model reads as opposed must not overrule the sentence that says it."""
    return bool(score) and score.get("contradict", 0.0) >= 0.5 and score.get("entail", 0.0) < 0.5


def _linked(ids: list, links: set) -> bool:
    """Whether the blocks `ids` form one connected group in the graph's links among packed nodes."""
    ids = [str(i) for i in ids]
    if len(ids) < 2:
        return True
    seen, todo = {ids[0]}, [ids[0]]
    while todo:
        cur = todo.pop()
        for other in ids:
            if other not in seen and frozenset((cur, other)) in links:
                seen.add(other)
                todo.append(other)
    return len(seen) == len(ids)


async def verify_citations(answer: str, citations: list, texts: dict, links=(), *, threshold: float = 0.55,
                           nli_model: str | None = None, embed=None) -> dict:
    """Check every sentence of `answer` (citation markers `[n]` index `citations`) against the full
    text of the blocks (`texts`: node_id -> text). `links`: pairs of node ids the graph connects.

    Returns {"sentences": [{text, cited, supported_by, supported, check, reason}], "supported",
    "unsupported" (sentence texts), "citations_checked", "citations_supporting", "citation_precision",
    "checked_with"}."""
    links = {frozenset((str(a), str(b))) for a, b in (links or ())}
    ids = [str(c.get("node_id")) for c in citations or []]
    body = {i + 1: texts.get(nid) or (citations[i].get("text") or "") for i, nid in enumerate(ids)}
    numbers = {n: _numbers(t) for n, t in body.items()}
    sentences = []
    for raw in _sentence_split(answer):
        clean, cited = _marks(raw)
        cited = [n for n in cited if n in body]
        judged = bool(_content_words(clean)) and (len(_content_words(clean)) >= 2 or _numbers(clean))
        sentences.append({"text": clean, "raw": raw, "cited": cited, "judge": judged})

    use_nli = nli_model is not None
    sim: dict = {}
    want = [s["text"] for s in sentences if s["judge"]]
    if want and body:
        if embed is None:
            from opencortyx.core.inference.embedding import embed_batch as embed
        try:
            vecs = await embed(want + [body[n][:_MAX_BLOCK_CHARS] for n in body])
        except Exception:                                     # noqa: BLE001 - words and numbers still check
            vecs = []
        if vecs and len(vecs) == len(want) + len(body):
            svec = dict(zip(want, vecs[:len(want)]))
            bvec = dict(zip(list(body), vecs[len(want):]))

            def _cos(a, b):
                num = sum(x * y for x, y in zip(a, b))
                da = sum(x * x for x in a) ** 0.5
                db_ = sum(y * y for y in b) ** 0.5
                return num / (da * db_) if da and db_ else 0.0
            for t, v in svec.items():
                for n, bv in bvec.items():
                    if v and bv:
                        sim[(n, t)] = _cos(v, bv)

    # The verification model reads a sentence against the parts of a block that best match it (a
    # long passage is cut into windows; the model reads ~512 tokens), and only against the blocks
    # that could support it: the ones it cites, and the few the words or similarity point to. It is
    # a CONTRADICTION detector first - "did not approve" against "approved" - and a second way to
    # be supported; it never rejects a sentence the other checks support on its own, because such
    # models answer "neutral" to correct framing ("The policy states that ...").
    nli_scores: dict = {}                             # (block, sentence) -> {"entail", "contradict"}
    if use_nli:
        pairs, keys = [], []
        for s in sentences:
            if not s["judge"]:
                continue
            t = s["text"]
            ranked = sorted(body, key=lambda n: -sim.get((n, t), 0.0))
            cand = list(dict.fromkeys([n for n in s["cited"] if n in body]
                                      + [n for n in ranked if _words_in(t, body[n], 0.4)][:3] + ranked[:2]))[:5]
            for n in cand:
                for w in _windows_for(t, body[n]):
                    pairs.append((w, t))
                    keys.append((n, t, w))
        from opencortyx.core.inference import nli as _nli
        scored = await _nli.entails(nli_model, pairs) if pairs else []
        if scored is None:
            use_nli = False
        else:
            for (n, t, w), r in zip(keys, scored):
                cur = nli_scores.setdefault((n, t), {"entail": 0.0, "contradict": 0.0})
                cur["entail"] = max(cur["entail"], r["entail"])
                # a contradiction counts only where the window is about the sentence at all
                if _words_in(t, w, 0.3) or any(len(x) > 6 and x[:6] in w.lower() for x in _content_words(t)):
                    cur["contradict"] = max(cur["contradict"], r["contradict"])

    def supports(ns: list, text: str) -> tuple[bool, str]:
        """Whether blocks `ns` (one, or several the graph links) support `text`, and by which check."""
        have = set().union(*(numbers[n] for n in ns)) if ns else set()
        if _numbers(text) - have:
            return False, "numbers"
        lows = " ".join(body[n] for n in ns).lower()
        if any(name not in lows for name in _names_in(text)):
            return False, "names"
        if use_nli and any(_contradicted(nli_scores.get((n, text))) for n in ns):
            return False, "contradicted"
        if len(ns) == 1:
            n = ns[0]
            if _words_in(text, body[n]):
                return True, "words+nli" if use_nli else "words"
            if sim.get((n, text), 0.0) >= threshold:
                return True, "similarity+nli" if use_nli else "similarity"
            if use_nli and (nli_scores.get((n, text)) or {}).get("entail", 0.0) >= 0.5:
                return True, "nli"
            return False, "nli" if use_nli else "similarity"
        return _words_in(text, " ".join(body[n] for n in ns)), "words+nli" if use_nli else "words"

    def compose(text: str, first: list) -> list | None:
        """Blocks the graph connects that TOGETHER state `text`: grown from `first` (or from the
        block most like the sentence), adding a linked block that holds a number or name still
        missing, at most six. None when no such set covers it."""
        need = _numbers(text) | _names_in(text)

        def covers(n):
            low = body[n].lower()
            return sum(1 for x in need if str(x).lower() in low or str(x) in numbers[n])
        starts = [first] if first else [[n] for n in sorted(body, key=lambda n: (-covers(n), -sim.get((n, text), 0.0)))[:3]]
        for start in starts:
            got = grow(list(dict.fromkeys(start)), need)
            if got:
                return got
        return None

    def grow(chosen: list, need: set) -> list | None:
        while len(chosen) < 6:
            have = " ".join(body[n] for n in chosen).lower()
            missing = {x for x in need if str(x).lower() not in have and str(x) not in _numbers(have)}
            if not missing:
                break
            nxt = None
            for n in body:
                if n in chosen or not any(frozenset((ids[n - 1], ids[c - 1])) in links for c in chosen):
                    continue
                low = body[n].lower()
                gain = sum(1 for x in missing if str(x).lower() in low or str(x) in numbers[n])
                if gain and (nxt is None or gain > nxt[1]):
                    nxt = (n, gain)
            if nxt is None:
                return None
            chosen.append(nxt[0])
        return chosen if len(chosen) > 1 else None

    checked = supporting = 0
    for s in sentences:
        s.update({"supported": True, "supported_by": [], "check": "none", "reason": None})
        if not s["judge"]:
            continue
        text, cited = s["text"], s["cited"]
        found, why = None, None
        for n in cited:                                  # 1. a cited block on its own
            ok, how = supports([n], text)
            checked += 1
            if ok:
                supporting += 1
                found = found or ([n], how)
            elif how == "contradicted":
                why = "a cited source contradicts it"
        if found is None and len(cited) > 1 and _linked([ids[n - 1] for n in cited], links):
            ok, how = supports(cited, text)                # 2. the cited blocks together, linked
            if ok:
                found = (cited, how)
                supporting += len(cited)                   # none alone, all together
        if found is None:                                # 3. another block: re-attributed
            for n in body:
                if n in cited:
                    continue
                ok, how = supports([n], text)
                if ok:
                    found = ([n], how)
                    if cited:
                        why = why or "its citation does not support it; another source does"
                    break
        if found is None:                                # 4. several blocks the graph links
            group = compose(text, [n for n in cited if n in body])
            if group:
                ok, how = supports(group, text)
                if ok:
                    found = (group, "composed")
        against = [n for n in body if use_nli and _contradicted(nli_scores.get((n, text)))]
        if against and not any((nli_scores.get((n, text)) or {}).get("entail", 0.0) >= 0.5 for n in body):
            # A source says otherwise and none says it. It overrules the support found only when it
            # is at least as much about the sentence's specifics - its names and numbers - as that
            # support is: "Yes, X is permitted" against the passage on X loses to it, while a list
            # of three licences is not undone by a letter about the same customer's fourth.
            key = _numbers(text) | _names_in(text)

            def cov(ns):
                low = " ".join(body[n] for n in ns).lower()
                return sum(1 for x in key if str(x).lower() in low or str(x) in _numbers(low))
            if found is None or max(cov([n]) for n in against) >= cov(found[0]):
                found, why = None, "a source contradicts it"
        if found is None:
            s.update({"supported": False, "check": "nli" if use_nli else "words+similarity",
                      "reason": why or "no source supports it"})
        else:
            s.update({"supported_by": found[0], "check": found[1], "reason": why})
    judged = [s for s in sentences if s["judge"]]
    return {"sentences": [{k: s[k] for k in ("text", "cited", "supported_by", "supported", "check", "reason")}
                          for s in sentences],
            "supported": all(s["supported"] for s in judged),
            "unsupported": [s["text"] for s in judged if not s["supported"]],
            "unsupported_raw": [s["raw"] for s in judged if not s["supported"]],
            "judged": len(judged),
            "citations_checked": checked, "citations_supporting": supporting,
            "citation_precision": round(supporting / checked, 4) if checked else None,
            "checked_with": "nli" if use_nli else "words+similarity"}


def strict_answer(answer: str, report: dict) -> str:
    """`answer` without the sentences `report` found unsupported (their citation markers go too)."""
    drop = set(report.get("unsupported_raw") or [])
    if not drop:
        return answer
    kept = [s for s in _sentence_split(answer) if s not in drop]
    return " ".join(kept).strip()
