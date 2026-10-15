"""Schema-mapping API against a running stack.

    docker compose exec -e OPENCORTYX_API_KEY=ax_... api python -m pytest tests/integration -m integration
"""
import uuid

import pytest

pytestmark = pytest.mark.integration

TAG = uuid.uuid4().hex[:6]

EDITOR = {
    "keyRows": [
        {"path": "", "kind": "node", "node_type": f"T{TAG}Root", "decision": "node", "becomes": "node"},
        {"path": "lines[]", "kind": "node", "node_type": f"T{TAG}Line", "decision": "node", "becomes": "node"},
        {"path": "lines[].sku", "kind": "attribute", "type": "string", "role": "metadata", "becomes": "label"},
        {"path": "products[]", "kind": "node", "node_type": f"T{TAG}Product", "decision": "node", "becomes": "node"},
        {"path": "products[].sku", "kind": "attribute", "type": "string", "role": "id", "becomes": "id"},
    ],
    "edgeRules": [{"edgeLabel": "of_product", "fromNode": f"T{TAG}Line", "toNode": f"T{TAG}Product",
                   "toKey": "sku", "readFrom": f"T{TAG}Line", "props": []}],
    "nodeRules": [], "attributeRules": [], "reading": {}, "__touched": True,
}

ADVANCED_RULES = {
    "discriminatorRules": [{"id": "d1", "fieldPath": "kind"}],
    "zipCorrelations": [{"id": "z1"}],
    "patternRules": [{"id": "p1"}],
    "compositeKeyRules": [{"id": "c1"}],
}


@pytest.fixture
def api(client, auth_headers):
    created = []

    def call(method, path, body=None):
        return client.request(method, path, json=body, headers=auth_headers)

    call.created = created
    yield call
    for mid in created:
        call("DELETE", f"/schema-mappings/{mid}")


def _create(api, paradigm, payload):
    r = api("POST", "/schema-mappings", {"name": f"test {paradigm} {TAG}", "paradigm": paradigm,
                                         "payload": payload})
    assert r.status_code == 200, r.text
    mid = r.json()["id"]
    api.created.append(mid)
    return mid


def test_list_returns_items_with_editor_models(api):
    r = api("GET", "/schema-mappings")
    assert r.status_code == 200
    items = r.json().get("items")
    assert isinstance(items, list)
    assert all((m.get("payload") or {}).get("editor") for m in items)


def test_list_filters_by_paradigm(api):
    _create(api, "structured", {"editor": {"tableRules": [], "edgeRules": [], "__touched": True}})
    r = api("GET", "/schema-mappings?paradigm=structured")
    assert r.status_code == 200
    assert all(m["paradigm"] == "structured" for m in r.json()["items"])


@pytest.mark.parametrize("paradigm,payload", [
    ("semistructured", {"editor": EDITOR}),
    ("structured", {"editor": {"tableRules": [], "edgeRules": [], "__touched": True}}),
    ("unstructured", {"editor": {"chunking": {"strategy": "markdown", "mode": "save", "size": 1200,
                                              "overlap": 150, "node": "Chunk"}, "__touched": True}}),
])
def test_create_each_paradigm(api, paradigm, payload):
    mid = _create(api, paradigm, payload)
    r = api("GET", f"/schema-mappings/{mid}")
    assert r.status_code == 200 and r.json()["id"] == mid


def test_value_matched_edge_compiles_to_join_rule(api):
    mid = _create(api, "semistructured", {"editor": EDITOR})
    m = api("GET", f"/schema-mappings/{mid}").json()
    rules = (m["payload"].get("structures") or [{}])[0].get("edge_rules") or []
    assert len(rules) == 1
    assert rules[0]["type"] == "OF_PRODUCT"
    assert rules[0]["to"]["kind"] == "join" and rules[0]["to"]["on"] == "sku"
    back = [e for e in m["payload"]["editor"].get("edgeRules", []) if e.get("toKey")]
    assert len(back) == 1 and back[0]["toKey"] == "sku"


def test_update_renames_and_recompiles(api):
    mid = _create(api, "semistructured", {"editor": EDITOR})
    r = api("PUT", f"/schema-mappings/{mid}", {"name": f"renamed {TAG}"})
    assert r.status_code == 200 and r.json()["name"] == f"renamed {TAG}"
    edited = {**EDITOR, "edgeRules": [{**EDITOR["edgeRules"][0], "edgeLabel": "supplies"}]}
    r = api("PUT", f"/schema-mappings/{mid}", {"payload": {"editor": edited}})
    rules = (r.json()["payload"].get("structures") or [{}])[0].get("edge_rules") or []
    assert r.status_code == 200 and rules and rules[0]["type"] == "SUPPLIES"


def test_save_preserves_rules_the_editor_does_not_render(api):
    mid = _create(api, "semistructured", {"editor": {**EDITOR, **ADVANCED_RULES}})
    before = api("GET", f"/schema-mappings/{mid}").json()["payload"]
    editor = dict(before.get("editor") or {})
    after = api("PUT", f"/schema-mappings/{mid}",
                {"payload": {**before, "editor": {**editor, "__touched": True}}}).json()["payload"]
    for key in ADVANCED_RULES:
        assert (after.get("editor") or {}).get(key) == editor.get(key), key


def test_usage_lists_pipelines(api):
    mid = _create(api, "semistructured", {"editor": EDITOR})
    r = api("GET", f"/schema-mappings/{mid}/usage")
    assert r.status_code == 200 and "pipelines" in r.json()


def test_conformance_requires_an_asset(api):
    mid = _create(api, "semistructured", {"editor": EDITOR})
    assert api("GET", f"/schema-mappings/{mid}/conformance").status_code == 422


def test_inline_json_ingest_accepts_mapping(api):
    mid = _create(api, "semistructured", {"editor": EDITOR})
    r = api("POST", "/json/ingest_inline", {
        "data": {"lines": [{"sku": "A1"}, {"sku": "ZZZ"}], "products": [{"sku": "A1", "name": "Thing"}]},
        "source_label": f"test-{TAG}.json",
        "options": {"mapping_ids": [mid], "root_type": f"T{TAG}Root"}})
    assert r.status_code == 200, r.text
    assert r.json().get("task_id") or r.json().get("run_id")


def test_delete_removes_mapping(api):
    mid = _create(api, "semistructured", {"editor": EDITOR})
    assert api("DELETE", f"/schema-mappings/{mid}").status_code == 200
    api.created.remove(mid)
    assert api("GET", f"/schema-mappings/{mid}").status_code == 404
