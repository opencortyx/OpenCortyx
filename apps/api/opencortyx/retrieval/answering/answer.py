"""Grounded answer generation (optional, on top of /context).

Generates an answer constrained to retrieved context, then VERIFIES every sentence is
grounded (trust.verify_grounding). Unsupported sentences are flagged — this is the
no-hallucination guarantee in action. Abstains when there is no grounded context.
"""
import re

from opencortyx.core.contracts import RetrieveContext
from opencortyx.retrieval.answering import trust
from opencortyx.retrieval.answering import memory as _mem
from opencortyx.core.inference.llm import complete, stream_complete
from opencortyx.core.config import runtime as rc
from opencortyx.core.inference.registry import NoModelConfigured, resolve_model
from opencortyx.capabilities.modules.trust import score_relevance
from opencortyx.domain.control import ExtractionTemplate
from opencortyx.core.db import V2Session

_ABSTAIN = "I don't have enough grounded information in the knowledge base to answer that."

_DEFAULT_NON_ANSWER = ["i don't know", "i do not know", "no information", "not mentioned", "does not contain",
                       "doesn't contain", "cannot answer", "can't answer", "cannot be answered",
                       "can't be answered", "no relevant information", "not provided in the context",
                       "no data", "isn't in the context", "is not in the context", "no specific detail",
                       "no specific information", "there is no mention", "not specified",
                       "i need more information", "could not find", "not enough information",
                       "enough grounded information", "don't have enough"]
# STRONG refusal / deflection markers — a non-answer dressed up as a reply. Count REGARDLESS of length.
_DEFAULT_REFUSAL = ["can't assist", "cannot assist", "unable to assist", "can't help with",
                    "cannot help with", "i'm not able to", "i am not able to", "as an ai",
                    "as an artificial intelligence", "i don't have access", "i do not have access",
                    "unable to access", "i'm unable to", "i am unable to", "unable to provide",
                    "i don't possess", "consult your", "contact your", "i'm sorry, but i can",
                    "i cannot provide", "i can't provide", "don't have access to personal",
                    "access personal information", "respect your privacy", "privacy reasons"]


async def _abstain_phrases(workspace_id):
    """The editable non-answer + refusal phrase lists from builtin:answer.generate (the Guardrail
    phrase library), falling back to the defaults. Read per request so edits apply immediately."""
    from sqlalchemy import select as _select
    na, rf = _DEFAULT_NON_ANSWER, _DEFAULT_REFUSAL
    try:
        async with V2Session() as db:
            t = (await db.execute(_select(ExtractionTemplate).where(
                ExtractionTemplate.workspace_id == workspace_id, ExtractionTemplate.name == "builtin:answer.generate"))).scalar_one_or_none()
        mech = (t.mechanism or {}) if t else {}
        na = mech.get("non_answer_phrases") or na
        rf = mech.get("refusal_phrases") or rf
    except Exception:
        pass
    return [str(p).lower() for p in na if str(p).strip()], [str(p).lower() for p in rf if str(p).strip()]


def _is_non_answer(ans: str, non_answer=None, refusal=None) -> bool:
    a = (ans or "").strip().lower()
    refusal = refusal if refusal is not None else _DEFAULT_REFUSAL
    non_answer = non_answer if non_answer is not None else _DEFAULT_NON_ANSWER
    if any(p in a for p in refusal):                  # refusals count at any length
        return True
    return len(a) < 700 and any(p in a for p in non_answer)


def _fully_ungrounded(ans: str, unsupported: list) -> bool:
    """True when EVERY substantive sentence of the answer is unsupported by the context (i.e. the
    cosine grounding check found nothing grounded) → treat as a fabrication and abstain. Mirrors the
    sentence split used by trust.verify_grounding so the counts line up."""
    import re
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", ans or "") if len(s.strip()) > 12]
    return bool(sents) and len(unsupported) >= len(sents)


