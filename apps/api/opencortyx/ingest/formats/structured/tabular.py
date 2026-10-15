"""DuckDB-backed tabular file reader for the structured pipeline — CSV / TSV / Parquet / Excel.

One engine for every file format. DuckDB streams larger-than-memory (out-of-core, spills to disk),
sniffs CSV dialect (delimiter / quote / header) automatically, and reads Parquet/Excel natively —
so a trillion-row Parquet reads without loading into RAM. Every file becomes "columns + rows",
which lets the SAME structured pipeline run on files exactly as it does on databases:
  • catalog mode   → one Table node + Attribute (column) nodes, like SQL introspection.
  • one-node-per-row → each row becomes an entity, columns → attributes, FK columns → edges.

Nothing here writes the graph; it only turns bytes → columns/rows. `introspect_file` returns the
SAME dict shape as ingest_structured._sync_introspect, so catalog-mode node creation is reused.
"""
import os
from typing import Any, Dict, Iterator, List, Optional

CSV_EXTS = {".csv", ".tsv", ".txt", ".psv", ".tab"}
PARQUET_EXTS = {".parquet", ".pq"}
EXCEL_EXTS = {".xlsx", ".xlsm"}
ARROW_EXTS = {".orc", ".feather", ".arrow", ".ipc", ".avro"}
#: Excel 97–2003. DuckDB reads only the XML formats; claiming .xls as tabular (so it routes here) and
#: refusing it with a reason beats the old fall-through, which read the binary file as CSV.
LEGACY_EXCEL_EXTS = {".xls"}
TABULAR_EXTS = CSV_EXTS | PARQUET_EXTS | EXCEL_EXTS | ARROW_EXTS | LEGACY_EXCEL_EXTS


def is_tabular(filename: str) -> bool:
    return os.path.splitext((filename or "").lower())[1] in TABULAR_EXTS


def file_format(filename: str) -> str:
    ext = os.path.splitext((filename or "").lower())[1]
    if ext in PARQUET_EXTS:
        return "parquet"
    if ext in EXCEL_EXTS:
        return "excel"
    if ext in LEGACY_EXCEL_EXTS:
        return "xls"
    if ext in ARROW_EXTS:
        return ext.lstrip(".")          # orc | feather | arrow | ipc | avro
    return "csv"


def table_name_of(filename: str) -> str:
    return os.path.splitext(os.path.basename(filename or "file"))[0] or "file"


def _q(s: str) -> str:
    """Single-quote-escape a string for embedding in a DuckDB SQL literal."""
    return str(s).replace("'", "''")


def _con():
    import duckdb
    con = duckdb.connect()
    return con


def _register_excel(con, path: str, sheet: Optional[str]) -> str:
    """Read one worksheet with openpyxl and expose it to DuckDB as a view.

    Not DuckDB's `read_xlsx`: that lives in the `excel` extension, which DuckDB downloads on first
    use — so on a host without outbound internet (air-gapped, or a container with no egress) every
    workbook failed with "Failed to download extension". openpyxl ships in the image and reads the
    same files offline. The first row is the header, as `read_xlsx` assumed.
    """
    view = f"_xlsx_{abs(hash((path, sheet))) % (10 ** 9)}"
    try:
        import pyarrow as pa
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[sheet] if sheet else wb.worksheets[0]
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
    except KeyError as e:
        raise RuntimeError(f"the workbook has no sheet named {sheet!r}") from e
    except Exception as e:                      # noqa: BLE001 — a corrupt file is a normal outcome
        raise RuntimeError(f"could not read Excel file: {e}") from e
    while rows and all(v is None for v in rows[-1]):
        rows.pop()                              # trailing formatted-but-empty rows are not data
    header = [str(h).strip() if h is not None and str(h).strip() else f"column_{i + 1}"
              for i, h in enumerate(rows[0] if rows else [])]
    seen: Dict[str, int] = {}
    for i, h in enumerate(header):              # duplicate header cells → h, h_2, h_3 …
        seen[h] = seen.get(h, 0) + 1
        if seen[h] > 1:
            header[i] = f"{h}_{seen[h]}"
    body = [r[:len(header)] + [None] * (len(header) - len(r)) for r in rows[1:]]
    cols = {h: [r[i] for r in body] for i, h in enumerate(header)}
    try:
        table = pa.table(cols)
    except (pa.ArrowInvalid, pa.ArrowTypeError):
        # a column mixing numbers and text: keep every value, as text
        table = pa.table({h: [None if v is None else str(v) for v in vals] for h, vals in cols.items()})
    con.register(view, table)
    return view


