"""Built-in extractor catalog — every node/edge type the platform produces, as a template row.

Two flavors:
  • DETERMINISTIC (mechanism.ref = "deterministic.*" / "pipeline.*", no prompt): produced by coded
    pipelines (SQL introspection, chunker, rule generators, the structured LLM pipeline). These are
    VIEW-ONLY in the Studio — you see the node/edge type and its attributes, but you can't edit or
    run them as generic templates (the code owns them). They exist so the catalog shows the full
    ontology with attributes, tagged structured / unstructured.
  • EXECUTABLE (mechanism.ref = "*.llm" + a prompt): real templates the generic enrichment engine
    runs over nodes — e.g. business semantics on tables, a custom extractor over Chunks.

Metadata (category / deterministic) lives INSIDE the `mechanism` JSONB so there is NO schema
migration. `locked` is derived from the `builtin:` name prefix by the API.
"""
import uuid
from sqlalchemy import select
from opencortyx.domain import ExtractionTemplate


_BOUND_TEXT = {"select": "*", "bound": "text"}


def _returns(fields, *, free_keys=False, one=False) -> str:
    """The ONE output contract, rendered from the fields the extractor actually declares.

    Before this there were SEVEN top-level keys across the built-ins — `records`, `segments`,
    `rules`, `reports`, `computations`, `contradictions`, plus a flat multi-key object — and five
    prompts that stated no contract at all. `_records()` absorbed most of that through a
    "first list-of-dicts value" fallback, so the variation was invisible in the graph and visible
    only to whoever had to read the prompts. It also made the framework's own appended line
    contradict six of them outright.

    One envelope, always `records`. The only degree of freedom is whether the FIELD NAMES are
    declared (the normal case, rendered here from `emit`) or invented by the model (`free_keys`,
    for an open-vocabulary extractor whose keys `emit` cannot know in advance).

    Rendered rather than typed so prompt and contract cannot drift: add an attribute to a card and
    the example grows with it.
    """
    if free_keys:
        shape = '"<field name you choose>": "<value>"'
    else:
        shape = ", ".join('"%s": "..."' % f for f in fields)
    count = ("Exactly one object" if one else "One object per item")
    naming = ("Choose the field names yourself; keep them short and reusable."
              if free_keys else "Use exactly these field names.")
    return ('Return ONLY JSON: {"records": [{%s}]}\n'
            '%s; {"records": []} if none. %s\n' % (shape, count, naming))


def _t(name, category, *, node=None, attributes=None, edges=None, ref=None,
       prompt=None, source=None, deterministic=True, emit_attributes=None, model_capable=False):
    """Compact builder for a catalog template row."""
    mech = {"ref": ref or ("deterministic.introspect" if deterministic else "unstructured.llm"),
            "category": category, "deterministic": deterministic}
    if prompt:
        mech["prompt"] = prompt
    if model_capable:                      # uses a MODEL for its purpose (may not need a prompt) →
        mech["model_capable"] = True       # the Studio shows a model selector + Edit for it

    emit = {}
    nodes = []
    if node:
        nodes.append({"type": node, "attributes": attributes or []})
    if nodes:
        emit["nodes"] = nodes
    if edges:
        emit["edges"] = edges
    if emit_attributes:
        emit["attributes"] = emit_attributes
    return {"name": name, "category": category, "source": source,
            "mechanism": mech, "emit": emit, "status": "active"}


_PICK_HELPERS = r'''
def _pick(inputs, names, default=None):
    """First non-empty value among `names` — attribute names differ per schema mapping."""
    for n in names:
        v = (inputs or {}).get(n)
        if v not in (None, "", [], {}):
            return v
    for k, v in (inputs or {}).items():                  # mappers often prefix: order_null_rate
        if v in (None, "", [], {}):
            continue
        kl = str(k).lower()
        for n in names:
            if kl.endswith("_" + n):
                return v
    return default

def _base(node):
    """What a node was ingested AS, seeing through an anchor retype (Source → Aadhaar)."""
    node = node or {}
    return (node.get("properties") or {}).get("__base_type") or node.get("type") or ""
'''

_STRUCTURED: list = []

def _attr_llm(prompt, model=None):
    return {"mechanism": "llm", "prompt": prompt, "model": model}


def _attr_extractor(name, node_type, attributes, description, category="structured"):
    """An enricher whose every attribute has its own mechanism — no shared prompt to edit."""
    row = _t(name, category, deterministic=False, ref="unstructured.attributes",
             source={"select": node_type},
             emit_attributes=[{"on": "source", "set": {k: "" for k in attributes}}])
    row["mechanism"]["attr_overrides"] = dict(attributes)
    row["mechanism"]["description"] = description
    return row


_TABLE_ATTRS = {
    "description": _attr_llm("Write the business meaning of this table in 2-3 sentences.\n"
                             "Table: {table_name}\nColumns: {columns}\n\n"
                             'Return ONLY JSON: {"description":"..."}'),
    "domain": _attr_llm("Which business domain does this table belong to (Sales, Finance, HR, "
                        "Operations, Marketing, …)?\nTable: {table_name}\nColumns: {columns}\n\n"
                        'Return ONLY JSON: {"domain":"..."}'),
    "synonyms": _attr_llm("List the other names business users call this table.\n"
                          "Table: {table_name}\nColumns: {columns}\n\n"
                          'Return ONLY JSON: {"synonyms":["..."]}'),
    "sample_questions": _attr_llm("List questions this table can answer, as an analyst would ask "
                                  "them.\nTable: {table_name}\nColumns: {columns}\n\n"
                                  'Return ONLY JSON: {"sample_questions":["..."]}'),
}

