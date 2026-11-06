"""A citation must support the sentence it is attached to.

Mutation tests: start from an answer its sources support, change it the way a model gets it wrong
- swap a number, move a value to another record, swap a name, negate it, cite the wrong source - and
check each change is caught. No model or network: similarity uses a word-count embedding, and the
verification model is a stand-in that reads negation.
"""
import asyncio
import math
import re
import uuid

import pytest

from opencortyx.retrieval.answering import trust


async def _embed(texts):
    """A deterministic bag-of-words vector: similar wording, similar vector."""
    out = []
    for t in texts:
        v = [0.0] * 64
        for w in re.findall(r"[a-z0-9]+", (t or "").lower()):
            v[hash(w) % 64] += 1.0
        out.append(v)
    return out


def _cites(*texts):
    return [{"node_id": str(uuid.uuid4()), "text": t[:240], "type": "Record"} for t in texts]


def _check(answer, cites, links=(), nli=None, **kw):
    texts = {c["node_id"]: full for c, full in zip(cites, kw.pop("full", [c["text"] for c in cites]))}
    return asyncio.run(trust.verify_citations(answer, cites, texts, links, embed=_embed, nli_model=nli, **kw))


BERLIN = "Branch policy Berlin: the cash handling limit at the Berlin branch is 40,000 EUR per day."
MUNICH = "Branch policy Munich: the cash handling limit at the Munich branch is 50,000 EUR per day."


def test_a_sentence_its_citation_supports_is_supported():
    r = _check("The cash handling limit at the Berlin branch is 40,000 EUR per day [1].", _cites(BERLIN, MUNICH))
    (s,) = r["sentences"]
    assert r["supported"] and s["supported_by"] == [1] and r["citation_precision"] == 1.0


def test_a_swapped_number_is_caught_even_though_the_number_is_elsewhere_in_the_context():
    r = _check("The cash handling limit at the Berlin branch is 50,000 EUR per day [1].", _cites(BERLIN, MUNICH))
    assert not r["supported"] and r["unsupported"], "50,000 is Munich's, not Berlin's"


def test_a_value_moved_to_another_record_is_caught():
    a = "Order DBO-1: customer John Doe, total 120 EUR, status shipped."
    b = "Order DBO-2: customer Jane Roe, total 75 EUR, status pending."
    r = _check("John Doe's order DBO-1 totals 75 EUR.", _cites(a, b))
    assert not r["supported"], "the total is from another order, and the two orders are not linked"


def test_facts_joined_across_records_the_graph_links_are_supported():
    customer = "Customer C-1: John Doe, based in Oslo."
    order = "Order DBO-1 was placed by customer C-1 and contains one Keyboard."
    cites = _cites(customer, order)
    r = _check("John Doe ordered a Keyboard.", cites, links=[(cites[0]["node_id"], cites[1]["node_id"])])
    assert r["supported"] and r["sentences"][0]["supported_by"] == [1, 2]
    unlinked = _check("John Doe ordered a Keyboard.", cites)
    assert not unlinked["supported"], "without the link the join is a guess"


def test_a_wrong_citation_is_reported_and_the_sentence_reattributed_to_the_source_that_says_it():
    r = _check("The cash handling limit at the Munich branch is 50,000 EUR per day [1].", _cites(BERLIN, MUNICH))
    (s,) = r["sentences"]
    assert s["supported"] and s["supported_by"] == [2]
    assert s["reason"] == "its citation does not support it; another source does"
    assert r["citation_precision"] == 0.0, "the citation the reader saw was wrong"


def test_a_swapped_name_is_caught():
    r = _check("The cash handling limit at the Hamburg branch is 40,000 EUR per day [1].", _cites(BERLIN, MUNICH))
    assert not r["supported"]


def test_negation_is_caught_by_the_verification_model(monkeypatch):
    block = "The board approved the 2026 budget on 3 March."

    async def fake_entails(model_id, pairs):
        out = []
        for premise, hyp in pairs:
            negated = (" not " in f" {hyp.lower()} ") != (" not " in f" {premise.lower()} ")
            overlap = len(set(hyp.lower().split()) & set(premise.lower().split())) / max(1, len(hyp.split()))
            out.append({"entail": 0.0 if negated else (0.9 if overlap > 0.5 else 0.1),
                        "contradict": 0.95 if negated and overlap > 0.4 else 0.01})
        return out
    monkeypatch.setattr("opencortyx.core.inference.nli.entails", fake_entails)
    good = _check("The board approved the 2026 budget on 3 March [1].", _cites(block), nli="nli-model")
    bad = _check("The board did not approve the 2026 budget on 3 March [1].", _cites(block), nli="nli-model")
    assert good["supported"] and good["checked_with"] == "nli"
    assert not bad["supported"] and "contradicts it" in bad["sentences"][0]["reason"]


