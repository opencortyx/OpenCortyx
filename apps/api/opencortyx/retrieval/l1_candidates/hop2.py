"""L1 - Hop-2 lexical expansion: search the ENTITIES the first pass found, exactly.

    question -> pass 1 (8 surfaces) -> top of the pool
                                          |
                                          v  entity-shaped spans the QUESTION never named
                                       "Seattle Marathon", "Door Dash"
                                          |
                                          v  PHRASE match, exact adjacency
                                       pass 2 -> pooled into the same RRF

WHY THIS IS NOT `prf_set`, WHICH MEASURED -3.17

`prf_set` embedded the whole top passage and re-queried BY VECTOR. That drifts by construction:
the feedback vector resembles the top of the first pass in its entirety, so the second pass
returns everything that also resembles it - measured as 35 questions made worse by a flood of
near-duplicates.

This extracts DISCRETE SPANS and matches them as PHRASES. A phrase either occurs in a node's text
or it does not. There is no similarity gradient to slide down, so the failure mode that killed
`prf_set` is not available to this mechanism. The two share only the words "second pass".

WHAT IT TARGETS

The measured wall on this corpus is that embeddings cannot link two statements of one concept:
`"Jon lost job as a banker"` vs `"Gina lost job at Door Dash"` score cosine 0.525, at p25 of the
corpus distribution, while an unrelated pair scores 0.521. No threshold separates them. But a
literal `"Seattle Marathon"` appearing in two sessions IS linkable - only lexically.

That aims at the retrieval failure ranking cannot reach: 23% of missed evidence is NOT_RETRIEVED
(never in the pool at all), and BM25 is where the unreachable gold lives - probing 20 unreachable
gold turns, BM25 found 16 at ranks 246-1305 while dense found 3 at depth 4000. Reordering cannot
help a node that was never a candidate; a second lexical pass on a span the first pass DISCOVERED
can, because the span is evidence the corpus itself supplied rather than vocabulary the question
had to guess.

THREE RULES, ALL COMPUTED

No gazetteer and no type table - spans are runs of capitalised words in prose (`prose_spans`), the
same structural discipline L0 applies to the question.

1. A span the QUESTION already named is dropped. Pass 1 searched it; searching it again buys
   duplicates, which is the shape of the `prf_set` failure.
2. A span occurring in more than `DF_CEILING` of the scoped corpus is dropped, its document
   frequency measured LIVE. This is `attribute_specificity` again: `speaker` held 2 distinct
   values over 629 turns, so matching on it returned every turn a person ever spoke. A span that
   matches everything discriminates nothing.
3. At most `MAX_SPANS`, ordered by how many top-of-pass candidates carry them. A cost bound, not
   a recall bound - a span appearing in one hit of 400 is not the bridge between two sessions.
"""
from __future__ import annotations

import re
import uuid
from typing import Iterable, Optional

from opencortyx.core.substrate import NodeRepo
from opencortyx.retrieval.contracts import Candidate, Evidence
from opencortyx.retrieval.l1_candidates.lexical import candidate, document_filter
from opencortyx.core.inference.embedding import sparse_query

#: How deep into each surface's ranking counts as "the top of the first pass". Deliberately
#: shallow: the spans worth a second search are the ones the strongest hits agree on.
TOP_RANK = 10

#: Cost bound. Each span is one indexed phrase query.
MAX_SPANS = 6

#: A span in more than this share of the scoped corpus is a stop-word for this corpus, whatever
#: it looks like. Measured live, never listed.
DF_CEILING = 0.05

#: Below this a span is too short for a phrase match to mean anything ("Jo" is two characters and
#: matches inside a hundred words). All-caps acronyms are exempt - see `discover_spans`.
MIN_SPAN_CHARS = 4

#: Retrieval depth for the second pass. Shallow for the same reason the hypothetical sweep is
#: shallow: at 1.19% packed evidence, volume buries rather than adds.
HOP2_K = 60


#: One WORD, Unicode-aware. `[^\W\d_]` is "letter" in any script, so this matches `Müller` and
#: `Zoë` as single tokens; an internal apostrophe keeps `O'Brien` and `d’Artagnan` whole.
_TOKEN = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")

