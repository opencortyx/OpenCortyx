"""Query parser for the OpenCortyx Graph Query primitive.

Supports both:
  1. Human/LLM text DSL:
       START John
       TRAVERSE worked_with
       TRAVERSE likes
       RETURN evidence

     With advanced parameters:
       START label="John" type="Person"
       TRAVERSE rel="worked_with" direction="both" hops=1
       FILTER properties.department == "Engineering"
       INTERSECT (
         START "Nate"
         TRAVERSE likes
       )
       LIMIT 10
       RETURN evidence, paths

  2. Structured JSON step list:
       [{"op": "START", "label": "John"}, {"op": "TRAVERSE", "rel": "worked_with"}, ...]
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any, List, Optional, Union

from opencortyx.stores.graph_query.ast import (
    Direction,
    FilterOp,
    FilterOpType,
    GraphQuerySpec,
    IntersectOp,
    LimitOp,
    MatchOp,
    PathOp,
    ReturnOp,
    StartOp,
    StepOp,
    TraverseOp,
    UnionOp,
)


def _clean_val(v: str) -> Any:
    v = v.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if v.lower() == "null" or v.lower() == "none":
        return None
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        return v


def parse_args(arg_str: str) -> tuple[list[Any], dict[str, Any]]:
    """Parse positional and keyword arguments from an argument string."""
    positional = []
    kwargs = {}
    if not arg_str.strip():
        return positional, kwargs

    try:
        tokens = shlex.split(arg_str)
    except ValueError:
        tokens = arg_str.split()

    for tok in tokens:
        tok = tok.strip().rstrip(",")
        if not tok:
            continue
        if "=" in tok:
            k, v = tok.split("=", 1)
            kwargs[k.strip().lower()] = _clean_val(v)
        else:
            positional.append(_clean_val(tok))
    return positional, kwargs


def parse_step_dict(d: dict) -> StepOp:
    """Parse a single JSON step dictionary into its typed StepOp."""
    op_name = str(d.get("op", "")).strip().upper()
    if op_name == "START":
        return StartOp(
            query=d.get("query"),
            label=d.get("label"),
            node_id=d.get("node_id") or d.get("id"),
            node_ids=d.get("node_ids") or d.get("ids"),
            type=d.get("type"),
            collection_id=d.get("collection_id"),
        )
    elif op_name == "TRAVERSE":
        direction_val = str(d.get("direction", "both")).lower()
        direction = Direction(direction_val) if direction_val in [d.value for d in Direction] else Direction.BOTH
        rel = d.get("rel") or d.get("type") or d.get("edge_type")
        if rel == "*":
            rel = None
        return TraverseOp(
            rel=rel,
            direction=direction,
            hops=int(d.get("hops", 1)),
            min_confidence=float(d["min_confidence"]) if d.get("min_confidence") is not None else None,
            min_weight=float(d["min_weight"]) if d.get("min_weight") is not None else None,
            target_type=d.get("target_type"),
        )
    elif op_name == "FILTER":
        op_type = FilterOpType(d.get("operator", "=="))
        return FilterOp(
            field=d.get("field", "label"),
            operator=op_type,
            value=d.get("value"),
        )
    elif op_name == "MATCH":
        direction_val = str(d.get("direction", "both")).lower()
        direction = Direction(direction_val) if direction_val in [d.value for d in Direction] else Direction.BOTH
        return MatchOp(
            rel=d.get("rel") or d.get("type"),
            direction=direction,
            target_type=d.get("target_type"),
            target_label=d.get("target_label"),
        )
    elif op_name == "INTERSECT":
        return IntersectOp(subquery=d.get("subquery"))
    elif op_name == "UNION":
        return UnionOp(subquery=d.get("subquery"))
    elif op_name == "PATH":
        return PathOp(
            track=bool(d.get("track", True)),
            max_depth=int(d["max_depth"]) if d.get("max_depth") is not None else None,
        )
    elif op_name == "LIMIT":
        return LimitOp(count=int(d.get("count", d.get("limit", 50))))
    elif op_name == "RETURN":
        fields = d.get("fields") or d.get("returns") or ["count", "nodes"]
        if isinstance(fields, str):
            fields = [f.strip() for f in fields.split(",") if f.strip()]
        return ReturnOp(fields=fields)
    else:
        raise ValueError(f"Unknown operation: {op_name}")


def parse_dsl_line(line: str) -> Optional[StepOp]:
    """Parse a single text line into a StepOp."""
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("//"):
        return None

    parts = line.split(None, 1)
    op_name = parts[0].upper()
    arg_str = parts[1] if len(parts) > 1 else ""

    if op_name == "START":
        pos, kwargs = parse_args(arg_str)
        label = kwargs.get("label") or (pos[0] if pos else None)
        query = kwargs.get("query")
        node_id = kwargs.get("id") or kwargs.get("node_id")
        node_type = kwargs.get("type")
        collection_id = kwargs.get("collection_id") or kwargs.get("collection")
        return StartOp(label=str(label) if label is not None else None,
                       query=str(query) if query is not None else None,
                       node_id=str(node_id) if node_id is not None else None,
                       type=str(node_type) if node_type is not None else None,
                       collection_id=str(collection_id) if collection_id is not None else None)

    elif op_name == "TRAVERSE":
        pos, kwargs = parse_args(arg_str)
        rel = kwargs.get("rel") or kwargs.get("type") or (pos[0] if pos else None)
        if rel == "*":
            rel = None
        direction_str = str(kwargs.get("direction", pos[1] if len(pos) > 1 and pos[1] in ("out", "in", "both") else "both")).lower()
        direction = Direction.OUT if direction_str == "out" else (Direction.IN if direction_str == "in" else Direction.BOTH)
        hops = int(kwargs.get("hops", kwargs.get("depth", 1)))
        return TraverseOp(rel=rel, direction=direction, hops=hops,
                          target_type=kwargs.get("target_type"))

    elif op_name == "FILTER":
        # Supports: FILTER type == "Person" or FILTER properties.department == "Engineering"
        for op in ["==", "!=", ">=", "<=", ">", "<", "contains", "in"]:
            if f" {op} " in arg_str:
                left, right = arg_str.split(f" {op} ", 1)
                return FilterOp(
                    field=left.strip(),
                    operator=FilterOpType(op),
                    value=_clean_val(right),
                )
        pos, kwargs = parse_args(arg_str)
        if kwargs.get("field"):
            return FilterOp(field=kwargs["field"],
                            operator=FilterOpType(kwargs.get("operator", "==")),
                            value=kwargs.get("value"))
        elif pos:
            return FilterOp(field="type", operator=FilterOpType.EQ, value=pos[0])
        raise ValueError(f"Invalid FILTER syntax: {arg_str}")

    elif op_name == "MATCH":
        pos, kwargs = parse_args(arg_str)
        rel = kwargs.get("rel") or kwargs.get("type") or (pos[0] if pos else None)
        direction_str = str(kwargs.get("direction", "both")).lower()
        direction = Direction.OUT if direction_str == "out" else (Direction.IN if direction_str == "in" else Direction.BOTH)
        return MatchOp(rel=rel, direction=direction, target_type=kwargs.get("target_type"),
                       target_label=kwargs.get("target_label"))

    elif op_name == "INTERSECT":
        sub = arg_str.strip()
        if sub.startswith("(") and sub.endswith(")"):
            sub = sub[1:-1].strip()
        return IntersectOp(subquery=sub)

    elif op_name == "UNION":
        sub = arg_str.strip()
        if sub.startswith("(") and sub.endswith(")"):
            sub = sub[1:-1].strip()
        return UnionOp(subquery=sub)

    elif op_name == "PATH":
        pos, kwargs = parse_args(arg_str)
        return PathOp(track=bool(kwargs.get("track", True)),
                      max_depth=int(kwargs["max_depth"]) if kwargs.get("max_depth") else None)

    elif op_name == "LIMIT":
        pos, kwargs = parse_args(arg_str)
        count = int(kwargs.get("count", pos[0] if pos else 50))
        return LimitOp(count=count)

    elif op_name == "RETURN":
        fields = [f.strip().lower() for f in arg_str.split(",") if f.strip()]
        if not fields:
            fields = ["evidence", "nodes", "paths", "edges"]
        return ReturnOp(fields=fields)

    else:
        raise ValueError(f"Unrecognized graph query operator: {op_name}")


def parse_query_dict(d: dict) -> GraphQuerySpec:
    """Parse an agent JSON structure: {start, traverse, return, limit, filter}."""
    steps: List[StepOp] = []

    # 1. START
    start_val = d.get("start")
    if start_val:
        if isinstance(start_val, str):
            steps.append(StartOp(label=start_val))
        elif isinstance(start_val, dict):
            steps.append(StartOp(
                label=start_val.get("label"),
                query=start_val.get("query"),
                node_id=start_val.get("id") or start_val.get("node_id"),
                type=start_val.get("type"),
                collection_id=start_val.get("collection_id"),
            ))
        elif isinstance(start_val, list):
            steps.append(StartOp(node_ids=[str(x) for x in start_val]))

    # 2. TRAVERSE
    traverse_val = d.get("traverse")
    if traverse_val:
        if isinstance(traverse_val, list):
            for t in traverse_val:
                if isinstance(t, str):
                    steps.append(TraverseOp(rel=t))
                elif isinstance(t, dict):
                    dir_str = str(t.get("direction", "both")).lower()
                    dir_enum = Direction.OUT if dir_str == "out" else (Direction.IN if dir_str == "in" else Direction.BOTH)
                    steps.append(TraverseOp(
                        rel=t.get("edge") or t.get("rel") or t.get("type"),
                        direction=dir_enum,
                        hops=int(t.get("hops", 1)),
                        target_type=t.get("target_type"),
                    ))
        elif isinstance(traverse_val, dict):
            dir_str = str(traverse_val.get("direction", "both")).lower()
            dir_enum = Direction.OUT if dir_str == "out" else (Direction.IN if dir_str == "in" else Direction.BOTH)
            steps.append(TraverseOp(
                rel=traverse_val.get("edge") or traverse_val.get("rel") or traverse_val.get("type"),
                direction=dir_enum,
                hops=int(traverse_val.get("hops", 1)),
                target_type=traverse_val.get("target_type"),
            ))
        elif isinstance(traverse_val, str):
            steps.append(TraverseOp(rel=traverse_val))

    # 3. FILTER
    filter_val = d.get("filter")
    if filter_val and isinstance(filter_val, dict):
        steps.append(FilterOp(
            field=filter_val.get("field", "label"),
            operator=FilterOpType(filter_val.get("operator", "==")),
            value=filter_val.get("value"),
        ))

    # 4. LIMIT
    limit_val = int(d.get("limit", 50))

    # 5. RETURN
    ret_val = d.get("return") or d.get("returns") or ["count", "nodes"]
    if isinstance(ret_val, str):
        ret_val = [r.strip() for r in ret_val.split(",")]

    return GraphQuerySpec(
        steps=steps,
        limit=limit_val,
        returns=ret_val,
    )


def parse_graph_query(
    query: Optional[Union[str, dict]] = None,
    steps: Optional[List[Union[dict, StepOp]]] = None,
    limit: int = 50,
    returns: Optional[List[str]] = None,
) -> GraphQuerySpec:
    """Parse text DSL or step list into a complete GraphQuerySpec."""
    if isinstance(query, dict):
        return parse_query_dict(query)

    # Check if query is passed as a JSON string describing {start, traverse, ...}
    if query and isinstance(query, str) and query.strip().startswith("{") and query.strip().endswith("}"):
        try:
            parsed_json = json.loads(query.strip())
            if isinstance(parsed_json, dict) and ("start" in parsed_json or "traverse" in parsed_json):
                return parse_query_dict(parsed_json)
        except Exception:
            pass

    parsed_steps: List[StepOp] = []

    if steps:
        for s in steps:
            if isinstance(s, StepOp):
                parsed_steps.append(s)
            elif isinstance(s, dict):
                parsed_steps.append(parse_step_dict(s))

    if query and query.strip():
        # Handle multi-line block parsing, including nested INTERSECT / UNION blocks with parentheses
        lines = query.strip().splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or line.startswith("#") or line.startswith("//"):
                i += 1
                continue

            # Check for block operations like INTERSECT ( ... ) or UNION ( ... )
            m_block = re.match(r"^(INTERSECT|UNION)\s*\((.*)$", line, re.IGNORECASE)
            if m_block:
                op_name = m_block.group(1).upper()
                block_content = m_block.group(2)
                # Check if closed on same line
                if block_content.rstrip().endswith(")"):
                    inner = block_content.rstrip()[:-1].strip()
                else:
                    inner_lines = [block_content] if block_content.strip() else []
                    i += 1
                    closed = False
                    while i < len(lines):
                        nxt = lines[i]
                        if ")" in nxt:
                            before, _, _ = nxt.partition(")")
                            inner_lines.append(before)
                            closed = True
                            break
                        inner_lines.append(nxt)
                        i += 1
                    if not closed:
                        raise ValueError(f"{op_name} block is missing its closing ')'")
                    inner = "\n".join(inner_lines).strip()

                if op_name == "INTERSECT":
                    parsed_steps.append(IntersectOp(subquery=inner))
                else:
                    parsed_steps.append(UnionOp(subquery=inner))
                i += 1
                continue

            step = parse_dsl_line(line)
            if step:
                parsed_steps.append(step)
            i += 1

    # Extract limit and return preferences if specified as steps
    spec_limit = limit
    spec_returns = returns or ["count", "nodes"]

    for s in parsed_steps:
        if isinstance(s, LimitOp):
            spec_limit = s.count
        elif isinstance(s, ReturnOp):
            spec_returns = s.fields

    return GraphQuerySpec(
        steps=parsed_steps,
        limit=spec_limit,
        returns=spec_returns,
    )