def test_without_a_verification_model_the_word_check_runs_and_says_so():
    r = _check("The board approved the 2026 budget [1].", _cites("The board approved the 2026 budget."))
    assert r["checked_with"] == "words+similarity" and r["supported"]


def test_when_the_verification_model_fails_the_other_checks_still_run(monkeypatch):
    async def broken(model_id, pairs):
        return None
    monkeypatch.setattr("opencortyx.core.inference.nli.entails", broken)
    r = _check("The board approved the 2026 budget [1].", _cites("The board approved the 2026 budget."), nli="x")
    assert r["checked_with"] == "words+similarity" and r["supported"]


def test_a_citation_marker_past_the_list_is_ignored_and_the_sentence_still_checked():
    r = _check("The cash handling limit at the Berlin branch is 40,000 EUR per day [9].", _cites(BERLIN))
    assert r["supported"] and r["sentences"][0]["cited"] == []


def test_list_items_are_sentences_of_their_own():
    answer = "Limits:\n- Berlin branch limit is 40,000 EUR [1]\n- Munich branch limit is 90,000 EUR [2]"
    r = _check(answer, _cites(BERLIN, MUNICH))
    texts = {s["text"]: s["supported"] for s in r["sentences"]}
    assert texts["Berlin branch limit is 40,000 EUR"] is True
    assert texts["Munich branch limit is 90,000 EUR"] is False


def test_a_sentence_with_nothing_to_check_is_not_judged():
    r = _check("Yes.", _cites(BERLIN))
    assert r["judged"] == 0 and r["supported"]


def test_strict_removes_only_the_unsupported_sentences():
    answer = ("The Berlin branch limit is 40,000 EUR per day [1]. "
              "The Berlin branch also opens on Sundays [1].")
    cites = _cites(BERLIN)
    r = _check(answer, cites)
    assert r["unsupported"] == ["The Berlin branch also opens on Sundays."]
    assert trust.strict_answer(answer, r) == "The Berlin branch limit is 40,000 EUR per day [1]."


def test_the_full_text_is_checked_not_the_240_character_snippet():
    long_text = "Preamble. " * 40 + "Espresso extraction takes 25 seconds at 9 bar."
    cites = [{"node_id": str(uuid.uuid4()), "text": long_text[:240], "type": "Chunk"}]
    r = _check("Espresso extraction takes 25 seconds [1].", cites, full=[long_text])
    assert r["supported"]


def test_a_stale_source_never_reaches_the_answer(monkeypatch):
    from opencortyx.retrieval.answering import answer as ans
    live, gone = uuid.uuid4(), uuid.uuid4()

    class _Repo:
        async def get_many(self, ws, ids):
            from types import SimpleNamespace
            return [SimpleNamespace(id=live, properties={}), SimpleNamespace(id=gone, properties={"__stale": True})]
    monkeypatch.setattr("opencortyx.core.substrate.NodeRepo", _Repo)
    pkg = {"context": f'{{"node_id": "{live}", "text": "a"}}\n{{"node_id": "{gone}", "text": "b"}}',
           "citations": [{"node_id": str(live)}, {"node_id": str(gone)}], "links": [[str(live), str(gone)]]}
    out = asyncio.run(ans._drop_stale(uuid.uuid4(), pkg))
    assert [c["node_id"] for c in out["citations"]] == [str(live)]
    assert str(gone) not in out["context"] and out["links"] == [] and not out.get("abstained")


# ── the citation-check model: what the registry accepts, and the switch ─────────────────────────

def _hub(monkeypatch, task, files=("config.json", "model.safetensors", "tokenizer.json")):
    from types import SimpleNamespace
    info = SimpleNamespace(id="org/m", pipeline_tag=task, gated=False,
                           siblings=[SimpleNamespace(rfilename=f, size=10) for f in files])

    class _Api:
        def model_info(self, repo, files_metadata=True, token=None):
            return info
    monkeypatch.setattr("huggingface_hub.HfApi", _Api)


@pytest.mark.parametrize("task", ["text-classification", "zero-shot-classification"])
def test_an_nli_classifier_runs_as_the_citation_check_and_not_as_a_generator(monkeypatch, task):
    from opencortyx.core.inference import hf_hub
    _hub(monkeypatch, task)
    assert hf_hub.info("org/m", role="verification")["runs_in_opencortyx"]
    gen = hf_hub.info("org/m")
    assert not gen["runs_in_opencortyx"] and "text-generation" in gen["why_not"]