def _sniff_csv(con, path: str) -> Dict[str, Any]:
    """DuckDB's CSV sniffer → dialect. Best-effort; falls back to comma/header."""
    try:
        row = con.execute(
            f"SELECT Delimiter, Quote, Escape, HasHeader, SkipRows "
            f"FROM sniff_csv('{_q(path)}')").fetchone()
        return {"delimiter": row[0], "quote": row[1], "escape": row[2],
                "has_header": bool(row[3]), "skip_rows": int(row[4] or 0)}
    except Exception:
        return {"delimiter": ",", "quote": '"', "escape": '"', "has_header": True, "skip_rows": 0}


def _norm_delim(d: Optional[str]) -> Optional[str]:
    if d is None:
        return None
    m = {"\\t": "\t", "tab": "\t", "\\0": "", "comma": ",", "pipe": "|", "semicolon": ";"}
    return m.get(d, d)


# friendly type (what the UI shows / user picks) → the DuckDB type to CAST to
_FRIENDLY_TO_DUCK = {
    "string": "VARCHAR", "text": "VARCHAR", "int": "BIGINT", "integer": "BIGINT",
    "float": "DOUBLE", "double": "DOUBLE", "decimal": "DECIMAL(18,4)", "numeric": "DECIMAL(18,4)",
    "bool": "BOOLEAN", "boolean": "BOOLEAN", "date": "DATE", "datetime": "TIMESTAMP",
    "timestamp": "TIMESTAMP", "time": "TIME", "bytes": "BLOB", "uuid": "UUID", "json": "JSON",
}


def _apply_column_types(relation: str, column_types: Optional[dict]) -> str:
    """Wrap a relation so chosen columns are re-typed to what the user picked (when auto-detect got it
    wrong). TRY_CAST → a value that won't convert becomes NULL instead of aborting the whole read."""
    if not column_types:
        return relation
    reps = []
    for col, t in column_types.items():
        if not col or not t:
            continue
        duck = _FRIENDLY_TO_DUCK.get(str(t).strip().lower())
        if not duck:
            continue
        ident = str(col).replace('"', '""')
        reps.append(f'TRY_CAST("{ident}" AS {duck}) AS "{ident}"')
    if not reps:
        return relation
    return f"(SELECT * REPLACE ({', '.join(reps)}) FROM {relation})"


def _register_arrow(con, path: str, fmt: str) -> str:
    """Read a columnar file with pyarrow and expose it to DuckDB as a view.

    Raises a readable error naming the format when the optional reader is missing, instead of
    letting the file fall through to the CSV sniffer and fail with a dialect-detection message that
    points nowhere.
    """
    view = f"_arrow_{abs(hash(path)) % (10 ** 9)}"
    try:
        if fmt == "orc":
            from pyarrow import orc as _orc
            table = _orc.read_table(path)
        elif fmt == "avro":
            table = _read_avro(path)
        else:                                   # feather / arrow / ipc
            import pyarrow.feather as _feather
            table = _feather.read_table(path)
    except ImportError as e:
        raise RuntimeError(
            f"reading .{fmt} needs an optional dependency that is not installed: {e}") from e
    except Exception as e:                      # noqa: BLE001 — a corrupt file is a normal outcome
        raise RuntimeError(f"could not read {fmt.upper()} file: {e}") from e
    con.register(view, table)
    return view


def _read_avro(path: str):
    """Avro → Arrow. There is no pyarrow Avro reader, so fastavro streams it into Arrow."""
    import fastavro
    import pyarrow as pa
    with open(path, "rb") as fh:
        reader = fastavro.reader(fh)
        rows = list(reader)
        fields = [f["name"] for f in (reader.writer_schema or {}).get("fields", [])]
    if not rows:
        # A valid file with no rows still declares its columns; an empty table without them is
        # unreadable ("must have at least one column").
        return pa.table({f: pa.array([], pa.string()) for f in fields})
    return pa.Table.from_pylist(rows)