_ATTRIBUTE_ATTRS = {
    "description": _attr_llm("Describe this column's business meaning in one sentence.\n"
                             "Column: {label}\nTable: {table_name}\n\n"
                             'Return ONLY JSON: {"description":"..."}'),
    "semantic_type": _attr_llm("Classify this column.\nColumn: {label}\n\n"
                               'Return ONLY JSON: {"semantic_type":"identifier|currency|email|date|'
                               'categorical|text|metric"}'),
    "is_pii": _attr_llm("Does this column hold personally identifiable information?\n"
                        "Column: {label}\n\nReturn ONLY JSON: {\"is_pii\":true|false}"),
}

_STRUCTURED_LLM = [
    _attr_extractor("builtin:struct.business_semantics", "Table", _TABLE_ATTRS,
                    "The business meaning of a table — each field written by its own prompt and "
                    "model, so they can be tuned (or switched off) independently."),
    _attr_extractor("builtin:struct.column_semantics", "Attribute", _ATTRIBUTE_ATTRS,
                    "The business meaning of a column — description, semantic type and PII flag, "
                    "each minted by its own prompt and model."),
]

def _pystruct(name, node, attributes, code, edges, description, *, source, category="structured"):
    row = _t(name, category, node=node, attributes=attributes, edges=edges,
             ref="unstructured.python", source=source, deterministic=False)
    row["mechanism"]["language"] = "python"
    row["mechanism"]["code"] = _PICK_HELPERS + code     # attribute names are per-mapping, not fixed
    row["mechanism"]["description"] = description
    return row


_STRUCT_GEN = [
    # ── SEGMENTS — LLM over one table, unchanged prompt; DEPENDS_ON now resolves the columns the
    #    model named (a list endpoint), which the old code did with a by-column lookup table.
    _t("builtin:struct.segment", "structured", node="Segment",
       attributes=["name", "description", "condition_logic", "definition_type"],
       ref="structured.llm", source={"select": "Table"}, deterministic=False, model_capable=True,
       prompt=("You are a data architect. Propose meaningful business SEGMENTS (cohorts) over this "
               "table's rows.\nTable: {table_name}\nColumns: {columns}\n\n"
               + _returns(["name", "description", "definition_type", "condition_logic",
                           "depends_on_fields"])
               + '`definition_type` is CATEGORICAL or BEHAVIORAL; `condition_logic` is a SQL WHERE '
                 'expression; `depends_on_fields` is a list of column names.')),
    # ── COMPUTATIONS — per table now (the old pass prompted over every table at once). Same node and
    #    edge types; `scope: global` keeps ONE node per metric name across tables, as before.
    _t("builtin:struct.computation", "structured", node="Computation",
       attributes=["name", "formula_type", "formula_expression", "output_type", "unit", "description"],
       ref="structured.llm", source={"select": "Table"}, deterministic=False, model_capable=True,
       prompt=("You are a data architect. Propose valuable COMPUTATIONS / KPIs for this table.\n"
               "Table: {table_name}\nColumns: {columns}\n\n"
               + _returns(["name", "formula_type", "formula_expression", "output_type", "unit",
                           "description", "depends_on_properties"])
               + "`depends_on_properties` is a list of column names.")),
    _t("builtin:struct.context_rule", "structured", node="ContextRule",
       attributes=["title", "text", "context_type", "keywords", "is_warning", "priority"],
       ref="structured.llm", source={"select": "Table"}, deterministic=False, model_capable=True,
       prompt=("From this table, infer 1-3 business rules an analyst must know before querying it "
               "(exclusions, special filters, calculation quirks). Be specific and actionable.\n"
               "TABLE: {table_name}\nDESCRIPTION: {description}\nCOLUMNS: {columns}\n\n"
               + _returns(["title", "text", "context_type", "keywords", "is_warning", "priority"])
               + "`keywords` is a list; `is_warning` is true or false; `priority` is 1-10.")),

    # ── DATA QUALITY — the null-rate rule, now editable code over each column node.
    _pystruct("builtin:struct.data_quality", "DataQuality",
              ["quality_type", "severity", "affected_table_id", "affected_attribute_id",
               "description", "null_pct"],
              r'''
# author-editable thresholds: below _MIN nothing is reported; the rest set the severity bands.
_MIN = 0.05
_HIGH = 0.25
_CRITICAL = 0.5
# author-editable: what the column's null measure is called in YOUR schema mapping
_NULL_FIELDS = ("null_rate", "null_ratio", "nulls", "null_fraction", "null_pct", "null_percent")
_COLUMN_FIELDS = ("physical_column", "column", "column_name", "name", "label")

def extract(inputs):
    try:
        rate = float(_pick(inputs, _NULL_FIELDS) or 0)
    except (TypeError, ValueError):
        rate = 0.0
    if rate > 1:                                         # stored as a percentage, not a rate
        rate = rate / 100.0
    if rate <= _MIN:
        return []
    column = _pick(inputs, _COLUMN_FIELDS, "column")
    pct = round(rate * 100, 1)
    severity = "CRITICAL" if rate > _CRITICAL else ("HIGH" if rate > _HIGH else "MEDIUM")
    return [{
        "quality_type": "NULL_ISSUE",
        "severity": severity,
        "affected_table_id": str(_pick(inputs, ("parent_table_id", "parent_id", "table_id")) or ""),
        "affected_attribute_id": str(inputs.get("id") or ""),
        "description": ("~" + str(pct) + "% of '" + str(column) + "' is NULL — joins/filters will "
                        "drop those rows."),
        "null_pct": pct,
        "label": "NULLs in " + str(column),
    }]
''',
              [{"type": "HAS_QUALITY_ISSUE", "from": "source", "to": "emitted"},
               {"type": "HAS_QUALITY_ISSUE", "per": "record",
                "from": {"node_id": "$affected_table_id"}, "to": "emitted"}],
              "Flags columns whose NULL rate is high enough to silently drop rows from joins and "
              "filters, with a severity band.",
              source={"select": "Attribute"}),

    # ── LINEAGE — one node per table recording where it came from.
    _pystruct("builtin:struct.lineage", "Lineage",
              ["source_system", "source_table_or_api", "target_table_id",
               "transformation_type", "refresh_frequency"],
              r'''
# author-editable: where this table's origin and system are recorded in YOUR schema mapping
_ORIGIN_FIELDS = ("source_table", "table_name", "physical_table", "origin", "source", "label")
_SYSTEM_FIELDS = ("source_system", "system", "connector", "datasource", "platform")

def extract(inputs):
    origin = _pick(inputs, _ORIGIN_FIELDS, "")
    return [{
        "source_system": _pick(inputs, _SYSTEM_FIELDS, "sql"),
        "source_table_or_api": origin,
        "target_table_id": str(inputs.get("id") or ""),
        "transformation_type": "DIRECT",
        "refresh_frequency": "ON_INGEST",
        "label": "Lineage: " + str(inputs.get("label") or origin),
    }]
''',
              [{"type": "HAS_LINEAGE", "from": "source", "to": "emitted"}],
              "Records where each table came from and how often it refreshes.",
              source={"select": "Table"}),
]

