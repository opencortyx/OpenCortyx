"""The MMR cut that trims retrieval to the context budget: what counts as the same evidence twice."""
import uuid

from opencortyx.retrieval.contracts import Candidate
from opencortyx.retrieval.l3_rerank.reranker import diversify

PROJECT = uuid.uuid4()


def cand(node_type, text, score, parent=PROJECT):
    return Candidate(node_id=uuid.uuid4(), node_type=node_type, text=text, rerank_score=score,
                     properties={"source_ref": str(parent)})


def test_a_records_lead_and_risk_are_two_facts_not_one():
    risk = cand("Risk", "Risk RSK-4. description=Settlement mismatch under load", 0.62)
    lead = cand("Lead", "Lead Marek Nowak. employee_id=EMP-3; discipline=backend", 0.60)
    other = cand("Sponsor", "Sponsor Ingrid Halvorsen. role=Chief Operating Officer", 0.50, uuid.uuid4())
    assert lead in diversify([risk, lead, other], limit=2)


def test_two_chunks_of_one_document_are_still_one_piece_of_evidence():
    doc = uuid.uuid4()
    a = cand("Chunk", "Leave policy: employees accrue holiday monthly", 0.62, doc)
    b = cand("Chunk", "Carry-over is capped at five days per year", 0.60, doc)
    elsewhere = cand("Chunk", "Expenses are reimbursed within thirty days", 0.50, uuid.uuid4())
    assert diversify([a, b, elsewhere], limit=2) == [a, elsewhere]
