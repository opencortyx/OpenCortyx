"""The format catalog — ONE source of truth for every file OpenCortyx can ingest.

Each FAMILY is declared once, with its paradigm and the reader module that turns its bytes into the
model its pipeline consumes:

    structured      → rows and columns   (formats/structured/)      → ingest/structured/
    semistructured  → the JSON tree      (formats/semistructured/)  → ingest/semistructured/
    unstructured    → text               (formats/unstructured/)    → ingest/unstructured/

Everything else is derived from this table: `FORMATS` (extension → route, which routes uploads and
feeds the UI) and the detection registry (MIME types and magic bytes). Adding a format is one entry
here and, if it is new, one reader module.

Why routing by format matters: `/sources/upload` once sent every file to the document pipeline. A
29 MB JSONL became ~29,000 chunks and ~435,000 LLM calls; records belong on the walker (zero LLM
calls for structure) and tables on DuckDB. Only prose is chunked.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DOCUMENT = "document"                 # parse → chunk → per-chunk LLM extraction
SEMISTRUCTURED = "semistructured"     # walk an object tree → nodes/edges (deterministic)
STRUCTURED = "structured"             # DuckDB → tables → rows/columns
ROUTES = (DOCUMENT, SEMISTRUCTURED, STRUCTURED)


@dataclass(frozen=True)
class Family:
    """Formats read the same way: one reader, one paradigm."""
    name: str                                   # detection's canonical name, e.g. "JSON"
    route: str
    reader: str                                 # module under ingest/formats/ that reads it
    extensions: tuple
    label: str = ""
    mime_types: tuple = ()
    magic_bytes: tuple = ()
    decoder: str = ""                           # semi-structured parser name
    ext_labels: dict = field(default_factory=dict)   # per-extension UI label, when it differs


@dataclass(frozen=True)
class Format:
    """One extension, as routing and the UI see it."""
    ext: str
    route: str
    decoder: str = ""
    label: str = ""
    reader: str = ""

    @property
    def name(self) -> str:
        return self.label or self.ext.upper()


_ST, _SS, _UN = "structured/", "semistructured/", "unstructured/"
FAMILIES: tuple = (
    # ── structured: tabular, read by DuckDB ─────────────────────────────────────────────────────
    Family("CSV", STRUCTURED, _ST + "tabular", ("csv",), "CSV", ("text/csv", "application/csv")),
    Family("TSV", STRUCTURED, _ST + "tabular", ("tsv",), "TSV", ("text/tab-separated-values",)),
    Family("PARQUET", STRUCTURED, _ST + "tabular", ("parquet",), "Apache Parquet",
           ("application/vnd.apache.parquet", "application/x-parquet"), (b"PAR1",)),
    Family("XLSX", STRUCTURED, _ST + "tabular", ("xlsx", "xlsm", "xls"), "Excel Spreadsheet",
           ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel"),
           ext_labels={"xlsx": "Excel", "xlsm": "Excel", "xls": "Excel"}),
    Family("ORC", STRUCTURED, _ST + "tabular", ("orc",), "Apache ORC", ("application/orc",), (b"ORC\x00",)),
    Family("FEATHER", STRUCTURED, _ST + "tabular", ("feather", "arrow"), "Apache Arrow / Feather",
           ("application/x-feather", "application/vnd.apache.arrow.file"), (b"ARROW1\x00\x00",)),
    Family("AVRO", STRUCTURED, _ST + "tabular", ("avro",), "Apache Avro", ("application/avro",), (b"Obj\x01",)),

    # ── semi-structured: read, or translated, into the JSON tree the walker reads ───────────────
    Family("JSON", SEMISTRUCTURED, _SS + "json_reader", ("json", "geojson", "har", "fhir"), "JSON",
           ("application/json", "application/geo+json", "application/fhir+json"), decoder="json",
           ext_labels={"geojson": "GeoJSON", "har": "HTTP archive", "fhir": "FHIR bundle"}),
    Family("JSONL", SEMISTRUCTURED, _SS + "jsonl_reader", ("jsonl", "ndjson"), "JSON Lines",
           ("application/x-ndjson", "application/jsonlines"), decoder="jsonl",
           ext_labels={"jsonl": "JSON Lines", "ndjson": "NDJSON"}),
    Family("XML", SEMISTRUCTURED, _SS + "xml_to_json", ("xml", "rss", "atom"), "XML",
           ("application/xml", "text/xml", "application/rss+xml", "application/atom+xml"), decoder="xml",
           ext_labels={"rss": "RSS feed", "atom": "Atom feed"}),
    Family("YAML", SEMISTRUCTURED, _SS + "yaml_to_json", ("yaml", "yml"), "YAML",
           ("application/x-yaml", "text/yaml"), decoder="yaml"),
    Family("TOML", SEMISTRUCTURED, _SS + "toml_to_json", ("toml",), "TOML",
           ("application/toml", "text/toml"), decoder="toml"),

    # ── unstructured: prose, read to text, then chunked ─────────────────────────────────────────
    Family("PDF", DOCUMENT, _UN + "documents", ("pdf",), "PDF Document", ("application/pdf",), (b"%PDF",)),
    Family("DOCX", DOCUMENT, _UN + "documents", ("docx", "doc"), "Microsoft Word",
           ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword")),
    Family("PPTX", DOCUMENT, _UN + "documents", ("pptx", "odt"), "Presentation / OpenDocument",
           ("application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.oasis.opendocument.text")),
    Family("TEXT", DOCUMENT, _UN + "documents", ("txt", "log", "hl7", "csl"), "Plain Text", ("text/plain",)),
    Family("MARKDOWN", DOCUMENT, _UN + "documents", ("md", "markdown", "rst"), "Markdown",
           ("text/markdown", "text/x-rst")),
    Family("HTML", DOCUMENT, _UN + "documents", ("html", "htm"), "HTML Document", ("text/html",)),
    Family("EMAIL", DOCUMENT, _UN + "documents", ("eml", "msg"), "Email Archive",
           ("message/rfc822", "application/vnd.ms-outlook")),
    Family("LATEX", DOCUMENT, _UN + "documents", ("tex",), "LaTeX Document", ("application/x-tex", "text/x-tex")),
)

FORMATS: dict[str, Format] = {
    ext: Format(ext=ext, route=f.route, decoder=f.decoder, label=f.ext_labels.get(ext, ""), reader=f.reader)
    for f in FAMILIES for ext in f.extensions}

#: Formats whose bytes the semi-structured path knows how to decode.
DECODERS = {f.decoder for f in FORMATS.values() if f.decoder}


def extension_of(filename: str) -> str:
    return os.path.splitext((filename or "").lower())[1].lstrip(".")


def asset_kind(node) -> str:
    """What kind of asset a catalogued node is — "structured" | "document" | "semistructured".

    One rule for every page that groups or describes assets (the catalogue, an asset's detail, data
    quality), read from what the asset IS — a table, or a file of a known format — never from the
    type its root node happens to carry, which a mapping can rename (`Document`, `Contract`, …)."""
    from opencortyx.domain.ontology import base_type_of
    props = node.properties or {}
    if base_type_of(node) == "Table":
        return "structured"
    name = props.get("source_file") or props.get("filename") or ""
    fmt = FORMATS.get(name.rsplit(".", 1)[-1].lower()) if "." in name else None
    if fmt:
        return fmt.route
    return "semistructured" if props.get("__source") == "json" else "document"
