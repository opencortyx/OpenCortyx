"""The data plane's control side: the asset registry, the evidence ledger and the source-key map.

    assets.py       data_asset - which assets exist and where each one's data is
    evidence.py     evidence   - what backs each fact: the chunk, row or record, at a version
    source_keys.py  source_key - a source's own id ("customer 101") -> the graph node
    connectors.py   a saved SQL connector as DuckDB attaches it

The data itself is in stores/data (Delta tables, external sources); these are Postgres rows that
index it. See docs/knowledge-layer-implementation.md, section 2.2.
"""