def _grounding_blocks(pkg: dict) -> list:
    """The units grounding compares an answer against — one per retrieved node.

    NOT `context.split("\\n\\n")`. The packaged context is JSON, so a blank-line split returns the
    WHOLE document as a single block, and that block is then both too long and too vague to ground
    anything: the embedder silently truncates past its input window, so everything after it is
    invisible, and one averaged vector over 18k chars matches no specific sentence.

    Measured on "Who is the sponsor and where are they based?": retrieval scored trust 1.0 and put
    the answering node second, the context contained `based_in=Oslo` verbatim — and the answer was
    rejected as ungrounded because the 18,093-char context collapsed to one block. The failure grows
    with the graph, which is why it appeared only once extraction tripled the context size.

    One block per node is the right unit: a sentence is checked against the node that should support
    it rather than against everything.

    The node text comes from the CONTEXT lines (one JSON object per node), not from `citations`.
    Citation text is a 240-char display snippet; grounding against it rejected any answer drawn from
    further into a chunk. Measured: "how long does espresso extraction take?" streamed the correct
    "25 seconds" from coffee-brewing.txt, then the number check found no "25" in the first 240 chars
    and the answer was replaced by an abstain — the model had been shown text the gate never saw.
    """
    import json
    blocks = []
    for line in (pkg.get("context") or "").split("\n"):
        try:
            t = json.loads(line).get("text") if line.strip() else ""
        except (ValueError, AttributeError):
            t = ""
        if t and str(t).strip():
            blocks.append(str(t))
    if blocks:
        return blocks
    blocks = [t for t in ((c.get("text") or "") for c in (pkg.get("citations") or [])) if t.strip()]
    if blocks:
        return blocks
    return [b for b in (pkg.get("context") or "").split("\n\n") if b.strip()]