def _relation(con, path: str, fmt: str, overrides: Optional[dict]) -> str:
    """Build the DuckDB read expression for `path`, honouring user overrides when present.
    Returns a SQL relation usable as `SELECT * FROM <relation>`."""
    overrides = overrides or {}
    if os.path.getsize(path) == 0:
        raise ValueError("the file is empty — there is no table to read")
    if fmt == "xls":
        raise ValueError("Excel 97–2003 files (.xls) cannot be read — save it as .xlsx and upload it again")
    p = _q(path)
    if fmt == "parquet":
        base = f"read_parquet('{p}')"
    elif fmt in ("orc", "feather", "arrow", "ipc", "avro"):
        base = _register_arrow(con, path, fmt)
    elif fmt == "excel":
        base = _register_excel(con, path, overrides.get("sheet"))
    else:
        # CSV / TSV / delimited
        args = [f"'{p}'"]
        delim = _norm_delim(overrides.get("delimiter"))
        if delim is not None:
            args.append(f"delim='{_q(delim)}'")
        if "has_header" in overrides:
            args.append(f"header={'true' if overrides['has_header'] else 'false'}")
        quote = overrides.get("quote")
        escape = overrides.get("escape")
        if delim is not None:
            if quote is None:
                quote = '"'
            if escape is None:
                escape = quote or '"'
        if quote is not None:
            args.append(f"quote='{_q(quote)}'")
        if escape is not None:
            args.append(f"escape='{_q(escape)}'")
        if overrides.get("encoding"):
            args.append(f"encoding='{_q(overrides['encoding'])}'")
        if overrides.get("skip_rows"):
            args.append(f"skip={int(overrides['skip_rows'])}")
        # Ragged rows (a short last field, a trailing delimiter) are normal in database exports and
        # must not fail the whole read — pad them instead of aborting.
        if overrides.get("null_padding", True):
            args.append("null_padding=true")
        if delim is not None and overrides.get("strict") is not True:
            args.append("strict_mode=false")
        if len(args) == 1:                          # no overrides → full auto-detect (sniffs dialect)
            args.append("auto_detect=true")
        args.append("all_varchar=true" if overrides.get("all_varchar") else "all_varchar=false")
        base = f"read_csv({', '.join(args)})"
    # user-picked column type overrides (apply to every format)
    return _apply_column_types(base, overrides.get("column_types"))


CSV_ENCODING_FALLBACKS = ("utf-16", "utf-16le", "utf-16be", "utf-8-sig", "cp1252", "latin-1")

_CSV_FALLBACKS = (
    {},
    {"quote": '"', "escape": '"'},
    {"quote": "", "escape": ""},
    {"quote": "", "escape": "", "all_varchar": True},
)


def resolve_csv_settings(con, path: str, fmt: str, overrides: Optional[dict]):
    """Find settings that actually READ this file, and report them.

    A pinned delimiter is not enough on its own: DuckDB still sniffs quote/escape and can fail hard.
    Rather than handing that raw error to the user, walk the fallbacks and return the first that
    works, together with the effective overrides — so the preview can SHOW what it had to use and the
    ingest can reuse exactly the same settings.

    Returns (relation, effective_overrides). Raises the ORIGINAL error if nothing works, since that
    message is the most informative one about the file itself.
    """
    overrides = dict(overrides or {})
    if fmt != "csv":
        return _relation(con, path, fmt, overrides), overrides

    encodings = [None] if overrides.get("encoding") else [None, *CSV_ENCODING_FALLBACKS]
    first_error = None
    for enc in encodings:
        for extra in _CSV_FALLBACKS:
            attempt = {**extra, "encoding": enc} if enc else dict(extra)
            eff = {**overrides, **{k: v for k, v in attempt.items() if k not in overrides}}
            try:
                relation = _relation(con, path, fmt, eff)
                con.execute(f"SELECT * FROM {relation} LIMIT 1").fetchall()  # force the sniff NOW
                return relation, eff
            except Exception as e:                    # noqa: BLE001 — try the next combination
                if first_error is None:
                    first_error = e                   # keep the FIRST error: it describes the file
    raise first_error


def _duck_type_bucket(t: str) -> str:
    """Coarse DuckDB type → a friendly bucket used for role guessing / display."""
    t = (t or "").upper()
    if any(k in t for k in ("INT", "DECIMAL", "DOUBLE", "FLOAT", "REAL", "NUMERIC", "HUGEINT")):
        return "numeric"
    if "BOOL" in t:
        return "boolean"
    if any(k in t for k in ("DATE", "TIME", "TIMESTAMP")):
        return "date"
    return "text"


