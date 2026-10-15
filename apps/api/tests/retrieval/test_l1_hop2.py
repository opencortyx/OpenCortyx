"""Hop-2 span discovery: the rules that keep it from becoming `prf_set` with extra steps.

Each test pins a behaviour that a live probe caught the implementation getting wrong. They are
regression tests for measured defects, not restatements of the code.
"""
import uuid

from opencortyx.retrieval.contracts import Candidate, Evidence
from opencortyx.retrieval.l1_candidates import hop2


def _cand(text, rank=0, node_type="Turn", label=""):
    c = Candidate(node_id=uuid.uuid4(), node_type=node_type, label=label, text=text)
    c.add(Evidence(surface="bm25", rank=rank, raw_score=1.0, scale="bm25"))
    return c


def _spans(lists, asked=()):
    return dict(hop2.discover_spans(lists, asked))


def test_a_multiword_proper_noun_is_discovered():
    got = _spans([[_cand("We both ran the Seattle Marathon last year.")]])
    assert "Seattle Marathon" in got


def test_a_span_the_question_already_named_is_not_re_searched():
    """Rule 1. Re-searching what pass 1 already searched buys duplicates - the prf_set failure."""
    got = _spans([[_cand("Gina lost her job at Door Dash.")]], asked=["Gina"])
    assert "Gina" not in got
    assert "Door Dash" in got


def test_rule_1_drops_a_span_whose_every_token_the_question_named():
    """Naming "Seattle Marathon" means "Seattle" was searched too."""
    got = _spans([[_cand("The Seattle Marathon was in Seattle in November.")]],
                 asked=["Seattle Marathon"])
    assert "Seattle" not in got and "Seattle Marathon" not in got


def test_rule_1_keeps_a_span_carrying_a_token_the_question_never_named():
    """Containment was raw SUBSTRING, which deleted any span merely CONTAINING a mention. A
    two-character mention - this corpus really has one, "Jo" - erased `Jones`/`Johnson`, a
    one-character mention erased everything, and asking about `Dash` erased the strictly more
    specific `Door Dash`. Only a full token-subset means "already searched"."""
    assert "Rebecca Lyons" in _spans([[_cand("Later I met Rebecca Lyons")]], asked=["A"])
    assert "Door Dash" in _spans([[_cand("Later they used Door Dash daily")]], asked=["Dash"])
    assert "Ina Garten" in _spans([[_cand("Later we cooked. Ina Garten wrote it.")]],
                                  asked=["Jon and Gina"])


def test_sentence_initial_common_words_are_not_entities():
    """Measured defect: reusing L0's QUESTION parser on dialogue returned `Thanks`, `Glad`,
    `Yeah`, `Cool` - every sentence-opening word. Capitalisation says nothing in that slot.

    The scanned text must SHOW the lower-case twin, because that is the evidence the trim needs;
    real dialogue supplies it constantly ("Cool, thanks Mel!")."""
    got = _spans([[_cand("Thanks Mel! Glad you came. Yeah it was fun. Cool stuff. "
                         "cool, thanks again, glad you did, yeah really.")]])
    for noise in ("Thanks", "Glad", "Yeah", "Cool"):
        assert noise not in got, f"{noise!r} is sentence-initial capitalisation, not a name"


def test_a_sentence_opener_is_kept_when_nothing_evidences_it_as_a_common_word():
    """ABSENT IS NOT EVIDENCE - the bug that cost the module its own headline examples.

    The first version trimmed any leading token not seen mid-sentence, so `"Seattle Marathon was
    held in November"` yielded `Marathon` and `"Door Dash employs Gina"` yielded `Dash`. Fact and
    Claim text almost always OPENS with its subject, so the entity being searched for was exactly
    the token most likely to be cut. A kept-but-odd span is a phrase that matches almost nothing;
    a truncated span matches the WRONG things."""
    assert "Seattle Marathon" in _spans([[_cand("Seattle Marathon was held in November", node_type="Fact")]])
    assert "Door Dash" in _spans([[_cand("Door Dash employs Gina", node_type="Fact")]])
    assert "Rebecca Lyons" in _spans([[_cand("Rebecca Lyons is a doctor", node_type="Fact")]])


def test_a_name_seen_mid_sentence_survives_the_same_rule():
    """`Woohoo` is evidenced lower-case, so it is trimmed and the name behind it survives."""
    got = _spans([[_cand("Woohoo Melanie! I passed the interview. woohoo indeed.")]])
    assert "Melanie" in got, got


def test_a_span_never_crosses_a_sentence_boundary():
    """Measured defect: the question parser's capitalised run admits `.` and `:`, so it produced
    spans like "all had such a great time. [D4:9] Caroline: That"."""
    got = _spans([[_cand("We had a great time. Caroline: That is a good memory.")]])
    assert all("." not in s and ":" not in s for s in got), got


def test_a_container_label_is_not_content():
    """`_mk` falls back to `label` when a node has no text, which put the literal span "Session"
    top of the list at x14 support - a slot taken from a real entity."""
    got = _spans([[_cand("Session 12:09 am on 13 September, 2023.", node_type="Session")]])
    assert "Session" not in got


def test_only_the_top_of_the_pass_is_mined():
    """A span from rank 400 is not something the strongest hits agree on."""
    deep = _cand("Nobody mentions Atlantis City here.", rank=hop2.TOP_RANK + 5)
    assert "Atlantis City" not in _spans([[deep]])


def test_support_counts_candidates_and_orders_the_result():
    lists = [[_cand("I met Rebecca Lyons downtown."),
              _cand("Later Rebecca Lyons called back. see you later."),
              _cand("A note about Perseid meteors.")]]
    ordered = hop2.discover_spans(lists, [])
    assert ordered[0][0] == "Rebecca Lyons" and ordered[0][1] == 2


def test_the_same_node_is_counted_once_across_surfaces():
    """A node returned by three surfaces is one piece of evidence, not three votes."""
    c = _cand("I met Rebecca Lyons downtown.")
    dup = Candidate(node_id=c.node_id, node_type="Turn", text=c.text)
    dup.add(Evidence(surface="node_embedding", rank=0, raw_score=0.9, scale="cosine"))
    assert hop2.discover_spans([[c], [dup]], [])[0][1] == 1


def test_no_spans_means_no_query_rather_than_match_everything():
    """Empty must never degrade into an unfiltered search - the failure shape that produced a
    surface matching the whole corpus."""
    assert hop2.discover_spans([[]], []) == []