_LOWER_WORD = re.compile(r"[^\W\d_]+")

_WORD = re.compile(r"[^\W_]+")

_JOINERS = {"of", "de", "van", "der", "den", "du", "da", "&"}


def prose_spans(body: str):
    """Yield `(tokens, start_offset)` for every run of capitalised words in `body`.

    A SCANNER, not a character-class regex. The previous pattern was `[A-Z][a-z]+`, which is
    ASCII-only and requires a lower-case tail, so it silently found NOTHING in two whole
    categories of real entity:

      * non-ASCII names - `Zoë Müller` produced no span at all, because `ë` is not in `[a-z]`
        and `Müller` has no `[a-z]` immediately after its capital;
      * acronyms - `IBM`, `NASA`, `BBC`, which are precisely the spans an EXACT PHRASE search is
        best at, since they are unambiguous and rare.

    Capitalisation is tested with `str.isupper()` on the first character, which is defined for
    every script Python knows, instead of being enumerated as a range. Runs may be joined by the
    lower-case connectors in `_JOINERS` ("Bank of England"), but a joiner never starts or ends a
    span - `_JOINERS` is about what sits BETWEEN names.

    Spans never cross sentence punctuation, because the scan only ever joins ADJACENT word
    tokens: anything between them that is not whitespace or a joiner ends the run. That is what
    stopped the earlier spans like `"all had such a great time. [D4:9] Caroline: That"`.
    """
    toks = [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(body or "")]
    i, n = 0, len(toks)
    while i < n:
        word, w_start, w_end = toks[i]
        # A single capitalised letter is a pronoun ("I") or an initial, never a name on its own.
        # Initials arrive punctuated ("J. R. R.") and the scan already breaks on punctuation.
        if not word[:1].isupper() or len(word) < 2:
            i += 1
            continue
        run, last_end, j = [word], w_end, i + 1
        while j < n:
            nxt, n_start, n_end = toks[j]
            between = body[last_end:n_start]
            if between.strip(" 	"):          # punctuation or a newline ends the name
                break
            if nxt[:1].isupper() and len(nxt) >= 2:
                run.append(nxt)
                last_end, j = n_end, j + 1
                continue
            # a joiner continues the run ONLY if a capitalised word follows it
            if (nxt.lower() in _JOINERS and j + 1 < n
                    and toks[j + 1][0][:1].isupper()
                    and not body[n_end:toks[j + 1][1]].strip(" 	")):
                run.append(nxt)
                run.append(toks[j + 1][0])
                last_end, j = toks[j + 1][2], j + 2
                continue
            break
        yield run, w_start
        i = j


def _sentence_initial(body: str, start: int) -> bool:
    """Is this occurrence the first word of a sentence?

    `detect_mentions` was written for QUESTIONS - one sentence, so it guards position 0 only.
    Run over multi-sentence dialogue prose it promotes every sentence-opening word to a mention,
    and measured on three questions that is exactly what it returned: `Thanks`, `Glad`, `Yeah`,
    `Cool`, `Absolutely`. Capitalisation carries no information in that position.
    """
    i = start - 1
    while i >= 0 and body[i] in " \t\"'([":
        i -= 1
    return i < 0 or body[i] in ".!?;\n\r"


