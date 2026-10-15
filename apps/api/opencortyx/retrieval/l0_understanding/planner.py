"""L0 — Query planner: turns a question into a structured retrieval plan.

    question -> intent + mentions + constraints -> STRATEGY WEIGHTS + hop budget + sub-queries

The output is a set of HYPOTHESES about where the answer might live, each with a weight. A weight
governs how loudly a surface VOTES during fusion, and it never falls below `_FLOOR`:

  - A surface at weight 0.15 still contributes RRF votes if it ran.
  - A weight can therefore be wrong without making a question unanswerable.

Whether a surface RUNS AT ALL is a separate decision, made in `policy.py` and attached to the plan
as `plan.policy`. Keeping the two apart is deliberate. For a long time only weights existed, so
every surface ran on every question and an exact lookup paid for a full ANN sweep and a graph walk
before down-weighting both to the floor — the classification was computed and then ignored by the
thing it was meant to govern. Narrowing execution is made safe by a confidence gate and by
widening when the narrow set underfills, not by pretending the choice does not exist.

The previous engine sat at the other extreme: retrievers were fixed per PROFILE, chosen before the
question was read. A lookup and a four-hop reasoning question executed identical plans.

Weights are floors-and-boosts, never a partition. They do not sum to 1, because they are not
shares of a budget — they are independent confidences that a given surface is worth weighting up
during fusion.
"""
from __future__ import annotations

import re

from typing import Optional

from opencortyx.retrieval.contracts import Intent, QueryPlan
from opencortyx.retrieval.l0_understanding import constraints as C
from opencortyx.retrieval.l0_understanding import entities as E
from opencortyx.retrieval.l0_understanding.intent import (
    classify, is_relational, is_temporal,
)
from opencortyx.retrieval.l0_understanding.policy import plan_execution
from opencortyx.retrieval.l0_understanding.vocabulary import (
    TypeVocabulary, score_vocabulary,
)

#: Every surface the plan can weight. Registered here so a plan can never silently name a surface
#: that does not exist — the failure mode that let `_TYPE_ORDER` demote an unregistered type.
SURFACES = (
    "attr_embedding", "bm25", "identifier", "fuzzy", "metadata", "entity",
    "graph", "ontology",
)

#: No surface ever falls below this. A plan is a hypothesis about the answer's location, and a
#: wrong hypothesis must cost latency, not recall.
_FLOOR = 0.15


def _base_weights() -> dict[str, float]:
    return {s: _FLOOR for s in SURFACES}


def _normalize(query: str) -> str:
    q = (query or "").strip()
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"[“”]", '"', q)
    q = re.sub(r"[‘’]", "'", q)
    return q


def decompose(query: str, plan: QueryPlan) -> list[str]:
    """Split a question that asks several things into retrievable parts.

    Conservative on purpose: splits on explicit conjunction of clauses, conditional/temporal
    clauses (if/after/before/because/while/when), and multi-entity shared predicates.
    """
    q = plan.normalized_query or query or ""
    parts: list[str] = []

    # 1. Relative-clause chains: "who also...", "which then..."
    m = re.split(r"\s+(?:and (?:also )?)?(?:who|which|that)\s+(?:also|then)\s+", q, flags=re.I)
    if len(m) > 1:
        parts.extend([p.strip(" ?.,") for p in m if len(p.strip()) > 8])

    # 2. Conjunctions / conditional clauses: "X if Y", "X after Y", "X before Y", "X because Y", "X while Y"
    if not parts:
        m_clause = re.split(r"\s+(?:if|after|before|because|while|when|besides|prior to)\s+", q, flags=re.I)
        if len(m_clause) > 1 and all(len(c.strip()) > 8 for c in m_clause):
            parts.extend([p.strip(" ?.,") for p in m_clause])

    # 3. "through / via" chains
    if not parts and plan.intent is Intent.MULTI_HOP:
        m2 = re.match(r"^(.*?)\s+(?:through|via|by way of)\s+(.*)$", q, re.I)
        if m2 and all(len(g.strip()) > 6 for g in m2.groups()):
            parts = [m2.group(1).strip(" ?.,"), m2.group(2).strip(" ?.,")]

    # 4. Shared / multiple entities: "What kind of interests do Joanna and Nate share?" -> "Joanna interests", "Nate interests"
    if not parts and len(plan.mentions) >= 2 and any(w in q.lower() for w in ("share", "both", "together", "common", "and", "like", "appreciate")):
        ent_names = [m.text for m in plan.mentions]
        topic = q
        for name in ent_names:
            topic = re.sub(re.escape(name), " ", topic, flags=re.I)
        topic = re.sub(r"^\s*(what|which|who|whom|whose|when|where|why|how)\s+(?:is|are|was|were|do|does|did|has|have|had|kind of)?\s*", " ", topic, flags=re.I)
        topic = re.sub(r"\b(share|both|together|in common|and|do|does|did|is|are|the|of)\b", " ", topic, flags=re.I)
        topic = re.sub(r"\s+", " ", topic).strip(" ?.,")
        if topic and len(topic) > 2:
            for name in ent_names[:3]:
                parts.append(f"{name} {topic}")

    clean_parts = []
    for p in parts:
        p_clean = p.strip(" ?.,")
        if len(p_clean) > 4 and p_clean.lower() != q.lower() and p_clean not in clean_parts:
            clean_parts.append(p_clean)

    if len(clean_parts) < 2:
        return []
    return clean_parts[:4]


