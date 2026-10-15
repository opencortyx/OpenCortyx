"""Live degree: an edge updates its endpoints' degree the moment it is written, and a later write of
the node does not clobber it. Runs against the configured graph store and skips when it is
unreachable."""


async def test_edge_updates_degree_at_once(live_org):
    from opencortyx.core.substrate import NodeRepo
    repo = NodeRepo()
    a = await repo.upsert_node(workspace_id=live_org, type="Entity", label="A", date_link=False)
    b = await repo.upsert_node(workspace_id=live_org, type="Entity", label="B", date_link=False)
    assert await repo.add_edge(workspace_id=live_org, source_id=a.id, target_id=b.id, type="RELATES_TO")
    assert not await repo.add_edge(workspace_id=live_org, source_id=a.id, target_id=b.id, type="RELATES_TO")
    a, b = await repo.get(live_org, a.id), await repo.get(live_org, b.id)
    assert a.degree == 1 and b.degree == 1, f"one edge, written twice, gave degrees {a.degree}/{b.degree}"


async def test_degree_not_clobbered_by_later_prop_update(live_org):
    """Ingestion rewrites a node's record after edges were drawn to it; the stale in-memory degree
    must not overwrite the stored one."""
    from opencortyx.core.substrate import NodeRepo
    repo = NodeRepo()
    x = await repo.upsert_node(workspace_id=live_org, type="Entity", label="X", date_link=False)
    y = await repo.upsert_node(workspace_id=live_org, type="Entity", label="Y", date_link=False)
    await repo.add_edge(workspace_id=live_org, source_id=x.id, target_id=y.id, type="RELATES_TO")
    x.properties = {**(x.properties or {}), "note": "touched after edge"}      # x.degree is still 0 here
    await repo.save(x)
    x = await repo.get(live_org, x.id)
    assert x.degree == 1, f"degree clobbered by a later props update: {x.degree}"
    assert (x.properties or {}).get("note") == "touched after edge"