def discover_spans(candidate_lists: Iterable[Iterable[Candidate]],
                   asked: Iterable[str]) -> list[tuple[str, int]]:
    """Entity-shaped spans in the top of the first pass, minus what the question already named.

    Returns `(span, support)` ordered by support - the number of top-of-pass candidates whose
    text carries the span. Reads only ranks already assigned by the surfaces, so it neither
    re-fuses nor mutates the pool.

    A sentence-opening word is trimmed off the FRONT of a span only when the scanned text itself
    shows that word used in lower case ("thanks", "later", "yeah") and never shows it capitalised
    mid-sentence. Absence of evidence is not evidence: without that rule the first version cut
    `Seattle Marathon` down to `Marathon` and `Door Dash` to `Dash`.
    """
    asked_tokens = [{w.lower() for w in _WORD.findall(a)} for a in asked if a and a.strip()]
    asked_tokens = [toks for toks in asked_tokens if toks]

    seen_nodes: set[uuid.UUID] = set()
    occurrences: list[tuple[uuid.UUID, str, list[str], bool]] = []
    mid_tokens: set[str] = set()
    lower_words: set[str] = set()
    for lst in candidate_lists:
        for cand in (lst or []):
            rank = cand.best_rank()
            if rank is None or rank >= TOP_RANK:
                continue
            if cand.node_id in seen_nodes:
                continue
            seen_nodes.add(cand.node_id)
            body = cand.text
            if not body:
                continue
            for tokens, span_start in prose_spans(body):
                if not tokens:
                    continue
                initial = _sentence_initial(body, span_start)
                occurrences.append((cand.node_id, cand.node_type or "", tokens, initial))
                # Every token except a sentence-initial FIRST one is capitalised by choice.
                for i, tok in enumerate(tokens):
                    if i or not initial:
                        mid_tokens.add(tok)
            for w in _LOWER_WORD.findall(body):
                if w[:1].islower():
                    lower_words.add(w)

    counts: dict[str, int] = {}
    per_node: dict[uuid.UUID, set[str]] = {}
    for node_id, node_type, tokens, initial in occurrences:
        if (initial and tokens[0].lower() in lower_words
                and tokens[0] not in mid_tokens):
            # Evidenced common word in the opening slot. With something behind it, trim and keep
            # the name; ALONE, the whole span is that common word and there is nothing to keep.
            tokens = tokens[1:]
            if not tokens:
                continue
        # Connectors are only meaningful BETWEEN names ("Bank of England"); left dangling by the
        # trim they are noise.
        while tokens and tokens[0].lower() in _JOINERS:
            tokens = tokens[1:]
        if not tokens:
            continue
        span = " ".join(tokens)
        low = span.lower()
        acronym = all(t.isupper() and len(t) >= 2 for t in tokens)
        if len(span) < MIN_SPAN_CHARS and not acronym:
            continue
        span_tokens = {t.lower() for t in tokens}
        if any(span_tokens <= toks for toks in asked_tokens):
            continue
        if low == node_type.lower():
            continue
        if per_node.setdefault(node_id, set()).__contains__(span):
            continue                          # one node is one vote, however often it repeats
        per_node[node_id].add(span)
        counts[span] = counts.get(span, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


class Hop2LexicalSurface:
    """Exact PHRASE retrieval for spans the first pass discovered: the words must be adjacent and in
    order, so "Door Dash" is not answered by a node mentioning a door and a dash."""
    surface = "hop2_lexical"

    def __init__(self, repo: Optional[NodeRepo] = None):
        self._repo = repo or NodeRepo()

    async def document_frequency(self, workspace_id: uuid.UUID, spans: list[str],
                                 collection_ids: Optional[list] = None) -> dict[str, float]:
        """Share of the scoped corpus each span occurs in."""
        if not spans:
            return {}
        total = await self._repo.vectors.count(document_filter(workspace_id, collection_ids))
        if not total:
            return {s: 1.0 for s in spans}
        return {span: await self._repo.vectors.count(document_filter(workspace_id, collection_ids, phrase=span)) / float(total)
                for span in spans}

    async def retrieve(self, workspace_id: uuid.UUID, spans: list[str], k: int = HOP2_K,
                       collection_ids: Optional[list] = None) -> list[Candidate]:
        """One phrase query per span, ranked by BM25; evidence names the span that fired."""
        out: list[Candidate] = []
        for span in spans or []:
            hits = await self._repo.vectors.search_sparse(
                await sparse_query(span), document_filter(workspace_id, collection_ids, phrase=span), k)
            nodes = {str(n.id): n for n in await self._repo.get_many(workspace_id, [h.node_id for h in hits])}
            for rank, hit in enumerate(h for h in hits if str(h.node_id) in nodes):
                c = candidate(nodes[str(hit.node_id)])
                c.add(Evidence(surface=self.surface, rank=rank, raw_score=float(hit.score),
                               scale="bm25", detail=span, matched_text=span))
                out.append(c)
        return out
