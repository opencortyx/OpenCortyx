import datetime


def _utc_today() -> str:
    """The date the substrate stamps.

    NodeRepo writes created_date/modified_date from `datetime.now(timezone.utc)`, so a test that
    builds its expectation from `date.today()` — local time — disagrees with the code under test
    for however many hours this machine is offset from UTC, every day, across midnight.
    """
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


async def _key_nodes(org):
    from opencortyx.core.substrate import NodeRepo
    return await NodeRepo().find(org, exact_types=["KeyNode"])


async def test_json_walker_properties_conformance(live_org):
    from opencortyx.ingest.semistructured.ingest import ingest_json

    data = {
        "programme": {
            "programme_id": "PRG-7",
            "name": "Atlas Modernisation",
            "budget_eur": 4200000,
            "sponsor": {
                "sponsor_id": "SP-1",
                "name": "Ingrid Halvorsen",
                "role": "Chief Operating Officer",
                "based_in": "Oslo"
            }
        }
    }
    # whole_data: the walk itself, whatever the workspace stores records in (a Delta workspace walks
    # a plain JSON file only when the upload asks for the whole data in the graph).
    res = await ingest_json(live_org, data, source_label="test_run", embed_nodes=False, whole_data=True)
    assert res["status"] == "ok"

    today_str = _utc_today()
    key_nodes = await _key_nodes(live_org)
    assert key_nodes, "No KeyNode was ingested"

    for node in key_nodes:
        props = node.properties or {}
        assert "text" not in props, f"KeyNode {node.label} has a 'text' property: {props}"
        assert "key" not in props, f"KeyNode {node.label} has a 'key' property: {props}"
        assert "Label" in props, f"KeyNode {node.label} is missing 'Label' property"

        assert props.get("__source") == "json"
        assert props.get("created_date") == today_str
        assert props.get("modified_date") == today_str
        assert props.get("__audit_version") == 1

        if props["Label"] == "Programme":
            assert props.get("programme_id") == "PRG-7"
            assert props.get("name") == "Atlas Modernisation"
            assert props.get("budget_eur") == 4200000
        elif props["Label"] == "Sponsor":
            assert props.get("sponsor_id") == "SP-1"
            assert props.get("name") == "Ingrid Halvorsen"
            assert props.get("role") == "Chief Operating Officer"
            assert props.get("based_in") == "Oslo"


async def test_json_walker_preserves_existing_audit_properties(live_org):
    from opencortyx.ingest.semistructured.ingest import ingest_json
    today_str = _utc_today()

    data = {
        "programme": {
            "programme_id": "PRG-7",
            "name": "Atlas Modernisation",
            "budget_eur": 4200000,
            "__source": "custom_source",
            "created_date": "1999-12-31",
            "modified_date": "2000-01-01",
            "__audit_version": 999
        }
    }
    # whole_data: the walk itself, whatever the workspace stores records in (a Delta workspace walks
    # a plain JSON file only when the upload asks for the whole data in the graph).
    res = await ingest_json(live_org, data, source_label="test_run", embed_nodes=False, whole_data=True)
    assert res["status"] == "ok"

    programme = next(n for n in await _key_nodes(live_org) if n.properties.get("Label") == "Programme")
    props = programme.properties
    assert props.get("__source") == "json"
    assert props.get("created_date") == "1999-12-31"
    assert props.get("modified_date") == today_str
    assert props.get("__audit_version") == 999
    assert "Atlas Modernisation" in (props.get("__text") or "")


def test_node_text_prefers_stored_text_and_derives_it_otherwise():
    from opencortyx.core.substrate.document import from_row

    props = {
        "name": "Payments Core Rewrite",
        "Label": "Project",
        "status": "amber",
        "__source": "json",
        "spend_eur": 1850000,
        "project_id": "PRJ-11",
        "created_date": "2026-08-10",
        "modified_date": "2026-08-10",
        "__audit_version": 2,
    }
    derived = from_row(props, "Payments Core Rewrite", "KeyNode")
    assert derived.startswith("KeyNode Payments Core Rewrite.")
    for part in ("status=amber", "spend_eur=1850000", "project_id=PRJ-11"):
        assert part in derived
    assert "__source" not in derived and "__audit_version" not in derived

    stored = dict(props, __text="Custom summary of Payments Core Rewrite")
    assert from_row(stored, "Payments Core Rewrite", "KeyNode") == "Custom summary of Payments Core Rewrite"


async def test_an_id_field_is_an_attribute_not_the_node_identity(live_org):
    """The same `employee_id` in two files — or twice in one — is two records, two nodes. Merging them
    by id let the file read last overwrite what the first said; sameness is the Resolution Engine's."""
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.ingest.semistructured.ingest import ingest_json
    from opencortyx.ingest.semistructured.schema import compile_walk_spec

    spec = compile_walk_spec([{"status": "declared", "keys": [
        {"kind": "node", "path": "projects[]", "node_type": "Project"},
        {"kind": "node", "path": "projects[].lead", "node_type": "Lead"},
        {"kind": "attribute", "path": "projects[].lead.employee_id", "role": "id"}]}])
    lead = lambda d: {"employee_id": "EMP-3", "name": "Marek Nowak", "discipline": d}
    await ingest_json(live_org, {"projects": [{"name": "Payments", "lead": lead("backend")},
                                              {"name": "Data", "lead": lead("backend")}]},
                      source_label="first.json", walk_spec=spec)
    await ingest_json(live_org, {"projects": [{"name": "Settlement", "lead": lead("platform")}]},
                      source_label="second.json", walk_spec=spec)

    leads = await NodeRepo().find(live_org, exact_types=["Lead"])
    assert len(leads) == 3
    assert sorted(n.properties["discipline"] for n in leads) == ["backend", "backend", "platform"]
    assert all(n.properties["employee_id"] == "EMP-3" for n in leads)
