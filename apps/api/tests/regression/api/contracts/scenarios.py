"""Request contracts, derived from the live OpenAPI schema: every body field × every way to break it (CONT)."""
from __future__ import annotations

import functools

from tests.regression.framework.matrix import Scenario

WRONG = {"string": 12345, "integer": "not-a-number", "number": "not-a-number", "boolean": {"no": "bool"},
         "array": "not-a-list", "object": "not-an-object"}


@functools.lru_cache(maxsize=1)
def openapi() -> dict:
    import opencortyx.main as main
    return main.app.openapi()


def resolve(schema: dict) -> dict:
    comp = openapi()["components"]["schemas"]
    while "$ref" in schema:
        schema = comp[schema["$ref"].split("/")[-1]]
    return schema


def kinds(prop: dict) -> tuple[str, bool]:
    """(json type, nullable) of a property, looking through anyOf[..., null]."""
    prop = resolve(prop)
    if "anyOf" in prop:
        types = [resolve(x).get("type") for x in prop["anyOf"]]
        return next((t for t in types if t and t != "null"), "any"), "null" in types
    return prop.get("type", "any"), False


def sample(prop: dict):
    prop = resolve(prop)
    if "default" in prop and prop["default"] is not None:
        return prop["default"]
    if prop.get("enum"):
        return prop["enum"][0]
    t, _ = kinds(prop)
    return {"string": "regression", "integer": 1, "number": 1.5, "boolean": True, "array": [], "object": {}}.get(t, "x")


def build() -> list[Scenario]:
    from tests.regression.framework.api import HEAVY, NEVER_EXECUTE
    spec, out = openapi(), []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            m = method.upper()
            if (m, path) in NEVER_EXECUTE or (m, path) in HEAVY:
                continue
            ref = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
            if not ref:
                continue
            body = resolve(ref)
            props, required = body.get("properties", {}), set(body.get("required", []))
            if not props:
                continue
            baseline = {k: sample(v) for k, v in props.items() if k in required}
            for field, prop in props.items():
                t, nullable = kinds(prop)
                cases = []
                if t in WRONG:
                    cases.append(("wrong_type", WRONG[t], "422"))
                if not nullable and field in required:
                    cases.append(("null", None, "422"))
                if field in required:
                    cases.append(("missing", "<missing>", "422"))
                if resolve(prop).get("enum"):
                    cases.append(("invalid_enum", "not-a-member", "422"))
                if t == "string":
                    cases.append(("oversized_100k", "x" * 100_000, "no 5xx"))
                    cases.append(("control_chars", "a\x00b\x1fc‮", "no 5xx"))
                for name, value, exp in cases:
                    out.append(Scenario("CONT", "api", f"request contract:{body.get('title', '?')}",
                                        f"{m} {path}", f"{field}={name}", f"{t}{'?' if nullable else ''}"
                                        f"{' required' if field in required else ''}", exp,
                                        "contract" if exp == "422" else "negative", "API up; tenant A",
                                        {"method": m, "path": path, "field": field, "value": value,
                                         "baseline": baseline, "expect": exp}))
    return out