_INT_TYPES = {"TINYINT", "SMALLINT", "INTEGER", "INT", "BIGINT", "HUGEINT", "INT1", "INT2",
              "INT4", "INT8", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}


def canonical_type(duck_type: str) -> str:
    """DuckDB's storage type → the REAL data type users expect: int / float / decimal / bool / date /
    datetime / time / string / bytes / uuid / json. Keeps precision-carrying types honest (a money
    column reads 'decimal', not 'DOUBLE'). Unknown types fall through as their lowercased base name."""
    t = (duck_type or "").upper().strip()
    base = t.split("(")[0].strip()                 # DECIMAL(10,2) → DECIMAL
    if base in _INT_TYPES:
        return "int"
    if base in ("FLOAT", "REAL", "DOUBLE", "FLOAT4", "FLOAT8"):
        return "float"
    if base in ("DECIMAL", "NUMERIC"):
        return "decimal"
    if base in ("BOOLEAN", "BOOL", "LOGICAL"):
        return "bool"
    if base == "DATE":
        return "date"
    if base.startswith("TIMESTAMP") or base == "DATETIME":
        return "datetime"
    if base.startswith("TIME"):
        return "time"
    if base in ("VARCHAR", "CHAR", "TEXT", "STRING", "BPCHAR", "NVARCHAR"):
        return "string"
    if base in ("BLOB", "BYTEA", "BINARY", "VARBINARY"):
        return "bytes"
    if base == "UUID":
        return "uuid"
    if base == "JSON":
        return "json"
    return base.lower() or "string"


def guess_role(name: str, duck_type: str) -> str:
    """Type/name-based fallback role for columns the Step-1 card didn't pin (mirrors the UI)."""
    n = (name or "").lower()
    bucket = _duck_type_bucket(duck_type)
    if n == "id":
        return "identity"
    if n.endswith("_id"):
        return "fk"
    if bucket == "numeric":
        return "measure"
    if bucket == "text" and any(k in n for k in ("text", "comment", "note", "review", "description", "body")):
        return "text"
    return "dimension"


def _columns(con, relation: str) -> List[Dict[str, str]]:
    rows = con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
    # DESCRIBE → (column_name, column_type, null, key, default, extra). `type` is the REAL data type
    # (int/float/date/string/…); `raw_type` keeps DuckDB's storage type for fidelity.
    return [{"name": r[0], "type": canonical_type(r[1]), "raw_type": r[1],
             "bucket": _duck_type_bucket(r[1])} for r in rows]


def preview_file(path: str, filename: str, overrides: Optional[dict] = None,
                 sample_n: int = 20) -> Dict[str, Any]:
    """Detect the file's shape and return a preview WITHOUT ingesting: detected parse settings,
    columns (name + type + guessed role), the first `sample_n` rows, and a row count."""
    fmt = file_format(filename)
    con = _con()
    try:
        detected: Dict[str, Any] = {"format": fmt}
        if fmt == "csv":
            detected.update(_sniff_csv(con, path))
            detected["encoding"] = (overrides or {}).get("encoding") or "utf-8"
        elif fmt == "excel":
            detected["sheet"] = (overrides or {}).get("sheet") or None
        relation, effective = resolve_csv_settings(con, path, fmt, overrides)
        if fmt == "csv":
            # Report what the file was ACTUALLY read with — including anything the fallback had to
            # add — so the UI shows the truth and can save those settings with the mapping.
            for k in ("delimiter", "quote", "escape", "has_header", "skip_rows", "encoding"):
                if k in effective:
                    detected[k] = effective[k]
            detected["effective"] = effective
        columns = _columns(con, relation)
        for c in columns:
            c["role"] = guess_role(c["name"], c["raw_type"])
        names = [c["name"] for c in columns]
        rows = con.execute(f"SELECT * FROM {relation} LIMIT {int(sample_n)}").fetchall()
        sample_rows = [{names[i]: _jsonable(r[i]) for i in range(len(names))} for r in rows]
        try:
            row_count = con.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()[0]
        except Exception:
            row_count = None
        return {"format": fmt, "detected": detected, "table": table_name_of(filename),
                "columns": columns, "sample_rows": sample_rows, "row_count": row_count}
    finally:
        con.close()


