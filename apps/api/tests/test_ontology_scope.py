"""The ontology is per organization over shared platform rows: shadowing and copy-on-write."""
import asyncio
import uuid

from opencortyx.domain.ontology import PLATFORM_ORG, TypeRegistry, effective, find_type, own_type

ORG, OTHER = uuid.uuid4(), uuid.uuid4()


def _row(org, name="Person", kind="node", description="built-in", **kw):
    return TypeRegistry(workspace_id=org, name=name, kind=kind, description=description,
                        properties_schema=kw.pop("properties_schema", {"fields": [{"name": "age"}]}),
                        source=kw.pop("source", "system"), status=kw.pop("status", "active"), **kw)


class _Db:
    """What own_type / find_type use of a session: get by key, add."""
    def __init__(self, *rows):
        self.rows = {(r.workspace_id, r.name, r.kind): r for r in rows}
        self.added = []

    async def get(self, _model, key):
        return self.rows.get(key)

    def add(self, row):
        self.added.append(row)
        self.rows[(row.workspace_id, row.name, row.kind)] = row


def test_an_org_row_shadows_the_platform_row_whatever_the_order():
    base, mine = _row(PLATFORM_ORG), _row(ORG, description="mine")
    for rows in ([base, mine], [mine, base]):
        (only,) = effective(rows)
        assert only is mine


def test_distinct_types_are_all_kept():
    rows = [_row(PLATFORM_ORG), _row(PLATFORM_ORG, kind="entity"), _row(ORG, name="Mine")]
    assert {(r.name, r.kind) for r in effective(rows)} == {("Person", "node"), ("Person", "entity"), ("Mine", "node")}


def test_changing_a_builtin_copies_it_into_the_org_and_leaves_the_platform_row_alone():
    base = _row(PLATFORM_ORG, allowed_source_types=["A"])
    db = _Db(base)
    own = asyncio.run(own_type(db, ORG, "Person", "node"))
    assert own is not base and own.workspace_id == ORG and db.added == [own]
    own.description = "edited"
    own.properties_schema["fields"].append({"name": "x"})
    own.allowed_source_types.append("B")
    assert base.description == "built-in"
    assert base.properties_schema == {"fields": [{"name": "age"}]} and base.allowed_source_types == ["A"]


def test_the_orgs_own_row_is_reused_not_copied_again():
    mine = _row(ORG, description="mine")
    db = _Db(_row(PLATFORM_ORG), mine)
    assert asyncio.run(own_type(db, ORG, "Person", "node")) is mine and db.added == []


def test_another_orgs_row_is_invisible():
    db = _Db(_row(OTHER, name="Private"))
    assert asyncio.run(find_type(db, ORG, "Private", "node")) is None
    assert asyncio.run(own_type(db, ORG, "Private", "node")) is None


def test_find_prefers_the_orgs_own_row():
    mine = _row(ORG, description="mine")
    db = _Db(_row(PLATFORM_ORG), mine)
    assert asyncio.run(find_type(db, ORG, "Person", "node")) is mine
    assert asyncio.run(find_type(db, OTHER, "Person", "node")).workspace_id == PLATFORM_ORG
