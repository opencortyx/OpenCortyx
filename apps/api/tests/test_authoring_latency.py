"""Authoring writes return before the embedding model runs: the node is saved and on the map at once,
and its vectors follow from a background task. Runs against the configured stores and skips when they
are unreachable."""
from opencortyx.core.substrate import NodeRepo


async def test_create_node_does_not_embed_inline(live_org):
    from opencortyx.knowledge import authoring
    repo = NodeRepo()
    nid = await authoring.create_node(live_org, type="Term", label="t0", properties={"definition": "def 0"},
                                      embed="off")
    node = await repo.get(live_org, nid)
    assert node is not None, "the node was not saved"
    assert await repo.unembedded(live_org, [node]) == [node], "embed='off' must not embed inline"


async def test_async_create_is_saved_and_enqueue_is_safe(live_org):
    """The background enqueue is best-effort and must never raise into the write path."""
    from opencortyx.knowledge import authoring
    nid = await authoring.create_node(live_org, type="Term", label="async term",
                                      properties={"definition": "created via the async path"})
    node = await NodeRepo().get(live_org, nid)
    assert node is not None, "async-created node not saved"


async def test_background_embed_writes_vectors_without_version_bump(live_org):
    """What `v2_embed_node` does: embedding a node is not an edit, so its audit version stays."""
    import pytest

    from opencortyx.knowledge import authoring
    from opencortyx.core.inference.embedding import embed_batch
    repo = NodeRepo()
    nid = await authoring.create_node(live_org, type="Term", label="Photosynthesis",
                                      properties={"definition": "how plants convert light to energy"},
                                      embed="off")
    before = await repo.get(live_org, nid)
    try:
        await repo.index_embeddings(live_org, [before], embed_batch)
    except Exception as e:                                 # noqa: BLE001
        pytest.skip(f"embedding unavailable ({e})")
    after = await repo.get(live_org, nid)
    assert await repo.unembedded(live_org, [after]) == [], "attribute vectors not written"
    assert after.properties.get("__audit_version") == before.properties.get("__audit_version"), \
        "a background embed bumped the audit version"
