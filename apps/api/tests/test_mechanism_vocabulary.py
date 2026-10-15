"""The Studio and the engine must agree on what a mechanism is.

They had drifted, and the drift was silent in the worst direction: the Studio offered
`unstructured.topic` and `structured.sql`, neither of which `_produce_parsed` has a branch for, so an
extractor built on either **saved, ran, reported success and wrote nothing**. In the other direction
`structured.llm` and `unstructured.attributes` were implemented and used by seven built-ins, and
could not be picked at all.

The engine owns the vocabulary (`DEPENDENT_MECHANISM_REFS`, declared beside the branches that
implement it). These tests hold the Studio to it.

Read as TEXT, not imported: `enrich_templates` pulls in the whole ingest stack, and this needs to run
in the api container (which has no `apps/web`) and on a bare checkout (which has no dependencies).
Comparing two files is the whole job.
"""
import re
from pathlib import Path

import pytest


def _find(*parts):
    for parent in Path(__file__).resolve().parents:
        cand = parent.joinpath(*parts)
        if cand.exists():
            return cand
    return None


ENGINE = _find("apps", "api", "opencortyx", "ingest", "enrich_templates.py") \
    or _find("opencortyx", "ingest", "enrich_templates.py")
STUDIO = _find("apps", "web", "app", "dashboard", "custom-assets", "extractorVocab.ts")
# The preview badge colours live with the rest of the Studio's state (split out of page.tsx).
DRAFT = _find("apps", "web", "app", "dashboard", "custom-assets", "useExtractorDraft.ts")


@pytest.fixture(scope="module")
def engine_src():
    if ENGINE is None:
        pytest.skip("enrich_templates.py not reachable from here")
    return ENGINE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def declared(engine_src):
    m = re.search(r"DEPENDENT_MECHANISM_REFS = \((.*?)\)", engine_src, re.S)
    assert m, "the engine no longer declares DEPENDENT_MECHANISM_REFS"
    return re.findall(r'"([a-z_.]+)"', m.group(1))


def test_every_declared_mechanism_has_a_branch(engine_src, declared):
    """The list is only worth having if it describes the code beside it."""
    body = engine_src[engine_src.index("async def _produce_parsed"):
                      engine_src.index("def _select_types")]
    llm_refs = re.search(r"_LLM_REFS = \((.*?)\)", engine_src, re.S)
    shared = set(re.findall(r'"([a-z_.]+)"', llm_refs.group(1))) if llm_refs else set()
    attr_ref = re.search(r'_ATTR_REF = "([a-z_.]+)"', engine_src)
    if attr_ref:
        shared.add(attr_ref.group(1))
    for ref in declared:
        assert f'"{ref}"' in body or ref in shared, \
            f"{ref} is declared but _produce_parsed has no branch for it"


def test_the_studio_offers_exactly_what_the_engine_implements(declared):
    if STUDIO is None:
        pytest.skip("apps/web not reachable from here (running inside the api container?)")
    m = re.search(r"const DEPENDENT_MECHANISMS = \[(.*?)\];", STUDIO.read_text(encoding="utf-8"), re.S)
    assert m, "DEPENDENT_MECHANISMS not found in the Studio page"
    offered = re.findall(r'"([a-z_.]+)"', m.group(1))

    phantom = [r for r in offered if r not in declared]
    missing = [r for r in declared if r not in offered]
    assert not phantom, (
        "the Studio offers mechanism(s) the engine does not implement: %s — choosing one saves, "
        "runs, reports success and writes nothing" % ", ".join(phantom))
    assert not missing, (
        "the engine implements mechanism(s) the Studio will not let anyone pick: %s"
        % ", ".join(missing))


def test_the_preview_badge_covers_every_reported_mechanism(declared):
    """A mechanism with no badge rendered as an unexplained grey chip — which is exactly how
    `source` looked on cog.claim's `speaker` before it had a control."""
    if DRAFT is None:
        pytest.skip("apps/web not reachable from here")
    m = re.search(r"const mechBadge = \(m: string\) => \(\{(.*?)\}\[m\]",
                  DRAFT.read_text(encoding="utf-8"), re.S)
    assert m, "mechBadge not found"
    coloured = set(re.findall(r'"?([a-z-]+)"?:\s*"bg-', m.group(1)))

    # `_mech_short` shortens a ref to its last segment; the LLM refs all collapse to "llm" and
    # `unstructured.attributes` to "per-attribute". Plus what the preview reports directly.
    short = set()
    for ref in declared:
        if ref.endswith(".llm"):
            short.add("llm")
        elif ref == "unstructured.attributes":
            short.add("per-attribute")
        else:
            short.add(ref.split(".", 1)[1])
    unstyled = sorted((short | {"source", "copy", "constant"}) - coloured)
    assert not unstyled, "mechBadge has no colour for: %s" % ", ".join(unstyled)
