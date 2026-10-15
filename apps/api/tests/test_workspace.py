"""A workspace's name, mode and central node.

The central node is the workspace itself as a graph node: its id is the workspace_id, its type follows the
mode and its label the name. A rename relabels it and a mode switch retypes it without changing its
id, so whatever hangs from it stays attached.
"""
import uuid

import pytest

from opencortyx.knowledge import workspace as ws


def test_the_central_type_follows_the_mode():
    org = uuid.uuid4()
    assert ws.Profile(org, "Vivek", "person").central_type == "Person"
    assert ws.Profile(org, "Acme", "team").central_type == "Organization"


def test_a_blank_name_shows_a_stable_made_up_one():
    org = uuid.uuid4()
    p = ws.Profile(org, "", "person")
    assert p.display_name == ws.made_up_name(org) == ws.Profile(org, "", "team").display_name
    assert " " in p.display_name
    assert ws.Profile(org, "Vivek", "person").display_name == "Vivek"


def test_names_and_modes_are_checked():
    assert ws.clean_name("  Vivek   Alex ") == "Vivek Alex"
    assert ws.clean_name(None) == ""
    with pytest.raises(ws.InvalidWorkspace):
        ws.clean_name("x" * (ws.NAME_MAX + 1))
    with pytest.raises(ws.InvalidWorkspace):
        ws.clean_name(42)
    with pytest.raises(ws.InvalidWorkspace):
        ws.clean_mode("family")


async def test_the_central_node_is_renamed_and_retyped_in_place(live_org):
    from opencortyx.core.substrate import NodeRepo
    repo = NodeRepo()
    node = await ws.ensure_central_node(live_org, ws.Profile(live_org, "Vivek", "person"))
    assert node.id == live_org and node.type == "Person" and node.label == "Vivek"
    assert ws.is_central(node) and (node.properties or {}).get(ws.CENTRAL_KEY)

    other = await repo.upsert_node(workspace_id=live_org, type="Document", label="notes", date_link=False)
    assert await repo.add_edge(workspace_id=live_org, source_id=live_org, target_id=other.id, type="OWNS")

    node = await ws.ensure_central_node(live_org, ws.Profile(live_org, "Acme", "team"))
    assert node.id == live_org and node.type == "Organization" and node.label == "Acme"
    stored = await repo.get(live_org, live_org)
    assert stored.type == "Organization" and stored.label == "Acme"
    assert [e.target_id for e in await repo.edges(live_org, source_ids=[live_org], types=["OWNS"])] == [other.id]
