"""Default walker for records: JSON, JSON Lines, XML, YAML and other semi-structured sources.

Objects, lists and values become nodes with deterministic ids, `HAS_<KEY>` edges to their parent and
`NEXT` edges between siblings.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Union
from opencortyx.core.substrate import NodeRepo
from .base import BaseWalker


class SemiStructuredWalker(BaseWalker):
    """Records (JSON, JSON Lines, XML, YAML, ...): walked recursively into nodes and structural edges."""

    kind = "semistructured"

    def __init__(
        self,
        workspace_id: Optional[uuid.UUID] = None,
        repo: Optional[NodeRepo] = None,
        max_depth: int = 10,
        default_node_type: Optional[str] = None,
        persist: bool = True,
        max_array: int = 500,
    ):
        """`persist=False` walks without writing, reporting what would be created."""
        self.persist = persist
        self.workspace_id = workspace_id or uuid.NAMESPACE_OID
        self.repo = repo or NodeRepo()
        self.counts: Dict[str, int] = {"nodes": 0, "edges": 0}
        self.max_depth = max_depth
        # Elements walked per array. Unbounded, one 1M-element array became a million nodes; the
        # container keeps the true `count`, so the cut is visible rather than silent.
        self.max_array = max_array
        self.default_node_type = default_node_type
        self._root_id: Optional[uuid.UUID] = None
        self.emitted_nodes: List[Dict[str, Any]] = []
        self.emitted_edges: List[Dict[str, Any]] = []
        self.created_by_type: Dict[str, List[str]] = {}
        # Writes are buffered and flushed in batches (nodes first, then the edges between them): one
        # node at a time cost four store round trips each, and a 5,000-record file took over 10 minutes.
        self._nodes_out: List[Dict[str, Any]] = []
        self._edges_out: List[Dict[str, Any]] = []

    FLUSH_AT = 500

    async def _flush(self) -> None:
        if self._nodes_out:
            nodes, self._nodes_out = self._nodes_out, []
            await self.repo.upsert_many(self.workspace_id, nodes)
        if self._edges_out:
            edges, self._edges_out = self._edges_out, []
            await self.repo.add_edges(workspace_id=self.workspace_id, edges=edges)


    async def ingest_file(self, workspace_id: uuid.UUID, content: bytes, filename: str, *,
                          pipeline: Optional[list] = None, pipeline_id: Optional[str] = None,
                          collection_id: Optional[uuid.UUID] = None, reporter=None,
                          options: Optional[dict] = None, **_: Any) -> Dict[str, Any]:
        """Decode the file with the parser that owns its format, then read it through the pipeline's
        structure cards and steps, or walk it into a plain graph when no pipeline is given."""
        import hashlib
        from opencortyx.ingest.formats import semistructured as parsers
        opts = dict(options or {})
        parsed = parsers.parse(content, filename)
        data = parsed.data
        if not _has_records(data):
            # `[]`, `{}` or a bare value: valid syntax with nothing to read. Said as the run's reason
            # instead of walking it into an empty placeholder node that then counts as a finding.
            raise parsers.ParseError(f"'{filename}' holds no records — it is empty or a single bare value")
        checksum = hashlib.sha256(content).hexdigest()   # of the bytes, whatever the parser normalises
        if pipeline:
            from opencortyx.ingest.semistructured import run_semistructured
            return await run_semistructured(
                workspace_id, data, pipeline_stages=list(pipeline), source_label=filename,
                collection_id=collection_id, pipeline_id=pipeline_id, options=opts, reporter=reporter,
                checksum=checksum)
        from opencortyx.ingest.semistructured.ingest import ingest_json
        # A record-oriented file (a JSON array, JSONL, a YAML stream) is walked as its RECORDS: each
        # one under the Source. The parsed tree wraps them as {"records": [...]} — the shape the
        # Mapper's paths are drafted against, kept for the pipeline path above — and walked as-is it
        # became ONE record holding every ticket as a child ("records: 1" for a 2-ticket file).
        if parsed.records:
            data = [r.data for r in parsed.records]
        return await ingest_json(
            workspace_id, data, source_label=filename, collection_id=collection_id, checksum=checksum,
            root_type=opts.get("root_type"), max_depth=int(opts.get("max_depth", 6)),
            max_array=int(opts.get("max_array", 500)), max_records=int(opts.get("max_records", 100000)),
            embed_nodes=bool(opts.get("embed_nodes", False)), mapping=opts.get("mapping") or None,
            replace=bool(opts.get("replace")), whole_data=bool(opts.get("whole_data")))

    async def walk(
        self,
        data: Union[Dict[str, Any], List[Any]],
        source_label: str = "semistructured",
        collection_id: Optional[uuid.UUID] = None,
        root_id: Optional[uuid.UUID] = None,
        path_suffix: str = "",
    ) -> Dict[str, Any]:
        """Walk the payload and persist its nodes and edges.

        `root_id` hangs this payload off an asset that already exists, and `path_suffix` keeps the
        ids of its records distinct. A streaming read calls this once per record, and the FILE is the
        asset: without both, every record would become its own `Source` in the catalogue, or records
        of the same shape would collapse onto one another's ids."""
        self.emitted_nodes.clear()
        self.emitted_edges.clear()
        self.created_by_type.clear()
        self.counts = {"nodes": 0, "edges": 0}

        if root_id is None:
            root_id = uuid.uuid5(uuid.NAMESPACE_OID, f"{self.workspace_id}:source:{source_label}")
            if self.persist:
                await self.repo.upsert_node(
                    node_id=root_id,
                    workspace_id=self.workspace_id,
                    type="Source",
                    label=source_label,
                    properties={"filename": source_label, "__source": "semistructured"},
                    collection_id=collection_id
                )
            self.counts["nodes"] += 1
            self.created_by_type.setdefault("Source", []).append(str(root_id))
        # Provenance for everything below the root. The structural HAS_* edges alone cannot be
        # followed by `descendants()` or the run outcome, which both select on `source_ref`, so a
        # JSON run read as "produced nothing" and a rollback left its records behind.
        self._root_id = root_id

        if isinstance(data, dict):
            for k, v in data.items():
                await self._walk_element(v, k, parent_id=root_id, depth=1, collection_id=collection_id,
                                         path_suffix=path_suffix)
        elif isinstance(data, list):
            prev_id = None
            for idx, item in enumerate(data):
                suffix = f"{path_suffix}:{idx}" if path_suffix else str(idx)
                cid = await self._walk_element(item, "record", parent_id=root_id, depth=1,
                                                collection_id=collection_id, path_suffix=suffix)
                if prev_id and cid:
                    await self._create_edge(prev_id, cid, "NEXT")
                prev_id = cid

        await self._flush()
        return {
            "status": "ok",
            "source": source_label,
            "source_id": str(root_id),
            "created_by_type": self.created_by_type,
            **self.counts
        }

    async def _create_node(
        self,
        node_type: str,
        label: str,
        parent_id: Optional[uuid.UUID],
        attributes: Dict[str, Any],
        collection_id: Optional[uuid.UUID],
        edge_key: str = "",
        path_suffix: str = ""
    ) -> uuid.UUID:
        """Creates and stores a graph node with a deterministic UUID5."""
        import datetime
        # UTC, because NodeRepo stamps modified_date from UTC. Mixing clocks gives one node a
        # created_date and a modified_date from different days for however many hours this host is
        # offset from UTC — which then reads as "modified before it was created".
        today_str = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

        # Where the node sits, never an `id` value in the data: that merged records of different files.
        node_id = uuid.uuid5(uuid.NAMESPACE_OID, f"{self.workspace_id}:{parent_id}:{node_type}:{path_suffix}")

        clean_type = node_type[:1].upper() + node_type[1:]
        target_type = self.default_node_type or clean_type

        props = dict(attributes or {})
        props["__source"] = "json"
        generated = [k for k in ("created_date", "modified_date", "Label") if k not in props]
        props.setdefault("created_date", today_str)
        props["modified_date"] = today_str
        props.setdefault("__audit_version", 1)
        props["Label"] = clean_type
        props["__generated"] = generated       # written by the walker, not the source: never compared

        # Ensure prose text context is present for full-text search
        if "__text" not in props:
            prose_parts = [f"{k}: {v}" for k, v in props.items() if not str(k).startswith("__") and isinstance(v, (str, int, float, bool))]
            if prose_parts:
                props["__text"] = "\n".join(prose_parts)

        if self.persist:
            self._nodes_out.append({"node_id": node_id, "type": target_type, "label": label[:200],
                                    "properties": props, "source_ref": self._root_id,
                                    "collection_id": collection_id})
            if len(self._nodes_out) >= self.FLUSH_AT:
                await self._flush()

        self.counts["nodes"] += 1
        self.created_by_type.setdefault(target_type, []).append(str(node_id))
        self.emitted_nodes.append({"id": str(node_id), "type": target_type, "label": label, "attributes": props})

        # Draw structural edge from parent
        if parent_id is not None:
            rel = edge_key or node_type
            edge_type = f"HAS_{rel.upper()}"
            await self._create_edge(parent_id, node_id, edge_type)

        return node_id

    async def _create_edge(self, src: uuid.UUID, dst: uuid.UUID, edge_type: str, props: Optional[dict] = None) -> None:
        if self.persist and src and dst:
            self._edges_out.append({"source_id": src, "target_id": dst, "type": edge_type,
                                    "properties": props or {}})
        self.counts["edges"] += 1
        self.emitted_edges.append({"source": str(src), "target": str(dst), "type": edge_type})

    async def _walk_element(
        self,
        value: Any,
        key: str,
        parent_id: uuid.UUID,
        depth: int,
        collection_id: Optional[uuid.UUID],
        path_suffix: str = ""
    ) -> Optional[uuid.UUID]:
        if depth > self.max_depth:
            return None

        # ── 1. Dictionary / Object ───────────────────────────────────────────
        if isinstance(value, dict):
            attributes = {}
            children = []

            for k, v in value.items():
                if isinstance(v, (dict, list)):
                    children.append((k, v))
                else:
                    attributes[k] = v

            node_type = str(key)
            label = str(attributes.get("name") or attributes.get("id") or key)
            node_id = await self._create_node(
                node_type=node_type, label=label, parent_id=parent_id,
                attributes=attributes, collection_id=collection_id,
                edge_key=key, path_suffix=path_suffix or key
            )

            for child_key, child_val in children:
                await self._walk_element(child_val, child_key, parent_id=node_id, depth=depth + 1,
                                         collection_id=collection_id)
            return node_id

        # ── 2. Array / List ──────────────────────────────────────────────────
        elif isinstance(value, list):
            # Check if all items are scalars
            is_scalar_list = all(not isinstance(x, (dict, list)) for x in value)
            walked = value[: self.max_array]
            if is_scalar_list:
                # Store directly as a list attribute on the parent
                # Also create individual items if array has elements
                container_id = await self._create_node(
                    node_type=key, label=key, parent_id=parent_id,
                    attributes={"items": walked, "count": len(value)}, collection_id=collection_id,
                    edge_key=key, path_suffix=path_suffix or key
                )
                prev_id = None
                for idx, elem in enumerate(walked):
                    elem_id = await self._create_node(
                        node_type=f"{key}_item", label=str(elem), parent_id=container_id,
                        attributes={"value": elem, "index": idx}, collection_id=collection_id,
                        edge_key=key, path_suffix=str(idx)
                    )
                    if prev_id:
                        await self._create_edge(prev_id, elem_id, "NEXT")
                    prev_id = elem_id
                return container_id

            # Array of objects
            container_id = await self._create_node(
                node_type=key, label=key, parent_id=parent_id,
                attributes={"count": len(value)}, collection_id=collection_id,
                edge_key=key, path_suffix=path_suffix or key
            )
            prev_id = None
            for idx, elem in enumerate(walked):
                elem_id = await self._walk_element(
                    elem, key, parent_id=container_id, depth=depth + 1,
                    collection_id=collection_id, path_suffix=str(idx)
                )
                if prev_id and elem_id:
                    await self._create_edge(prev_id, elem_id, "NEXT")
                prev_id = elem_id
            return container_id

        # ── 3. Primitive Value ───────────────────────────────────────────────
        else:
            return await self._create_node(
                node_type=key, label=f"{key}: {value}", parent_id=parent_id,
                attributes={"value": value}, collection_id=collection_id,
                edge_key=key, path_suffix=path_suffix or str(key)
            )

    @classmethod
    def introspect(cls, data_or_bytes: Any, filename: str = "") -> dict:
        """Introspect semi-structured data to derive its schema structure without persisting."""
        from opencortyx.ingest.formats import semistructured as parsers
        from opencortyx.ingest.semistructured.schema import build_structure

        if isinstance(data_or_bytes, bytes):
            data = parsers.parse(data_or_bytes, filename).data
        else:
            data = data_or_bytes

        record = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        return build_structure(record)


def _has_records(data) -> bool:
    """Whether a decoded payload holds any value at all. Structural on purpose: the parsers wrap
    what they read (`[]` decodes to `{"records": [[]]}`), so only a value somewhere in the tree —
    not the shape of the envelope — says there is something to walk. Iterative, so a deeply
    nested file is answered rather than hitting the recursion limit."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
        elif node is not None and node != "":
            return True
    return False
