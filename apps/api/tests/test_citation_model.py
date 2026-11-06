"""The citation-check model on real sentences - run where it is downloaded, skipped elsewhere.

These are the cases a word check gets wrong, from a real answer that went wrong: the built-in model
answered "Yes, sublicences are permitted" to a policy that says "Sublicensing is not permitted under
any licence type", and the words matched. Read sentence by sentence (trust._windows_for), the model
catches it; and one unrelated policy sentence must not overrule the one that states a true answer
(trust._contradicted).

Run inside a container that has the model in its cache (the Models page downloads it):
    python -m pytest tests/test_citation_model.py
"""
import asyncio
import uuid

import pytest

from opencortyx.retrieval.answering import trust
from tests.goldset import domain

MODEL = "cross-encoder/nli-deberta-v3-xsmall"


def _downloaded() -> bool:
    try:
        from opencortyx.core.inference import hf_hub
        return hf_hub.is_downloaded(MODEL)
    except Exception:                                         # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _downloaded(), reason=f"{MODEL} is not downloaded here")

POLICY = domain.policy_markdown()
BOARD = "The board approved the 2026 budget on 3 March. The Berlin branch cash limit is 40,000 EUR per day."

CASES = [
    (POLICY, "Yes, sublicences are permitted under the licensing policy [1].", False),
    (POLICY, "Sublicensing is not permitted under any licence type [1].", True),
    (POLICY, "The licensing policy states that a licensee may terminate early with ninety days written notice [1].", True),
    (POLICY, "A licensee may terminate early without any notice [1].", False),
    (POLICY, "Fees are reviewed every January and capped at a five percent increase per year [1].", True),
    (POLICY, "Fees are never reviewed [1].", False),
    (POLICY, "Education licences receive a thirty percent discount [1].", True),
    (BOARD, "The board approved the 2026 budget on 3 March [1].", True),
    (BOARD, "The board did not approve the 2026 budget [1].", False),
    (BOARD, "The board rejected the 2026 budget [1].", False),
    (BOARD, "The Berlin branch cash limit is 40,000 EUR per day [1].", True),
    (BOARD, "The Berlin branch has no cash limit [1].", False),
]


@pytest.mark.parametrize("source,sentence,supported", CASES)
def test_the_model_tells_a_sentence_its_source_states_from_one_it_contradicts(source, sentence, supported):
    cite = {"node_id": str(uuid.uuid4()), "text": source[:240]}
    r = asyncio.run(trust.verify_citations(sentence, [cite], {cite["node_id"]: source}, (), nli_model=MODEL))
    assert r["checked_with"] == "nli"
    assert r["supported"] is supported, r["sentences"]
