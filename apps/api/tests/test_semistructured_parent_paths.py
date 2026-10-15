"""Parent resolution in the semi-structured compiler — the author's arrow must survive a shared name.

`compile_walk_spec` and `owning_node_type` both decided "is this node an ancestor of that path?" with a
plain string prefix, `path.startswith(p)`. So `conversation` counted as the ancestor of
`conversation_meta` — two SIBLING keys that merely share letters.

The harmful case is silent. An author declares that `conversation_meta` hangs off Conversation. Its
true natural parent is the root, so the compiler has to emit that re-parent. Instead the prefix test
decided the natural parent already WAS Conversation, judged the declaration redundant, and dropped it:
`parents` came out empty, the walker fell back to JSON nesting, and the node landed on the root. No
error and no warning — the canvas said one thing and ingestion built another.

Measured against the running compiler before the fix, and kept verbatim below as tests:

    conversation_meta  declared -> Conversation   =>  parents {}                          dropped
    meta               declared -> Conversation   =>  parents {'meta': 'Conversation'}   correct

The only difference between those two inputs is the name.

The web canvas resolves parents with the same rule (components/mapper/declaredShape.ts,
`isAncestorPath`), so the picture and ingestion cannot disagree about where a node hangs.

Pure unit tests: no database and no walker run — the compiled spec IS what ingestion executes.
"""
from opencortyx.ingest.semistructured.schema import compile_walk_spec, owning_node_type


def _node(path, node_type, **extra):
    return {"kind": "node", "path": path, "node_type": node_type, "decision": "node", **extra}


ROOT = _node("", "Root")
CONV = _node("conversation", "Conversation")


def _parents(*keys):
    return compile_walk_spec([{"name": "t", "status": "declared", "keys": list(keys)}]).get("parents", {})


def _structure(*keys):
    return {"name": "t", "keys": list(keys)}


# ── compile_walk_spec: a declared parent is never silently dropped ────────────────────────────────
def test_a_declared_parent_survives_when_the_childs_name_extends_a_siblings():
    """THE harmful case. The arrow says Conversation, so ingestion must hang Meta off Conversation."""
    assert _parents(ROOT, CONV, _node("conversation_meta", "Meta", parent="Conversation")) == \
        {"conversation_meta": "Conversation"}


def test_the_same_declaration_without_a_shared_name_is_unchanged():
    """The control: this always worked, and the fix must not change it."""
    assert _parents(ROOT, CONV, _node("meta", "Meta", parent="Conversation")) == {"meta": "Conversation"}


def test_no_spurious_reparent_when_the_declared_parent_really_is_the_natural_one():
    """The other face of the same bug. Meta sits beside conversation, so Root IS its natural parent and
    declaring Root must emit nothing. The prefix test believed the natural parent was Conversation and
    emitted a redundant re-parent to Root instead."""
    assert _parents(ROOT, CONV, _node("conversation_meta", "Meta", parent="Root")) == {}


def test_a_real_descendant_is_still_recognised():
    """Segment-aware must not become too strict: a dotted child IS under its parent."""
    assert _parents(ROOT, CONV, _node("conversation.turns[]", "Turn", parent="Conversation")) == {}


def test_a_container_still_owns_its_own_elements():
    """`sessions` holds `sessions[]` — the one legitimate non-dotted ancestor, and it must be kept."""
    assert _parents(ROOT, _node("sessions", "Sessions"), _node("sessions[]", "Session", parent="Sessions")) == {}


# ── owning_node_type: a field belongs to the node it is actually under ────────────────────────────
def test_a_top_level_field_is_owned_by_the_root_not_by_a_sibling_with_a_shared_name():
    """`conversation_meta` is a top-level field, so it belongs to the root — not to Conversation. Handed
    to the wrong node, its text is routed there and every extractor bound to the root misses it."""
    assert owning_node_type(_structure(ROOT, CONV), "conversation_meta") == "Root"


def test_a_field_under_a_node_is_still_owned_by_that_node():
    assert owning_node_type(_structure(ROOT, CONV), "conversation.title") == "Conversation"


def test_the_deepest_owning_node_wins():
    s = _structure(ROOT, CONV, _node("conversation.session_*[]", "Turn"))
    assert owning_node_type(s, "conversation.session_*[].text") == "Turn"


def test_an_element_field_is_owned_by_the_element_not_the_container():
    s = _structure(ROOT, _node("sessions", "Sessions"), _node("sessions[]", "Session"))
    assert owning_node_type(s, "sessions[].title") == "Session"


def test_a_node_path_still_owns_itself():
    """`owning_node_type` has always matched a path to the node AT that path; the fix keeps that."""
    assert owning_node_type(_structure(ROOT, CONV), "conversation") == "Conversation"
