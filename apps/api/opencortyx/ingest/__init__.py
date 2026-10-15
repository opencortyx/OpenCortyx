"""Ingestion: read a source into graph nodes and edges.

One file flows through three layers:

    formats/        bytes -> the model its paradigm reads. catalog.py lists all 39 extensions;
                    one reader per format family (xml_to_json, yaml_to_json, tabular, documents, ...)
    detection/      which paradigm a file is: extension, MIME type, content -> a route
    walkers/        the DEFAULT walker per paradigm, used when nothing is declared

and, when a pipeline declares structure, the full path for its paradigm:

    structured/     rows and columns -> Table / Attribute / row nodes
    semistructured/ the JSON tree -> nodes through declared structure cards
    unstructured/   text -> chunks -> Source / Chunk nodes, then extraction

Around them:

    extraction/     extractors that run over nodes already written
    pipelines/      pipeline definitions, schema-signature routing, conformance checks
    runs/           one run's progress, outcome, and purge
    sources/        where files come from: folder sync, directory tree, data lakes

`runner.ingest_file()` is the entry point: detect the paradigm, then hand the file to its walker.
"""
