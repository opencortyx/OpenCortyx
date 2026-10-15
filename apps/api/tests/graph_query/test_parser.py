"""Unit tests for the Graph Query DSL and JSON pipeline parser."""

from opencortyx.stores.graph_query.ast import (
    Direction,
    FilterOp,
    FilterOpType,
    IntersectOp,
    LimitOp,
    MatchOp,
    ReturnOp,
    StartOp,
    TraverseOp,
    UnionOp,
)
from opencortyx.stores.graph_query.parser import parse_graph_query


def test_parse_simple_dsl():
    query = """
    START John
    TRAVERSE worked_with
    TRAVERSE likes
    RETURN evidence
    """
    spec = parse_graph_query(query=query)
    assert len(spec.steps) == 4
    assert isinstance(spec.steps[0], StartOp)
    assert spec.steps[0].label == "John"

    assert isinstance(spec.steps[1], TraverseOp)
    assert spec.steps[1].rel == "worked_with"
    assert spec.steps[1].direction == Direction.BOTH

    assert isinstance(spec.steps[2], TraverseOp)
    assert spec.steps[2].rel == "likes"

    assert isinstance(spec.steps[3], ReturnOp)
    assert spec.steps[3].fields == ["evidence"]


def test_parse_advanced_dsl_params():
    query = """
    START label="Alice" type="Person"
    TRAVERSE rel="colleague_of" direction="out" hops=2
    FILTER properties.department == "Engineering"
    MATCH rel="likes" target_type="Topic"
    LIMIT 25
    RETURN evidence, paths, nodes
    """
    spec = parse_graph_query(query=query)
    assert len(spec.steps) == 6

    start: StartOp = spec.steps[0]
    assert start.label == "Alice"
    assert start.type == "Person"

    trav: TraverseOp = spec.steps[1]
    assert trav.rel == "colleague_of"
    assert trav.direction == Direction.OUT
    assert trav.hops == 2

    filt: FilterOp = spec.steps[2]
    assert filt.field == "properties.department"
    assert filt.operator == FilterOpType.EQ
    assert filt.value == "Engineering"

    match: MatchOp = spec.steps[3]
    assert match.rel == "likes"
    assert match.target_type == "Topic"

    lim: LimitOp = spec.steps[4]
    assert lim.count == 25
    assert spec.limit == 25

    ret: ReturnOp = spec.steps[5]
    assert ret.fields == ["evidence", "paths", "nodes"]
    assert spec.returns == ["evidence", "paths", "nodes"]


def test_parse_block_intersect_and_union():
    query = """
    START "John"
    TRAVERSE likes
    INTERSECT (
        START "Nate"
        TRAVERSE likes
    )
    UNION (
        START "Sam"
        TRAVERSE likes
    )
    RETURN evidence
    """
    spec = parse_graph_query(query=query)
    assert len(spec.steps) == 5
    assert isinstance(spec.steps[0], StartOp)
    assert spec.steps[0].label == "John"

    assert isinstance(spec.steps[1], TraverseOp)
    assert spec.steps[1].rel == "likes"

    assert isinstance(spec.steps[2], IntersectOp)
    assert "START \"Nate\"" in spec.steps[2].subquery

    assert isinstance(spec.steps[3], UnionOp)
    assert "START \"Sam\"" in spec.steps[3].subquery

    assert isinstance(spec.steps[4], ReturnOp)


def test_parse_json_steps():
    steps = [
        {"op": "START", "label": "Bob", "type": "Employee"},
        {"op": "TRAVERSE", "rel": "managed_by", "direction": "in"},
        {"op": "FILTER", "field": "properties.level", "operator": ">=", "value": 3},
        {"op": "LIMIT", "count": 10},
        {"op": "RETURN", "fields": ["nodes", "paths"]},
    ]
    spec = parse_graph_query(steps=steps)
    assert len(spec.steps) == 5
    assert spec.steps[0].label == "Bob"
    assert spec.steps[1].direction == Direction.IN
    assert spec.steps[2].operator == FilterOpType.GTE
    assert spec.steps[2].value == 3
    assert spec.limit == 10
    assert spec.returns == ["nodes", "paths"]


def test_parse_declarative_json_query():
    json_str = """{
        "start": "John",
        "traverse": [
            {"edge": "worked_with", "direction": "out"},
            {"edge": "likes", "direction": "out"}
        ],
        "return": ["path", "fact"],
        "limit": 10
    }"""
    spec = parse_graph_query(query=json_str)
    assert len(spec.steps) == 3
    assert spec.steps[0].label == "John"
    assert spec.steps[1].rel == "worked_with"
    assert spec.steps[1].direction == Direction.OUT
    assert spec.steps[2].rel == "likes"
    assert spec.steps[2].direction == Direction.OUT
    assert spec.limit == 10
    assert spec.returns == ["path", "fact"]
