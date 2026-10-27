"""Query decomposition: which questions are split, how, and that a split never loses evidence.

Pure: the workflow and the model are fakes, so these run in milliseconds and pin the behaviour the
module promises - the question as asked is always searched, a chain that loses its answer stops,
and a plan naming things the question does not is thrown away."""
import uuid

import pytest

from opencortyx.retrieval import decomposition
from opencortyx.retrieval.contracts import ContextBlock, RetrievalResult
from opencortyx.retrieval.decomposition import model, steps


def _block(name: str, tokens: int = 10) -> ContextBlock:
    return ContextBlock(node_id=uuid.uuid5(uuid.NAMESPACE_URL, name), role="primary", text=name,
                        tokens=tokens)


class FakeSearch:
    """Each question returns the blocks registered for it, and every question asked is recorded."""

    def __init__(self, table: dict[str, list[str]]):
        self.table = table
        self.asked: list[str] = []

    async def __call__(self, q: str) -> RetrievalResult:
        self.asked.append(q)
        return RetrievalResult(blocks=[_block(n) for n in self.table.get(q, [])])


def fake_model(*replies: str):
    queue = list(replies)
    prompts: list[str] = []

    async def complete(prompt, max_tokens=200, temperature=0.0):
        prompts.append(prompt)
        return queue.pop(0)
    complete.prompts = prompts
    return complete


async def no_model(prompt, max_tokens=200, temperature=0.0):
    raise RuntimeError("no generation model registered")


# ── which questions ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "What is the title of the PEP that PEP 3124 replaces?",
    "Who wrote the RFC that was made obsolete by RFC 9260?",
    "PEP 809 depends on another PEP. What is the status of that other PEP, and its title?",
    "RFC 9304 replaced an older RFC. In which year was that older RFC published?",
    "Which was published first, RFC 9155 or RFC 9410? Give both dates.",
    "Compare invoice INV-2041 and INV-2077.",
])
def test_chains_and_comparisons_are_compound(q):
    assert steps.is_compound(q)


@pytest.mark.parametrize("q", [
    "What is the title of PEP 322?",
    "What is the Python module pdb for?",
    'In PEP 1 ("PEP Purpose and Guidelines"), what does the section "PEP Audience" say first?',
    "Which document says: \"offers a broad description\" ... and how does that line end?",
    "What is Joanna allergic to?",
])
def test_plain_lookups_are_searched_as_they_are(q):
    assert not steps.is_compound(q)


# ── the split that needs no model ─────────────────────────────────────────────────────────────

def test_a_comparison_is_one_independent_step_per_subject():
    plan = steps.split_comparison("Which was published first, RFC 9155 or RFC 9410? Give both dates.")
    assert plan is not None and plan.source == "rules" and not plan.chained
    assert [s.text for s in plan.steps] == [
        "Which was published first, RFC 9155? Give both dates.",
        "Which was published first, RFC 9410? Give both dates.",
    ]


def test_a_question_naming_one_subject_is_not_a_comparison():
    assert steps.split_comparison("Which was published first, RFC 9155?") is None


# ── reading the model's plan ──────────────────────────────────────────────────────────────────

Q = "Who wrote the RFC that was made obsolete by RFC 9260?"


def test_a_numbered_plan_with_placeholders_parses():
    plan = steps.parse_steps(Q, "1. Which RFC was made obsolete by RFC 9260?\n2. Who wrote #1?")
    assert plan is not None and plan.chained
    assert plan.steps[1].depends_on == [1]
    assert plan.steps[1].resolve({1: "RFC 4460"}) == "Who wrote RFC 4460?"


@pytest.mark.parametrize("text", [
    "1. Who wrote the RFC that was made obsolete by RFC 9260?",            # one step
    "1. Who wrote #2?\n2. Which RFC was made obsolete by RFC 9260?",       # forward reference
    "1. Which RFC was made obsolete by RFC 9260?\n2. Who wrote RFC 4460?",  # invented answer
    "1. Which RFC was made obsolete by RFC 9260?\n3. Who wrote #1?",       # skipped number
    "",
])
def test_an_unusable_plan_is_refused(text):
    assert steps.parse_steps(Q, text) is None


async def test_the_model_continuing_the_pattern_is_cut_at_the_next_question():
    complete = fake_model("1. Which RFC was made obsolete by RFC 9260?\n2. Who wrote #1?\n\n"
                          "Question: What else?\n1. Something unrelated here")
    plan = await model.split(Q, complete)
    assert plan is not None and len(plan.steps) == 2