_STRUCT_EMIT = {
    "builtin:struct.segment": {
        "nodes": [{"type": "Segment", "label": "$name", "id": ["name"],
                   "attributes": ["name", "description", "condition_logic", "definition_type"]}],
        "edges": [{"type": "HAS_SEGMENT", "from": "source", "to": "emitted"},
                  {"type": "DEPENDS_ON", "per": "record", "from": "emitted",
                   "to": {"match": "Attribute", "on": {"physical_column": "$depends_on_fields"}}}],
    },
    "builtin:struct.computation": {
        "nodes": [{"type": "Computation", "label": "$name", "id": ["name"], "scope": "global",
                   "attributes": ["name", "formula_type", "formula_expression", "output_type",
                                  "unit", "description"]}],
        "edges": [{"type": "HAS_METRIC", "from": "source", "to": "emitted"},
                  {"type": "DERIVED_FROM", "per": "record", "from": "emitted",
                   "to": {"match": "Attribute", "on": {"physical_column": "$depends_on_properties"}},
                   "properties": {"formula_expression": "$formula_expression"}}],
    },
    "builtin:struct.context_rule": {
        "nodes": [{"type": "ContextRule", "label": "$title", "id": ["title"], "scope": "global",
                   "attributes": ["title", "text", "context_type", "keywords", "is_warning",
                                  "priority"]}],
        "edges": [{"type": "HAS_CONTEXT", "from": "source", "to": "emitted"},
                  {"type": "APPLIES_TO", "from": "emitted", "to": "source"}],
    },
    "builtin:struct.data_quality": {
        "nodes": [{"type": "DataQuality", "label": "$label", "key": "null_issue",
                   "attributes": ["quality_type", "severity", "affected_table_id",
                                  "affected_attribute_id", "description", "null_pct"]}],
    },
    "builtin:struct.lineage": {
        "nodes": [{"type": "Lineage", "label": "$label", "key": "lineage",
                   "attributes": ["source_system", "source_table_or_api", "target_table_id",
                                  "transformation_type", "refresh_frequency"]}],
    },
}
for _row in _STRUCT_GEN:
    _spec = _STRUCT_EMIT.get(_row["name"])
    if _spec:
        _row["emit"] = {**_row["emit"], **_spec}     # keep the builder's edges unless overridden

# ── UNSTRUCTURED — deterministic (no prompt, view-only) ───────────────────────
_UNSTRUCTURED = []

_COMMUNITY_CODE = r'''
_MIN_SIZE = 3
_MAX_COMMUNITIES = 50

def extract(inputs):
    ents = cortex.find(type="entity", limit=5000)
    if len(ents) < _MIN_SIZE:
        return []
    parent = {}
    for e in ents:
        parent[e["id"]] = e["id"]

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for rel in cortex.edges(type="RELATES_TO", limit=50000):
        a, b = rel.get("source"), rel.get("target")
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

    comps = {}
    for e in ents:
        comps.setdefault(find(e["id"]), []).append(e)
    groups = sorted([g for g in comps.values() if len(g) >= _MIN_SIZE],
                    key=len, reverse=True)[:_MAX_COMMUNITIES]

    out = []
    for members in groups:
        labels = [m["label"] for m in members if m.get("label")][:40]
        listing = ", ".join(labels)
        summary = ""
        if cortex:                                   # falsy unless a model is configured
            try:
                summary = (cortex.generate(cortex.prompt.replace("{members}", listing),
                                           system="You are a precise analyst.",
                                           max_tokens=200, temperature=0.2) or "").strip()
            except Exception:
                summary = ""
        if not summary:
            summary = "Community of " + str(len(members)) + " related entities: " + listing[:300]
        out.append({
            "label": "Community: " + (labels[0] if labels else "cluster"),
            "text": summary,
            "size": len(members),
            "members": labels,
            "member_ids": [m["id"] for m in members],
            # identity = the smallest member id, so a stable cluster keeps its node across runs
            "cluster_key": min(m["id"] for m in members),
        })
    return out
'''

