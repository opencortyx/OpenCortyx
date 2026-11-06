"""The Extractors form and the engine must agree on what a mechanism is.

They drifted once, silently and in the worst direction: the editor offered `unstructured.topic` and
`structured.sql`, neither of which `_produce_parsed` has a branch for, so an extractor built on either
**saved, ran, reported success and wrote nothing**. This test also pointed at a file that had moved
(`ingest/enrich_templates.py`) and so skipped itself for months without anyone noticing - it now
fails, rather than skips, when the engine is not where it says.

The engine owns the vocabulary (`DEPENDENT_MECHANISM_REFS`, declared beside the branches that
implement it). The form (`apps/web/lib/extractors/form.ts`) declares `SAVED_REFS`, every mechanism it
saves. Read as TEXT, not imported, so this runs in the api container (which has no `apps/web`, where
only the engine half runs) and on a bare checkout.
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


ENGINE = _find("apps", "api", "opencortyx", "ingest", "extraction", "templates.py") \
    or _find("opencortyx", "ingest", "extraction", "templates.py")
FORM = _find("apps", "web", "lib", "extractors", "form.ts")


@pytest.fixture(scope="module")
def engine_src():
    assert ENGINE is not None, "the extraction engine is not at ingest/extraction/templates.py"
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


def test_the_form_saves_only_what_the_engine_implements(declared):
    if FORM is None:
        pytest.skip("apps/web not reachable from here (running inside the api container?)")
    m = re.search(r"export const SAVED_REFS = \[(.*?)\]", FORM.read_text(encoding="utf-8"), re.S)
    assert m, "SAVED_REFS not found in lib/extractors/form.ts"
    saved = re.findall(r'"([a-z_.]+)"', m.group(1))
    phantom = [r for r in saved if r not in declared]
    assert saved and not phantom, (
        "the form saves mechanism(s) the engine does not implement: %s - such an extractor saves, "
        "runs, reports success and writes nothing" % ", ".join(phantom))


def test_the_form_and_the_engine_reserve_the_same_field_names(engine_src):
    if FORM is None:
        pytest.skip("apps/web not reachable from here")
    engine = re.search(r"RESERVED_FIELDS = frozenset\(\{(.*?)\}\)", engine_src, re.S)
    form = re.search(r"export const RESERVED_FIELDS = \[(.*?)\]", FORM.read_text(encoding="utf-8"), re.S)
    assert engine and form
    assert set(re.findall(r'"([a-z_]+)"', engine.group(1))) == set(re.findall(r'"([a-z_]+)"', form.group(1)))
