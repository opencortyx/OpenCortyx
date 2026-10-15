"""Tests for the graph query execution engine."""
import uuid

import pytest


from opencortyx.stores.graph_query.ast import (
    Direction,
    FilterOp,
    FilterOpType,
    GraphPath,
    GraphQuerySpec,
    LimitOp,
    MatchOp,
    StartOp,
    TraverseOp,
    TraversedStep,
)
from opencortyx.stores.graph_query.executor import (
    GraphQueryContext,
    _compare,
    _get_nested_field,
    execute_filter,
    execute_limit,
    run_pipeline,
)


def test_comparison_operator_semantics():
    assert _compare("Engineering", FilterOpType.EQ, "engineering")
    assert _compare("Engineering", FilterOpType.NEQ, "Sales")
    assert _compare("Alice and Bob", FilterOpType.CONTAINS, "Bob")
    assert not _compare("Alice and Bob", FilterOpType.CONTAINS, "Charlie")
    assert _compare("Admin", FilterOpType.IN, ["Admin", "Viewer"])
    assert _compare(10, FilterOpType.GT, 5)
    assert _compare(5, FilterOpType.GTE, 5)
    assert _compare(3, FilterOpType.LT, 8)
    assert _compare(4, FilterOpType.LTE, 4)


def test_nested_field_extraction():
    data = {
        "type": "Person",
        "label": "John Doe",
        "properties": {
            "department": "AI Research",
            "metrics": {"score": 98.5}
        }
    }
    assert _get_nested_field(data, "label") == "John Doe"
    assert _get_nested_field(data, "properties.department") == "AI Research"
    assert _get_nested_field(data, "properties.metrics.score") == 98.5
    assert _get_nested_field(data, "properties.missing") is None


def test_graph_path_readable_string():
    path = GraphPath(
        seed_id="n1",
        steps=[
            TraversedStep(from_id="n1", edge_id="e1", edge_type="worked_with", to_id="n2", direction="out"),
            TraversedStep(from_id="n2", edge_id="e2", edge_type="likes", to_id="n3", direction="out"),
        ]
    )
    labels = {"n1": "John", "n2": "Alice", "n3": "Tennis"}
    d = path.to_dict(labels)
    assert d["length"] == 2
    assert d["seed_id"] == "n1"
    assert d["terminal_id"] == "n3"
    assert d["readable_path"] == "(John) -[worked_with]-> (Alice) -[likes]-> (Tennis)"


def test_filter_and_limit_execution():
    ctx = GraphQueryContext(workspace_id=uuid.uuid4(), repo=None)
    ctx.register_node({"id": "1", "label": "Nate", "type": "Person", "properties": {"dept": "Sales"}})
    ctx.register_node({"id": "2", "label": "Joanna", "type": "Person", "properties": {"dept": "Engineering"}})
    ctx.register_node({"id": "3", "label": "Sam", "type": "Person", "properties": {"dept": "Engineering"}})

    ctx.paths = [
        GraphPath(seed_id="1", steps=[]),
        GraphPath(seed_id="2", steps=[]),
        GraphPath(seed_id="3", steps=[]),
    ]

    # Filter by department
    filter_op = FilterOp(field="properties.dept", operator=FilterOpType.EQ, value="Engineering")
    execute_filter(ctx, filter_op)

    assert len(ctx.active_nodes) == 2
    assert "1" not in ctx.active_nodes
    assert "2" in ctx.active_nodes
    assert "3" in ctx.active_nodes

    # Limit to 1
    limit_op = LimitOp(count=1)
    execute_limit(ctx, limit_op)

    assert len(ctx.active_nodes) == 1
    assert len(ctx.paths) == 1


async def _graph(repo, nodes, edges=()):
    """Fill `repo` with `nodes` [(key, label, type, props)] and `edges` [(src, type, dst)]."""
    org, ids = uuid.uuid4(), {}
    for key, label, ntype, props in nodes:
        node = await repo.upsert_node(workspace_id=org, type=ntype, label=label, properties=props, date_link=False)
        ids[key] = node.id
    for src, etype, dst in edges:
        await repo.add_edge(workspace_id=org, source_id=ids[src], target_id=ids[dst], type=etype)
    return org


@pytest.mark.asyncio
async def test_run_pipeline_start_and_traverse(memory_repo):
    repo = memory_repo
    org = await _graph(repo,
        [("john", "John", "Person", {"text": "John is a senior researcher who loves coffee."}),
         ("coffee", "Coffee", "Topic", {"summary": "Coffee brewing and espresso roasting"})],
        [("john", "likes", "coffee")])
    ctx = GraphQueryContext(workspace_id=org, repo=repo)
    # `paths` and `evidence` are opt-in: the default projection is the CHEAP one (count + nodes),
    # because this tool is called repeatedly and evidence is ~47 tok/node.
    spec = GraphQuerySpec(steps=[
        StartOp(label="John"),
        TraverseOp(rel="LIKES", direction=Direction.OUT),
    ], returns=["count", "nodes", "paths", "evidence"])

    result = await run_pipeline(ctx, spec)

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["nodes"][0]["label"] == "Coffee"
    assert len(result["paths"]) == 1
    assert "(John) -[likes]-> (Coffee)" in result["paths"][0]["readable_path"]
    assert "Coffee brewing" in result["evidence"]
    # the navigation affordance that makes a follow-up query informed rather than a guess
    assert result["available_edges"] == [{"rel": "likes", "direction": "in", "n": 1}]
    # evidence_blocks is the structured twin of `evidence` and must NOT ship alongside it
    assert "evidence_blocks" not in result


@pytest.mark.asyncio
async def test_default_projection_is_cheap(memory_repo):
    """No RETURN clause must yield count + nodes only - never the expensive projections."""
    repo = memory_repo
    org = await _graph(repo, [("john", "John", "Person", {"text": "John likes coffee."})])
    result = await run_pipeline(GraphQueryContext(workspace_id=org, repo=repo),
                                GraphQuerySpec(steps=[StartOp(label="John")]))

    assert set(result) == {"status", "count", "execution_time_ms", "nodes"}
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_start_truncation_is_visible(memory_repo):
    """A capped seed must say so: `count` is this tool's 13-token probe, and it must not lie."""
    repo = memory_repo
    org = await _graph(repo, [(i, "n%d" % i, "Turn", {}) for i in range(4)])
    result = await run_pipeline(GraphQueryContext(workspace_id=org, repo=repo),
                                GraphQuerySpec(steps=[StartOp(label="n")], limit=3))

    assert result["count"] == 3
    assert result["truncated"] is True
    assert "lower bound" in result["truncated_note"]


@pytest.mark.asyncio
async def test_match_keeps_nodes_with_a_matching_neighbor(memory_repo):
    repo = memory_repo
    org = await _graph(repo,
        [("a", "Alice", "Person", {}), ("b", "Bob", "Person", {}), ("t", "Tennis", "Topic", {})],
        [("a", "LIKES", "t")])
    spec = GraphQuerySpec(steps=[StartOp(type="person"), MatchOp(rel="likes", target_label="ten")])
    result = await run_pipeline(GraphQueryContext(workspace_id=org, repo=repo), spec)
    assert [n["label"] for n in result["nodes"]] == ["Alice"]