def sheet_names(path: str) -> List[str]:
    """Every sheet in a workbook, in workbook order. [] when they cannot be read.

    Read with openpyxl, not DuckDB: the excel extension ships `read_xlsx` and nothing that lists
    sheets (checked on duckdb 1.5.5 — `duckdb_functions()` matching '%sheet%' is empty), so a
    DuckDB-based enumeration silently returns nothing and every sheet after the first stays dropped.
    `read_only` streams the workbook metadata instead of loading the cells.

    Best-effort by design: a workbook whose sheet list cannot be read — a legacy `.xls`, a corrupt
    archive — must still ingest as a single default-sheet table (what it did before), never fail the
    whole run.
    """
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            return [str(n) for n in wb.sheetnames]
        finally:
            wb.close()
    except Exception:
        return []


def introspect_file(path: str, filename: str, overrides: Optional[dict] = None,
                    sample_n: int = 20) -> List[Dict[str, Any]]:
    """Return the file's tables in the shape ingest_structured._sync_introspect produces, so
    catalog-mode node creation (Table + Attribute + FK flags) is reused unchanged.

    One entry per table. That is one table for every format except a MULTI-SHEET workbook, which is
    genuinely several tables: DuckDB's `read_xlsx` reads ONE sheet (the first, unless `sheet` says
    otherwise), so returning a single entry meant every sheet after the first was silently dropped —
    no error, no count, nothing in the report to say the data existed.

    An explicit `sheet` override still means "this one sheet", exactly as before: the author named
    it, and a saved mapping that pins a sheet must keep reading that sheet and nothing else.
    """
    ov = overrides or {}
    if file_format(filename) == "excel" and not ov.get("sheet"):
        names = sheet_names(path)
        if len(names) > 1:
            out: List[Dict[str, Any]] = []
            for name in names:
                t = _introspect_one(path, filename, {**ov, "sheet": name}, sample_n)
                # The SHEET names the table — `Q1`, `Q3` — because within one workbook the file stem
                # is the same for all of them and could not tell two sheets apart.
                t["table"] = name
                t["sheet"] = name
                out.append(t)
            return out
    return [_introspect_one(path, filename, ov or None, sample_n)]


def _introspect_one(path: str, filename: str, overrides: Optional[dict] = None,
                    sample_n: int = 20) -> Dict[str, Any]:
    """One relation (a file, or one sheet of a workbook) → its introspection entry."""
    pv = preview_file(path, filename, overrides, sample_n)
    sample = pv["sample_rows"]
    colmeta = []
    for c in pv["columns"]:
        name = c["name"]
        vals = [row.get(name) for row in sample]
        nulls = sum(1 for v in vals if v is None)
        samples = [str(v) for v in vals if v is not None][:5]
        role = c.get("role")
        colmeta.append({
            "name": name, "type": c["type"], "raw_type": c.get("raw_type"), "nullable": True,
            "primary_key": role == "identity", "foreign_key": role == "fk",
            "references_table": None, "references_column": None,
            "null_rate": round(nulls / len(sample), 3) if sample else None,
            "sample_values": samples,
        })
    return {"table": pv["table"], "schema": "", "row_count": pv["row_count"],
            "is_view": False, "columns": colmeta}


def iter_row_batches(path: str, filename: str, overrides: Optional[dict] = None,
                     batch_size: int = 2000) -> Iterator[List[Dict[str, Any]]]:
    """Stream the file's rows as LISTS of dicts, one Arrow record-batch at a time (out-of-core —
    never materialises the whole file). The graph-write path commits one batch per transaction, so
    batch_size bounds memory + transaction size regardless of file size."""
    fmt = file_format(filename)
    con = _con()
    try:
        # same resolution as the preview, so the rows ingested are the rows that were shown
        relation, _eff = resolve_csv_settings(con, path, fmt, overrides)
        reader = con.execute(f"SELECT * FROM {relation}").fetch_record_batch(batch_size)
        for batch in reader:
            yield [{k: _jsonable(v) for k, v in row.items()} for row in batch.to_pylist()]
    finally:
        con.close()


def _jsonable(v: Any) -> Any:
    """Coerce DuckDB/Arrow scalars to JSON-friendly python (dates → iso strings, Decimal → float)."""
    import datetime
    import decimal
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", "ignore")
    return str(v)
