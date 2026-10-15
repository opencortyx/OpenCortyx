"""L1 — entity resolution: binding a mention to a canonical node, once, with its evidence."""
from __future__ import annotations

import math
import uuid

import pytest

from opencortyx.retrieval.contracts import EntityMention
from opencortyx.retrieval.l1_candidates.entity_resolver import (
    AMBIGUITY_MARGIN, MIN_BINDING_CONFIDENCE, EntityResolver,
)

ORG = uuid.UUID("e5954895-7b42-5a9b-93f6-911febc30bca")


def mention(text: str) -> EntityMention:
    return EntityMention(text=text, start=0, end=len(text))


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _unit(i: int) -> list[float]:
    v = [0.0] * 768
    v[i] = 1.0
    return v


class Embedder:
    """Vectors by value, so a test sets exactly how alike two names are: `similar(a, b, 0.62)`."""

    def __init__(self):
        self._vectors: dict[str, list[float]] = {}

    def vector(self, text: str) -> list[float]:
        key = text.split(": ", 1)[-1].strip().lower()
        if key not in self._vectors:
            self._vectors[key] = _unit(len(self._vectors) * 2)
        return self._vectors[key]

    def similar(self, a: str, b: str, cosine: float) -> None:
        base = self.vector(a)
        axis = base.index(1.0) + 1
        self._vectors[b.lower()] = [cosine * x for x in base]
        self._vectors[b.lower()][axis] = math.sqrt(1 - cosine * cosine)

    def same(self, a: str, b: str) -> None:
        self._vectors[b.lower()] = self.vector(a)

    async def embed(self, text: str):
        return self.vector(text)

    async def embed_batch(self, texts):
        return [self.vector(t) for t in texts]


async def _node(repo, label, type="Entity", embedder=None, **props):
    node = await repo.upsert_node(workspace_id=ORG, type=type, label=label, properties=props, date_link=False)
    if embedder is not None:
        await repo.index_embeddings(ORG, [node], embedder.embed_batch)
    return node


@pytest.mark.anyio
async def test_an_exact_label_outranks_every_weaker_signal(memory_repo):
    exact = await _node(memory_repo, "Nate")
    await _node(memory_repo, "Nate went hiking with Joanna", type="Turn")
    r = await EntityResolver(repo=memory_repo).resolve(ORG, mention("Nate"))
    assert r.node_id == exact.id
    assert "exact" in r.matched_by


@pytest.mark.anyio
async def test_an_alias_binds_the_node_it_belongs_to(memory_repo):
    msft = await _node(memory_repo, "Microsoft", aliases=["MSFT"])
    r = await EntityResolver(repo=memory_repo).resolve(ORG, mention("MSFT"))
    assert r.node_id == msft.id and "alias" in r.matched_by


@pytest.mark.anyio
async def test_weak_signals_do_not_add_up_to_a_strong_one(memory_repo):
    """Three corroborating guesses are still guesses."""
    await _node(memory_repo, "Nate Corp")
    r = await EntityResolver(repo=memory_repo).resolve(ORG, mention("Nate"))
    assert len(r.matched_by) > 1
    assert r.confidence < 0.98, "corroboration was summed into exact-match certainty"


@pytest.mark.anyio
async def test_containment_is_scaled_by_how_much_of_the_label_it_covers(memory_repo):
    """A common noun inside a whole conversation turn must not bind to that turn: a resolved entity
    seeds graph expansion, so a wrong binding starts the walk where the question never pointed."""
    await _node(memory_repo, "[D3:11] Jolene: Well... we planned to play the console with my partner.", type="Turn")
    await _node(memory_repo, "Nintendo Co")
    resolver = EntityResolver(repo=memory_repo)
    buried = await resolver.resolve(ORG, mention("Console"))
    named = await resolver.resolve(ORG, mention("Nintendo"))
    assert not buried.resolved, f"a common noun bound to a sentence at {buried.confidence}"
    assert named.confidence > buried.confidence


@pytest.mark.anyio
async def test_ambiguity_is_reported_rather_than_resolved_away(memory_repo):
    await _node(memory_repo, "Apple")
    await _node(memory_repo, "Apple", type="Company")
    r = await EntityResolver(repo=memory_repo).resolve(ORG, mention("Apple"))
    assert r.resolved
    assert r.alternatives, "a tie was resolved silently"
    assert r.confidence - r.alternatives[0][2] <= AMBIGUITY_MARGIN


