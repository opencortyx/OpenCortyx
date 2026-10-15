"""What the grounding gate checks an answer against — pure, no model, no stores.

The model is handed each node's FULL text in `context`; citations carry only a 240-char display
snippet. Grounding against the snippet rejected any fact that sat later in a chunk, so a correct,
already-streamed answer was swapped for an abstain.
"""
import json

from opencortyx.retrieval.answering.answer import _grounding_blocks
from opencortyx.retrieval.answering.trust import _numbers, unsupported_numbers

COFFEE = ("The Art of Coffee Brewing\n\nCoffee brewing is both a science and a craft. " + "x " * 150
          + "Espresso forces hot water through finely ground coffee in about 25 seconds.")


def _pkg(*texts):
    context = "\n".join(json.dumps({"node_id": str(i), "type": "Chunk", "text": t}) for i, t in enumerate(texts))
    return {"context": context, "citations": [{"node_id": str(i), "text": t[:240]} for i, t in enumerate(texts)]}


def test_blocks_are_the_full_node_text_not_the_citation_snippet():
    blocks = _grounding_blocks(_pkg(COFFEE, "Exploring the Red Planet"))
    assert blocks == [COFFEE, "Exploring the Red Planet"]


def test_a_number_past_the_snippet_is_still_evidence():
    blocks = _grounding_blocks(_pkg(COFFEE))
    assert not unsupported_numbers("It takes about 25 seconds.", _numbers(" ".join(blocks)))


def test_falls_back_to_citations_when_context_is_not_json():
    pkg = {"context": "plain text context", "citations": [{"node_id": "a", "text": "snippet"}]}
    assert _grounding_blocks(pkg) == ["snippet"]