_COMMUNITY = _t("builtin:doc.community", "unstructured", node="Community",
                attributes=["label", "text", "size", "members"],
                ref="unstructured.python", deterministic=False, model_capable=True,
                prompt=("These related entities form a community: {members}.\n"
                        "Write a 60-word summary of the common theme that connects them."))
_COMMUNITY["mechanism"]["language"] = "python"
_COMMUNITY["mechanism"]["code"] = _COMMUNITY_CODE
_COMMUNITY["mechanism"]["description"] = (
    "Clusters entities into communities (connected components over RELATES_TO) and writes a themed "
    "Community node per cluster. Powers 'big picture' global search.")
_COMMUNITY["emit"] = {
    "nodes": [{"type": "Community", "label": "$label", "id": ["cluster_key"], "scope": "global",
               "attributes": ["label", "text", "size", "members"]}],
    "edges": [{"type": "IN_COMMUNITY", "per": "record",
               "from": {"node_id": "$member_ids"}, "to": "emitted"}],
}
_UNSTRUCTURED.append(_COMMUNITY)


_TEMPORAL_HELPERS = r'''
import re
import dateparser
import parsedatetime
from datetime import datetime, timezone

_cal = parsedatetime.Calendar()

# --- author-editable: which phrases to pick up out of the text ---
_PHRASE = re.compile(
    r"\b(yesterday|today|tomorrow|last\s+\w+|next\s+\w+|this\s+\w+|"
    r"\d+\s+\w+\s+ago|\d{4}-\d{2}-\d{2})\b", re.I)

def _time_zero(inputs):
    """The anchor relative phrases resolve against: the node's own timestamp."""
    tz = inputs.get("haystack_dates") or inputs.get("created_date") or inputs.get("valid_from") or ""
    if isinstance(tz, list) and tz:
        tz = str(tz[0])
    elif isinstance(tz, str) and ";" in tz:
        tz = tz.split(";")[0].strip()
    tz = str(tz).strip()
    if tz:
        dt = dateparser.parse(tz)
        if dt:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        ts, ok = _cal.parse(tz)
        if ok:
            return datetime(*ts[:6]).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)

def _normalize(phrase, base):
    dt = dateparser.parse(phrase, settings={"RELATIVE_BASE": base.replace(tzinfo=None)})
    if dt:
        return dt.strftime("%Y-%m-%d")
    ts, ok = _cal.parse(phrase, sourceTime=base.replace(tzinfo=None))
    return datetime(*ts[:6]).strftime("%Y-%m-%d") if ok else ""

def _kind(phrase):
    return "absolute" if re.match(r"\d{4}-\d{2}-\d{2}", str(phrase)) else "relative"

def _detect(text):
    """Find candidate date/time phrases. Uses the SDK model (cortex.generate) when this extractor is
    model_capable (high recall — catches 'two weeks ago', 'before the trip'), else the deterministic
    pattern regex (free)."""
    if cortex:                                       # injected only when model_capable is on
        try:
            recs = cortex.generate(
                'List EVERY date or time expression in the TEXT (absolute like "2023-05-01" AND '
                'relative like "last tuesday", "two weeks ago", "before the trip"). '
                'Return JSON {"records":[{"phrase":"..."}]}.\n\nTEXT:\n' + text[:6000], json=True)
            got = [str(r.get("phrase")).strip() for r in (recs or []) if isinstance(r, dict) and r.get("phrase")]
            if got:
                return got
        except Exception:
            pass                                     # fall through to regex on any model failure
    return [m.group(0) for m in _PHRASE.finditer(text)]
'''

# DETECTOR (+inline normalize): detect date/time phrases in the text, resolve each to ISO vs Time Zero.
_EXTRACT_TEMPORAL_PATTERN_CODE = _TEMPORAL_HELPERS + r'''
def extract(inputs):
    base = _time_zero(inputs)
    out, seen = [], set()
    for phrase in _detect(inputs.get("text") or ""):
        key = phrase.lower()
        if not phrase or key in seen:
            continue
        seen.add(key)
        iso = _normalize(phrase, base)
        out.append({"phrase": phrase, "normalized_date": iso, "kind": _kind(phrase)})
    return out
'''