def _toks(s: str) -> int:
    """Cheap token estimate (≈4 chars/token) for cost/observability accounting."""
    return max(0, len(s or "") // 4)


# Editable answer-pipeline prompts (Studio → category 'answer'). Read at request time so edits take
# effect immediately; fall back to the built-in default if the template is missing.
_DEFAULT_RELEVANCE = ("You are a relevance evaluator. Does the CONTEXT contain enough information to "
                      "accurately answer the QUESTION? Reply strictly with YES or NO.\n\n"
                      "Context:\n{context}\n\nQuestion: {query}")
_DEFAULT_GENERATE = ("The CONTEXT below is the user's OWN saved information (their notes, memory and "
                     "documents). Answer the QUESTION using ONLY this context. Do NOT refuse on privacy "
                     "grounds and do NOT say 'as an AI' — this is the user's own data and they are "
                     "asking about themselves.\n\n"
                     "Think first inside <think>...</think>. For counting / listing / 'how many' / "
                     "'which' questions: list every relevant item you find with its owner and status, "
                     "then apply the question's constraints — count ONLY what the user CURRENTLY has "
                     "(exclude things they only considered or wanted, things they sold or gave away, "
                     "and things that belong to someone else). For time questions, note the relevant "
                     "dates before concluding.\n\n"
                     "Then, AFTER </think>, give the final answer directly and specifically. State "
                     "every relevant fact you used from the context — including status, severity, "
                     "counts and amounts — rather than only the headline. Put ONLY the final answer "
                     "after </think> — no reasoning and no citation markers. If the answer is "
                     "genuinely not present in the context, reply with exactly: I don't know."
                     "\n\nContext:\n{context}\n\nQuestion: {query}")

# generation system prompt — shared by the streaming + non-streaming paths
_ANS_SYS = ("You answer strictly and only from the provided context, which is the user's own data. "
            "Never refuse and never claim you lack access — just answer from the context or say "
            "'I don't know'. You may reason inside <think>...</think> first; put ONLY the final "
            "answer after </think>.")

# NOT anchored to the start: a small model frequently emits the answer first and the reasoning after,
# so `^\s*<think>` matched nothing and the block survived into the answer. See _strip_reasoning.
_THINK_RE = re.compile(r"\s*<think>.*?</think>\s*", re.I | re.S)
#: An unterminated <think> (the model never closes the tag) would otherwise leak the entire tail.
_THINK_OPEN_RE = re.compile(r"\s*<think>.*\Z", re.I | re.S)


def _strip_reasoning(text: str) -> str:
    """Drop EVERY <think>…</think> reasoning block so none reaches the user, citations, or the
    grounding gate. If the model didn't use the block, the text is returned unchanged.

    `count=1` assumed the model puts its reasoning first, as instructed. A 7B often does not:
    measured, the answer to "how many projects are part of Atlas Modernisation?" came back as
    `2  <think>Atlas Modernisation is a programme that includes…</think>` — answer first, reasoning
    after. A leading-only strip left the whole reasoning block in the user-visible answer and fed it
    to the sentence-grounding gate, which then had to verify the model's private deliberation as if
    it were a claim. Strip all of them, wherever they land.
    """
    out = _THINK_RE.sub("", text or "")
    return _THINK_OPEN_RE.sub("", out).strip()


def _gen_info(template: str, model_spec: dict) -> dict:
    """Summary of HOW the answer was generated — surfaced to the UI trace so you can confirm the model,
    the template, and whether the CoT <think> scaffold is active WITHOUT opening the Extractors page.
    `instructions` is the template minus the {context}/{query} body so the UI shows the real prompt."""
    return {"model": (model_spec or {}).get("model_id"),
            "template": "builtin:answer.generate",
            "cot": "<think>" in (template or ""),
            "instructions": (template or "").split("Context:")[0].strip()[:800]}


async def _answer_prompt(workspace_id, name: str, default: str) -> str:
    from sqlalchemy import select as _select
    try:
        async with V2Session() as db:
            t = (await db.execute(_select(ExtractionTemplate).where(
                ExtractionTemplate.workspace_id == workspace_id, ExtractionTemplate.name == name))).scalar_one_or_none()
        p = ((t.mechanism or {}).get("prompt") if t else "") or ""
        return p.strip() or default
    except Exception:
        return default


def _crag_rejects(trust_score: float, threshold: float, pkg: dict) -> bool:
    """Should the CRAG relevance judge veto this answer?

    ONE definition, shared by the batch and streaming paths. They are twins, and each previously
    carried its own copy of this condition — so a fix applied to `/answer` silently did not reach
    `/answer/stream`, which is the endpoint the Assistant UI actually calls.

    CRAG is ADVISORY whenever retrieval already returned solid evidence. Either signal vouches for
    the context and keeps the answer:

      * enough grounded citations, or
      * retrieval's OWN measured confidence — `pkg["confidence"]` is max(cross-encoder, cosine) over
        the kept nodes, the same number the retrieval-side abstain gate already cleared.

    The judge is the `utility` model — often the same model as generation (it is used when no
    utility model is registered), and on a constrained one it is deliberately small. A 1B model returning a binary
    YES/NO must not overrule a numeric relevance the retrieval stack measured — the same rule
    `trust._relevance` applies to the reranker: a weak secondary opinion may PROMOTE, never bury.
    Measured: 14 of 47 end-to-end failures abstained on contexts verified complete.
    """
    if trust_score >= threshold:
        return False
    if pkg.get("low_relevance"):
        # Retrieval found nothing closely resembling the question and said so instead of refusing:
        # here neither the citation count nor its confidence vouches - the judge alone decides.
        return True
    enough_citations = len(pkg.get("citations") or []) >= rc.crag_min_citations()
    confident_retrieval = float(pkg.get("confidence") or 0.0) >= threshold
    return not (enough_citations or confident_retrieval)


_BRACKET = re.compile(r"\[([^\[\]\n]{1,400})\]")
_NODE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _mark_support(answer: str, citations: list) -> list:
    """The citations, each flagged `supports` when the answer rests on it (`trust.supporting_citations`).

    Every retrieved block stays in the list — the reader can still open what was considered — but
    the ones the answer actually used are marked, so the chat shows those as its sources and the
    rest as other evidence. When nothing can be attributed, all are marked: better to show every
    candidate than to present an answer as if it had no source."""
    cited = {str(citations[int(n) - 1].get("node_id")) for n in re.findall(r"\[(\d+)\]", answer or "")
             if 0 < int(n) <= len(citations or [])}
    support = trust.supporting_citations(answer, citations, cited)
    return [{**c, "supports": (str(c.get("node_id")) in support) if support else True}
            for c in citations or []]


def _number_citations(answer: str, citations: list) -> str:
    """Inline source markers as numbered references into the citation list the reader is shown.

    The generator is asked to cite like `[Type#id]`, and a small model copies that literally — the
    Assistant then displayed "[Type#0519b2d9-b101-…, Type#d06d5498-…]" in the answer. A marker naming
    cited nodes becomes `[n]` (n = that citation's position); one naming nothing that was cited
    (`[Type#relationships]`) is dropped. Plain bracketed text, with no `#` and no node id, is kept."""
    index = {str(c.get("node_id") or "").lower(): i + 1 for i, c in enumerate(citations or [])}

    def swap(m):
        inner = m.group(1)
        ids = _NODE_ID.findall(inner)
        if "#" not in inner and not ids:
            return m.group(0)
        nums = sorted({index[i.lower()] for i in ids if i.lower() in index})
        return "".join(f"[{n}]" for n in nums)

    out = _BRACKET.sub(swap, answer or "")
    out = re.sub(r"[ \t]+([.,;:!?])", r"\1", out)          # "vendors ." left by a dropped marker
    return re.sub(r"[ \t]{2,}", " ", out).strip()


async def answer_query(query: str, ctx: RetrieveContext) -> dict:
    """Conversation-aware entry point.

    A follow-up is resolved to a standalone question BEFORE retrieval. Retrieval embeds the query as
    written, so "which vendor does that project use?" carries nothing to match on and the engine
    abstains on a subject it named one turn earlier. Prior turns in the CONTEXT cannot fix this — by
    then retrieval has already run.

    `ctx.conversation_id` is a chat thread id, and the thread is the conversation's record: the
    caller appends the question before answering and the answer after (`knowledge.chat_history`).
    Nothing is written here.
    """
    conv = getattr(ctx, "conversation_id", None)
    asked = query
    if conv is not None:
        try:
            query = await _mem.contextualize(query, ctx.workspace_id, conv)
        except Exception:
            pass

    res = await _answer_query_inner(query, ctx)
    if conv is not None and query != asked:
        res["rewritten_query"] = query       # surface the resolution for the UI / trace
    return res


async def _run_pipeline(query: str, ctx: RetrieveContext, stream: bool):
    """THE answer pipeline. `/answer` and `/answer/stream` are both thin consumers of it.

    It used to exist twice — `_answer_query_inner` and a "streaming twin" — and the copies had
    silently drifted. The streaming path, which is the one the Assistant UI actually calls, was
    missing the retry-on-refusal, the `_fully_ungrounded` hallucination gate, and the CRAG guard. So
    the product could emit a fabrication that `/answer` would have rejected, and every measurement
    taken against `/answer` described a pipeline the user was not running. Duplicated logic does not
    stay in sync; it only looks like it does until someone measures.

    Yields the same event stream in both modes:
        {"type": "meta",  ...}   once, before any tokens
        {"type": "token", ...}   only when `stream` is True
        {"type": "done",  ...}   once, carrying the full answer payload
    """
    from opencortyx.retrieval.adapter import build_context_v2
    pkg = await build_context_v2(query, ctx)
    spend = {"in": 0, "out": 0}
    base = {"retrievers": pkg.get("retrievers"), "trace": pkg.get("citations", []),
            "stage_trace": pkg.get("stage_trace", []),
            "tokens_context": pkg.get("tokens_used", 0), "route": pkg.get("route") or "knowledge",
            **({"data": pkg["data"]} if pkg.get("data") else {})}

    def _abstain(caveat, **extra):
        # Every non-answer says which kind it is: UNKNOWN with a reason, or CLARIFY with the choices.
        outcome = pkg.get("outcome") or "UNKNOWN"
        reason = pkg.get("unknown_reason") or extra.pop("unknown_reason", None) or "no source answers it"
        text = _ABSTAIN
        if outcome == "CLARIFY" and pkg.get("clarify"):
            text = _clarify_text(pkg["clarify"])
        elif pkg.get("unknown_reason"):
            text = f"UNKNOWN: {pkg['unknown_reason']}."
        return {"type": "done", "answer": text, "abstained": True, "grounded": True,
                "outcome": outcome, "unknown_reason": reason if outcome == "UNKNOWN" else None,
                **({"clarify": pkg["clarify"]} if outcome == "CLARIFY" and pkg.get("clarify") else {}),
                "citations": [], "caveats": [caveat] if isinstance(caveat, str) else caveat,
                "tokens_prompt": spend["in"], "tokens_completion": spend["out"],
                "tokens_total": spend["in"] + spend["out"], **extra, **base}

    if pkg.get("abstained") or (pkg.get("low_relevance") and getattr(ctx, "fast", False)):
        # Fast mode runs no relevance judge, so a low-relevance context is refused as before.
        if stream:
            yield {"type": "token", "text": _abstain([])["answer"]}
        yield _abstain(pkg.get("caveats", []), confidence=pkg.get("confidence", 0.0))
        return

    context = pkg["context"]
    gen_spec = await resolve_model(ctx.workspace_id, "generation")
    util_spec = await resolve_model(ctx.workspace_id, "utility")
    na_phrases, rf_phrases = await _abstain_phrases(ctx.workspace_id)

    threshold = ctx.abstain_threshold
    if threshold is None:
        threshold = rc.abstain_threshold()
    # CRAG evaluate step - only where its verdict can decide the answer (`_crag_rejects`): with solid
    # retrieval it is advisory, so it is not run and the trust score is retrieval's own measured
    # confidence. Fast mode skips it too. On a CPU model the judge costs a minute or more.
    if getattr(ctx, "fast", False) or not _crag_rejects(0.0, threshold, pkg):
        _crag = {"trust_score": round(float(pkg.get("confidence") or 0.0), 3), "tokens_in": 0, "tokens_out": 0,
                 "eval_context": context[:6000]}
    else:
        _crag = await score_relevance(ctx.workspace_id, query, context, util_spec=util_spec)
    trust_score = _crag["trust_score"]
    spend["in"] += _crag["tokens_in"]
    spend["out"] += _crag["tokens_out"]
    eval_context = _crag["eval_context"]
    if _crag_rejects(trust_score, threshold, pkg):
        if stream:
            yield {"type": "token", "text": _ABSTAIN}
        yield _abstain("Context was retrieved but evaluated as irrelevant (CRAG rejection).",
                       trust_score=trust_score, retrieved_context=eval_context,
                       crag={"verdict": "rejected", "score": trust_score, "threshold": threshold,
                             "reason": "The relevance evaluator judged the retrieved context "
                                       "insufficient to answer."})
        return

    ans_tmpl = await _answer_prompt(ctx.workspace_id, "builtin:answer.generate", _DEFAULT_GENERATE)
    ans_prompt = ans_tmpl.replace("{context}", context).replace("{query}", query)
    base["generation"] = _gen_info(ans_tmpl, gen_spec)
    spend["in"] += _toks(ans_prompt) + _toks(_ANS_SYS)

    if stream:
        yield {"type": "meta", "trust_score": trust_score, "confidence": pkg["confidence"], **base}

    # Generation — the ONLY genuine difference between the two entry points.
    parts: list = []
    try:
        if stream:
            # Withhold <think>...</think> from the user: buffer until it closes, then stream on.
            buf, gate_open, mode = "", False, None
            async for chunk in stream_complete(ans_prompt, system=_ANS_SYS, max_tokens=1000,
                                               temperature=0.1, model_spec=gen_spec):
                parts.append(chunk)
                if gate_open:
                    yield {"type": "token", "text": chunk}
                    continue
                buf += chunk
                if mode is None:
                    head = buf.lstrip()
                    if head.lower().startswith("<think>"):
                        mode = "think"
                    elif len(head) >= 7:
                        mode = "plain"
                if mode == "plain":
                    yield {"type": "token", "text": buf}
                    buf, gate_open = "", True
                elif mode == "think":
                    i = buf.lower().find("</think>")
                    if i != -1:
                        rest = buf[i + len("</think>"):].lstrip()
                        if rest:
                            yield {"type": "token", "text": rest}
                        buf, gate_open = "", True
            if not gate_open and buf:
                yield {"type": "token", "text": _strip_reasoning(buf)}
        else:
            parts.append(await complete(ans_prompt, system=_ANS_SYS, max_tokens=1000,
                                        temperature=0.1, model_spec=gen_spec))
    except Exception as e:
        # Not a judgement on the evidence: the answer could not be generated. `model_missing` lets
        # the caller say "connect a model" instead of "not enough information".
        yield _abstain([], error=str(e), model_missing=isinstance(e, NoModelConfigured),
                       citations=pkg["citations"], confidence=pkg["confidence"])
        return

    _raw = "".join(parts)
    spend["out"] += _toks(_raw)        # the FULL generation (incl. reasoning) is what we paid for
    ans = _strip_reasoning(_raw)

    if _is_non_answer(ans, na_phrases, rf_phrases) and trust_score >= threshold:
        try:
            _retry = await complete(ans_prompt, system=_ANS_SYS, max_tokens=1000, temperature=0.35,
                                    model_spec=gen_spec)
            spend["in"] += _toks(ans_prompt) + _toks(_ANS_SYS)
            spend["out"] += _toks(_retry)
            _retry = _strip_reasoning(_retry)
            if _retry and not _is_non_answer(_retry, na_phrases, rf_phrases):
                ans = _retry
                base["generation"] = {**(base.get("generation") or {}), "retried": True}
                if stream:                    # the user already saw the refusal — correct it
                    yield {"type": "token", "text": "\n\n" + ans}
        except Exception:
            pass                              # retry is best-effort; fall through to abstain

    if _is_non_answer(ans, na_phrases, rf_phrases):
        yield _abstain("No grounded answer in the retrieved context.", trust_score=trust_score,
                       crag={"verdict": "no_answer", "score": trust_score, "threshold": threshold})
        return

    if rc.grounding_mode() == "entailment":
        grounded, unsupported = await trust.verify_grounding_judge(query, ans, context,
                                                                   model_spec=util_spec)
    else:
        grounded, unsupported = await trust.verify_grounding(
            ans, _grounding_blocks(pkg), threshold=rc.grounding_threshold())

    if rc.grounding_mode() == "entailment" and not grounded:
        yield _abstain("Answer was not entailed by the retrieved context (grounding gate).",
                       trust_score=trust_score)
        return
    if rc.grounding_mode() != "entailment" and _fully_ungrounded(ans, unsupported):
        yield _abstain("No sentence in the answer was grounded in the retrieved context.",
                       trust_score=trust_score)
        return

    numbered = _number_citations(ans, pkg["citations"])
    if pkg.get("data") and grounded and not unsupported and rc.answer_write_back():
        from opencortyx.knowledge.writeback import write_back
        base["written_back"] = await write_back(ctx.workspace_id, query, ans, pkg["data"])
    yield {"type": "done", "answer": numbered,
           "abstained": False, "grounded": grounded,
           "outcome": "PARTIAL" if unsupported else "ANSWERED", "unknown_reason": None,
           "unsupported_sentences": unsupported, "citations": _mark_support(numbered, pkg["citations"]),
           "caveats": pkg.get("caveats", []), "confidence": pkg["confidence"],
           "trust_score": trust_score,
           "crag": {"verdict": "accepted", "score": trust_score, "threshold": threshold},
           "tokens_used": pkg.get("tokens_used"), "tokens_prompt": spend["in"],
           "tokens_completion": spend["out"], "tokens_total": spend["in"] + spend["out"], **base}


def _clarify_text(choices: list) -> str:
    """The question back when a name means two things: which one, by what tells them apart."""
    parts = []
    for c in choices:
        opts = " or ".join(f"{o.get('label')} ({str(o.get('node_id'))[:8]})" for o in c.get("candidates") or [])
        parts.append(f'"{c.get("mention")}" matches more than one record: {opts}. Which one do you mean?')
    return " ".join(parts) or _ABSTAIN


async def _answer_query_inner(query: str, ctx: RetrieveContext) -> dict:
    """Batch entry point: run the one pipeline, return its final event as the response body."""
    done = None
    async for ev in _run_pipeline(query, ctx, stream=False):
        if ev.get("type") == "done":
            done = ev
    out = dict(done or {})
    out.pop("type", None)
    return out


async def answer_query_stream(query: str, ctx: RetrieveContext):
    """Conversation-aware streaming entry point — the twin of `answer_query`.

    The Assistant UI calls `/answer/stream`, so anything the batch path does around the answer has to
    happen here too or the product behaves differently from every measurement taken against
    `/answer`. It previously did NOT: a follow-up was never rewritten, so "which vendor does that
    project use?" reached retrieval as written and abstained.
    """
    conv = getattr(ctx, "conversation_id", None)
    asked = query
    if conv is not None:
        try:
            query = await _mem.contextualize(query, ctx.workspace_id, conv)
        except Exception:
            pass

    async for ev in _run_pipeline(query, ctx, stream=True):
        if ev.get("type") == "done" and query != asked:
            ev["rewritten_query"] = query
        yield ev
