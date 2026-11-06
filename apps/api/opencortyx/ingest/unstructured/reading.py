"""How a document was read, for the Mapper: its layout and what extractors found in it.

A document has no fields to map. It is read by its layout - title, section headings, paragraphs,
list items, tables - into rows of the workspace's `unstructured` table (one row per element, see
`dataplane.py`), and prose longer than the Chunk size (`chunk_window_chars`) is split at sentence
ends into consecutive elements. What the Mapper shows of it is that reading, and the kinds of thing
extractors found in it: the nodes its asset MENTIONS.
"""
from __future__ import annotations

from collections import Counter

#: Element types that head a section; everything else under one is that section's content.
HEADINGS = ("Title", "SectionHeader")
#: Prose: what the Chunk size splits (layout/assembler.SPLIT_LABELS, as element types).
PROSE = ("Text", "Paragraph", "ListItem", "Caption", "Footnote", "HandwrittenText", "Reference")
#: The most sections and examples a summary lists.
SECTIONS_SHOWN = 60
SAMPLES = 3


def summarize(rows: list[dict], split_at: int) -> dict:
    """The reading of one document from its rows (`ordinal`, `element_type`, `level`,
    `section_path`, `text`): how many of each element, its sections with how much each holds, and
    its prose - how many passages after splitting, their average and the longest."""
    rows = sorted(rows, key=lambda r: int(r.get("ordinal") or 0))
    kinds = Counter(str(r.get("element_type") or "Text") for r in rows)
    under = Counter(str(r.get("section_path") or "") for r in rows
                    if str(r.get("element_type")) not in HEADINGS)
    sections = [{"title": str(r.get("text") or "").strip(), "level": int(r.get("level") or 0),
                 "items": under.get(str(r.get("section_path") or ""), 0)}
                for r in rows if str(r.get("element_type")) in HEADINGS][:SECTIONS_SHOWN]
    prose = [str(r.get("text") or "") for r in rows if str(r.get("element_type")) in PROSE]
    lengths = [len(t) for t in prose]
    return {
        "elements": [{"type": k, "count": n} for k, n in kinds.most_common()],
        "sections": sections,
        "outside_sections": under.get("", 0),
        "passages": len(prose),
        "longest": max(lengths, default=0),
        "average": round(sum(lengths) / len(lengths)) if lengths else 0,
        "split_at": split_at,
        "samples": [t[:400] for t in prose[:SAMPLES]],
    }


async def reading(workspace_id, asset) -> dict:
    """`summarize` for one document asset, read from its rows in the data store."""
    import anyio

    from opencortyx.core.config import runtime
    from opencortyx.core.dataplane import assets
    from opencortyx.stores.data import Views
    refs = await assets.refs(workspace_id, ids=[asset.id])

    def read() -> list[dict]:
        with Views(workspace_id, refs, lock=True) as v:
            view = next(iter(v.names), None)
            if view is None:
                return []
            name = '"' + view.replace('"', '""') + '"'
            return v.query(f"SELECT ordinal, element_type, level, section_path, text FROM {name}").to_pylist()

    return summarize(await anyio.to_thread.run_sync(read), runtime.chunk_window_chars())


async def found(workspace_id, asset_id) -> list[dict]:
    """The kinds of thing extractors found in this file, with how many of each: the nodes the asset
    MENTIONS (an extractor links what it finds from the file it read)."""
    from opencortyx.core.substrate import NodeRepo
    repo = NodeRepo()
    edges = await repo.edges(workspace_id, source_ids=[str(asset_id)], types=["MENTIONS"])
    ids = list({str(e.target_id) for e in edges})
    kinds: Counter = Counter()
    for i in range(0, len(ids), 500):
        kinds.update(n.type for n in await repo.get_many(workspace_id, ids[i:i + 500]))
    return [{"type": t, "count": n} for t, n in kinds.most_common()]


def upload_ref(asset) -> str | None:
    """The stored upload a document was read from, when the server still keeps it (uploads are kept
    for a while after their last use: core/uploads.py). The checksum of a read document is the
    sha256 the upload store names its files by."""
    from opencortyx.core import uploads
    checksum = str((asset.metadata_ or {}).get("checksum") or "")
    if len(checksum) != 64:
        return None
    return uploads.PREFIX + checksum if uploads._path(checksum).exists() else None