# NORMALIZER: same Time-Zero normalization in three input shapes so it can run standalone OR chained.
_DATE_NORMALIZER_CODE = _TEMPORAL_HELPERS + r'''
def extract(inputs):
    """Tri-mode:
      • records mode  — inputs['records'] present (fed a detector's output): normalize each in place.
      • phrase  mode  — inputs['phrase'] present (running over one emitted phrase node): normalize it.
      • text    mode  — detect phrases in inputs['text'] (LLM via cortex if model_capable else regex).
    Time Zero (the reference date) always comes from the node's own haystack_dates/created_date/
    valid_from — which an emitted phrase node inherits from its source (e.g. a Turn's session date)."""
    base = _time_zero(inputs)

    recs = inputs.get("records")                     # records mode — a prior detector handed us phrases
    if isinstance(recs, list) and recs:
        out = []
        for r in recs:
            if not isinstance(r, dict):
                continue
            phrase = r.get("phrase") or r.get("normalized_date")
            if not phrase:
                continue
            iso = _normalize(str(phrase), base)
            if iso:
                nr = dict(r); nr["phrase"] = str(phrase); nr["normalized_date"] = iso
                nr.setdefault("kind", _kind(phrase))
                out.append(nr)
        return out

    if inputs.get("phrase"):                         # phrase mode — one emitted phrase node
        phrase = str(inputs["phrase"])
        iso = _normalize(phrase, base)
        return [{"phrase": phrase, "normalized_date": iso, "kind": inputs.get("kind") or _kind(phrase)}] if iso else []

    out, seen = [], set()                            # text mode — detect then normalize
    for phrase in _detect(inputs.get("text") or ""):
        key = phrase.lower()
        if not phrase or key in seen:
            continue
        seen.add(key)
        iso = _normalize(phrase, base)
        if iso:
            out.append({"phrase": phrase, "normalized_date": iso, "kind": _kind(phrase)})
    return out
'''


def _py(name, node, edge, attributes, code, description="", model_capable=False):
    """Build one coded (Python) built-in extractor: one extract(inputs) call → records → one node
    type + a Chunk→node edge. Executable (deterministic=False) so the generic engine runs it.
    `model_capable` → the sandbox gets the `cortex` SDK handle and the Studio shows a model picker."""
    row = _t(name, "unstructured", node=node, attributes=attributes,
             edges=[{"type": edge, "from": "source", "to": "emitted"}],
             ref="unstructured.python", source=dict(_BOUND_TEXT), deterministic=False,
             model_capable=model_capable)
    row["mechanism"]["language"] = "python"
    row["mechanism"]["code"] = code
    if description:
        row["mechanism"]["description"] = description
    return row


_PYTHON = [
    # NORMALIZER: normalizes a detector's output (records/phrase mode) or self-detects (text mode).
    _py("builtin:py.date_normalizer", "DateRef", "HAS_DATE_REF",
        ["phrase", "normalized_date", "kind"], _DATE_NORMALIZER_CODE,
        "Normalizes date/time expressions to an ISO date against the document's timestamp (Time Zero). "
        "Runs after a detector (records/phrase mode) or standalone (self-detects). Deterministic."),
]

_MEMORY = [
    _t("builtin:mem.summarizer", "memory", ref="pipeline.memory", model_capable=True,
       prompt=("You distill an AI working session into durable memory.\n"
               "From the transcript below, extract: a 2-3 sentence SUMMARY of what was done, the "
               "DECISIONS made (with rationale), and durable FACTS / preferences learned.\n\n"
               "Transcript:\n{transcript}\n\n"
               'Return ONLY JSON: {"summary":"...","decisions":[{"title":"...","rationale":"..."}],'
               '"facts":["..."],"preferences":["..."]}')),
]

_SECTION_HELPERS = _PICK_HELPERS + r'''
_DERIVED = ("Summary", "Topic", "Keyword", "Question", "Community")
_READING_UNITS = ("Chunk", "Record")

def _asset():
    return cortex.node() or {}

def _props():
    return _asset().get("properties") or {}

def _kids():
    return cortex.children(limit=2000)

def _content(kids):
    """The asset's reading units — by ROLE, not by the name "Chunk", because the schema mapping
    decides what they are called (Record / Turn / anything the user chose)."""
    units = [k for k in kids if _base(k) in _READING_UNITS]
    if units:
        return units
    return [k for k in kids
            if str((k.get("properties") or {}).get("text") or "").strip()
            and _base(k) not in _DERIVED and not str(k.get("type") or "").endswith("Profile")]

def _title():
    return _asset().get("label") or _pick(_props(), ("filename", "title", "name"), "asset")

def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def _section(key, title, fields):
    """One section record: the fields, plus the flat `text` the catalog UI and embeddings read."""
    rec = dict(fields)
    rec["section"] = key
    rec["label"] = title + " · " + _title()
    rec["text"] = key + ": " + ", ".join(str(k) + "=" + str(v) for k, v in fields.items())
    return [rec]
'''


def _pysec(name, node, attrs, code, description):
    """A COMPUTED profile section: python mechanism over the asset node → one section node + edge."""
    row = _t(name, "unstructured", node=node, attributes=attrs,
             edges=[{"type": "HAS_SECTION", "from": "source", "to": "emitted"}],
             ref="unstructured.python", source={"select": "Source"}, deterministic=False)
    row["mechanism"]["language"] = "python"
    row["mechanism"]["code"] = _SECTION_HELPERS + code
    row["mechanism"]["description"] = description
    row["emit"]["nodes"][0].update({"label": "$label", "key": name.split(".")[-1]})
    return row


