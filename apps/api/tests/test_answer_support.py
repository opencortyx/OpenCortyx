"""Which retrieved evidence an answer actually rests on — pure, no model, no stores.

Retrieval packs a dozen blocks and an answer uses a few. The chat lists the ones it used as its
sources and folds the rest away, so attributing wrongly either hides the real source or presents a
record that merely resembles the question as if it supported the answer.
"""
from opencortyx.retrieval.answering.answer import _mark_support
from opencortyx.retrieval.answering.trust import supporting_citations

PROJECT = {"node_id": "p", "text": "project_id: PRJ-11\nname: Payments Core Rewrite\nstatus: amber\nspend_eur: 1850000"}
RISK_4 = {"node_id": "r4", "text": "risk_id: RSK-4\ndescription: Settlement mismatch under load\nseverity: high"}
RISK_8 = {"node_id": "r8", "text": "risk_id: RSK-8\ndescription: Vendor key-person dependency\nseverity: high"}
RISK_9 = {"node_id": "r9", "text": "risk_id: RSK-9\ndescription: Historical load gaps\nseverity: medium"}
LEAD = {"node_id": "l", "text": "employee_id: EMP-3\nname: Marek Nowak\ndiscipline: backend"}
CITATIONS = [PROJECT, RISK_4, RISK_8, RISK_9, LEAD]
ANSWER = ("Marek Nowak leads the Payments Core Rewrite project. "
          "Its biggest risk is Settlement mismatch under load.")


def test_the_answer_rests_on_the_records_that_state_it():
    assert supporting_citations(ANSWER, CITATIONS) == {"p", "r4", "l"}


def test_every_citation_is_kept_and_flagged():
    marked = _mark_support(ANSWER, CITATIONS)
    assert [c["node_id"] for c in marked] == ["p", "r4", "r8", "r9", "l"]
    assert {c["node_id"]: c["supports"] for c in marked} == {"p": True, "r4": True, "r8": False, "r9": False, "l": True}


def test_a_block_cited_by_marker_is_always_a_source():
    marked = _mark_support("The vendor dependency is a concern [3].", CITATIONS)
    assert next(c for c in marked if c["node_id"] == "r8")["supports"]


def test_nothing_attributable_shows_everything_rather_than_nothing():
    marked = _mark_support("Yes.", CITATIONS)
    assert all(c["supports"] for c in marked)
