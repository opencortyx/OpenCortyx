"""NebulaGraph adapter.

One space holds every organization. A node is a vertex with the `node` tag and an edge is a `rel`
edge: types are properties, so a new node or relationship type never changes the schema. Vertex
ids are node UUIDs, and an edge's rank is derived from its type, so one relationship of a type
between two nodes is exactly one edge. A node's degree lives on a second tag, `node_stats`, that
only ever receives atomic increments, so rewriting a node record never loses a concurrent count.

The client is synchronous and runs in worker threads, which keeps it usable from Celery tasks that
each run on a fresh event loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from opencortyx.stores.graph import query as q
from opencortyx.stores.graph.exceptions import (
    GraphStoreConnectionError,
    GraphStoreOperationError,
    GraphStoreValidationError,
)
from opencortyx.stores.graph.models import (
    Edge, EdgeQuery, EdgeWrite, Neighbor, NeighborGroup, Node, NodeQuery, Path, edge_key, type_variants,
)
from opencortyx.stores.graph.port import GraphStore

logger = logging.getLogger("opencortyx.stores.graph.nebulagraph")

_KEY_LEN = 120          # stored length of label_key and source_key, equal to their index length

_NODE_PROPS = (
    ("workspace_id", "string NOT NULL"), ("collection_id", "string NULL"), ("type", "string NOT NULL"),
    ("base_type", "string NULL"), ("entity_type", "string NULL"), ("label", "string NULL"),
    ("label_key", "string NULL"), ("name_key", "string NULL"), ("properties", "string NULL"),
    ("source_ref", "string NULL"),
    ("source_key", "string NULL"), ("status", "string NULL"), ("confidence", "double NULL"),
    ("level", "int64 NULL"), ("sensitivity", "string NULL"), ("created_at", "int64 NULL"),
    ("updated_at", "int64 NULL"),
)
# Spaces created before the cluster view also carry x, y and lod columns and a node_by_x index from
# the old tile map. Nothing reads or writes them now; the schema step only ever adds columns.
_EDGE_PROPS = (
    ("id", "string NOT NULL"), ("workspace_id", "string NOT NULL"), ("type", "string NOT NULL"),
    ("weight", "double NULL"), ("confidence", "double NULL"), ("properties", "string NULL"),
    ("created_at", "int64 NULL"),
)
_NODE_INDEXES = {
    "node_by_type": "workspace_id(36), type(80)",
    "node_by_base_type": "workspace_id(36), base_type(80)",
    "node_by_source_ref": "workspace_id(36), source_ref(36)",
    "node_by_source_key": f"workspace_id(36), source_key({_KEY_LEN})",
    "node_by_collection": "workspace_id(36), collection_id(36)",
    "node_by_label": f"workspace_id(36), label_key({_KEY_LEN})",
    "node_by_name": f"workspace_id(36), name_key({_KEY_LEN})",
    "node_by_status": "workspace_id(36), status(16)",
    "node_by_created": "workspace_id(36), created_at",
}
_EDGE_INDEXES = {
    "rel_by_type": "workspace_id(36), type(80)",
    "rel_by_id": "workspace_id(36), id(36)",
}
#: The tenant field's name before it was renamed `workspace_id`; a space an older release created
#: still has it until `_rename_tenant_field` runs at start-up.
_LEGACY_TENANT = "org_id"
_NODE_COLUMNS = ", ".join(name for name, _ in _NODE_PROPS)
_EDGE_COLUMNS = ", ".join(name for name, _ in _EDGE_PROPS)
_BATCH = 200
_IN_CHUNK = 500
#: Rows per read (per partition) when a pass covers a whole workspace or a wide window of it. The
#: graph service frees memory only every few seconds: one answer of ~115,000 rows, ids only, held
#: ~400 MB of its 1 GB, and a few such passes at once took it down. Measured over 179,000 nodes, a
#: whole pass peaks at ~620 MB with 2000, ~250 MB with 500 and ~120 MB with 200 (and takes longer).
_SLICE = 500
#: NebulaGraph refuses a write to a vertex or edge another request is writing at that moment ("More
#: than one request trying to add/update/delete one edge/vertex at the same time"). That is optimistic
#: concurrency, and the answer is to try again shortly: every ingest touches the same hub vertices (the
#: day's Date node, the workspace node), so two workers writing at once failed whole files on it.
_CONFLICT = "more than one request"
_CONFLICT_RETRIES = 8
#: Degree updates in flight at once; each vertex is its own statement (UPSERT takes one).
_DEGREE_PARALLEL = 8


def _lit(value: Any) -> str:
    """An nGQL literal. Strings are JSON-escaped, which nGQL reads back exactly."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else "NULL"
    return json.dumps(str(value).replace("\x00", ""), ensure_ascii=False)


def _vid(value) -> str:
    """A vertex id literal. Only UUIDs are vertex ids, so nothing else reaches a statement."""
    return f'"{uuid.UUID(str(value))}"'


def _list(values) -> str:
    return "[" + ", ".join(_lit(v) for v in values) + "]"


#: Values per lookup when matching one of several values on an index.
_ANY_CHUNK = 100


def _any_of(base: str, field: str, values) -> str:
    """`base AND field IN values`, written as one exact match per value. NebulaGraph answers an OR of
    exact index matches with one index lookup each, but an IN on an index's second column (after
    `workspace_id`) with more than one value scans the workspace's whole index and filters it in the
    graph service: measured on 179,000 nodes, 1.6 s and up to ~365 MB for 5 ids, against 0.05 s.
    Every subtree walk and id lookup paid that, which is what kept the graph service falling over."""
    return " OR ".join(f"({base} AND {field} == {_lit(v)})" for v in values)


def _rank(e: Edge) -> int:
    key = edge_key(e.type, e.properties)
    return int.from_bytes(hashlib.sha1(key.encode()).digest()[:8], "big") & (2 ** 63 - 1)


