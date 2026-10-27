"""L0 routes: which kinds of answer a question may need. Weighted hypotheses, several at once.

    KNOWLEDGE  the graph: entities, facts, relationships (always considered)
    PASSAGE    document text, read as passages (always considered)
    DATA_SQL   the rows themselves, through SQL (retrieval/l2_data): counts, totals, rankings,
               listings, a value of one record that no node carries
    AGGREGATE  counting the graph's own nodes (l2_graph/aggregate.py)
    CLARIFY    the question names something that binds to two different things equally well

A route is a reason to TRY a layer, never a reason to skip the others: a question routed to data
that the data cannot answer still gets the ordinary retrieval, as before.
"""
from __future__ import annotations

from opencortyx.retrieval.contracts import Intent, QueryPlan

#: Two bindings this close in confidence, with the same name, are one name for two things.
AMBIGUOUS_MARGIN = 0.9


def routes(plan: QueryPlan, question: str) -> dict[str, float]:
    from opencortyx.retrieval.l2_data import is_data_question
    out = {"KNOWLEDGE": 1.0, "PASSAGE": 0.8}
    if plan.intent == Intent.AGGREGATION:
        out["AGGREGATE"] = 0.9
        out["DATA_SQL"] = 0.9
    elif is_data_question(question):
        out["DATA_SQL"] = 0.7
    elif plan.intent in (Intent.EXACT_LOOKUP, Intent.ENTITY) and plan.mentions:
        # "Does customer 101 have a free pass?" - a value of one record may live only in its row.
        out["DATA_SQL"] = 0.4
    return out


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def ambiguous_bindings(bound) -> list[dict]:
    """Mentions whose best binding has an equally good alternative of the same name."""
    out = []
    for r in bound:
        best = float(r.confidence or 0.0)
        same = [(str(nid), label, float(conf)) for nid, label, conf in (r.alternatives or [])
                if _norm(label) == _norm(r.label) and str(nid) != str(r.node_id)
                and float(conf) >= best * AMBIGUOUS_MARGIN]
        if same:
            out.append({"mention": r.mention.text,
                        "candidates": [{"node_id": str(r.node_id), "label": r.label}]
                                      + [{"node_id": n, "label": lbl} for n, lbl, _ in same]})
    return out