def build_plan(query: str, vocab: Optional[TypeVocabulary] = None) -> QueryPlan:
    """The full L0 pass. Pure and synchronous — no I/O, so it is cheap to test and to trace.

    `vocab` is the corpus's discovered type vocabulary, read by the caller (the orchestrator
    loads and caches it per org). Passing it in rather than fetching it here is what keeps this
    function pure: with no vocabulary the plan simply carries no type hypotheses and behaves
    exactly as it did before they existed.
    """
    norm = _normalize(query)
    cls = classify(norm)

    plan = QueryPlan(
        raw_query=query or "",
        normalized_query=norm,
        intent=cls.intent,
        cardinality=cls.cardinality,
        mentions=E.detect_mentions(norm),
        temporal=C.extract_temporal(norm),
        attributes=C.extract_attribute_constraints(norm),
        strategies=_base_weights(),
    )
    plan.notes.append(f"intent={cls.intent.value} conf={cls.confidence:.2f} {cls.signals}")

    w = plan.strategies

    def boost(surface: str, value: float, why: str):
        if value > w.get(surface, 0.0):
            w[surface] = value
            plan.notes.append(f"{surface}<-{value:.2f} ({why})")

    # ── intent-shaped weighting ──────────────────────────────────────────────────────────────
    if cls.intent is Intent.EXACT_LOOKUP:
        boost("entity", 0.95, "exact lookup needs a resolved node")
        boost("identifier", 0.9, "a code or reference resolves exactly")
        boost("bm25", 0.85, "identifiers are lexical, not semantic")
        boost("attr_embedding", 0.7, "the answer is an attribute")
        boost("fuzzy", 0.5, "identifier variants")
    elif cls.intent is Intent.KEYWORD:
        boost("bm25", 0.95, "document language")
        boost("identifier", 0.6, "a quoted code is still lexical")
    elif cls.intent is Intent.SEMANTIC:
        boost("attr_embedding", 0.9, "meaning, not wording")
        boost("bm25", 0.45, "lexical still votes")
    elif cls.intent is Intent.ENTITY:
        boost("entity", 0.95, "resolve then describe")
        boost("graph", 0.75, "a profile is its neighbourhood")
        boost("attr_embedding", 0.6, "attributes are the profile")
    elif cls.intent is Intent.RELATIONSHIP:
        boost("graph", 0.9, "the answer is an edge")
        boost("entity", 0.8, "endpoints must resolve")
    elif cls.intent is Intent.MULTI_HOP:
        boost("graph", 0.98, "the answer is a path")
        boost("entity", 0.85, "seeds must resolve")
    elif cls.intent is Intent.TEMPORAL:
        boost("attr_embedding", 0.9, "the date is an attribute surface")
        boost("metadata", 0.8, "time is a filter")
    elif cls.intent is Intent.AGGREGATION:
        boost("metadata", 0.85, "count over a filtered set")
        boost("graph", 0.7, "membership is structural")

    # ── signal-shaped weighting, independent of the winning intent ───────────────────────────
    if plan.mentions:
        boost("entity", max(w["entity"], 0.7), f"{len(plan.mentions)} mention(s)")
        boost("fuzzy", max(w["fuzzy"], 0.45), "mentions may be spelled differently")
    if plan.temporal or is_temporal(norm):
        boost("metadata", max(w["metadata"], 0.7), "temporal cue")
        boost("attr_embedding", max(w["attr_embedding"], 0.65), "date lives in an attribute")
    if is_relational(norm):
        boost("graph", max(w["graph"], 0.75), "relational language")
    if plan.attributes:
        boost("metadata", max(w["metadata"], 0.8), "explicit constraint")

    plan.node_types, plan.edge_types, vocab_notes = score_vocabulary(norm, vocab)
    plan.notes.extend(vocab_notes)

    if plan.edge_types:
        top_edge = max(plan.edge_types.values())
        boost("graph", max(w["graph"], 0.45 + 0.5 * top_edge),
              f"question names {len(plan.edge_types)} relation(s) the corpus has")

    if plan.node_types:
        # A corpus-grounded type hypothesis is what the ontology surface searches within, so it
        # is the one signal that can switch that surface from silent to useful.
        top_node = max(plan.node_types.values())
        boost("ontology", 0.4 + 0.5 * top_node, "question named a type the corpus has")
        plan.notes.append(f"node type hypotheses: {list(plan.node_types)[:4]}")

    # A weak classification widens rather than narrows. Inclusive of the no-signal default (0.35),
    # which is precisely the case that most needs widening: nothing in the question was recognised.
    if cls.confidence <= 0.35:
        for s in ("attr_embedding", "bm25"):
            boost(s, max(w[s], 0.6), "low-confidence intent, widening recall")

    # ── hop budget ───────────────────────────────────────────────────────────────────────────
    plan.max_hops = {
        Intent.MULTI_HOP: 3,
        Intent.RELATIONSHIP: 2,
        Intent.ENTITY: 2,
        Intent.AGGREGATION: 2,
    }.get(cls.intent, 1)

    # ── attribute targeting ──────────────────────────────────────────────────────────────────
    targets = E.detect_attribute_targets(norm)
    if targets:
        plan.answer_type_hint = targets[0]
        plan.notes.append(f"attribute target(s): {targets}")
    elif plan.node_types:
        plan.answer_type_hint = next(iter(plan.node_types))   # ordered strongest first
        plan.notes.append(f"answer type from corpus vocabulary: {plan.answer_type_hint}")

    plan.sub_queries = decompose(query, plan)
    if plan.sub_queries:
        plan.notes.append(f"decomposed into {len(plan.sub_queries)}")

    plan.confidence = cls.confidence
    plan.policy = plan_execution(plan, confidence=cls.confidence)
    plan.notes.append(
        f"execute={sorted(plan.policy.run)} skip={sorted(plan.policy.skipped)} "
        f"({plan.policy.reason})")

    return plan


def query_representations(plan: QueryPlan) -> dict[str, str]:
    """The distinct strings worth embedding for this question.

    One embedding of a long question is dominated by its phrasing — interrogative, auxiliaries,
    politeness — and matches text that is phrased like a question rather than text that answers
    one. A concept-only representation strips that frame.

    Every representation is returned as TEXT; embedding them is L1's job, so this stays pure.
    """
    q = plan.normalized_query or plan.raw_query
    reps: dict[str, str] = {"full": q}

    concept = re.sub(
        r"^\s*(what|which|who|whom|whose|when|where|why|how)\s+"
        r"(is|are|was|were|do|does|did|has|have|had|can|could|would|will|might)?\s*", "", q, flags=re.I)
    concept = re.sub(r"\?+$", "", concept).strip()
    if concept and concept.lower() != q.lower() and len(concept) > 3:
        reps["concept"] = concept

    if plan.mentions:
        reps["entity"] = " ".join(m.text for m in plan.mentions)

    if plan.answer_type_hint:
        reps["attribute"] = plan.answer_type_hint

    for i, sq in enumerate(plan.sub_queries):
        reps[f"sub{i}"] = sq

    return reps