def _llmsec(name, node, field, key):
    """A NARRATIVE profile section: an ordinary editable prompt over the asset node."""
    row = _t(name, "unstructured", node=node, attributes=["text"],
             edges=[{"type": "HAS_SECTION", "from": "source", "to": "emitted"}],
             ref="unstructured.llm", source={"select": "Source"}, deterministic=False,
             model_capable=True, prompt=_NARRATIVE.replace("{field}", field))
    row["emit"]["nodes"][0].update({"label": field.split("(")[0].strip().title(), "key": key})
    return row


# CONTENT FIRST, stats last: a model handed a JSON stats block before the text it is meant to read
# will describe the stats block. Seen live — asset descriptions came back as the stats echoed back.
_NARRATIVE = ("You are a data-catalog assistant. Write ONLY the {field} for this asset, factually and "
              "from the CONTENT below alone — never invent owners, dates, SLAs, or numbers.\n\n"
              "Asset: {title}\n\nCONTENT:\n{excerpt}\n\n"
              "(reference only, do not describe: {stats})\n\n"
              + _returns(["text"], one=True))

_PROFILE = [
    _pysec("builtin:doc.trust", "TrustProfile",
           ["certification", "classification", "contains_pii", "data_confidence"], r'''
def extract(inputs):
    kids = _kids()
    p = _props()
    contains_pii = bool(_pick(p, ("contains_pii", "has_pii", "pii"))) or any(
        _pick(k.get("properties") or {}, ("is_pii", "pii", "sensitive")) for k in kids)
    checks = [bool(_content(kids)),
              bool(_pick(p, ("description", "business_description", "summary"))),
              any(_base(k) == "Summary" for k in kids)]
    completeness = round(100 * sum(1 for c in checks if c) / 3)
    return _section("trust", "Trust", {
        "certification": _pick(p, ("certification", "certified", "status"), "Draft"),
        "classification": ("Confidential (PII)" if contains_pii
                           else _pick(p, ("sensitivity", "classification"), "Internal")),
        "contains_pii": contains_pii,
        "data_confidence": "High" if completeness >= 80 else ("Medium" if completeness >= 50 else "Low"),
    })
''', "Certification, sensitivity classification and PII presence — how far this asset can be trusted."),

    # narrative sections — ordinary editable prompts, one node per asset each
    _llmsec("builtin:doc.exec_summary", "ExecutiveSummary",
            "executive summary (2-3 sentences)", "exec_summary"),
]


_ANSWER = [
    _t("builtin:answer.relevance", "answer", ref="pipeline.answer", model_capable=True,
       prompt=("You are a relevance evaluator. Does the CONTEXT contain enough information to "
               "accurately answer the QUESTION? Reply strictly with YES or NO.\n\n"
               "Context:\n{context}\n\nQuestion: {query}")),
    _t("builtin:answer.generate", "answer", ref="pipeline.answer", model_capable=True,
       prompt=("The CONTEXT below is the user's OWN saved information (their notes, memory and "
               "documents). Answer the QUESTION directly and specifically using ONLY this context. "
               "Do NOT refuse on privacy grounds and do NOT say 'as an AI' — this is the user's own "
               "data and they are asking about themselves. Cite sources inline like [Type#id]. If the "
               "answer is genuinely not present in the context, reply with exactly: I don't know.\n\n"
               "Context:\n{context}\n\nQuestion: {query}")),
]

for _a in _ANSWER:
    if _a["name"] == "builtin:answer.generate":
        _a["mechanism"]["non_answer_phrases"] = [
            "i don't know", "i do not know", "no information", "not mentioned", "does not contain",
            "doesn't contain", "cannot answer", "can't answer", "no relevant information",
            "not provided in the context", "no data", "isn't in the context", "is not in the context",
            "not specified", "could not find", "not enough information", "don't have enough"]
        _a["mechanism"]["refusal_phrases"] = [
            "can't assist", "cannot assist", "unable to assist", "i'm not able to", "i am not able to",
            "as an ai", "as an artificial intelligence", "i don't have access", "i do not have access",
            "i'm unable to", "i am unable to", "i don't possess", "consult your", "contact your",
            "i cannot provide", "i can't provide", "respect your privacy", "privacy reasons"]

_RELATIONSHIPS = [
    {"name": "builtin:relate.entities", "category": "unstructured",
     "source": dict(_BOUND_TEXT),
     "mechanism": {
         "ref": "relate.llm", "category": "unstructured", "deterministic": False,
         "from_type": "entity", "to_type": "entity", "via": "MENTIONS",
         "relation": "RELATES_TO", "direction": "from_to",
         "prompt": ("Given the passage and the entities found in it, identify which pairs of entities "
                    "are directly related. ALWAYS include identity & naming links: \"my dog's name is "
                    "Max\" → (dog, has_name, Max); \"Max is a dog\" → (Max, is_a, dog); \"Sarah is my "
                    "manager\" → (Sarah, is_a, manager). Use a short verb phrase for the relation."),
     },
     # descriptive only — the relate engine reads `mechanism`; this powers the card + ontology.
     "emit": {"edges": [{"type": "RELATES_TO", "from": "entity", "to": "entity"}]},
     "status": "active"},
]

_SEMISTRUCTURED = []

BUILTIN_TEMPLATES = (_STRUCTURED + _STRUCTURED_LLM + _STRUCT_GEN + _UNSTRUCTURED
                     + _PYTHON + _MEMORY + _PROFILE + _ANSWER
                     + _RELATIONSHIPS + _SEMISTRUCTURED)
_BUILTIN_NAMES = {t["name"] for t in BUILTIN_TEMPLATES}