def test_a_generator_is_refused_as_the_citation_check(monkeypatch):
    from opencortyx.core.inference import hf_hub
    _hub(monkeypatch, "text-generation")
    out = hf_hub.info("org/m", role="verification")
    assert not out["runs_in_opencortyx"] and "inference" in out["why_not"]


def test_switching_the_citation_check_off_uses_no_model(monkeypatch):
    from opencortyx.core.inference import nli

    async def registered(ws, role):
        return {"provider": "transformers", "model_id": "cross-encoder/nli-deberta-v3-xsmall"}
    monkeypatch.setattr("opencortyx.core.inference.registry.resolve_model", registered)
    monkeypatch.setattr("opencortyx.core.config.runtime.get", lambda k: True if k == "citation_check" else None)
    assert asyncio.run(nli.model_for(uuid.uuid4())) == "cross-encoder/nli-deberta-v3-xsmall"
    monkeypatch.setattr("opencortyx.core.config.runtime.get", lambda k: False if k == "citation_check" else None)
    assert asyncio.run(nli.model_for(uuid.uuid4())) is None


def test_a_hosted_model_is_never_used_as_the_in_process_check(monkeypatch):
    from opencortyx.core.inference import nli

    async def hosted(ws, role):
        return {"provider": "openai", "model_id": "gpt-x"}
    monkeypatch.setattr("opencortyx.core.inference.registry.resolve_model", hosted)
    monkeypatch.setattr("opencortyx.core.config.runtime.get", lambda k: True)
    assert asyncio.run(nli.model_for(uuid.uuid4())) is None


def test_facts_from_several_linked_records_are_supported_together():
    """"Heath Partners holds LIC-1097, LIC-1004 and LIC-1007": the customer in one record, each
    licence in its own, all linked to the customer. No record says it; together they do."""
    cust = "Customer C-101: Heath Partners, region LATAM."
    l1, l2, l3 = (f"Licence {x} is held by customer C-101." for x in ("LIC-1097", "LIC-1004", "LIC-1007"))
    other = "Licence LIC-2000 is held by customer C-999."
    cites = _cites(cust, l1, l2, l3, other)
    links = [(cites[0]["node_id"], c["node_id"]) for c in cites[1:4]]
    s = "Heath Partners holds the licences LIC-1097, LIC-1004 and LIC-1007."
    r = _check(s, cites, links=links)
    assert r["supported"] and sorted(r["sentences"][0]["supported_by"]) == [1, 2, 3, 4]
    assert not _check(s, cites)["supported"], "unlinked, the same records prove nothing"
    wrong = _check("Heath Partners holds the licences LIC-1097 and LIC-2000.", cites, links=links)
    assert not wrong["supported"], "LIC-2000 is another customer's, not linked to Heath Partners"


def test_a_sentence_one_source_contradicts_is_not_rescued_by_another_that_shares_its_words(monkeypatch):
    policy = "Sublicensing is not permitted under any licence type."
    letter = "Under the licensing policy, licences are permitted for broadcast and retail."

    async def fake_entails(model_id, pairs):
        return [{"entail": 0.0, "contradict": 0.97 if "not permitted" in p and "are permitted" in h else 0.01}
                for p, h in pairs]
    monkeypatch.setattr("opencortyx.core.inference.nli.entails", fake_entails)
    r = _check("Yes, sublicences are permitted under the licensing policy.", _cites(policy, letter), nli="m")
    assert not r["supported"] and r["sentences"][0]["reason"] == "a source contradicts it"


def test_a_letter_about_another_licence_does_not_undo_a_correct_list(monkeypatch):
    """The model reads "Licensee: Heath Partners ... licence LIC-1107" as opposed to "Heath Partners
    holds LIC-1097, LIC-1004 and LIC-1007". That letter names none of the three licences; the linked
    records name them all - the specifics decide."""
    cust = "Customer C-101: Heath Partners, region LATAM."
    ls = [f"Licence {x} is held by customer C-101." for x in ("LIC-1097", "LIC-1004", "LIC-1007")]
    sibling = "Licensee: Heath Partners (C-101). This letter confirms the terms of licence LIC-1107."
    cites = _cites(cust, *ls, sibling)
    links = [(cites[0]["node_id"], c["node_id"]) for c in cites[1:4]]

    async def fake_entails(model_id, pairs):
        return [{"entail": 0.0, "contradict": 0.9 if "LIC-1107" in p else 0.0} for p, h in pairs]
    monkeypatch.setattr("opencortyx.core.inference.nli.entails", fake_entails)
    r = _check("Heath Partners holds the licences LIC-1097, LIC-1004 and LIC-1007.", cites, links=links, nli="m")
    assert r["supported"], r["sentences"]
