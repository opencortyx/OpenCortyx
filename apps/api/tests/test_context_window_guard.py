"""The context window is a hard wall, so the dispatcher must make every request fit.

vLLM rejects an over-long prompt with a 400 instead of trimming it. Without a guard that means one
oversized node extracts NOTHING — measured: a 200,000-character input against an 8,192-token window
failed every extractor on that node. These tests pin the three properties that make the guard worth
having: it fits, it keeps BOTH ends of the template, and it says so.
"""
import asyncio

import pytest

from opencortyx.core.inference import llm as L
from opencortyx.core.config import settings


@pytest.fixture(autouse=True)
def _no_endpoint_probe(monkeypatch):
    """Discovery talks to a server; these tests are about the arithmetic, so pin the window."""
    async def _win(base_url, model_id):
        return 8192
    monkeypatch.setattr(L, "_endpoint_window", _win)


def _fit(prompt, max_tokens=1200):
    return asyncio.run(
        L._fit_to_window(prompt, max_tokens, "http://vllm:8000/v1", "opencortyx-llm", discover=True))


def test_short_prompt_is_untouched():
    p = "Extract entities.\n\nTEXT:\nAlice met Bob in Paris."
    assert _fit(p) == p


def test_oversized_prompt_is_brought_under_budget():
    budget = int((8192 - 1200 - L._PROMPT_OVERHEAD_TOKENS) * L._CHARS_PER_TOKEN)
    out = _fit("x" * 200_000)
    assert len(out) <= budget, f"still over budget: {len(out)} > {budget}"


def test_both_ends_of_the_template_survive():
    """The output contract usually sits at the TAIL. Trimming the end would delete it."""
    head = "Extract objective, verifiable FACTS.\n\nTEXT:\n"
    tail = '\n\nReturn ONLY JSON: {"records": [{"statement": "..."}]}'
    out = _fit(head + ("filler. " * 40_000) + tail)
    assert out.startswith("Extract objective, verifiable FACTS.")
    assert out.endswith(tail)
    assert "elided" in out, "the elision must be marked, not silent"


def test_elision_is_counted():
    from opencortyx.core import observability as obs

    def _total():
        try:
            return sum(s.value for m in obs.REGISTRY.collect()
                       for s in m.samples
                       if s.name == "opencortyx_prompt_chars_dropped_total")
        except Exception:      # prometheus_client absent → the guard still has to work
            return None

    before = _total()
    _fit("y" * 200_000)
    after = _total()
    if before is not None:
        assert after > before, "characters were dropped without being recorded"


def test_impossible_request_is_left_for_the_provider_to_reject():
    """max_tokens alone over the window: shrinking the caller's requested OUTPUT would be a
    different lie, so the guard declines to act and the provider gets to say what is wrong."""
    p = "z" * 100_000
    assert _fit(p, max_tokens=9000) == p


def test_configured_window_is_the_fallback(monkeypatch):
    """An endpoint that reports no window must not mean 'no limit'."""
    async def _none(base_url, model_id):
        return None
    monkeypatch.setattr(L, "_endpoint_window", _none)
    out = asyncio.run(
        L._fit_to_window("q" * 500_000, 500, None, "some-model", discover=False))
    budget = int((settings.LLM_MAX_MODEL_LEN - 500 - L._PROMPT_OVERHEAD_TOKENS) * L._CHARS_PER_TOKEN)
    assert len(out) <= budget