STRUCT_GENERATOR_NAMES = [
    "builtin:struct.segment", "builtin:struct.computation", "builtin:struct.data_quality",
    "builtin:struct.lineage", "builtin:struct.context_rule",
]
#: Org-wide catalogues — no source node, emitted once per run.
STRUCT_ORG_NAMES = []
#: The asset profile sections, in card order (5 computed, then 4 narrative).
PROFILE_SECTION_NAMES = [t["name"] for t in _PROFILE]


READING_SHAPES = {
    "document": {
        "label": "Files (documents)",
        "configured_in": "Step 1 · Source & reading rules, or a bound unstructured schema mapping",
        "nodes": [
            {"type": "Source", "role": "the asset itself", "attributes":
                ["filename", "mime", "checksum", "language", "word_count"],
             "measured": ["checksum", "language", "word_count"]},
            {"type": "Chunk", "role": "the reading unit the extractors run over",
             "attributes": ["text", "ordinal"], "measured": ["text", "ordinal"]},
        ],
        "edges": [{"type": "CONTAINS", "from": "Source", "to": "Chunk"},
                  {"type": "NEXT", "from": "Chunk", "to": "Chunk"}],
    },
    "semistructured": {
        "label": "JSON / semi-structured",
        "configured_in": "the structure cards, or a bound semi-structured schema mapping",
        "nodes": [
            {"type": "Record", "role": "one object from the payload",
             "attributes": ["text", "valid_from", "seq"], "measured": ["seq"]},
            {"type": "Chunk", "role": "reading unit when text is rolled up across records",
             "attributes": ["text", "valid_from", "record_index", "unit_group"],
             "measured": ["record_index", "unit_group"]},
        ],
        "edges": [{"type": "HAS_RECORD", "from": "Source", "to": "Record"},
                  {"type": "NEXT", "from": "Record", "to": "Record"},
                  {"type": "REFERENCES", "from": "Record", "to": "Record"},
                  {"type": "CONTAINS", "from": "Source", "to": "Chunk"}],
    },
    "structured": {
        "label": "Databases (tables)",
        "configured_in": "Step 1 · Base schema, or a bound structured schema mapping",
        "nodes": [
            {"type": "Organization", "role": "the tenant hub org-wide catalogues hang off",
             "attributes": ["kind"], "measured": []},
            {"type": "Table", "role": "one table",
             "attributes": ["table_name", "schema_name", "row_count", "column_count",
                            "primary_key_field", "source_table", "source_system"],
             "measured": ["row_count", "column_count", "primary_key_field"]},
            {"type": "Attribute", "role": "one column",
             "attributes": ["physical_column", "physical_table", "data_type", "parent_table_id",
                            "is_primary_key", "is_foreign_key", "references_table",
                            "references_column", "nullable", "null_rate", "sample_values"],
             "measured": ["data_type", "nullable", "null_rate", "sample_values",
                          "is_primary_key", "is_foreign_key"]},
        ],
        "edges": [{"type": "HAS_ATTRIBUTE", "from": "Table", "to": "Attribute"},
                  {"type": "JOINS_TO", "from": "Table", "to": "Table"},
                  {"type": "BELONGS_TO", "from": "Table", "to": "Table"},
                  {"type": "HAS_MANY", "from": "Table", "to": "Table"},
                  {"type": "REFERENCES", "from": "Attribute", "to": "Attribute"}],
    },
    "memory": {
        "label": "Agent session memory",
        "configured_in": "written by the memory API when an agent saves a session (POST /v2/memory)",
        "nodes": [
            {"type": "Session", "role": "one working session",
             "attributes": ["title", "agent", "participants", "project", "status", "summary",
                            "started_at", "ended_at", "key_points", "checkpoints", "highlights",
                            "tags"],
             "measured": ["started_at", "ended_at", "agent"]},
            {"type": "Turn", "role": "one exchange in a session",
             "attributes": ["role", "ordinal", "ts", "text", "query", "answer", "context",
                            "thinking", "documents_retrieved", "agent", "user", "actor_type",
                            "actor_id"],
             "measured": ["ordinal", "ts", "role"]},
            {"type": "AppUser", "role": "the workspace account that started a session",
             "attributes": ["actor_id", "name", "email"], "measured": ["actor_id"]},
            {"type": "AppAgent", "role": "the agent that handled a session: the Assistant, an "
                                         "API key, or an MCP client a person signed into",
             "attributes": ["actor_id", "name"], "measured": ["actor_id"]},
            {"type": "Decision", "role": "a choice the session made",
             "attributes": ["title", "rationale", "alternatives", "ts"], "measured": ["ts"]},
            {"type": "Fact", "role": "something durable the session learned",
             "attributes": ["statement", "ts"], "measured": ["ts"]},
            {"type": "Preference", "role": "a stated preference to remember",
             "attributes": ["statement", "scope"], "measured": []},
            {"type": "Artifact", "role": "something the session produced",
             "attributes": ["kind", "ref", "title"], "measured": []},
        ],
        "edges": [{"type": "PART_OF", "from": "Turn", "to": "Session"},
                  {"type": "FOLLOWS", "from": "Turn", "to": "Turn"},
                  {"type": "CONTINUES", "from": "Session", "to": "Session"},
                  {"type": "DECIDED", "from": "Session", "to": "Decision"},
                  {"type": "LEARNED", "from": "Session", "to": "Fact"},
                  {"type": "PREFERS", "from": "Session", "to": "Preference"},
                  {"type": "PRODUCED", "from": "Session", "to": "Artifact"},
                  {"type": "STARTED", "from": "AppUser", "to": "Session"},
                  {"type": "HANDLED_BY", "from": "Session", "to": "AppAgent"},
                  {"type": "OWNED_BY", "from": "AppAgent", "to": "AppUser"},
                  {"type": "REFERENCES", "from": "Turn", "to": "any"}],
    },
}

