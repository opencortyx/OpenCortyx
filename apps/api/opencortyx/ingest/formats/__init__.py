"""Every file format OpenCortyx reads — bytes in, the model a pipeline consumes out.

    catalog.py        THE list: each format family, its paradigm and its reader (39 extensions)
    base.py           the reader contract: Parser, ParseResult, Record, ParseError

    structured/       → rows and columns       tabular.py (DuckDB: csv tsv parquet excel orc arrow avro)
    semistructured/   → the JSON tree          json_reader  jsonl_reader  (read natively)
                                               xml_to_json  yaml_to_json  toml_to_json  ini_to_json
                                               (translated into the same tree)
    unstructured/     → text                   documents.py (pdf, office, text) · docling/ (layout-aware)

Pattern: one Adapter per format, all converging on one model per paradigm, so each pipeline in
`ingest/<paradigm>/` reads a single shape however the bytes arrived. A new format is a catalog entry
and, if it needs one, a new adapter here — nothing downstream changes.
"""