@pytest.mark.anyio
async def test_a_misspelling_is_found_but_not_acted_on(memory_repo):
    """A near spelling shares no word with the mention; the label embedding nominates it, trigram
    similarity scores it, and the weak binding is reported without becoming a node identity."""
    embedder = Embedder()
    embedder.similar("Nate", "Nathan", 0.5)
    await _node(memory_repo, "Nathan", embedder=embedder)
    r = await EntityResolver(embedder.embed, repo=memory_repo).resolve(ORG, mention("Nate"))
    assert r.confidence < MIN_BINDING_CONFIDENCE
    assert r.node_id is None
    assert r.matched_by == ["fuzzy"]


@pytest.mark.anyio
async def test_finding_nothing_is_a_clean_unresolved_not_an_error(memory_repo):
    r = await EntityResolver(repo=memory_repo).resolve(ORG, mention("Zzzzznotathing"))
    assert not r.resolved and r.confidence == 0.0 and r.matched_by == []


@pytest.mark.anyio
async def test_resolution_searches_every_type_with_no_gazetteer(memory_repo):
    await _node(memory_repo, "Acme", type="Zorblat")
    r = await EntityResolver(repo=memory_repo).resolve(ORG, mention("Acme"))
    assert r.resolved, "a node of an unfamiliar type could not be resolved"


@pytest.mark.anyio
async def test_a_mention_is_resolved_once_and_cached(memory_repo):
    node = await _node(memory_repo, "Nate")
    resolver = EntityResolver(repo=memory_repo)
    first = await resolver.resolve(ORG, mention("Nate"))
    await memory_repo.delete_nodes(ORG, [node.id])
    second = await resolver.resolve(ORG, mention("nate"))
    assert first.node_id == second.node_id == node.id, "a cached mention was resolved again"


@pytest.mark.anyio
async def test_only_bound_entities_become_candidates(memory_repo):
    await _node(memory_repo, "Nate")
    resolver = EntityResolver(repo=memory_repo)
    resolved = await resolver.resolve_all(ORG, [mention("Nate"), mention("Zzzznope")])
    cands = resolver.to_candidates(resolved)
    assert len(cands) == 1
    ev = cands[0].evidence[0]
    assert ev.surface == "entity" and ev.scale == "binding"
    assert ev.matched_text == "Nate", "the candidate lost which mention produced it"


@pytest.mark.anyio
async def test_dense_matching_only_searches_names(memory_repo):
    """A mention compared against prose binds common nouns to sentences, so dense matching reads
    only name surfaces."""
    embedder = Embedder()
    embedder.similar("JPMorgan Chase", "JP Morgan Chase", 0.91)
    bank = await _node(memory_repo, "JPMorgan Chase", embedder=embedder)
    embedder.same("JP Morgan Chase", "a note about jp morgan chase")
    await _node(memory_repo, "Meeting note", type="Turn", embedder=embedder, text="a note about jp morgan chase")

    r = await EntityResolver(embedder.embed, repo=memory_repo).resolve(ORG, mention("JP Morgan Chase"))
    assert r.node_id == bank.id and "dense" in r.matched_by


@pytest.mark.anyio
async def test_a_weak_dense_resemblance_does_not_bind(memory_repo):
    """Dense corroborates; it does not decide."""
    embedder = Embedder()
    embedder.similar("Console", "gaming chair", 0.62)
    await _node(memory_repo, "gaming chair", type="Turn", embedder=embedder)
    r = await EntityResolver(embedder.embed, repo=memory_repo).resolve(ORG, mention("Console"))
    assert not r.resolved, f"a 0.62 resemblance bound at {r.confidence}"
    assert "dense" in r.matched_by, "the evidence for the near-miss was discarded"


@pytest.mark.anyio
async def test_no_embedder_means_no_dense_evidence_not_an_error(memory_repo):
    await _node(memory_repo, "Nate")
    r = await EntityResolver(embed=None, repo=memory_repo).resolve(ORG, mention("Nate"))
    assert r.resolved and "dense" not in r.matched_by


@pytest.mark.integration
@pytest.mark.anyio
async def test_resolution_against_the_real_graph():
    """Happy path and both negative paths, on the corpus this engine is measured against."""
    r = EntityResolver()
    joanna = await r.resolve(ORG, mention("Joanna"))
    lowered = await r.resolve(ORG, mention("joanna"))
    console = await r.resolve(ORG, mention("Console"))
    missing = await r.resolve(ORG, mention("Zzzzznotathing"))

    if joanna.confidence == 0.0:
        pytest.skip("org has no graph loaded")

    assert joanna.resolved and joanna.confidence >= 0.9
    assert "exact" in joanna.matched_by
    assert lowered.node_id == joanna.node_id, "resolution was case-sensitive"
    assert not console.resolved, f"the common noun 'Console' bound to {console.label!r} at {console.confidence}"
    assert not missing.resolved and missing.confidence == 0.0