#: The cards removed above. `seed_builtin_templates` prunes any `builtin:` row that is no longer in
#: the catalog, so existing workspaces drop them on the next seed without a migration.
RETIRED_LOADER_TEMPLATES = (
    "builtin:doc.source", "builtin:doc.chunk", "builtin:json.structure", "builtin:json.chunk",
    "builtin:struct.table", "builtin:struct.attribute", "builtin:struct.relationships",
    "builtin:struct.organization", "builtin:struct.enrich",
    "builtin:mem.session", "builtin:mem.turn", "builtin:mem.decision", "builtin:mem.fact",
    "builtin:mem.preference", "builtin:mem.artifact",
    # the pipeline shortcut stages and the default document extraction, removed by user decision
    "builtin:doc.summary", "builtin:doc.keyword", "builtin:extract.entity", "builtin:extract.claim",
)


async def seed_builtin_templates(db, workspace_id: uuid.UUID) -> int:
    """Ensure the built-in catalog exists for the org. Idempotent: inserts missing built-ins and
    prunes stale `builtin:` rows no longer in the catalog. Never touches user templates."""
    rows = (await db.execute(
        select(ExtractionTemplate).where(
            ExtractionTemplate.workspace_id == workspace_id,
            ExtractionTemplate.name.startswith("builtin:")))).scalars().all()
    existing = {r.name: r for r in rows}

    added = 0
    for tmpl in BUILTIN_TEMPLATES:
        cur = existing.get(tmpl["name"])
        if cur is None:
            db.add(ExtractionTemplate(
                id=uuid.uuid4(), workspace_id=workspace_id, name=tmpl["name"],
                source=tmpl["source"], mechanism=tmpl["mechanism"],
                emit=tmpl["emit"], status=tmpl["status"]))
            added += 1
        else:                                  # refresh shape so the catalog stays current,
            # but NEVER clobber a user's prompt/model edit (create_template stamps _user_edited).
            old = cur.mechanism or {}
            new_mech = dict(tmpl["mechanism"])
            if old.get("_user_edited"):
                for k in ("prompt", "model", "code", "model_capable",
                          "attr_overrides", "compare", "non_answer_phrases",
                          "refusal_phrases"):
                    if k in old:
                        new_mech[k] = old[k]
                new_mech["_user_edited"] = True
            cur.source, cur.mechanism = tmpl["source"], new_mech
            # preserve a user's edited emit (nodes/edges/relationships); else refresh to the catalog
            if old.get("_user_emit"):
                new_mech["_user_emit"] = True
            else:
                cur.emit = tmpl["emit"]

    stale = [r for n, r in existing.items() if n not in _BUILTIN_NAMES]
    for r in stale:
        await db.delete(r)


    # always commit: existing rows are refreshed in-place above, so their shape changes must persist
    await db.commit()
    return added


async def register_emit_types(db, workspace_id, emit, *, source="user", status="candidate") -> int:
    """Auto-add any node/edge types an extraction EMITS into the ontology dictionary.

    This is what makes "define a type" and "extract into it" one action: when a template emits a
    brand-new type (e.g. `Risk`), that word is also written into the dictionary — as a *candidate*
    you can review/promote on the Ontology page. The type is this organization's own; one it already
    sees (its own or built-in) is left untouched.
    Does NOT commit (the caller owns the transaction)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from opencortyx.domain.ontology import TYPE_KEY, TypeRegistry, find_type

    emit = emit or {}
    # What the readers themselves write is platform vocabulary, not ontology to review — whichever
    # path is registering (a mapping, an extractor card, an anchor), a person is never asked to
    # approve `Chunk`, `NEXT` or `SectionHeader`.
    from opencortyx.ingest.pipelines.conformance import platform_reading_types
    own = platform_reading_types()
    items = []   # (name, kind, properties_schema)
    for n in (emit.get("nodes") or []):
        if isinstance(n, dict) and n.get("type") and n["type"] not in own["nodes"]:
            fields = n.get("attributes") or []
            schema = {"fields": [{"name": f} for f in fields]} if fields else {}
            items.append((str(n["type"]), "node", schema))
    for e in (emit.get("edges") or []):
        if isinstance(e, dict) and (e.get("type") or e.get("rel")) \
                and (e.get("type") or e.get("rel")) not in own["edges"]:
            items.append((str(e.get("type") or e.get("rel")), "edge", {}))

    made = 0
    for name, kind, schema in items:
        if not name or await find_type(db, workspace_id, name, kind) is not None:
            continue
        stmt = pg_insert(TypeRegistry).values(
            workspace_id=workspace_id, name=name, kind=kind, description=f"(from extraction) {name}",
            properties_schema=schema, source=source, status=status
        ).on_conflict_do_nothing(index_elements=TYPE_KEY)
        res = await db.execute(stmt)
        made += (res.rowcount or 0)
    return made
