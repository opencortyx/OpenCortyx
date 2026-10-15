"""Mirror a source folder's DIRECTORY TREE into the graph, when the ingestion asks for it.

Off by default. A flat list of assets is the right shape for most sources — a bucket of invoices has
no meaningful hierarchy, and inventing one adds nodes nobody queries. But when the folder layout IS
information (`research/alice/drafts/`, `clients/acme/2026/contracts/`), losing it means losing the
only statement of which assets belong together, and no amount of extraction recovers it afterwards.

Shape:

    Directory "research"  ─HAS_SUBDIRECTORY→ Directory "research/alice"
                                                      │
                                                      ├─HAS_SUBDIRECTORY→ Directory ".../drafts"
                                                      └─HAS_ASSET→ Source "notes.md"

Ids are deterministic on the org + full relative path, so re-ingesting the same tree updates it
instead of growing a second copy — the same rule the walkers use for records. Directories are
addressed by their FULL path, never their bare name: two different `2026/` folders under different
clients are different directories, and keying on the leaf name would silently merge them.
"""
import uuid

from opencortyx.core.substrate import NodeRepo

_repo = NodeRepo()
_DNS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/directory")

#: The asset's containing folder → the asset. Kept distinct from HAS_SUBDIRECTORY so "what is IN this
#: folder" and "what is UNDER this folder" are separate questions the graph can answer.
EDGE_ASSET = "HAS_ASSET"
EDGE_SUBDIR = "HAS_SUBDIRECTORY"
NODE_TYPE = "Directory"


def _norm(path: str) -> str:
    """Posix-style, no leading/trailing slash, no drive letters — a stable key across connectors."""
    p = str(path or "").replace("\\", "/").strip()
    while "//" in p:
        p = p.replace("//", "/")
    return p.strip("/")


def segments_of(path: str) -> list:
    """The DIRECTORY prefixes of a file path, outermost first.

    `research/alice/drafts/paper.pdf` → ['research', 'research/alice', 'research/alice/drafts'].
    The final component is the file, never a directory. A bare filename yields [] — a file at the
    root has no containing folder to record, and inventing one would put every flat upload under a
    fake directory.
    """
    p = _norm(path)
    if not p or "/" not in p:
        return []
    parts = [x for x in p.split("/")[:-1] if x]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def dir_id(workspace_id, full_path: str) -> uuid.UUID:
    """Deterministic id for a directory — the FULL path, so `acme/2026` and `globex/2026` differ."""
    return uuid.uuid5(_DNS, f"{workspace_id}:{_norm(full_path)}")


async def link_asset_path(workspace_id, *, source_id, path: str) -> dict:
    """Create/refresh the directory chain for `path` and hang `source_id` off its deepest folder.

    Idempotent. Returns counts so the ingestion can report what the toggle actually did rather than
    claiming success silently.
    """
    segs = segments_of(path)
    if not segs:
        return {"directories": 0, "linked": False}

    made = 0
    prev_id = None
    for full in segs:
        did = dir_id(workspace_id, full)
        name = full.rsplit("/", 1)[-1]
        # `text` mirrors what every other node stores: a self-describing string, so a directory is
        # retrievable by name AND by its place in the tree ("drafts under research/alice").
        await _repo.upsert_node(workspace_id=workspace_id, type=NODE_TYPE, label=name, node_id=did,
            properties={"name": name, "path": full, "depth": full.count("/") + 1,
                        "text": f"Directory {name}. path={full}"})
        made += 1
        if prev_id is not None:
            await _repo.add_edge(workspace_id=workspace_id, source_id=prev_id, target_id=did,
                                 type=EDGE_SUBDIR, dedupe=True)
        prev_id = did

    if prev_id is not None and source_id:
        await _repo.add_edge(workspace_id=workspace_id, source_id=prev_id, target_id=source_id,
                             type=EDGE_ASSET, dedupe=True)
    return {"directories": made, "linked": bool(prev_id and source_id)}