@pytest.mark.parametrize("raw,clean", [
    ("RFC 4460", "RFC 4460"),
    ("Answer: RFC 4460.\nMore text", "RFC 4460"),
    ("unknown", ""),
    ("The passages do not say.", ""),
    ("RFC 2818. It was published in 2000.", "RFC 2818"),
])
def test_a_bridge_answer_is_cut_to_a_search_term(raw, clean):
    assert model.clean_answer(raw) == clean


# ── running it ────────────────────────────────────────────────────────────────────────────────

async def test_a_chain_writes_each_answer_into_the_next_step_and_keeps_the_question():
    search = FakeSearch({
        Q: ["rfc9260 header"],
        "Which RFC was made obsolete by RFC 9260?": ["rfc9260 header", "rfc9260 intro"],
        "Who wrote RFC 4460?": ["rfc4460 authors"],
    })
    complete = fake_model("1. Which RFC was made obsolete by RFC 9260?\n2. Who wrote #1?", "RFC 4460")
    res = await decomposition.run(Q, search, complete, budget_tokens=1000)

    assert search.asked[:2] == ["Which RFC was made obsolete by RFC 9260?", Q]
    assert search.asked[2] == "Who wrote RFC 4460?"
    texts = [b.text for b in res.blocks]
    assert texts[0] == "rfc4460 authors"                         # the final step leads
    assert {"rfc9260 header", "rfc9260 intro"} <= set(texts)     # the bridge stays as evidence
    assert len(texts) == len(set(texts))                         # each node once
    assert [s["answer"] for s in res.steps] == ["RFC 4460", ""]
    assert any(t.layer == "D" and t.stage == "bridge" for t in res.trace)


async def test_a_chain_that_loses_its_answer_stops_instead_of_searching_the_placeholder():
    search = FakeSearch({Q: ["rfc9260 header"]})
    complete = fake_model("1. Which RFC was made obsolete by RFC 9260?\n2. Who wrote #1?", "unknown")
    res = await decomposition.run(Q, search, complete, budget_tokens=1000)

    assert not any("#1" in q for q in search.asked)
    assert [s["ran"] for s in res.steps] == [True, False]
    assert [b.text for b in res.blocks] == ["rfc9260 header"]


async def test_a_comparison_runs_both_subjects_without_a_model():
    q = "Which was published first, RFC 9155 or RFC 9410? Give both dates."
    search = FakeSearch({
        q: ["rfc9155 a", "rfc9155 b", "rfc9155 c"],
        "Which was published first, RFC 9155? Give both dates.": ["rfc9155 a"],
        "Which was published first, RFC 9410? Give both dates.": ["rfc9410 a"],
    })
    res = await decomposition.run(q, search, no_model, budget_tokens=1000)
    assert len(search.asked) == 3
    assert {"rfc9155 a", "rfc9410 a"} <= {b.text for b in res.blocks}


async def test_without_a_model_a_chain_is_searched_as_asked():
    search = FakeSearch({Q: ["rfc9260 header"]})
    res = await decomposition.run(Q, search, no_model, budget_tokens=1000)
    assert search.asked == [Q]
    assert res.steps == []


async def test_a_plain_lookup_never_calls_the_model():
    search = FakeSearch({"What is the title of PEP 322?": ["pep322"]})
    complete = fake_model()
    await decomposition.run("What is the title of PEP 322?", search, complete, budget_tokens=1000)
    assert complete.prompts == []


def test_merge_interleaves_by_rank_within_the_budget():
    a = RetrievalResult(blocks=[_block("a1"), _block("a2"), _block("shared")])
    b = RetrievalResult(blocks=[_block("shared"), _block("b2"), _block("b3")])
    blocks, used = decomposition.merge([a, b], budget_tokens=40)
    assert [x.text for x in blocks] == ["a1", "shared", "a2", "b2"]
    assert used == 40
    blocks, _ = decomposition.merge([a, b], budget_tokens=0, max_blocks=3)
    assert len(blocks) == 3


async def test_comparisons_mode_never_asks_the_model_about_a_chain():
    search = FakeSearch({Q: ["rfc9260 header"]})
    complete = fake_model()
    res = await decomposition.run(Q, search, complete, budget_tokens=1000, mode="comparisons")
    assert complete.prompts == [] and search.asked == [Q] and res.steps == []


async def test_off_mode_searches_a_comparison_once():
    q = "Which was published first, RFC 9155 or RFC 9410? Give both dates."
    search = FakeSearch({q: ["rfc9155 a"]})
    await decomposition.run(q, search, no_model, budget_tokens=1000, mode="off")
    assert search.asked == [q]