def _micros(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000)


def _from_micros(value) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)


def _chunks(items: Sequence, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _py(v) -> Any:
    """A nebula ValueWrapper as a plain Python value."""
    if v is None or v.is_null() or v.is_empty():
        return None
    if v.is_string():
        return v.as_string()
    if v.is_int():
        return v.as_int()
    if v.is_double():
        return v.as_double()
    if v.is_bool():
        return v.as_bool()
    if v.is_map():
        return {k: _py(x) for k, x in v.as_map().items()}
    if v.is_list():
        return [_py(x) for x in v.as_list()]
    return str(v)


def _loads(text: str | None) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _node_from(vid: str, p: dict) -> Node | None:
    if not p or p.get("workspace_id") is None:
        return None
    return Node(
        id=uuid.UUID(vid), workspace_id=uuid.UUID(p["workspace_id"]), type=p.get("type") or "",
        label=p.get("label"), entity_type=p.get("entity_type"),
        properties=_loads(p.get("properties")),
        collection_id=q.as_uuid(p.get("collection_id")), source_ref=q.as_uuid(p.get("source_ref")),
        confidence=p["confidence"] if p.get("confidence") is not None else 1.0,
        level=p.get("level") or 0, sensitivity=p.get("sensitivity") or "normal",
        degree=max(0, p.get("degree") or 0),
        created_at=_from_micros(p.get("created_at")), updated_at=_from_micros(p.get("updated_at")),
    )


def _edge_from(src: str, dst: str, p: dict) -> Edge | None:
    if not p or p.get("workspace_id") is None:
        return None
    return Edge(
        id=uuid.UUID(p["id"]), workspace_id=uuid.UUID(p["workspace_id"]), source_id=uuid.UUID(src),
        target_id=uuid.UUID(dst), type=p.get("type") or "",
        weight=p["weight"] if p.get("weight") is not None else 1.0,
        properties=_loads(p.get("properties")),
        confidence=p["confidence"] if p.get("confidence") is not None else 1.0,
        created_at=_from_micros(p.get("created_at")),
    )


def _node_values(n: Node) -> str:
    props = n.properties or {}
    source_key = q.as_text(props.get("source_key"))
    label_key = q.label_key(n.label)
    values = (
        str(n.workspace_id), str(n.collection_id) if n.collection_id else None, n.type,
        props.get("__base_type") or n.type, n.entity_type, n.label,
        label_key[:_KEY_LEN] if label_key else None,
        (q.normalize_name(n.label) or None) and q.normalize_name(n.label)[:_KEY_LEN],
        json.dumps(props, ensure_ascii=False, default=str),
        str(n.source_ref) if n.source_ref else None,
        source_key[:_KEY_LEN] if source_key else None,
        q.node_status(n), float(n.confidence if n.confidence is not None else 1.0),
        int(n.level or 0), n.sensitivity or "normal", _micros(n.created_at), _micros(n.updated_at),
    )
    return f"{_vid(n.id)}:(" + ", ".join(_lit(v) for v in values) + ")"


def _edge_key(e: Edge) -> str:
    return f"{_vid(e.source_id)}->{_vid(e.target_id)}@{_rank(e)}"


def _edge_values(e: Edge) -> str:
    values = (str(e.id), str(e.workspace_id), e.type, float(e.weight if e.weight is not None else 1.0),
              float(e.confidence if e.confidence is not None else 1.0),
              json.dumps(e.properties or {}, ensure_ascii=False, default=str), _micros(e.created_at))
    return f"{_edge_key(e)}:(" + ", ".join(_lit(v) for v in values) + ")"


class NebulaGraphStore(GraphStore):
    provider_name = "nebulagraph"

    def __init__(self, host: str, port: int, username: str, password: str, space: str,
                 timeout_s: float = 30.0, pool_size: int = 10, partitions: int = 10):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.space = space
        self.timeout_ms = int(timeout_s * 1000)
        self.pool_size = pool_size
        self.partitions = partitions
        self._pool = None
        self._lock = threading.Lock()

    # connection and schema

    def _admin(self, stmt: str, space: str | None = None):
        from nebula3.Config import Config
        from nebula3.gclient.net import ConnectionPool
        cfg = Config()
        cfg.timeout = self.timeout_ms
        pool = ConnectionPool()
        if not pool.init([(self.host, self.port)], cfg):
            raise GraphStoreConnectionError(f"cannot reach NebulaGraph at {self.host}:{self.port}",
                                            provider=self.provider_name)
        try:
            with pool.session_context(self.username, self.password) as session:
                if space:
                    session.execute(f"USE `{space}`;")
                return session.execute(stmt)
        finally:
            pool.close()

    def _names(self, stmt: str) -> set[str]:
        rs = self._admin(stmt, self.space)
        if not rs.is_succeeded():
            return set()
        return {rs.row_values(i)[0].as_string() for i in range(rs.row_size())}

    def _wait(self, check, what: str, seconds: int = 120) -> None:
        deadline = time.time() + seconds
        while True:
            if check():
                return
            if time.time() > deadline:
                raise GraphStoreOperationError(f"NebulaGraph {what} not ready after {seconds}s",
                                               provider=self.provider_name)
            time.sleep(1)

    def _bootstrap(self) -> None:
        """Create the space, schema and indexes that are missing, and wait until they are usable."""
        self._admin('ADD HOSTS "nebula-storaged":9779;') if self.host == "nebulagraph" else None
        spaces = self._admin("SHOW SPACES;")
        existing = {spaces.row_values(i)[0].as_string() for i in range(spaces.row_size())} \
            if spaces.is_succeeded() else set()
        if self.space not in existing:
            create = (f"CREATE SPACE IF NOT EXISTS `{self.space}` (partition_num={self.partitions}, "
                      "replica_factor=1, vid_type=FIXED_STRING(36));")
            # A host added a moment ago is not ONLINE until its first heartbeat reaches metad, and
            # until then CREATE SPACE fails with "Host not enough!". On a fresh volume that is every
            # first start, so wait for the heartbeat rather than failing the install.
            def created() -> bool:
                rs = self._admin(create)
                if rs.is_succeeded():
                    return True
                if "Host not enough" not in rs.error_msg():
                    raise GraphStoreOperationError(f"create space failed: {rs.error_msg()}",
                                                   provider=self.provider_name)
                return False

            self._wait(created, "storage host", seconds=90)
        self._wait(lambda: self._admin("SHOW TAGS;", self.space).is_succeeded(), "space")

        made = False
        if "node" not in self._names("SHOW TAGS;"):
            columns = ", ".join(f"{name} {ddl}" for name, ddl in _NODE_PROPS)
            self._ddl(f"CREATE TAG IF NOT EXISTS node({columns});")
            made = True
        if "node_stats" not in self._names("SHOW TAGS;"):
            self._ddl("CREATE TAG IF NOT EXISTS node_stats(degree int64 DEFAULT 0);")
            made = True
        if "rel" not in self._names("SHOW EDGES;"):
            columns = ", ".join(f"{name} {ddl}" for name, ddl in _EDGE_PROPS)
            self._ddl(f"CREATE EDGE IF NOT EXISTS rel({columns});")
            made = True
        altered = False
        if not made:
            altered = self._rename_tenant_field("TAG", "node")
            altered = self._rename_tenant_field("EDGE", "rel") or altered
            altered = self._add_missing_columns("TAG", "node", _NODE_PROPS) or altered
            altered = self._add_missing_columns("EDGE", "rel", _EDGE_PROPS) or altered
        if made or altered:
            self._wait(self._schema_usable, "schema")

        new_indexes = []
        have = self._names("SHOW TAG INDEXES;")
        for name, fields in _NODE_INDEXES.items():
            if name not in have:
                self._ddl(f"CREATE TAG INDEX IF NOT EXISTS {name} ON node({fields});")
                new_indexes.append(("TAG", name))
        have = self._names("SHOW EDGE INDEXES;")
        for name, fields in _EDGE_INDEXES.items():
            if name not in have:
                self._ddl(f"CREATE EDGE INDEX IF NOT EXISTS {name} ON rel({fields});")
                new_indexes.append(("EDGE", name))
        if new_indexes:
            # Storage learns about a new index on its next heartbeat and does not index writes before
            # then. Wait until indexes answer; on a schema that already held data, rebuild them.
            self._wait(self._indexes_usable, "indexes")
            if not made:
                for kind, name in new_indexes:
                    self._ddl(f"REBUILD {kind} INDEX {name};")
                self._wait(lambda: self._rebuilt([n for _, n in new_indexes]), "index rebuild", 3600)
        logger.info("NebulaGraph space %s ready", self.space)

    def _rename_tenant_field(self, kind: str, name: str) -> bool:
        """Rename an older space's `org_id` to `workspace_id` on the `node` tag or the `rel` edge.

        nGQL cannot rename a column, so: add `workspace_id`, copy every value across, drop the
        indexes that name `org_id` (the index step after this recreates them on `workspace_id` and
        rebuilds them), then drop `org_id`. Each step is skipped once done, so a start-up that was
        interrupted part-way resumes where it stopped. True when anything changed."""
        legacy = _LEGACY_TENANT
        have = self._columns(kind, name)
        if legacy not in have:
            return False
        logger.info("NebulaGraph: renaming %s.%s to workspace_id", name, legacy)
        if "workspace_id" not in have:
            self._ddl(f'ALTER {kind} {name} ADD (workspace_id string NOT NULL DEFAULT "");')
        if kind == "TAG":
            find = f"LOOKUP ON {name} YIELD id(vertex) AS vid, {name}.{legacy} AS t;"

            def update(row):
                return f"UPDATE VERTEX ON {name} {_vid(row[0])} SET workspace_id = {_lit(row[1])};"
        else:
            find = (f"LOOKUP ON {name} YIELD src(edge) AS s, dst(edge) AS d, rank(edge) AS r, "
                    f"{name}.{legacy} AS t;")

            def update(row):
                return (f"UPDATE EDGE ON {name} {_vid(row[0])}->{_vid(row[1])}@{int(row[2])} "
                        f"SET workspace_id = {_lit(row[3])};")
        # Storage accepts writes to the new column only after its next heartbeat; until then the
        # copy fails and is simply tried again.
        self._wait(lambda: self._copy_rows(find, update), f"{name} workspace_id copy", 3600)
        rs = self._admin(f"SHOW {kind} INDEXES;", self.space)
        for i in range(rs.row_size() if rs.is_succeeded() else 0):
            row = [_py(v) for v in rs.row_values(i)]
            if row[1] == name and legacy in (row[2] or []):
                self._ddl(f"DROP {kind} INDEX IF EXISTS {row[0]};")
        self._wait(lambda: self._admin(f"ALTER {kind} {name} DROP ({legacy});", self.space).is_succeeded()
                   or legacy not in self._columns(kind, name), f"{name}.{legacy} drop")
        return True

    def _copy_rows(self, find: str, update) -> bool:
        """Run `update` for every row `find` yields, in batches on one session. False on any failure."""
        from nebula3.Config import Config
        from nebula3.gclient.net import ConnectionPool
        cfg = Config()
        cfg.timeout = self.timeout_ms
        pool = ConnectionPool()
        if not pool.init([(self.host, self.port)], cfg):
            return False
        try:
            with pool.session_context(self.username, self.password) as session:
                session.execute(f"USE `{self.space}`;")
                rs = session.execute(find)
                if not rs.is_succeeded():
                    return False
                rows = [[_py(v) for v in rs.row_values(i)] for i in range(rs.row_size())]
                for batch in _chunks(rows, _BATCH):
                    for row in batch:
                        if not session.execute(update(row)).is_succeeded():
                            return False
                return True
        finally:
            pool.close()

    def _columns(self, kind: str, name: str) -> set[str]:
        rs = self._admin(f"DESCRIBE {kind} {name};", self.space)
        return {rs.row_values(i)[0].as_string() for i in range(rs.row_size())} if rs.is_succeeded() else set()

    def _add_missing_columns(self, kind: str, name: str, columns) -> bool:
        """Add columns a newer release defines to a tag or edge created by an older one."""
        rs = self._admin(f"DESCRIBE {kind} {name};", self.space)
        have = {rs.row_values(i)[0].as_string() for i in range(rs.row_size())} if rs.is_succeeded() else set()
        missing = [f"{col} {ddl}" for col, ddl in columns if col not in have]
        if missing:
            self._ddl(f"ALTER {kind} {name} ADD ({', '.join(missing)});")
        return bool(missing)

    def _ddl(self, stmt: str) -> None:
        rs = self._admin(stmt, self.space)
        if not rs.is_succeeded():
            raise GraphStoreOperationError(f"{stmt[:80]} failed: {rs.error_msg()}", provider=self.provider_name)

    def _schema_usable(self) -> bool:
        """Whether graphd accepts writes naming every column this release defines."""
        probe = '"00000000-0000-0000-0000-000000000000"'
        node_values = ", ".join('""' if c in ("workspace_id", "type") else "NULL" for c, _ in _NODE_PROPS)
        edge_values = ", ".join('""' if c in ("id", "workspace_id", "type") else "NULL" for c, _ in _EDGE_PROPS)
        stmts = (f"INSERT VERTEX node({_NODE_COLUMNS}) VALUES {probe}:({node_values});",
                 f"UPSERT VERTEX ON node_stats {probe} SET degree = degree + 1;",
                 f"INSERT EDGE rel({_EDGE_COLUMNS}) VALUES {probe}->{probe}@0:({edge_values});",
                 f"DELETE EDGE rel {probe}->{probe}@0;", f"DELETE VERTEX {probe};")
        return all(self._admin(s, self.space).is_succeeded() for s in stmts)

    def _rebuilt(self, names: list[str]) -> bool:
        states = {}
        for kind in ("TAG", "EDGE"):
            rs = self._admin(f"SHOW {kind} INDEX STATUS;", self.space)
            if rs.is_succeeded():
                states.update({rs.row_values(i)[0].as_string(): rs.row_values(i)[1].as_string()
                               for i in range(rs.row_size())})
        failed = [n for n in names if states.get(n) == "FAILED"]
        if failed:
            raise GraphStoreOperationError(f"index rebuild failed: {failed}", provider=self.provider_name)
        return all(states.get(n) == "FINISHED" for n in names)

    def _indexes_usable(self) -> bool:
        probe = '"00000000-0000-0000-0000-000000000000"'
        self._admin(f'INSERT VERTEX node(workspace_id, type) VALUES {probe}:("__probe", "__probe");', self.space)
        self._admin(f'INSERT EDGE rel(id, workspace_id, type) VALUES {probe}->{probe}@0:("__probe", "__probe", "__probe");',
                    self.space)
        try:
            nodes = self._admin('LOOKUP ON node WHERE node.workspace_id == "__probe" AND node.type == "__probe" '
                                "YIELD id(vertex) AS vid;", self.space)
            edges = self._admin('LOOKUP ON rel WHERE rel.workspace_id == "__probe" AND rel.type == "__probe" '
                                "YIELD rel.id AS id;", self.space)
            return nodes.is_succeeded() and nodes.row_size() > 0 and edges.is_succeeded() and edges.row_size() > 0
        finally:
            self._admin(f"DELETE VERTEX {probe} WITH EDGE;", self.space)

    def _session_pool(self):
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._pool is None:
                from nebula3.Config import SessionPoolConfig
                from nebula3.gclient.net.SessionPool import SessionPool
                self._bootstrap()
                cfg = SessionPoolConfig()
                cfg.timeout = self.timeout_ms
                cfg.max_size = self.pool_size
                cfg.min_size = 1
                pool = SessionPool(self.username, self.password, self.space, [(self.host, self.port)])
                if not pool.init(cfg):
                    raise GraphStoreConnectionError("cannot open NebulaGraph session pool",
                                                    provider=self.provider_name)
                self._pool = pool
        return self._pool

    def _run(self, stmt: str):
        import random
        reconnected = conflicts = 0
        while True:
            try:
                rs = self._session_pool().execute(stmt)
            except Exception as e:                      # noqa: BLE001 - the client raises bare errors
                if reconnected:
                    raise GraphStoreConnectionError(f"NebulaGraph request failed: {e}",
                                                    provider=self.provider_name, original_error=e) from e
                reconnected += 1
                self._reset()
                continue
            if rs.is_succeeded():
                return rs
            message = rs.error_msg()
            if not reconnected and "session" in message.lower():
                reconnected += 1
                self._reset()
                continue
            if _CONFLICT in message.lower() and conflicts < _CONFLICT_RETRIES:
                conflicts += 1                          # jittered backoff: 20 ms, 40 ms ... ~2.5 s
                time.sleep(random.uniform(0.5, 1.0) * 0.02 * 2 ** conflicts)
                continue
            raise GraphStoreOperationError(f"{message} in: {stmt[:300]}", provider=self.provider_name)

    def _reset(self) -> None:
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.close()
                except Exception:                       # noqa: BLE001
                    pass
            self._pool = None

    async def _exec(self, stmt: str):
        return await asyncio.to_thread(self._run, stmt)

    async def _rows(self, stmt: str) -> list[list]:
        rs = await self._exec(stmt)
        return [[_py(v) for v in rs.row_values(i)] for i in range(rs.row_size())]

    async def initialize(self) -> None:
        await asyncio.to_thread(self._session_pool)

    async def health_check(self) -> bool:
        """Healthy means the graph is USABLE, which needs a storage host, not just a live graphd.

        `YIELD 1` only proves graphd answers. A cluster whose storaged is unregistered or down
        answers it perfectly well and then fails every read and write — which is exactly the state
        a first install lands in before `ADD HOSTS` runs. Reporting that as healthy is how a broken
        graph looks fine on the dashboard.
        """
        try:
            await self._exec("YIELD 1;")
            return bool(await self._online_storage_hosts())
        except Exception:                               # noqa: BLE001
            return False

    async def _online_storage_hosts(self) -> list:
        """(host, port, version) for each ONLINE storage host. Empty when the tier is unusable."""
        rs = await self._exec("SHOW HOSTS;")
        if not rs.is_succeeded():
            return []
        hosts = []
        for i in range(rs.row_size()):
            row = rs.row_values(i)
            if row[2].as_string().upper() != "ONLINE":
                continue
            version = row[6].as_string() if len(row) > 6 else ""
            hosts.append((row[0].as_string(), row[1].as_int(), version))
        return hosts

    async def describe(self) -> dict:
        try:
            online = await self._online_storage_hosts()
        except Exception as e:                          # noqa: BLE001
            return {"provider": self.provider_name, "version": "",
                    "detail": f"cluster status unavailable: {e}"}
        version = next((v for _h, _p, v in online if v), "")
        detail = (f"{len(online)} storage host(s) online" if online
                  else "NO storage host online - the graph cannot be read or written")
        return {"provider": self.provider_name, "version": version, "detail": detail}

    async def close(self) -> None:
        await asyncio.to_thread(self._reset)

    # nodes

    async def upsert_nodes(self, nodes: Sequence[Node]) -> None:
        for batch in _chunks(list(nodes), _BATCH):
            await self._exec(f"INSERT VERTEX node({_NODE_COLUMNS}) VALUES "
                             + ", ".join(_node_values(n) for n in batch) + ";")

    async def get_nodes(self, workspace_id, ids: Sequence) -> list[Node]:
        out = []
        unique = list(dict.fromkeys(str(i) for i in ids if i is not None))
        for batch in _chunks(unique, _IN_CHUNK):
            rows = await self._rows("FETCH PROP ON node, node_stats " + ", ".join(_vid(i) for i in batch)
                                    + " YIELD id(vertex) AS vid, properties(vertex) AS p;")
            for vid, props in rows:
                node = _node_from(vid, props)
                if node is not None and str(node.workspace_id) == str(workspace_id):
                    out.append(node)
        return out

    def _lookup_where(self, workspace_id, query: NodeQuery) -> tuple[list[str], bool]:
        """Index predicates for a LOOKUP, and whether they alone decide the query."""
        base = f"node.workspace_id == {_lit(str(workspace_id))}"
        exact = not (query.props or query.props_not or query.label_contains or query.exclude_types
                     or query.entity_type is not None or query.ids is not None)
        if query.source_refs is not None:
            refs = [str(r) for r in query.source_refs if r is not None]
            clauses = [_any_of(base, "node.source_ref", chunk) for chunk in _chunks(refs, _ANY_CHUNK)]
            return clauses, exact and not any((query.types, query.exact_types, query.collection_ids,
                                               query.source_key, query.status, query.label,
                                               query.created_after, query.created_before))
        if query.source_key is not None:
            key = str(query.source_key)
            return [f"{base} AND node.source_key == {_lit(key[:_KEY_LEN])}"], False
        if query.label is not None:
            key = q.label_key(query.label) or ""
            return [f"{base} AND node.label_key == {_lit(key[:_KEY_LEN])}"], False
        if query.name_key is not None:
            return [f"{base} AND node.name_key == {_lit(query.name_key[:_KEY_LEN])}"], False
        if query.types:
            wanted = type_variants(*query.types)
            clauses = [_any_of(base, "node.base_type", wanted), _any_of(base, "node.type", wanted)]
            return clauses, False
        if query.exact_types:
            return [_any_of(base, "node.type", list(query.exact_types))], \
                exact and not any((query.collection_ids, query.status, query.created_after,
                                   query.created_before))
        if query.collection_ids:
            ids = [str(c) for c in query.collection_ids]
            return [_any_of(base, "node.collection_id", ids)], \
                exact and not any((query.status, query.created_after, query.created_before))
        if query.status is not None:
            return [f"{base} AND node.status == {_lit(query.status)}"], \
                exact and query.created_after is None and query.created_before is None
        if query.created_after is not None or query.created_before is not None:
            since = "" if query.created_after is None else f" AND node.created_at > {_micros(query.created_after)}"
            until = "" if query.created_before is None else f" AND node.created_at <= {_micros(query.created_before)}"
            return [f"{base}{since}{until}"], exact
        return [base], exact

    async def _lookup_ids(self, workspace_id, query: NodeQuery) -> list[str]:
        clauses, exact = self._lookup_where(workspace_id, query)
        order = ""
        if exact and query.order_by and len(clauses) == 1:
            key = query.order_by.lstrip("-")
            if key in ("created_at", "updated_at"):
                direction = "DESC" if query.order_by.startswith("-") else "ASC"
                order = f", node.{key} AS k | ORDER BY $-.k {direction}"
        limit = f" | LIMIT {int(query.limit)}" if exact and query.limit is not None and (
            len(clauses) == 1) and (order or not query.order_by) else ""
        seen: dict[str, None] = {}
        for where in clauses:
            for row in await self._rows(f"LOOKUP ON node WHERE {where} YIELD id(vertex) AS vid{order}{limit};"):
                seen.setdefault(row[0], None)
        return list(seen)

    async def find_nodes(self, workspace_id, query: NodeQuery) -> list[Node]:
        if query.ids is not None:
            candidates = await self.get_nodes(workspace_id, query.ids)
        else:
            candidates = await self.get_nodes(workspace_id, await self._lookup_ids(workspace_id, query))
        return q.select_nodes(candidates, query)

    async def _created_slices(self, workspace_id, yields: str, after=None, before=None) -> list:
        """Rows for the workspace's nodes created in (after, before], read as `node_by_created` index
        ranges of at most `_SLICE` rows: a range whose capped read comes back full is halved and read
        again. Bounded answers whatever the workspace's size, at a few wasted reads per split."""
        base = f"node.workspace_id == {_lit(str(workspace_id))}"
        lo = int(_micros(after) or 0) if after is not None else -1
        hi = (int(_micros(before) or 0) if before is not None
              else int(_micros(datetime.now(timezone.utc)) or 0) + 86_400_000_000)
        out: list = []
        todo = [(lo, hi)]
        while todo:
            a, b = todo.pop()
            divisible = b - a > 1            # a single microsecond is read whole: it cannot be split
            stmt = f"LOOKUP ON node WHERE {base} AND node.created_at > {a} AND node.created_at <= {b} YIELD {yields}"
            rows = await self._rows(stmt + (f" | LIMIT {_SLICE + 1};" if divisible else ";"))
            if divisible and len(rows) > _SLICE:
                mid = (a + b) // 2
                todo += [(mid, b), (a, mid)]
                continue
            out += rows
        return out

    async def find_node_ids(self, workspace_id, query: NodeQuery) -> list[str]:
        if query == NodeQuery(created_after=query.created_after, created_before=query.created_before):
            rows = await self._created_slices(workspace_id, "id(vertex) AS vid",
                                              query.created_after, query.created_before)
            return [r[0] for r in rows]
        clauses, exact = self._lookup_where(workspace_id, query)
        if exact and query.ids is None and not query.order_by and query.limit is None:
            return await self._lookup_ids(workspace_id, query)       # the index answers; no FETCH
        return await super().find_node_ids(workspace_id, query)

    async def node_outlines(self, workspace_id) -> list[Node]:
        org = uuid.UUID(str(workspace_id))
        rows = await self._created_slices(workspace_id, "id(vertex) AS vid, node.type AS t, node.base_type AS b, "
                                                        "node.label AS l, node.source_ref AS s, node.status AS st")
        return [Node(id=uuid.UUID(vid), workspace_id=org, type=t, label=label, source_ref=q.as_uuid(ref),
                     properties={"__base_type": base or t, "__status": st or "active"})
                for vid, t, base, label, ref, st in rows]

    async def edge_ends(self, workspace_id, exclude_types: Sequence[str] = ()) -> list[tuple[str, str]]:
        # Each edge once, from its source node, a slice of nodes at a time: no answer holds all edges.
        org, skip, out = _lit(str(workspace_id)), set(exclude_types), []
        ids = [r[0] for r in await self._created_slices(workspace_id, "id(vertex) AS vid")]
        for batch in _chunks(ids, _IN_CHUNK):
            rows = await self._rows("GO FROM " + ", ".join(_vid(i) for i in batch)
                                    + f" OVER rel WHERE rel.workspace_id == {org} "
                                    "YIELD src(edge) AS s, dst(edge) AS d, rel.type AS t;")
            out += [(src, dst) for src, dst, t in rows if t not in skip]
        return out

    async def count_nodes(self, workspace_id, query: NodeQuery | None = None) -> int:
        query = query or NodeQuery()
        clauses, exact = self._lookup_where(workspace_id, query)
        if exact and query.ids is None and len(clauses) == 1:
            rows = await self._rows(f"LOOKUP ON node WHERE {clauses[0]} YIELD id(vertex) AS vid "
                                    "| YIELD count(*) AS n;")
            return int(rows[0][0]) if rows else 0
        return len(await self.find_nodes(workspace_id, NodeQuery(**{**query.__dict__, "limit": None, "order_by": None})))

    async def count_nodes_by_type(self, workspace_id) -> dict[str, int]:
        rows = await self._rows(f"LOOKUP ON node WHERE node.workspace_id == {_lit(str(workspace_id))} "
                                "YIELD node.type AS t | GROUP BY $-.t YIELD $-.t AS t, count(*) AS n;")
        return {t: int(n) for t, n in rows}

    async def add_to_degree(self, workspace_id, deltas: dict, current: dict | None = None) -> None:
        """Apply degree changes. A vertex in `current` (its degree as just read, for vertices the
        caller owns) is written with the rest in multi-row INSERTs; any other vertex gets an atomic
        UPSERT increment, since another writer may be changing it (the day's Date node is shared by
        every upload). UPSERT takes one vertex per statement: one per element of a document was
        thousands of round trips, most of an upload's time once the nodes and edges were batched."""
        current = current or {}
        bulk = {nid: max(0, int(current[nid]) + int(d)) for nid, d in deltas.items() if d and nid in current}
        for batch in _chunks(sorted(bulk), _BATCH):
            await self._exec("INSERT VERTEX node_stats(degree) VALUES "
                             + ", ".join(f"{_vid(nid)}:({bulk[nid]})" for nid in batch) + ";")
        gate = asyncio.Semaphore(_DEGREE_PARALLEL)

        async def one(nid, d):
            async with gate:
                await self._exec(f"UPSERT VERTEX ON node_stats {_vid(nid)} SET degree = degree + {int(d)};")
        await asyncio.gather(*(one(nid, d) for nid, d in deltas.items() if d and nid not in bulk))

    async def delete_nodes(self, workspace_id, ids: Sequence) -> int:
        nodes = await self.get_nodes(workspace_id, ids)
        if not nodes:
            return 0
        gone = {str(n.id) for n in nodes}
        deltas: dict[str, int] = {}
        for batch in _chunks(sorted(gone), _IN_CHUNK):
            rows = await self._rows("GO FROM " + ", ".join(_vid(i) for i in batch)
                                    + " OVER rel BIDIRECT YIELD src(edge) AS s, dst(edge) AS d, rank(edge) AS r;")
            for s, d, r in {tuple(row) for row in rows}:
                for end in (s, d):
                    if end not in gone:
                        deltas[end] = deltas.get(end, 0) - 1
        for batch in _chunks(sorted(gone), _IN_CHUNK):
            await self._exec("DELETE VERTEX " + ", ".join(_vid(i) for i in batch) + " WITH EDGE;")
        await self.add_to_degree(workspace_id, deltas)
        return len(gone)

    # edges

    async def upsert_edges(self, edges: Sequence[Edge], owned=None) -> EdgeWrite:
        result = EdgeWrite()
        owned = {str(i) for i in (owned or ())}
        edges = list({str(e.id): e for e in edges}.values())
        if not edges:
            return result
        by_org: dict[str, list[Edge]] = {}
        for e in edges:
            by_org.setdefault(str(e.workspace_id), []).append(e)
        for org, group in by_org.items():
            ends = {str(e.source_id) for e in group} | {str(e.target_id) for e in group}
            found = await self.get_nodes(org, list(ends))
            present = {str(n.id) for n in found}
            degree_now = {str(n.id): int(n.degree or 0) for n in found if str(n.id) in owned}
            writable = []
            for e in group:
                if str(e.source_id) in present and str(e.target_id) in present:
                    writable.append(e)
                else:
                    result.refused.append(e)
            deltas: dict[str, int] = {}
            for batch in _chunks(writable, _BATCH):
                rows = await self._rows("FETCH PROP ON rel " + ", ".join(_edge_key(e) for e in batch)
                                        + " YIELD rel.id AS id, rel.created_at AS c;")
                existing = {row[0]: row[1] for row in rows}
                for e in batch:
                    if str(e.id) in existing and existing[str(e.id)] is not None:
                        e.created_at = _from_micros(existing[str(e.id)])
                await self._exec(f"INSERT EDGE rel({_EDGE_COLUMNS}) VALUES "
                                 + ", ".join(_edge_values(e) for e in batch) + ";")
                for e in batch:
                    if str(e.id) in existing:
                        result.updated.append(e)
                    else:
                        result.created.append(e)
                        for end in (str(e.source_id), str(e.target_id)):
                            deltas[end] = deltas.get(end, 0) + 1
            await self.add_to_degree(org, deltas, current=degree_now)
        return result

    async def _go(self, workspace_id, ids: Sequence, direction: str, types, limit: int | None) -> list[Neighbor]:
        step = {"out": "", "in": " REVERSELY", "both": " BIDIRECT"}[direction]
        where = f"rel.workspace_id == {_lit(str(workspace_id))}"
        if types:
            where += f" AND rel.type IN {_list(list(types))}"
        out: list[Neighbor] = []
        seen: set[tuple[str, str]] = set()
        unique = list(dict.fromkeys(str(i) for i in ids if i is not None))
        for batch in _chunks(unique, _IN_CHUNK):
            stmt = ("GO 1 STEPS FROM " + ", ".join(_vid(i) for i in batch) + f" OVER rel{step} WHERE {where} "
                    "YIELD src(edge) AS s, dst(edge) AS d, properties(edge) AS ep, id($$) AS far, "
                    "properties($$) AS np")
            if limit is not None:
                # `LIMIT [n]` caps each vertex's edges in storage. `| LIMIT n` applies after GO has
                # fetched every neighbour with its properties: from a day's Date node (one edge per
                # node written that day, 185,000 after a thousand files) that ran the graph service
                # out of memory on a single question.
                stmt += f" LIMIT [{int(limit) - len(out)}]"
            for s, d, ep, far, np in await self._rows(stmt + ";"):
                edge = _edge_from(s, d, ep)
                node = _node_from(far, np) if np else None
                if edge is None or node is None or str(node.workspace_id) != str(workspace_id):
                    continue
                key = (str(edge.id), str(node.id))
                if key in seen:
                    continue
                seen.add(key)
                out.append(Neighbor(edge=edge, node=node, direction="out" if far == d else "in"))
            if limit is not None and len(out) >= limit:
                break
        return out[:limit] if limit is not None else out

    async def find_edges(self, workspace_id, query: EdgeQuery) -> list[Edge]:
        org = _lit(str(workspace_id))
        edges: list[Edge] = []
        if query.ids is not None:
            for batch in _chunks([str(i) for i in query.ids], _ANY_CHUNK):
                rows = await self._rows(f"LOOKUP ON rel WHERE {_any_of(f'rel.workspace_id == {org}', 'rel.id', batch)} "
                                        "YIELD src(edge) AS s, dst(edge) AS d, properties(edge) AS p;")
                edges += [e for e in (_edge_from(*row) for row in rows) if e]
        elif query.source_ids is not None or query.target_ids is not None or query.node_ids is not None:
            if query.source_ids is not None:
                hops = await self._go(workspace_id, query.source_ids, "out", query.types, None)
            elif query.target_ids is not None:
                hops = await self._go(workspace_id, query.target_ids, "in", query.types, None)
            else:
                hops = await self._go(workspace_id, query.node_ids or [], "both", query.types, None)
            edges = list({str(h.edge.id): h.edge for h in hops}.values())
        else:
            where = f"rel.workspace_id == {org}"
            if query.types:
                where = _any_of(where, "rel.type", list(query.types))
            rows = await self._rows(f"LOOKUP ON rel WHERE {where} "
                                    "YIELD src(edge) AS s, dst(edge) AS d, properties(edge) AS p;")
            edges = [e for e in (_edge_from(*row) for row in rows) if e]
        out = [e for e in edges if q.edge_matches(e, query)]
        return out[:query.limit] if query.limit is not None else out

    async def count_edges(self, workspace_id) -> int:
        rows = await self._rows(f"LOOKUP ON rel WHERE rel.workspace_id == {_lit(str(workspace_id))} "
                                "YIELD rel.id AS id | YIELD count(*) AS n;")
        return int(rows[0][0]) if rows else 0

    async def count_edges_by_type(self, workspace_id) -> dict[str, int]:
        rows = await self._rows(f"LOOKUP ON rel WHERE rel.workspace_id == {_lit(str(workspace_id))} "
                                "YIELD rel.type AS t | GROUP BY $-.t YIELD $-.t AS t, count(*) AS n;")
        return {t: int(n) for t, n in rows}

    async def delete_edges(self, workspace_id, edges: Sequence[Edge]) -> int:
        mine = [e for e in {str(e.id): e for e in edges}.values() if str(e.workspace_id) == str(workspace_id)]
        deleted = 0
        deltas: dict[str, int] = {}
        for batch in _chunks(mine, _BATCH):
            rows = await self._rows("FETCH PROP ON rel " + ", ".join(_edge_key(e) for e in batch)
                                    + " YIELD rel.id AS id;")
            existing = {row[0] for row in rows}
            present = [e for e in batch if str(e.id) in existing]
            if not present:
                continue
            await self._exec("DELETE EDGE rel " + ", ".join(_edge_key(e) for e in present) + ";")
            deleted += len(present)
            for e in present:
                for end in (str(e.source_id), str(e.target_id)):
                    deltas[end] = deltas.get(end, 0) - 1
        await self.add_to_degree(workspace_id, deltas)
        return deleted

    # traversal

    async def neighbors(self, workspace_id, node_ids: Sequence, *, edge_types: Sequence[str] | None = None,
                        direction: str = "out", limit: int = 200) -> list[Neighbor]:
        if direction not in ("out", "in", "both"):
            raise GraphStoreValidationError(
                f"direction must be out, in or both, not {direction!r}", provider="nebulagraph")
        return await self._go(workspace_id, node_ids, direction, edge_types, limit)

    async def neighbor_groups(self, workspace_id, node_id, *,
                              exclude_edge_types: Sequence[str] = ()) -> list[NeighborGroup]:
        # One GO per direction rather than BIDIRECT: the direction of each row is then known without
        # comparing ids, and a self-loop is counted once (as out) the same way the memory store does.
        where = f"rel.workspace_id == {_lit(str(workspace_id))}"
        if exclude_edge_types:
            where += f" AND NOT rel.type IN {_list(list(exclude_edge_types))}"
        out = []
        for direction, step in (("out", ""), ("in", " REVERSELY")):
            loops = "" if direction == "out" else f" AND id($$) != {_vid(node_id)}"
            rows = await self._rows(
                f"GO FROM {_vid(node_id)} OVER rel{step} WHERE {where}{loops} "
                "YIELD rel.type AS t, $$.node.type AS nt "
                "| GROUP BY $-.t, $-.nt YIELD $-.t AS t, $-.nt AS nt, count(*) AS n;")
            out += [NeighborGroup(edge_type=t, direction=direction, node_type=nt or "", count=int(n))
                    for t, nt, n in rows]
        return out

    async def neighbor_page(self, workspace_id, node_id, *, edge_type: str, direction: str, node_type: str,
                            offset: int = 0, limit: int = 100) -> list[Neighbor]:
        if direction not in ("out", "in"):
            raise GraphStoreValidationError(f"direction must be out or in, not {direction!r}",
                                            provider="nebulagraph")
        step = "" if direction == "out" else " REVERSELY"
        where = (f"rel.workspace_id == {_lit(str(workspace_id))} AND rel.type == {_lit(edge_type)} "
                 f"AND $$.node.type == {_lit(node_type)}")
        if direction == "in":
            where += f" AND id($$) != {_vid(node_id)}"
        rows = await self._rows(
            f"GO FROM {_vid(node_id)} OVER rel{step} WHERE {where} "
            "YIELD src(edge) AS s, dst(edge) AS d, properties(edge) AS ep, id($$) AS far, "
            f"properties($$) AS np | ORDER BY $-.far, $-.s, $-.d | LIMIT {max(0, int(offset))}, {max(0, int(limit))};")
        out = []
        for s, d, ep, far, np in rows:
            edge = _edge_from(s, d, ep)
            node = _node_from(far, np) if np else None
            if edge is not None and node is not None and str(node.workspace_id) == str(workspace_id):
                out.append(Neighbor(edge=edge, node=node, direction=direction))
        return out

    async def shortest_paths(self, workspace_id, source_id, target_id, *, max_depth: int = 3,
                             limit: int = 10) -> list[Path]:
        rows = await self._exec(
            f"FIND SHORTEST PATH WITH PROP FROM {_vid(source_id)} TO {_vid(target_id)} OVER rel BIDIRECT "
            f"UPTO {int(max_depth)} STEPS YIELD path AS p | LIMIT {int(limit)};")
        paths = []
        for i in range(rows.row_size()):
            path = rows.row_values(i)[0].as_path()
            nodes = [n.get_id().as_string() for n in path.nodes()]
            edges = []
            for rel in path.relationships():
                props = {k: _py(v) for k, v in rel.properties().items()}
                edge = _edge_from(rel.start_vertex_id().as_string(), rel.end_vertex_id().as_string(), props)
                if edge is None or str(edge.workspace_id) != str(workspace_id):
                    break
                edges.append(edge)
            else:
                paths.append(Path(node_ids=[uuid.UUID(n) for n in nodes], edges=edges))
        return paths

    async def clear_org(self, workspace_id) -> int:
        total = 0
        while True:
            rows = await self._rows(f"LOOKUP ON node WHERE node.workspace_id == {_lit(str(workspace_id))} "
                                    f"YIELD id(vertex) AS vid | LIMIT {_IN_CHUNK};")
            if not rows:
                return total
            ids = [row[0] for row in rows]
            await self._exec("DELETE VERTEX " + ", ".join(_vid(i) for i in ids) + " WITH EDGE;")
            total += len(ids)
