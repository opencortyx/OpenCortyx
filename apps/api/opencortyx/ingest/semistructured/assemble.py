"""Text assembly for semi-structured pipelines — turn ONE JSON record into reading units.

Given a record and its matched structure (schema.py), collect every TEXT-role value with its
position, group per the structure's `unit`, and bind the nearest DATE-role value:

  unit = "record" → the whole record is ONE unit (all its texts joined in order)
  unit = "parent" → one unit per outermost array element that contains text
                    (e.g. sessions[].turns[].content → one unit per SESSION, its turns joined —
                    the right granularity for conversations: context preserved, few LLM calls)
  unit = "value"  → one unit per individual text value (finest, most calls)

Pure functions; no DB, no LLM.
"""
from opencortyx.ingest.semistructured.schema import readable_paths, date_paths, _MAX_DEPTH


def _collect(value, schema_prefix: str, inst_prefix: str, wanted: set, out: list, depth: int = 0):
    """Depth-first collection of wanted-path values as (instance_path, schema_path, value),
    in document order. instance_path carries indices ('sessions[2].turns[0].content') so units
    can group by containing element."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            sp = f"{schema_prefix}.{k}" if schema_prefix else str(k)
            ip = f"{inst_prefix}.{k}" if inst_prefix else str(k)
            _collect(v, sp, ip, wanted, out, depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _collect(v, f"{schema_prefix}[]", f"{inst_prefix}[{i}]", wanted, out, depth + 1)
    else:
        if schema_prefix in wanted and value not in (None, ""):
            out.append((inst_prefix, schema_prefix, value))


def _group_key(instance_path: str, unit: str) -> str:
    """The grouping bucket for a text value. 'parent' groups by the OUTERMOST array element the
    value lives in ('sessions[2]'); values outside any array fall into one record-level bucket."""
    if unit == "record":
        return "record"
    if unit == "value":
        return instance_path                       # unique per value
    # parent: cut right after the first '[i]' segment
    idx = instance_path.find("]")
    return instance_path[: idx + 1] if idx != -1 else "record"


def units_for(record, structure: dict) -> list:
    """→ [{"text": str, "valid_from": str|None, "group": str, "paths": [schema paths]}] in
    document order. `paths` = which text-role KEYS contributed to the unit — the hook for
    per-key extractor attachment (runner routes a unit to its keys' attached extractors)."""
    t_paths, d_paths = set(readable_paths(structure)), set(date_paths(structure))
    if not t_paths:
        return []
    texts: list = []
    dates: list = []
    _collect(record, "", "", t_paths, texts)
    _collect(record, "", "", d_paths, dates)
    if not texts:
        return []
    unit = structure.get("unit") or "parent"

    groups: dict = {}                              # group_key → [text values] (insertion-ordered)
    gpaths: dict = {}                              # group_key → {contributing schema paths}
    for inst, sp, val in texts:
        g = _group_key(inst, unit)
        groups.setdefault(g, []).append(str(val))
        gpaths.setdefault(g, set()).add(sp)

    # date binding: for each group, the first date whose instance path shares the group's bucket;
    # fallback = the record's first date anywhere.
    fallback_date = str(dates[0][2]) if dates else None

    def _date_for(gkey: str):
        if gkey == "record":
            return fallback_date
        for inst, _sp, val in dates:
            if _group_key(inst, "parent") == gkey or inst.startswith(gkey):
                return str(val)
        return fallback_date

    return [{"text": "\n\n".join(vals), "valid_from": _date_for(g), "group": g,
             "paths": sorted(gpaths.get(g) or [])}
            for g, vals in groups.items() if any(v.strip() for v in vals)]
