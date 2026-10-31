"""Generic enrichment engine — run an executable extraction template over EXISTING nodes.

This is the "attach anything to anything" runner. Unlike chunk-time extraction (which reads
document text), this iterates nodes that are already in the graph — of ANY type, structured
(Table / Attribute / Computation / …) or unstructured (Entity / Chunk / Topic / …) — feeds each
node's own attributes into the template's prompt, and applies the template's `emit`:

  • emit.attributes  → MERGE new attributes onto the source node (e.g. an LLM-written
                       description or a PII flag on an Attribute), without clobbering existing keys.
  • emit.nodes       → CREATE new nodes derived from the source (e.g. a Risk node per Attribute).
  • emit.edges       → CREATE edges between the source and the emitted nodes.

Deterministic / built-in catalog templates (mechanism.ref starting with "deterministic" or
"pipeline", or with no prompt) are produced by coded pipelines and are NEVER executed here —
they are view-only. Only LLM templates with a prompt run.

Idempotency: emitted nodes can declare `id: [field, ...]` → a deterministic uuid5, so re-running
upserts instead of duplicating. Attribute merges are inherently idempotent.
"""
import json
import uuid
import re
from typing import Optional

from sqlalchemy import select

from opencortyx.core.db import V2Session
from opencortyx.core.substrate import NodeRepo
from opencortyx.core.substrate import document as _node_document
from opencortyx.core.inference.llm import complete
from opencortyx.core.inference.registry import NoModelConfigured
from opencortyx.stores.graph import NodeQuery
from opencortyx.stores.graph.query import normalize_name
from opencortyx.ingest.structured.catalog import _det_id, _parse_json, _s, _uedge

_repo = NodeRepo()

# Max nodes a single template run will touch — bounds LLM cost / runtime.
_RUN_LIMIT = 300


_LLM_REFS = ("unstructured.llm", "unstructured.generative_llm", "structured.llm")
# Relationship-extraction mechanisms: connect two entities that ALREADY exist in the graph and are
# both attached to a shared anchor node (e.g. a Chunk). See `_run_relate`.
_RELATE_REFS = ("relate.llm", "relate.fixed", "relate.regex")
_ATTR_REF = "unstructured.attributes"

DEPENDENT_MECHANISM_REFS = (
    "unstructured.llm",          # a prompt fills the records
    "structured.llm",            # the same, over a table/column rather than text
    "unstructured.attributes",   # no shared call — every attribute has its own mechanism
    "unstructured.python",       # author-written extract, taking inputs
    "unstructured.regex",
    "unstructured.dictionary",
    "unstructured.keyword",
    "unstructured.ner",
)
_PRODUCIBLE = set(_LLM_REFS) | set(_RELATE_REFS) | {"unstructured.regex", "unstructured.dictionary",
                                "unstructured.keyword", "unstructured.ner", "unstructured.python",
                                "seed.literal", _ATTR_REF}
_SPACY = None  # lazy-loaded spaCy pipeline (optional) for NER


class _MechView:
    """A template with a different `mechanism`, without touching the ORM row.

    A per-stage override (the edge a relate extractor traverses) must apply to THIS run only. Setting
    `t.mechanism = ...` on the loaded ORM object would flush the stage's choice back to the catalogue
    on the next commit, silently rewriting the extractor for every other pipeline that uses it.
    """
    __slots__ = ("_t", "mechanism")

    def __init__(self, t, mechanism):
        self._t, self.mechanism = t, mechanism

    def __getattr__(self, k):
        return getattr(self._t, k)


def _with_mechanism(t, mechanism):
    return _MechView(t, mechanism)


def is_executable(tmpl) -> bool:
    """A template runs in the generic engine if it has a real, implemented mechanism. Deterministic
    / pipeline / catalog templates are produced by code and stay view-only."""
    mech = tmpl.mechanism or {}
    ref = str(mech.get("ref") or "")
    if (tmpl.status or "active") != "active":
        return False
    if ref.startswith("deterministic") or ref.startswith("pipeline") or mech.get("deterministic"):
        return False
    if ref in _PRODUCIBLE:
        return True
    return bool(mech.get("prompt")) and ("llm" in ref or not ref)   # legacy: bare prompt = llm


_STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are",
              "was", "were", "be", "by", "as", "at", "that", "this", "it", "from", "we", "you",
              "they", "their", "our", "its", "but", "not", "can", "will", "has", "have", "had"}


def _keywords(text: str, n: int = 8) -> list:
    import re
    words = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", (text or "").lower())
    freq: dict = {}
    for w in words:
        if w in _STOPWORDS:
            continue
        freq[w] = freq.get(w, 0) + 1
    top = sorted(freq.items(), key=lambda x: -x[1])[:max(1, int(n))]
    return [{"term": w, "count": c} for w, c in top]


def _regex_records(pattern: str, text: str, ignore_case: bool = False) -> list:
    import re
    if not pattern:
        return []
    try:
        flags = re.IGNORECASE if ignore_case else 0
        out = []
        for m in re.finditer(pattern, text or "", flags):
            d = {k: v for k, v in (m.groupdict() or {}).items() if v is not None}
            d.setdefault("match", m.group(0))
            out.append(d)
        return out[:200]
    except re.error:
        return []


def _dictionary_records(terms, text: str) -> list:
    if isinstance(terms, str):
        terms = [t.strip() for t in terms.replace("\n", ",").split(",") if t.strip()]
    low = (text or "").lower()
    out = []
    for term in (terms or []):
        t = term if isinstance(term, str) else (term.get("term") if isinstance(term, dict) else None)
        if t and t.lower() in low:
            out.append({"term": t})
    return out


async def _ner_records(text: str, model_spec: dict | None = None, workspace_id=None) -> list:
    """Named entities and how each sentence connects them, read by the workspace's Entities model
    (spaCy, no LLM - ingest/extraction/text_reader.py): a record per entity {name, type}, then a
    record per relationship {name, type, relation, object, object_type} - the subject's name and type,
    so an edge can run from it to the object found by name. Without that model, spaCy's own package
    (entities only), then the extraction's registry model."""
    global _SPACY
    if workspace_id is not None:
        try:
            import anyio

            from opencortyx.ingest.extraction import text_reader
            spec = await text_reader.model(workspace_id)
            if spec:
                nlp = await anyio.to_thread.run_sync(text_reader.load, spec["model_id"])
                found = (await anyio.to_thread.run_sync(text_reader.read, nlp, [text or ""]))[0]
                ents = [{"name": t, "type": text_reader._type(label)} for t, label in found.entities]
                rels = [{**ents[a], "relation": rel, "object": ents[b]["name"], "object_type": ents[b]["type"]}
                        for a, rel, b, _ in found.relations if a < len(ents) and b < len(ents)]
                return ents + rels
        except Exception:                                    # noqa: BLE001 - the fallbacks below
            pass
    try:
        import spacy  # type: ignore
        if _SPACY is None:
            _SPACY = spacy.load("en_core_web_sm")
        doc = _SPACY((text or "")[:5000])
        return [{"name": e.text, "type": e.label_} for e in doc.ents]
    except Exception:
        try:
            raw = await complete(
                'Extract named entities. Return ONLY JSON {"records":[{"name":"...","type":"PERSON|ORG|..."}]}'
                "\n\n" + (text or "")[:4000],
                system="You extract named entities. JSON only.", max_tokens=600,
                json_mode=True, temperature=0.0, model_spec=model_spec)
            return _records(_parse_json(raw))
        except NoModelConfigured:
            raise                                    # the run must say what to connect, not find nothing
        except Exception:
            return []


def _seed_records(mech: dict) -> list:
    items = mech.get("items")
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except Exception:
            items = []
    return [i for i in (items or []) if isinstance(i, dict)]


_LLM_KEYWORD_PROMPT = ('Extract the {top_n} most important keywords or key phrases from the TEXT.\n'
                       'Return ONLY JSON: {"records":[{"term":"..."}]}\n\nTEXT:\n{chunk_text}')


#: The node an extractor's unreadable model output is kept on, for Quarantine to list, re-run or
#: fix by hand. One per (extractor, node): a re-run that fails again replaces it rather than piling up.
ANOMALY_TYPE = "DataQualityAnomaly"


def _unreadable(raw) -> bool:
    """The model answered, and nothing in the answer parses as JSON. An answer of `{}` or
    `{"records": []}` is a real "found nothing" and is not this."""
    body = str(raw or "").strip()
    return bool(body) and _parse_json(body) == {} and body.replace(" ", "") not in ("{}", "[]")


async def _record_unreadable(workspace_id, template_name, node_id, collection_id, text, raw) -> None:
    """Keep what the model said and what it was reading, so the extraction is recoverable from the
    Quarantine page instead of vanishing as "no records"."""
    try:
        await _repo.upsert_node(
            workspace_id=workspace_id, type=ANOMALY_TYPE,
            node_id=uuid.uuid5(uuid.NAMESPACE_URL, f"anomaly:{workspace_id}:{template_name}:{node_id}"),
            label=f"Template Extraction Failure: {template_name}",
            properties={"error_type": "ValidationError",
                        "error_message": "the model's answer was not valid JSON",
                        "failed_output": str(raw)[:4000], "chunk_text": str(text or "")[:4000],
                        "extractor_type": "template", "template_name": template_name},
            source_ref=node_id, collection_id=collection_id)
    except Exception as e:                            # noqa: BLE001 — recording never fails a run
        log.warning("enrich.anomaly_record_failed", template=str(template_name), error=str(e))


async def _produce_parsed(mech: dict, ctx: dict, text: str, model_spec: dict | None = None,
                          workspace_id=None, node_id=None, collection_id=None, template_name=None):
    """Run a mechanism → the object passed to _apply_emit. LLM returns its raw JSON (so attribute
    merges like $llm.x resolve); deterministic mechanisms return {"records":[...]}.
    `model_spec` (from the Model Registry) routes the LLM call to the chosen provider/model.
    `workspace_id` lets a model-capable Python extractor re-resolve a per-call model by registry name.
    `node_id` is the node being extracted from — it anchors the Python SDK's graph reads
    (`cortex.children()` / `cortex.neighbors()` mean "of THIS node")."""
    ref = str(mech.get("ref") or "")
    if ref in _LLM_REFS or (not ref and mech.get("prompt")):
        raw_prompt = str(mech.get("prompt", "") or "")
        reads_text = any(ph in raw_prompt for ph in ("{text}", "{chunk_text}"))
        if reads_text and not str(text or "").strip():
            return {}
        # The text this call is about fills `{text}` / `{chunk_text}` whenever the node's own context
        # lacks them. Left unfilled, the model was sent a literal "TEXT:\n{text}" and — asked to
        # extract from nothing — invented vendors and policies that no source contains.
        filled = {**ctx}
        if str(text or "").strip():
            filled.setdefault("text", text)
            filled.setdefault("chunk_text", text)
        prompt = _fill(mech.get("prompt", ""), filled)
        if "Return ONLY JSON" not in prompt:
            prompt += ('\n\nReturn ONLY JSON: {"records": [{...}]}\n'
                       'One object per item; {"records": []} if none.')
        raw = await complete(prompt, system="You extract precise structured data. JSON only.",
                             max_tokens=1200, json_mode=True, temperature=0.1, model_spec=model_spec)
        if template_name and workspace_id and _unreadable(raw):
            await _record_unreadable(workspace_id, template_name, node_id, collection_id, text, raw)
        return _parse_json(raw)
    if ref == _ATTR_REF:
        return {"records": []}
    if ref == "seed.literal":
        return {"records": _seed_records(mech)}
    if ref == "unstructured.regex":
        return {"records": _regex_records(mech.get("pattern") or "", text, bool(mech.get("ignore_case")))}
    if ref == "unstructured.dictionary":
        return {"records": _dictionary_records(mech.get("terms") or mech.get("gazetteer_terms") or [], text)}
    if ref == "unstructured.keyword":
        if mech.get("model"):
            prompt = _fill(mech.get("prompt") or _LLM_KEYWORD_PROMPT,
                           {**ctx, "top_n": ctx.get("top_n", mech.get("top_n", 8))})
            raw = await complete(prompt, system="You output concise keywords as JSON.",
                                 max_tokens=300, json_mode=True, temperature=0.1, model_spec=model_spec)
            return _parse_json(raw)
        return {"records": _keywords(text, int(ctx.get("top_n") or mech.get("top_n", 8)))}
    if ref == "unstructured.ner":
        return {"records": await _ner_records(text, model_spec, workspace_id)}
    if ref == "unstructured.python":
        from opencortyx.ingest.extraction.pyextractor import run_python_extractor_async
        inputs = {**(ctx or {}), "text": text}
        recs = await run_python_extractor_async(
            mech.get("code") or "", inputs, workspace_id=workspace_id, model_spec=model_spec,
            model_capable=bool(mech.get("model_capable")),
            node_id=node_id, collection_id=collection_id,
            prompt=_fill(mech.get("prompt") or "", ctx or {}),
            # and its own structured configuration (comparison passes, thresholds, …) so behaviour
            # stays editable DATA on the card rather than constants buried in the snippet
            config={k: v for k, v in mech.items()
                    if k not in ("code", "prompt", "language") and not k.startswith("_")})
        return {"records": recs}
    return {"records": []}


def _select_types(source) -> Optional[list]:
    """Resolve the template's `source.select` to a list of node types, or None for 'any'."""
    if not isinstance(source, dict):
        return None
    sel = source.get("select")
    if sel in (None, "*", ["*"]):
        return None
    if isinstance(sel, str):
        return [sel]
    if isinstance(sel, list):
        return [s for s in sel if s and s != "*"] or None
    return None


async def _node_ctx(node) -> dict:
    """Placeholder values available to the prompt: the node's own attributes + conveniences.
    For Table/Source-like parents we also surface child column/chunk names as `columns`."""
    props = node.properties or {}
    ctx = {"id": str(node.id), "type": node.type, "node_type": node.type,
           "label": node.label or "", **{k: v for k, v in props.items()}}
    ctx["attributes"] = json.dumps({k: v for k, v in props.items()
                                    if not str(k).startswith("__")}, default=str)[:4000]
    ctx.setdefault("chunk_text", props.get("text") or props.get("__text") or node.label or "")
    # is_asset_node, not a literal type list: an anchor rule can retype an asset (Source → Aadhaar)
    # and its columns/chunks are still exactly what the prompt needs.
    from opencortyx.domain.ontology import is_asset_node
    if is_asset_node(node):
        kids = await _repo.find(node.workspace_id, source_refs=[node.id])
        cols = []
        for k in kids:
            kp = k.properties or {}
            cols.append(kp.get("physical_column") or k.label)
        if cols:
            ctx.setdefault("columns", json.dumps(cols[:200]))
            ctx.setdefault("table_name", props.get("table_name") or node.label)
        from opencortyx.domain.ontology import base_type_of
        _derived = {"Summary", "Topic", "Keyword", "Question", "Community"}
        chunks = [k for k in kids if base_type_of(k) in ("Chunk", "Record")]
        if not chunks:
            chunks = [k for k in kids
                      if ((k.properties or {}).get("text") or "").strip()
                      and base_type_of(k) not in _derived and not k.type.endswith("Profile")]
        words = sum(len(((c.properties or {}).get("text") or "").split()) for c in chunks)
        summary = next(((k.properties or {}).get("text") for k in kids
                        if base_type_of(k) == "Summary"), "") or ""
        body = "\n\n".join(((c.properties or {}).get("text") or "") for c in chunks[:3]).strip()
        excerpt = "\n\n".join(p for p in (summary.strip(), body) if p)[:4000]
        ctx.setdefault("title", node.label or props.get("filename") or "asset")
        ctx.setdefault("excerpt", excerpt)
        ctx.setdefault("text", excerpt)                 # so {text} works in an asset prompt too
        ctx.setdefault("stats", json.dumps({
            "chunks": len(chunks), "words": words, "downstream": len(kids),
            "topics": sum(1 for k in kids if k.type == "Topic"),
            "language": props.get("language") or props.get("lang") or "—",
            "type": props.get("mime_type") or props.get("content_type") or props.get("mime") or "—"}))
    return ctx


def _extractor_text(node, ctx: dict) -> str:
    """What an extractor reads from a node: its prose, else the document the substrate stores for
    every node (`__text` — a row's or a record's fields as text), and only then its bare label."""
    props = node.properties or {}
    return str(props.get("text") or ctx.get("chunk_text") or ctx.get("excerpt")
               or props.get(_node_document.STORED_KEY) or node.label or "")


def _fill(prompt: str, ctx: dict) -> str:
    """Substitute {placeholders} from ctx. Robust to stray braces (manual replace, not .format)."""
    out = prompt or ""
    for k, v in ctx.items():
        out = out.replace("{" + str(k) + "}", _s(v))
    return out


def _is_placeholder(v) -> bool:
    """A value that is not a real extraction: the PROMPT's placeholder echoed back ("<the summary>",
    "<label>"), or a stray fragment of JSON syntax ("{", "}", "..."). Smaller local models routinely
    echo the skeleton instead of filling it in, or emit a lone brace when they have nothing to say;
    without this the graph gets Summary/Topic nodes labelled "<the summary>" — or, seen live, an
    ExecutiveSummary whose entire text is "{"."""
    s = str(v or "").strip()
    if s in ("{", "}", "[", "]", "...", "…", "{}", "[]", '""'):
        return True
    return len(s) > 1 and s.startswith("<") and s.endswith(">") and "\n" not in s


def _clean_records(recs: list) -> list:
    """Drop placeholder-echo values, then any record left with no real content."""
    out = []
    for r in recs:
        if not isinstance(r, dict):
            continue
        cleaned = {k: v for k, v in r.items() if not _is_placeholder(v)}
        if any(v not in (None, "", [], {}) for v in cleaned.values()):
            out.append(cleaned)
    return out


def _records(parsed) -> list:
    """Normalise an LLM JSON payload into a list of record dicts for node creation."""
    if isinstance(parsed, list):
        return _clean_records([r for r in parsed if isinstance(r, dict)])
    if isinstance(parsed, dict):
        if isinstance(parsed.get("records"), list):
            return _clean_records([r for r in parsed["records"] if isinstance(r, dict)])
        for v in parsed.values():                       # first list-of-dicts value
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                return _clean_records(v)
        # a single flat object with content → treat as one record
        if any(val not in (None, "", [], {}) for val in parsed.values()):
            return _clean_records([parsed])
    return []


def _from_production(parsed, field: str):
    """Read one field out of whatever a mechanism produced — top level, or its first record.

    EVERY mechanism's output arrives here as `{"records": [...]}` unless it is an LLM returning a
    flat object: `run_python_extractor_async` normalises a python extractor's return (a list, a bare
    dict, or {"records": [...]}) into that one shape, and the regex / dictionary / keyword branches
    of `_produce_parsed` build it directly. So the fields an author sees in their own `extract()`
    return sit one level DOWN from where this used to look.

    Consequence before this: `emit.attributes` with `on: "source"` — the Studio's "Enrich source" —
    could not read a deterministic extractor at ALL. Not a token problem; `$normalized_date`,
    `$llm.normalized_date` and `$out.normalized_date` all resolved to "" against
    `{"records": [{"normalized_date": "2022-03-03"}]}`, and the empty-value filter then dropped the
    attribute without a word. Measured on all four spellings. That is why the only built-ins that
    enrich a source node use the `unstructured.attributes` mechanism instead — they route around it.

    Falling through to `records[0]` is not a guess about which record is meant: `on: "source"` writes
    ONE value onto ONE node, so a spec pointing at a multi-record production has already said it
    wants a single value. Anything that genuinely needs every record emits NODES.
    """
    if not isinstance(parsed, dict):
        return ""
    if field in parsed:
        return parsed[field]
    recs = parsed.get("records")
    if isinstance(recs, list) and recs and isinstance(recs[0], dict):
        return recs[0].get(field, "")
    return ""


def _resolve(token, parsed: dict, ctx: dict):
    """Resolve a `set` value: $field / $llm.field (the mechanism's output), $source.field (the node),
    $llm|$record|$out (the whole output object), {ph}, or a literal.

    ONE `$` SYNTAX, EVERY SLOT. A bare `$normalized_date` used to resolve to "" here and to
    `record["normalized_date"]` in `_fill_field` — the resolver every OTHER slot uses (edge
    endpoints, node labels, `type_field`, and attribute merges onto a resolved node). So the same
    token silently did nothing in `emit.attributes` with `on: "source"` and worked everywhere else,
    which is not a syntax an author can hold in their head. It now means the same thing in both.

    The "" was itself a fix for a worse bug: `$topic` fell through to the ctx branch with no field
    and handed back the ENTIRE ctx as one attribute value — silent, enormous, and self-compounding,
    because ctx is rebuilt from the node's properties on the next run. Reading a bare `$x` as the
    mechanism's output fixes that case too, and does it by giving the author what they meant.

    `$llm.` is kept as an alias, not a requirement: for `unstructured.python`, `regex`, `dictionary`
    and `keyword` there is no model and no prompt anywhere in the extractor, so a card demanding
    `$llm.` was naming a component that is not involved. `$out.` says the true thing for all of them.
    """
    if not isinstance(token, str):
        return token
    if token in ("$llm", "$record", "$out"):
        if isinstance(parsed, dict) and "records" in parsed:
            recs = parsed.get("records")
            if not isinstance(recs, list):
                return parsed
            merged: dict = {}
            for r in recs:
                if not isinstance(r, dict):
                    continue
                for k, v in r.items():
                    if k not in merged or merged[k] == v:
                        merged[k] = v
                    elif isinstance(merged[k], str) and isinstance(v, str):
                        parts = [p.strip() for p in str(merged[k]).split(",")]
                        if v.strip() not in parts:
                            merged[k] = "%s, %s" % (merged[k], v)
                    else:
                        merged[k] = v
            return merged
        return parsed
    if token.startswith("$"):
        root, _, field = token[1:].partition(".")
        if not field:
            # bare `$x` — the mechanism's output, same as `_fill_field` reads it
            return _from_production(parsed, root)
        if root in ("llm", "record", "out"):
            return _from_production(parsed, field)
        return ctx.get(field, "") if isinstance(ctx, dict) else ""
    if "{" in token:
        return _fill(token, ctx)
    return token


def _label_for(rec: dict, ntype: str) -> str:
    return str(rec.get("name") or rec.get("title") or rec.get("label")
               or next((v for v in rec.values() if isinstance(v, str) and v.strip()), ntype))[:200]


def template_attr_catalog(template) -> list:
    """Derive a template's toggleable attributes from its `emit` (NOT hardcoded) — so custom
    extractors get the same per-stage enable/disable as built-ins. Union of the keys it merges onto
    the source (emit.attributes[].set) and the attributes of any nodes it emits (emit.nodes[].attributes)."""
    emit = (getattr(template, "emit", None) or {})
    keys, seen = [], set()

    def _add(k):
        k = str(k or "").strip()
        if k and k not in seen and not k.startswith("__"):
            seen.add(k)
            keys.append(k)

    for a in (emit.get("attributes") or []):
        if isinstance(a, dict):
            s = a.get("set")
            if isinstance(s, dict):
                for k in s.keys():
                    _add(k)
            elif isinstance(s, str):                 # set: "$llm" → whole object, keys unknown
                continue
    for spec in (emit.get("nodes") or []):
        if isinstance(spec, dict):
            for k in (spec.get("attributes") or []):
                _add(k)
    return [{"key": k, "label": k.replace("_", " ").strip().title(), "default": True} for k in keys]


def _override_active(ov) -> bool:
    """True if a per-attribute override is usable — an LLM prompt, or a non-LLM mechanism + its input."""
    if not isinstance(ov, dict):
        return False
    if (ov.get("prompt") or "").strip():
        return True
    mech = (ov.get("mechanism") or "").lower()
    if mech == "regex" and (ov.get("pattern") or "").strip():
        return True
    if mech == "copy" and (ov.get("input") or "").strip():
        return True
    if mech == "constant" and ov.get("value") not in (None, ""):
        return True
    return False


async def _gen_attr_value(workspace_id, override: dict, attr_key: str, ctx: dict):
    """Per-attribute generation: mint THIS attribute with its OWN mechanism + model. Lets one
    extractor produce different attributes different ways:
      • llm (default) — its own prompt + model
      • regex        — `pattern` over the `input` source field (or all text)
      • copy         — copy the value of an `input` source field
      • constant     — a literal `value`
    `input` selects WHICH source attribute feeds non-LLM mechanisms. Returns None on failure/no-match."""
    mech = (override.get("mechanism") or "llm").lower()
    inp_field = override.get("input")
    src_val = ctx.get(inp_field) if inp_field else None

    if mech == "copy":
        return src_val
    if mech == "constant":
        return override.get("value")
    if mech == "regex":
        import re
        text = str(src_val if inp_field else " ".join(str(v) for v in (ctx or {}).values() if isinstance(v, str)))
        try:
            m = re.search(override.get("pattern") or "", text)
        except Exception:
            return None
        if not m:
            return None
        return m.group(1) if m.groups() else m.group(0)

    # default: LLM with this attribute's own prompt + model
    prompt = _fill(override.get("prompt") or "", ctx)
    if not prompt.strip():
        return None
    spec = await resolve_model(workspace_id, "extraction", override.get("model"))
    wants_json = "json" in prompt.lower()
    try:
        raw = await complete(prompt, max_tokens=600, temperature=0.1, model_spec=spec,
                             json_mode=wants_json)
    except Exception:
        return None
    raw = (raw or "").strip()
    parsed = _parse_json(raw)
    if isinstance(parsed, dict) and parsed:
        if attr_key in parsed:
            return _clean_value(parsed[attr_key])
        vals = [v for v in parsed.values() if v not in (None, "", [], {})]
        if vals:
            return _clean_value(vals[0])
    elif isinstance(parsed, list) and parsed:
        return parsed
    return _clean_value(raw)


def _clean_value(v):
    """An attribute value, or None when the model returned a placeholder / bare JSON punctuation."""
    return None if (v is None or _is_placeholder(v) or str(v).strip() == "") else v


async def _gen_attr_values(workspace_id, overrides: dict, keys, ctx: dict) -> dict:
    """Values for SEVERAL attributes at once — {key: value} for the ones an override produced.

    Attribute-level authoring must not mean one network round-trip per field. A table with four
    semantic fields and twenty columns would cost 64 calls where a single combined prompt cost one,
    and that arithmetic is what kept "one prompt writes everything" alive. So: attributes whose
    overrides are LLM AND share a model are folded into ONE call — each field's own prompt becomes a
    section, the answer comes back keyed by field. Authoring stays per-attribute (own prompt, own
    model, own mechanism); only the transport is shared.

    A field missing from the combined answer falls back to its own call, so folding can only save
    calls, never lose a value. Non-LLM overrides (regex / copy / constant) never call anything."""
    out: dict = {}
    groups: dict = {}
    for k in keys:
        ov = overrides.get(k)
        if not _override_active(ov):
            continue
        if (ov.get("mechanism") or "llm").lower() != "llm":
            out[k] = await _gen_attr_value(workspace_id, ov, k, ctx)      # regex / copy / constant
        else:
            groups.setdefault(ov.get("model") or "", []).append((k, ov))

    for model, items in groups.items():
        if len(items) == 1:                                          # nothing to fold
            k, ov = items[0]
            out[k] = await _gen_attr_value(workspace_id, ov, k, ctx)
            continue
        spec = await resolve_model(workspace_id, "extraction", model or None)
        fields = ", ".join(f'"{k}"' for k, _ in items)
        parts = ["You will produce SEVERAL fields about the same subject. Follow each field's "
                 "instructions exactly.",
                 f"Return ONLY one JSON object whose keys are exactly: {fields}. Use null for a "
                 "field you cannot determine."]
        for k, ov in items:
            parts.append(f"\n### FIELD {k}\n{_fill(ov.get('prompt') or '', ctx)}")
        parsed = {}
        try:
            raw = await complete("\n".join(parts), system="You output one JSON object. JSON only.",
                                 max_tokens=1200, json_mode=True, temperature=0.1, model_spec=spec)
            parsed = _parse_json(raw)
        except Exception:                                            # noqa: BLE001 — fall back below
            parsed = {}
        for k, ov in items:
            v = _clean_value(parsed.get(k)) if isinstance(parsed, dict) else None
            out[k] = v if v is not None else await _gen_attr_value(workspace_id, ov, k, ctx)
    return out


# ── Declarative graph primitives (so relationship/entity extractors need NO per-extractor python) ──
def _fill_field(val, rec):
    """Substitute a `$field` reference from the LLM record; pass literals through."""
    if isinstance(val, str) and val.startswith("$"):
        return (rec or {}).get(val[1:], "")
    return val


#: An endpoint that expands to a list (match on a list-valued field, `node_id` holding several ids)
#: is bounded here so one bad record can't fan out into thousands of edges.
_MAX_ENDPOINT_FANOUT = 200


def _endpoint_type(ep) -> str:
    """The node type an edge endpoint names, if it names one ("Vendor", {"match": "Supplier"})."""
    if isinstance(ep, str):
        return "" if ep in ("source", "emitted", "org") else ep
    if isinstance(ep, dict):
        return str(ep.get("match") or ep.get("node") or ep.get("node_type") or ep.get("type") or "")
    return ""


def _emit_edges(emit: dict) -> list:
    """`emit.edges` with the Extractor Studio's "an existing node, found by a field" endpoint read.

    The Studio saves that choice as `from|to: "match_on"` plus `<side>MatchNode/Field/Value`, and
    nothing below understood it: the endpoint resolved to nothing and every such edge was dropped.
    Rewritten here into the `{"match", "on"}` spec `_resolve_endpoints` documents, so extractors
    already saved that way start linking without being re-saved."""
    out = []
    for e in (emit or {}).get("edges") or []:
        if isinstance(e, dict) and "match_on" in (e.get("from"), e.get("to")):
            e = dict(e)
            for side in ("from", "to"):
                if e.get(side) == "match_on":
                    node = e.get(f"{side}MatchNode") or ""
                    field = e.get(f"{side}MatchField") or "id"
                    value = e.get(f"{side}MatchValue") or ""
                    e[side] = {"match": node, "on": {field: value}} if node and value else None
        out.append(e)
    return out


def _named_id(workspace_id, node_type: str, name: str, collection_id=None):
    """The id of the one node of `node_type` called `name` (per collection when given)."""
    if not collection_id:
        # the same id the text reader gives it (ingest/extraction/text_reader.py): a book an extractor
        # names and the same book the reader finds in a sentence are one node, not two
        from opencortyx.ingest.extraction.text_reader import entity_id
        return entity_id(workspace_id, str(node_type), str(name))
    key = str(name).strip().lower()
    return _det_id(workspace_id, str(node_type).lower(), f"{collection_id}:{key}")


async def named_node(workspace_id, name: str, *, node_type: str = "Entity", entity_type=None, collection_id=None):
    """The node called `name`, created the first time. It has the id an emitted named node gets, so an
    edge endpoint and an emitted entity with the same name are one node. No lookup and no matching:
    which DIFFERENT names are the same thing is decided after the asset, by the resolution step."""
    node_type = entity_type or node_type                   # a typed name ("Person") is a node of that type
    nid = _named_id(workspace_id, node_type, name, collection_id)
    if await _repo.get(workspace_id, nid) is None:
        await _repo.upsert_node(node_id=nid, workspace_id=workspace_id, type=node_type, label=str(name).strip()[:200],
                                entity_type=entity_type or None, collection_id=collection_id, confidence=0.85,
                                properties={"name": str(name).strip(), "__identity": "entity"})
    return nid


async def _resolve_endpoints(workspace_id, ep, rec, source_node, emitted_node, collection_id, rec_nodes: Optional[dict] = None) -> list:
    """Resolve an edge endpoint spec → a LIST of (node_id, node_type). `ep` is one of:

      "source"                                   the node being extracted from
      "emitted"                                  the node this record produced
      "NodeTypeName"                             the node of this type emitted for this record
      "org"                                      the Organization node (its id IS the org id)
      {"resolve":"entity","name":"$f","type":"$f"}   the node with that name, CREATED if new (`named_node`)
      {"find":"entity","name":"$f"}              canonical entity, match-ONLY (never creates)
      {"match":"Type","on":{field:"$f"}}         existing node(s) by property value — `field` may be
                                                 "id"/"label"; a LIST value expands to many endpoints
      {"node_id":"$f"}                           node id(s) straight from a record field

    A list is returned because wiring is one-to-many: a segment depends on several columns, a
    community contains several members. Callers draw one edge per resolved endpoint."""
    if ep in (None, "source"):
        if source_node is not None and _is_virtual(source_node):
            # A chunk read from the data store is not a node: what links to "the source" links to
            # its document, and the evidence ledger keeps the exact chunk.
            return [(source_node.source_ref, "Source")] if source_node.source_ref else []
        return [(source_node.id, _vtype(source_node))] if source_node else []
    if ep == "emitted":
        return [(emitted_node.id, _vtype(emitted_node))] if emitted_node else []
    if rec_nodes:
        if isinstance(ep, str) and ep in rec_nodes:
            n = rec_nodes[ep]
            return [(n.id, _vtype(n))] if n is not None else []
        if isinstance(ep, dict):
            target = ep.get("node") or ep.get("node_type") or ep.get("type")
            if target and target in rec_nodes:
                n = rec_nodes[target]
                return [(n.id, _vtype(n))] if n is not None else []
    if ep == "org":
        row = await _repo.get(workspace_id, workspace_id)
        return [(row.id, _vtype(row))] if row is not None else []
    if isinstance(ep, dict):
        if ep.get("resolve") or ep.get("dedup"):
            name = str(_fill_field(ep.get("name", "$name"), rec) or "").strip()
            if not name:
                return []
            etype = _fill_field(ep.get("type"), rec) if ep.get("type") else None
            return [(await named_node(workspace_id, name, entity_type=etype, collection_id=collection_id), "entity")]
        if ep.get("find") == "entity":
            name = str(_fill_field(ep.get("name", "$name"), rec) or "").strip()
            if not name:
                return []
            from opencortyx.ingest.extraction.entities import is_identity
            scope = [collection_id] if collection_id else None
            norm = normalize_name(name)
            row = next((n for n in await _repo.find(workspace_id, label=name, collection_ids=scope)
                        if is_identity(n)), None)
            if row is None and norm:
                row = next((n for n in await _repo.find(workspace_id, name_key=norm, collection_ids=scope)
                            if is_identity(n)), None)
            if row is None and norm:
                from opencortyx.retrieval.l1_candidates.lexical import document_filter
                hits = await _repo.vectors.scroll(document_filter(workspace_id, scope, names_phrase=name),
                                                  limit=200, fields=["node_id"])
                row = next((n for n in await _repo.get_many(workspace_id, [h["node_id"] for h in hits])
                            if is_identity(n) and any(normalize_name(str(a)) == norm
                                                      for a in ((n.properties or {}).get("aliases") or []))), None)
            return [(row.id, _vtype(row))] if row is not None else []
        if ep.get("node_id") is not None:
            raw = _fill_field(ep.get("node_id"), rec)
            vals = raw if isinstance(raw, (list, tuple)) else [raw]
            ids = [i for i in (_uuid_or_none(v) for v in list(vals)[:_MAX_ENDPOINT_FANOUT]) if i]
            return [(n.id, _vtype(n)) for n in await _repo.get_many(workspace_id, ids)]
        if ep.get("match"):
            field, raw = next(iter((ep.get("on") or {}).items()), (None, None))
            if not field:
                return []
            filled = _fill_field(raw, rec)
            vals = [str(v).strip() for v in (filled if isinstance(filled, (list, tuple)) else [filled])
                    if str(v or "").strip()][:_MAX_ENDPOINT_FANOUT]
            if not vals:
                return []
            if field == "id":
                ids = [i for i in (_uuid_or_none(v) for v in vals) if i]
                rows = [n for n in await _repo.get_many(workspace_id, ids) if n.type == ep["match"]]
            elif field == "label":
                rows = []
                for v in vals:
                    rows += await _repo.find(workspace_id, label=v, exact_types=[ep["match"]])
            else:
                wanted = {v.lower() for v in vals}
                rows = [n for n in await _repo.find(workspace_id, exact_types=[ep["match"]])
                        if str((n.properties or {}).get(field) or "").lower() in wanted]
            return [(r.id, _vtype(r)) for r in rows[:_MAX_ENDPOINT_FANOUT]]
    return []


def _vtype(node) -> str:
    """The type an edge is VALIDATED against: what the node was ingested as, seeing through an anchor
    retype (Source → Aadhaar). The ontology constrains the types the platform owns, so validating a
    retyped asset by its new name rejected edges the platform itself had just produced."""
    if node is None:
        return ""
    return (node.properties or {}).get("__base_type") or node.type


def _uuid_or_none(v):
    if isinstance(v, uuid.UUID):
        return v
    try:
        return uuid.UUID(str(v))
    except (ValueError, AttributeError, TypeError):
        return None


async def _redge(*, workspace_id, source_id, target_id, type, properties=None) -> bool:
    """Write an edge; distinct `relation` values between one pair are distinct edges."""
    if not source_id or not target_id or str(source_id) == str(target_id):
        return False
    return await _repo.add_edge(workspace_id=workspace_id, source_id=source_id, target_id=target_id,
                                type=type, properties=properties or {})


# ── Relationship extraction over an anchor's EXISTING neighbors ──────────────────────────────────
def _node_text(n) -> str:
    return ((n.properties or {}).get("text") or n.label or str(n.id))[:160]


def _type_matches(n, t: str) -> bool:
    """Does node `n` satisfy the requested From/To type `t`? An identity node carries the type its
    extractor CARD declared plus an `entity_type` SUBTYPE (Person/Organization/…), and is recognised
    by its `__identity` stamp. So 'entity' matches any identity node whatever its declared type,
    while a subtype like 'Organization' matches on entity_type. Other node types (Topic, Event, …)
    match by `n.type` directly."""
    if not t or n.type == t:
        return True
    if (n.properties or {}).get("__identity"):
        # An identity node satisfies a generic "entity" request whatever the card named its type,
        # and satisfies a SUBTYPE request (Person/Organization/…) through `entity_type`.
        if str(t).strip().lower() == "entity":
            return True
        sub = getattr(n, "entity_type", None) or (n.properties or {}).get("type") or ""
        return str(sub).strip().lower() == str(t).strip().lower()
    return False


async def _relate_pairs(mech: dict, text: str, from_cands: list, to_cands: list,
                        model_spec: dict | None) -> list:
    """Decide which (from, to) entity pairs relate, given the anchor's text as context. Returns
    [(from_node, to_node, relation_label|None), ...].
      • relate.fixed — connect every From × To (deterministic, no LLM).
      • relate.regex — connect pairs whose BOTH labels appear in the anchor text (co-occurrence).
      • relate.llm   — ask the model; entities are referenced by INDEX ([0],[1],…) so duplicate
                       labels never collide, and indices map straight back to the real node ids."""
    ref = str(mech.get("ref") or "relate.llm")
    if ref == "relate.fixed":
        return [(a, b, None) for a in from_cands for b in to_cands if a.id != b.id]
    if ref == "relate.regex":
        low = (text or "").lower()
        return [(a, b, None) for a in from_cands if (a.label or "").lower() in low
                for b in to_cands if a.id != b.id and (b.label or "").lower() in low]

    # relate.llm — index-based, robust to duplicate labels
    from_list = "\n".join(f"[{i}] {_node_text(n)}" for i, n in enumerate(from_cands))
    to_list = "\n".join(f"[{j}] {_node_text(n)}" for j, n in enumerate(to_cands))
    base = (mech.get("prompt") or "Identify which FROM items are related to which TO items.").strip()
    prompt = (f"{base}\n\nCONTEXT:\n{(text or '')[:4000]}\n\n"
              f"FROM ({mech.get('from_type', 'entity')}):\n{from_list}\n\n"
              f"TO ({mech.get('to_type', 'entity')}):\n{to_list}\n\n"
              'Return ONLY JSON {"pairs": [[from_index, to_index, "relation"], ...]} using the bracketed '
              "indices above. Include a pair only when the context supports it; return [] if none.")
    try:
        raw = await complete(prompt, system="You extract relationships between listed items. JSON only.",
                             max_tokens=900, json_mode=True, temperature=0.1, model_spec=model_spec)
    except Exception:
        return []
    parsed = _parse_json(raw)
    rows = parsed.get("pairs") if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else None)
    out = []
    for p in (rows or []):
        try:
            i, j = int(p[0]), int(p[1])
            rel = (str(p[2]).strip() if len(p) > 2 and p[2] else None)
            if 0 <= i < len(from_cands) and 0 <= j < len(to_cands):
                out.append((from_cands[i], to_cands[j], rel))
        except Exception:
            continue
    return out


async def _run_relate(workspace_id: uuid.UUID, template, *, collection_id=None, limit: int = _RUN_LIMIT,
                      source_key: str | None = None, node_ids: list | None = None,
                      model_spec=None) -> dict:
    """Relationship extraction. The template's `source` selects ANCHOR nodes (e.g. Chunk) that serve
    as CONTEXT; for each anchor we gather the entities already attached to it (via `mechanism.via`,
    default MENTIONS), filter to `from_type`/`to_type` (which may be the same), decide which pairs
    relate (`_relate_pairs`), and draw `from -relation-> to` edges between those EXISTING nodes —
    validated against the ontology and deduped. Nothing new is minted; we only connect what's there."""
    mech = template.mechanism or {}
    from_type = mech.get("from_type")
    to_type = mech.get("to_type")
    relation = (mech.get("relation") or "RELATES_TO").strip() or "RELATES_TO"
    via = (mech.get("via") or "MENTIONS").strip() or "MENTIONS"
    both = (mech.get("direction") or "from_to") == "both"
    totals = {"nodes_seen": 0, "candidates": 0, "edges": 0, "skipped_edges": 0, "errors": 0,
              "edge_types": {}}
    if not from_type or not to_type:
        return {"status": "error", "reason": "from_type/to_type required", "name": template.name}

    anchors = await _select_nodes(workspace_id, template, node_ids=node_ids, source_key=source_key, limit=limit)
    async with V2Session() as db:
        for anchor in anchors:
            totals["nodes_seen"] += 1
            try:
                rows = await _repo.neighbors(workspace_id, [anchor.id], edge_types=[via], direction="out")
                from_cands = [n for (_, n) in rows if _type_matches(n, from_type)]
                to_cands = [n for (_, n) in rows if _type_matches(n, to_type)]
                if not from_cands or not to_cands:
                    continue
                totals["candidates"] += len(from_cands) + len(to_cands)
                text = (anchor.properties or {}).get("text") or anchor.label or ""
                for a, b, rel in await _relate_pairs(mech, text, from_cands, to_cands, model_spec):
                    if not a or not b or a.id == b.id:
                        continue
                    ok, _r = await validate_edge(db, workspace_id, etype=relation, source_type=a.type, target_type=b.type)
                    if not ok:
                        totals["skipped_edges"] += 1
                        continue
                    props = {**({"relation": rel} if rel else {}), "__by": template.name}   # producer tag
                    if await _redge(workspace_id=workspace_id, source_id=a.id, target_id=b.id,
                                    type=relation, properties=props):
                        totals["edges"] += 1
                        totals["edge_types"][relation] = totals["edge_types"].get(relation, 0) + 1
                    if both and await _redge(workspace_id=workspace_id, source_id=b.id, target_id=a.id,
                                             type=relation, properties=props):
                        totals["edges"] += 1
                        totals["edge_types"][relation] = totals["edge_types"].get(relation, 0) + 1
            except Exception as e:                       # noqa: BLE001 — isolate per-anchor failures
                totals["errors"] += 1
                log.warning("enrich.relate_failed", template=str(template.name), anchor_id=str(anchor.id), error=str(e))
    return {"status": "ok", "name": template.name, **totals}


async def _select_nodes(workspace_id, template, *, node_ids=None, source_key=None, limit=_RUN_LIMIT) -> list:
    """The nodes a template runs over: an explicit list (a prior DAG step's output), or the types its
    `source.select` names, optionally within one ingested source."""
    if node_ids:
        nodes = await _repo.get_many(workspace_id, node_ids)
        return [n for n in nodes if not source_key or (n.properties or {}).get("source_key") == source_key]
    data = template.source.get("data") if isinstance(template.source, dict) else None
    if data:
        # Input read where it lives - a table, a column, a JSON path, a document's chunks, an entity
        # type's records - not copied into the graph first (data_inputs.py).
        from opencortyx.ingest.extraction.data_inputs import select as _data_inputs
        return await _data_inputs(workspace_id, data, limit=limit)
    return await _repo.find(workspace_id, NodeQuery(types=_select_types(template.source), source_key=source_key,
                                              limit=limit))


from opencortyx.core.db import guarded_step as _guarded  # noqa: E402
from opencortyx.core.substrate.repo import _q_capture  # noqa: E402
from opencortyx.core.observability import get_logger
from opencortyx.domain.control import ExtractionTemplate
from opencortyx.core.inference.registry import resolve_model
from opencortyx.core.substrate.validation import validate_edge

log = get_logger(__name__)


_UNUSABLE_SUBJECT = {
    "i", "me", "my", "we", "us", "you", "he", "she", "they", "them", "it", "him", "her",
    "the speaker", "speaker", "speaker:", "the author", "the person", "someone", "somebody",
    "this person", "the user", "narrator", "", "none", "null", "n/a", "unknown",
}


def _unusable(v) -> bool:
    return str(v or "").strip().lower() in _UNUSABLE_SUBJECT


def _apply_from_source(spec, props, ctx):
    """Fill declared attributes from the SOURCE node's own properties.

    Two declared forms, both on the card:

        from_source: ["speaker"]
            copy the source's `speaker` onto the emitted node under the same name (original form).

        from_source: [{"attr": "subject", "from": "speaker", "when": "unusable"}]
            put the source's `speaker` into the emitted node's `subject`, either always or - with
            `when: "unusable"` - only when the model's own value is a pronoun or empty.

    The second form exists because a Fact whose subject is "the speaker" cannot be resolved to an
    entity, cannot be intersected with another entity's facts, and cannot be traced back to a
    person. The Turn already knows who spoke; asking a model to repeat it is how that gets lost.
    """
    for entry in (spec.get("from_source") or []):
        if isinstance(entry, str):
            v = (ctx or {}).get(entry)
            if v not in (None, "", [], {}):
                props[entry] = v
            continue
        if not isinstance(entry, dict):
            continue
        tgt = entry.get("attr")
        src = entry.get("from") or tgt
        if not tgt:
            continue
        when = str(entry.get("when") or "always").lower()
        if when == "unusable" and not _unusable(props.get(tgt)):
            continue
        v = (ctx or {}).get(src)
        if v not in (None, "", [], {}):
            props[tgt] = v
    return props


def _is_virtual(node) -> bool:
    """A chunk record from the data store, shaped like a node for the extractors but not in the graph."""
    return bool((getattr(node, "properties", None) or {}).get("__virtual"))


def _parent_ref(source_node):
    """What an emitted node points up to: its input node, or for a chunk that is not a graph node,
    that chunk's document."""
    if source_node is None:
        return None
    return source_node.source_ref if _is_virtual(source_node) else source_node.id


def _evidence_unit(source_node, tmpl):
    """The unit an extractor's output rests on, so everything `_apply_emit` writes carries evidence
    (core/dataplane/evidence.py). An ingest that already declared its unit (the Delta path, which
    reads chunks, rows and records from memory) keeps it; otherwise the input node is the unit: a
    chunk, a row or a record node of the asset it came from."""
    import contextlib

    from opencortyx.core.dataplane import evidence
    if evidence.current() is not None or source_node is None:
        return contextlib.nullcontext()
    props = source_node.properties or {}
    asset = props.get("__asset_id") or source_node.source_ref or props.get("source_id")
    if not asset or str(asset) == str(source_node.id):
        return contextlib.nullcontext()
    if props.get("__reading_unit") or props.get("text"):
        kind = "chunk"
    elif props.get("source_table") or props.get("__row_key"):
        kind = "row"
    else:
        kind = "record"
    quote = str(props.get("text") or source_node.label or "")[:300] or None
    return evidence.unit(asset, kind, props.get("__unit_key") or source_node.id, quote=quote,
                         extractor=getattr(tmpl, "name", None), version_ref=props.get("__version_ref"))


async def _apply_emit(db, workspace_id, tmpl, *, source_node, parsed, ctx, collection_id, attrs=None) -> dict:
    with _evidence_unit(source_node, tmpl):
        return await _apply_emit_unit(db, workspace_id, tmpl, source_node=source_node, parsed=parsed,
                                      ctx=ctx, collection_id=collection_id, attrs=attrs)


async def _apply_emit_unit(db, workspace_id, tmpl, *, source_node, parsed, ctx, collection_id,
                           attrs=None) -> dict:
    """`attrs` (optional set) — when provided, only these attribute keys are merged onto the node;
    lets a pipeline stage enable/disable individual attributes an extractor would otherwise emit."""
    counts = {"attributes": 0, "nodes": 0, "edges": 0, "node_types": {}, "edge_types": {}}
    emit = tmpl.emit or {}
    from opencortyx.knowledge import governance
    _off_types = await governance.disabled_types(db, workspace_id)
    def _node_off(nt): return ("node", nt) in _off_types or ("entity", nt) in _off_types
    def _edge_off(et): return ("edge", et) in _off_types
    # per-attribute prompt+model overrides (optional): {attr_key: {prompt, model}} — when present,
    # that attribute is minted by its OWN LLM call instead of the shared template prompt.
    overrides = (tmpl.mechanism or {}).get("attr_overrides") or {}

    for a in (emit.get("attributes") or []):
        if not isinstance(a, dict) or a.get("on", "source") != "source":
            continue
        on = a.get("on", "source")
        when = a.get("when")
        if when and not _resolve(when, parsed, ctx):
            continue
        setspec = a.get("set", {})
        if isinstance(setspec, str):                     # set: "$llm" → merge whole object
            resolved = _resolve(setspec, parsed, ctx)
            resolved = resolved if isinstance(resolved, dict) else {}
        else:
            # every attribute with its OWN mechanism/prompt/model — folded into as few calls as the
            # models allow (see _gen_attr_values); the rest resolve from the shared production.
            minted = await _gen_attr_values(workspace_id, overrides, list((setspec or {}).keys()), ctx)
            resolved = {}
            for k, v in (setspec or {}).items():
                resolved[k] = minted[k] if k in minted else _resolve(v, parsed, ctx)
        resolved = {k: v for k, v in resolved.items()
                    if v not in (None, "", [], {}) and not str(k).startswith("__")}
        if attrs is not None:                            # per-stage attribute enable/disable
            resolved = {k: v for k, v in resolved.items() if k in attrs}
        if on == "source" and source_node is not None and resolved and _is_virtual(source_node):
            # Attributes of a chunk that lives in the data store, not the graph: nothing to write
            # them onto. Counted, so a mapping that relies on them shows it.
            counts["chunk_attributes_skipped"] = counts.get("chunk_attributes_skipped", 0) + len(resolved)
        elif on == "source" and source_node is not None and resolved:
            props = dict(source_node.properties or {})
            props.update(resolved)
            by = list(props.get("__enriched_by") or [])
            if tmpl.name not in by:
                by.append(tmpl.name)
            props["__enriched_by"] = by
            source_node.properties = props
            await _repo.save(source_node)
            await _repo.link_date(workspace_id=workspace_id, node_id=source_node.id, created=False)
            counts["attributes"] += len(resolved)

    # 2) emitted nodes — one per LLM record, per declared node type, all written the same way. A spec
    #    with `resolve`/`dedup` names a thing: one node per name (`named_node`'s id), so the same name
    #    is the same node by id. Which DIFFERENT names are the same thing is the resolution step's job.
    node_specs = emit.get("nodes") or []
    edge_specs = _emit_edges(emit)
    attr_specs = emit.get("attributes") or []
    declared_types = {s.get("type") for s in node_specs if isinstance(s, dict) and s.get("type")}
    needs_recs = bool(node_specs) or any(
        isinstance(e, dict) and (e.get("per") == "record"
                                 or isinstance(e.get("from"), dict) or isinstance(e.get("to"), dict)
                                 or "org" in (e.get("from"), e.get("to"))
                                 or e.get("from") in declared_types or e.get("to") in declared_types
                                 or (isinstance(e.get("from"), dict) and (e.get("from").get("node") in declared_types or e.get("from").get("node_type") in declared_types))
                                 or (isinstance(e.get("to"), dict) and (e.get("to").get("node") in declared_types or e.get("to").get("node_type") in declared_types)))
        for e in edge_specs) or any(
        isinstance(a, dict) and a.get("on", "source") != "source" for a in attr_specs)
    recs = _records(parsed) if needs_recs else []
    emitted = []
    per_record = [None] * len(recs)               # the primary node emitted for each record (edge "emitted")
    per_record_by_type = [{} for _ in recs]        # nodes emitted for each record keyed by declared type
    src_vf = (source_node.properties or {}).get("valid_from") if source_node else None
    src_cd = (source_node.properties or {}).get("created_date") if source_node else None

    for spec in node_specs:
        if not isinstance(spec, dict) or not spec.get("type"):
            continue
        ntype = spec["type"]
        if _node_off(ntype):                       # switched off in the ontology: not written
            counts["skipped_disabled"] = counts.get("skipped_disabled", 0) + 1
            continue
        resolve = spec.get("resolve") or spec.get("dedup")   # `dedup` is the UI-facing alias
        idfields = spec.get("id") or []
        if isinstance(idfields, str):
            idfields = [f.strip() for f in idfields.split(",") if f.strip()]
        is_global = spec.get("scope") == "global"

        order = list(enumerate(recs))

        for ri, rec in order:
            # a node type read from the record ("$type"): each record is written as the type it names
            rtype = (str(_fill_field(ntype, rec) or "").strip() if ntype.startswith("$") else ntype)
            if not rtype or (rtype != ntype and _node_off(rtype)):
                continue
            label = _label_for(rec, rtype)
            if spec.get("label"):                  # explicit label from a record field ($text, …)
                label = (str(_fill_field(spec["label"], rec) or label))[:200]
            if overrides:                          # per-attribute prompt+model overrides (unchanged)
                rec = dict(rec)
                rctx = {**ctx, **{k: v for k, v in rec.items() if isinstance(v, (str, int, float))}, "label": label}
                for ak, ov in overrides.items():
                    if not _override_active(ov) or (attrs is not None and ak not in attrs):
                        continue
                    val = await _gen_attr_value(workspace_id, ov, ak, rctx)
                    if val is not None:
                        rec[ak] = val
            etype = None
            if resolve:                            # a named thing: its name is its label and its key
                label = str(_fill_field(spec.get("name", "$name"), rec) or label).strip()[:200]
                if not label:
                    continue
                etype = _fill_field(spec.get("type_field"), rec) if spec.get("type_field") else None
            # every emitted node: a deterministic id, then an upsert
            key = label if resolve else spec.get("key") or (
                (":".join(_s(rec.get(f)) for f in idfields) or label) if idfields else label)
            nid = (_named_id(workspace_id, rtype, key, collection_id) if resolve
                   else _det_id(workspace_id, rtype.lower(), key.strip().lower()) if is_global
                   else _det_id(workspace_id, rtype.lower(),
                                f"{source_node.id if source_node else 'global'}:{tmpl.name}:{key}"))
            rec_props = (rec if attrs is None else {k: v for k, v in rec.items() if k in attrs or k in idfields})
            props = {**rec_props, "__source": "enriched",
                     # a named thing is the thing itself (`__identity`), not a statement about it (`__template`)
                     **({"__identity": "entity", "__by": tmpl.name} if resolve else {"__template": tmpl.name})}
            _apply_from_source(spec, props, ctx)
            _req = [a for a in (spec.get("require") or []) if a]
            if _req and [a for a in _req if not str(props.get(a) or "").strip()]:
                counts["dropped_incomplete"] = counts.get("dropped_incomplete", 0) + 1
                continue
            inherited = []                               # copied from the source, not extracted
            if src_vf and "valid_from" not in props:     # inherit the source's date (temporal)
                props["valid_from"] = src_vf
                inherited.append("valid_from")
            if src_cd and "created_date" not in props:
                props["created_date"] = src_cd
                inherited.append("created_date")
            if inherited:
                props["__generated"] = inherited
            node = await _repo.upsert_node(node_id=nid, workspace_id=workspace_id, type=rtype, label=label, properties=props,
                entity_type=(etype or None),
                source_ref=None if (is_global or resolve) else _parent_ref(source_node),
                collection_id=collection_id, confidence=0.85)
            if node is not None:
                emitted.append(node)
                if per_record[ri] is None:
                    per_record[ri] = node
                if ri < len(per_record_by_type):
                    per_record_by_type[ri][rtype] = node
                counts["nodes"] += 1
                _nt = node.type or "node"
                counts["node_types"][_nt] = counts["node_types"].get(_nt, 0) + 1

    # 3) edges. A spec with a dict endpoint ({resolve}/{match}) or per:"record" iterates the LLM records
    #    (so a relationship triple wires two RESOLVED entities). Plain source/emitted keeps prior behavior.
    for espec in edge_specs:
        if not isinstance(espec, dict):
            continue
        etype = espec.get("type") or espec.get("rel")
        mode = espec.get("mode") or "relate.fixed"
        if not etype and mode != "relate.llm":
            continue
        frm, to = espec.get("from", "source"), espec.get("to", "emitted")
        is_record_edge = (espec.get("per") == "record" or isinstance(frm, dict) or isinstance(to, dict)
                          or "org" in (frm, to)
                          or frm in declared_types or to in declared_types
                          or (isinstance(frm, dict) and (frm.get("node") in declared_types or frm.get("node_type") in declared_types))
                          or (isinstance(to, dict) and (to.get("node") in declared_types or to.get("node_type") in declared_types)))
        if is_record_edge:
            if _node_off(_endpoint_type(frm)) or _node_off(_endpoint_type(to)):
                # An endpoint's type is switched off, so its nodes were deliberately not written; the
                # edge is skipped with them, not recorded as an unresolved write that failed.
                counts["skipped_disabled"] = counts.get("skipped_disabled", 0) + len(recs)
                continue
            for ri, rec in enumerate(recs):
                if "when" in espec and not _fill_field(espec.get("when"), rec):
                    continue
                rec_nodes = per_record_by_type[ri] if ri < len(per_record_by_type) else {}
                en = per_record[ri] if ri < len(per_record) else None
                sources = await _resolve_endpoints(workspace_id, frm, rec, source_node, en, collection_id, rec_nodes=rec_nodes)
                targets = await _resolve_endpoints(workspace_id, to, rec, source_node, en, collection_id, rec_nodes=rec_nodes)
                if not sources or not targets:
                    await _q_capture(workspace_id, "edge",
                                     {"source_id": str((sources or [("", "")])[0][0]),
                                      "target_id": str((targets or [("", "")])[0][0]),
                                      "type": etype or "RELATES_TO", "template": tmpl.name,
                                      "from_spec": frm, "to_spec": to,
                                      "unresolved": ("source" if not sources else "target"),
                                      "record": {k: v for k, v in (rec or {}).items()
                                                 if isinstance(v, (str, int, float, bool))},
                                      "source_node_id": str(getattr(source_node, "id", "") or "")},
                                     reason="endpoint_unresolved", retryable=False, asset=tmpl.name)
                    continue
                rec_etype = str(_fill_field(etype, rec) or etype or "").strip() if (etype and etype.startswith("$")) else (etype or "")
                if mode == "relate.llm":
                    if not rec_etype or espec.get("prompt"):
                        if not rec_etype:
                            rec_etype = str(rec.get("predicate") or rec.get("relation") or "").strip()
                        if not rec_etype or espec.get("prompt"):
                            root_text = ((source_node.properties or {}).get("text") or (source_node.properties or {}).get("content") or getattr(source_node, "label", "") or "")
                            s_label = _endpoint_label(frm, rec, "Sample", rec.get("label"), rec_nodes)
                            t_label = _endpoint_label(to, rec, "Sample", rec.get("label"), rec_nodes)
                            edge_prompt = espec.get("prompt")
                            if edge_prompt:
                                p_text = f"{edge_prompt}\n\nCONTEXT:\n{root_text[:3000]}\n\nFROM: {s_label}\nTO: {t_label}\n\nReturn ONLY the relationship (concise phrase or label)."
                            else:
                                p_text = f"Extract the exact relationship or predicate connecting FROM to TO from the context.\n\nCONTEXT:\n{root_text[:3000]}\n\nFROM: {s_label}\nTO: {t_label}\n\nReturn ONLY the relationship (concise phrase or label)."
                            try:
                                edge_model = espec.get("model") or (tmpl.mechanism or {}).get("model")
                                edge_spec = await resolve_model(workspace_id, "generation", edge_model) if edge_model else None
                                raw_rel = await complete(p_text, system="You extract relationships between entities. Return ONLY the relationship or predicate phrase.",
                                                          max_tokens=60, json_mode=False, temperature=0.1, model_spec=edge_spec)
                                gen_rel = str(raw_rel or "").strip().strip('"').strip("'")
                                if gen_rel:
                                    rec_etype = gen_rel
                            except Exception:
                                pass
                if not rec_etype:
                    rec_etype = "RELATES_TO"
                if _edge_off(rec_etype):           # switched off in the ontology: not written
                    counts["skipped_disabled"] = counts.get("skipped_disabled", 0) + 1
                    continue
                # One edge per resolved endpoint: an endpoint spec may legitimately expand to several
                # nodes (a segment depending on several columns, a community holding several members).
                for (sid, st), (tid, tt) in ((s, t) for s in sources for t in targets):
                    ok, _r = await validate_edge(db, workspace_id, etype=rec_etype, source_type=st, target_type=tt)
                    if not ok:
                        counts["skipped_edges"] = counts.get("skipped_edges", 0) + 1
                        await _q_capture(workspace_id, "edge",
                                         {"source_id": str(sid), "target_id": str(tid), "type": rec_etype,
                                          "source_type": st, "target_type": tt, "template": tmpl.name},
                                         reason="rejected_by_ontology", retryable=False, asset=tmpl.name)
                        continue
                    props = {k: _fill_field(v, rec) for k, v in (espec.get("properties") or {}).items()}
                    props = {k: v for k, v in props.items() if v not in (None, "")}
                    if await _redge(workspace_id=workspace_id, source_id=sid, target_id=tid, type=rec_etype,
                                    properties={**(props or {}), "__by": tmpl.name}):   # producer tag
                        counts["edges"] += 1
                        counts["edge_types"][rec_etype] = counts["edge_types"].get(rec_etype, 0) + 1
        else:
            targets = emitted or ([source_node] if source_node else [])
            for en in targets:
                s_node = source_node if (frm == "source" and source_node) else en
                t_node = en if to == "emitted" else (source_node if source_node else en)
                # A record read from the data store is not a node: what links to it links to its
                # document (as `_resolve_endpoints` does), the evidence ledger keeping the exact row.
                if s_node is not None and _is_virtual(s_node):
                    s_node = await _repo.get(workspace_id, s_node.source_ref) if s_node.source_ref else None
                if t_node is not None and _is_virtual(t_node):
                    t_node = await _repo.get(workspace_id, t_node.source_ref) if t_node.source_ref else None
                if s_node is None or t_node is None:
                    continue
                ok, _r = await validate_edge(db, workspace_id, etype=etype, source_type=_vtype(s_node),
                                             target_type=_vtype(t_node))
                if not ok:
                    counts["skipped_edges"] = counts.get("skipped_edges", 0) + 1
                    await _q_capture(workspace_id, "edge",
                                     {"source_id": str(s_node.id), "target_id": str(t_node.id),
                                      "type": etype, "source_type": s_node.type,
                                      "target_type": t_node.type, "template": tmpl.name},
                                     reason="rejected_by_ontology", retryable=False, asset=tmpl.name)
                    continue
                eprops = {"__by": tmpl.name}                                    # producer tag
                if await _uedge(workspace_id=workspace_id, source_id=s_node.id, target_id=t_node.id,
                                type=etype, properties=eprops):
                    counts["edges"] += 1
                    counts["edge_types"][etype] = counts["edge_types"].get(etype, 0) + 1

    for a in attr_specs:
        if not isinstance(a, dict):
            continue
        on = a.get("on", "source")
        if on == "source":
            continue                                     # handled in step 1
        setspec = a.get("set") or {}
        if not isinstance(setspec, dict):
            continue
        for ri, rec in enumerate(recs):
            if "when" in a and not _fill_field(a.get("when"), rec):
                continue
            targets = await _resolve_endpoints(workspace_id, on, rec, source_node,
                                               per_record[ri], collection_id)
            if not targets:
                await _q_capture(workspace_id, "node", {"template": tmpl.name, "on": _s(on)},
                                 reason="attribute_target_unresolved", retryable=False,
                                 asset=tmpl.name)
                continue
            resolved = {k: _fill_field(v, rec) for k, v in setspec.items()}
            confidence = resolved.pop("__confidence", None)
            resolved = {k: v for k, v in resolved.items()
                        if v not in (None, "", [], {}) and not str(k).startswith("__")}
            if attrs is not None:                        # per-stage attribute enable/disable
                resolved = {k: v for k, v in resolved.items() if k in attrs}
            if not resolved and confidence is None:
                continue
            for nid, _ntype in targets:
                node = await _repo.get(workspace_id, nid)
                if node is None:
                    continue
                if resolved:
                    props = dict(node.properties or {})
                    props.update(resolved)
                    by = list(props.get("__enriched_by") or [])
                    if tmpl.name not in by:
                        by.append(tmpl.name)
                    props["__enriched_by"] = by
                    node.properties = props
                if confidence is not None:
                    try:
                        node.confidence = float(confidence)
                    except (TypeError, ValueError):
                        pass
                await _repo.save(node)
                await _repo.link_date(workspace_id=workspace_id, node_id=node.id, created=False)
                counts["attributes"] += len(resolved) + (1 if confidence is not None else 0)
    if emitted:                                            # entities merged by name are in no asset's subtree
        from opencortyx.resolution.names import link_similar
        counts["near_names"] = await link_similar(workspace_id, emitted, _repo)
        from opencortyx.tasks.graph import embed_later
        embed_later(workspace_id, [n.id for n in emitted])
    return counts


async def run_template(workspace_id: uuid.UUID, template, *, collection_id=None, limit: int = _RUN_LIMIT,
                       source_key: str | None = None, node_ids: list | None = None,
                       attrs=None) -> dict:
    """Run ONE executable template. Independent templates (seed.literal / source=null) emit once;
    bound templates iterate the nodes they select — optionally scoped to one `source_key`, or to an
    explicit set of `node_ids` (DAG chaining: run a task over a prior step's exact output). Any
    mechanism (LLM / regex / dictionary / keyword / NER / seed) is supported. Per-item isolation."""
    if not is_executable(template):
        return {"status": "skipped", "reason": "not_executable", "name": template.name}
    mech = template.mechanism or {}
    ref = str(mech.get("ref") or "")
    totals = {"nodes_seen": 0, "attributes": 0, "nodes": 0, "edges": 0, "errors": 0}

    try:
        from opencortyx.core.inference.embedding import embed as _embed_warm
        await _embed_warm("warmup")
    except Exception as e:                            # noqa: BLE001 — never block a run on this
        log.warning("enrich.embedder_warmup_skipped", error=str(e))
    model_spec = await resolve_model(workspace_id, "extraction", mech.get("model"))

    # relationship extraction: connect existing neighbors of an anchor (its own engine)
    if ref.startswith("relate."):
        return await _run_relate(workspace_id, template, collection_id=collection_id, limit=limit,
                                 source_key=source_key, node_ids=node_ids, model_spec=model_spec)

    # independent: seed.literal or source=null → emit once (no node iteration)
    if ref == "seed.literal" or not template.source:
        async with V2Session() as db:
            try:
                parsed = ({"records": _seed_records(mech)} if ref == "seed.literal"
                          else await _produce_parsed(mech, {}, "", model_spec=model_spec, workspace_id=workspace_id,
                                                     collection_id=collection_id))
                c = await _apply_emit(db, workspace_id, template, source_node=None,
                                      parsed=parsed, ctx={}, collection_id=collection_id, attrs=attrs)
                for k in ("attributes", "nodes", "edges"):
                    totals[k] += c[k]
            except Exception as e:                       # noqa: BLE001
                totals["errors"] += 1
                log.warning("enrich.independent_failed", template=str(template.name), error=str(e))
            await db.commit()
        return {"status": "ok", "name": template.name, **totals}

    nodes = await _select_nodes(workspace_id, template, node_ids=node_ids, source_key=source_key, limit=limit)
    async with V2Session() as db:
        for node in nodes:
            totals["nodes_seen"] += 1
            try:
                ctx = await _node_ctx(node)
                text = _extractor_text(node, ctx)
                parsed = await _produce_parsed(mech, ctx, text, model_spec=model_spec, workspace_id=workspace_id,
                                               node_id=node.id, collection_id=collection_id,
                                               template_name=template.name)
                if not isinstance(parsed, (dict, list)):
                    # the main prompt produced no JSON — but per-attribute overrides (regex/copy/
                    # constant/own-prompt) can still fill their attributes, so don't skip the node.
                    if not (mech.get("attr_overrides")):
                        continue
                    parsed = {}
                c = await _apply_emit(db, workspace_id, template, source_node=node,
                                      parsed=parsed, ctx=ctx, collection_id=collection_id, attrs=attrs)
                for k in ("attributes", "nodes", "edges"):
                    totals[k] += c[k]
                await db.commit()
            except Exception as e:                       # noqa: BLE001 — isolate per-node failures
                totals["errors"] += 1
                log.warning("enrich.template_failed", template=str(template.name), node_id=str(node.id), error=str(e))
                await db.rollback()
    return {"status": "ok", "name": template.name, **totals}


def _mech_short(ref: str) -> str:
    """Short mechanism name for the Studio preview — 'unstructured.regex' → 'regex', bare → 'llm'."""
    ref = str(ref or "")
    if ref in _LLM_REFS or not ref:
        return "llm"
    if ref == _ATTR_REF:
        return "per-attribute"
    if ref.startswith("unstructured."):
        return ref.split(".", 1)[1]            # regex / dictionary / keyword / ner / python / llm
    if ref == "seed.literal":
        return "constant"
    return "llm"


def _endpoint_label(ep, rec, source_label: str, emitted_label: Optional[str], rec_nodes: Optional[dict] = None) -> str:
    """Describe an edge endpoint for the preview WITHOUT touching the DB (no entity resolution)."""
    if ep in (None, "source"):
        return source_label
    if ep == "emitted":
        return emitted_label or "new node"
    if rec_nodes:
        if isinstance(ep, str) and ep in rec_nodes:
            return f"{rec_nodes[ep]} ({ep})"
        if isinstance(ep, dict):
            nt = ep.get("node") or ep.get("node_type") or ep.get("type")
            if nt and nt in rec_nodes:
                return f"{rec_nodes[nt]} ({nt})"
    if ep == "org":
        return "Organization"
    if isinstance(ep, dict):
        if ep.get("resolve") == "entity":
            return str(_fill_field(ep.get("name", "$name"), rec) or "entity")
        if ep.get("find") == "entity":
            return f"{_fill_field(ep.get('name', '$name'), rec) or 'entity'} (existing)"
        if ep.get("node_id") is not None:
            return f"node {_s(_fill_field(ep.get('node_id'), rec))[:40]}"
        if ep.get("match"):
            field, raw = next(iter((ep.get("on") or {}).items()), (None, None))
            return f"{ep['match']} ({field}={_s(_fill_field(raw, rec))})"
    return _s(ep)


async def preview_template(workspace_id: uuid.UUID, spec: dict, sample_text: str, *, max_nodes: int = 12) -> dict:
    """Read-only dry run of an UNSAVED template over one sample. Runs the template's mechanism on
    `sample_text` and returns the nodes / edges / attributes it WOULD emit — each attribute annotated
    with how it's made (mechanism), its model, and the concrete value pulled
    from the sample. Writes NOTHING: no upserts, no embeddings, no entity resolution. This is what
    powers the Studio's live preview, so authors see the exact output before saving or running."""
    mech = (spec.get("mechanism") or {})
    emit = (spec.get("emit") or {})
    overrides = mech.get("attr_overrides") or {}
    main_short = _mech_short(mech.get("ref"))
    main_model = mech.get("model")

    try:
        model_spec = await resolve_model(workspace_id, "extraction", mech.get("model"))
    except Exception:                                    # noqa: BLE001
        model_spec = None

    text = sample_text or ""
    ctx = {"text": text, "chunk_text": text, "label": "", "type": "Sample",
           "node_type": "Sample", "id": "sample", "attributes": "{}"}

    parsed, error = {}, None
    try:
        parsed = await _produce_parsed(mech, ctx, text, model_spec=model_spec, workspace_id=workspace_id)
    except Exception as e:                               # noqa: BLE001 — surface, never raise
        error = str(e)
    if not isinstance(parsed, (dict, list)):
        parsed = {}

    def _meta(k):
        ov = overrides.get(k)
        if _override_active(ov):
            return ((ov.get("mechanism") or "llm").lower(), ov.get("model") or main_model)
        return (main_short, main_model)

    async def _value(k, base_val, rctx):
        ov = overrides.get(k)
        if _override_active(ov):
            try:
                return await _gen_attr_value(workspace_id, ov, k, rctx)
            except Exception:                            # noqa: BLE001
                return None
        return base_val

    def _row(k, val, mm, model):
        return {"key": k, "value": _s(val), "mechanism": mm, "model": model,
                "filled": val not in (None, "", [], {})}

    # 1) enrich-source attributes
    source_attributes = []
    for a in (emit.get("attributes") or []):
        if not isinstance(a, dict):
            continue
        setspec = a.get("set", {})
        if isinstance(setspec, str):                     # set: "$llm" → whole object
            resolved = _resolve(setspec, parsed, ctx)
            for k, v in (resolved if isinstance(resolved, dict) else {}).items():
                source_attributes.append(_row(k, v, main_short, main_model))
        else:
            for k, v in (setspec or {}).items():
                mm, model = _meta(k)
                val = await _value(k, _resolve(v, parsed, ctx), ctx)
                source_attributes.append(_row(k, val, mm, model))

    # 2) emitted nodes (one per LLM/deterministic record)
    recs = _records(parsed)
    nodes_out, rec_label = [], {}
    rec_nodes_by_type = [{} for _ in recs]
    for ri, rec in enumerate(recs[:max_nodes]):
        for spec_n in (emit.get("nodes") or []):
            if not isinstance(spec_n, dict) or not spec_n.get("type"):
                continue
            ntype = spec_n["type"]
            if ntype.startswith("$"):                      # the type the record names
                ntype = str(_fill_field(ntype, rec) or "").strip() or "Entity"
            declared = spec_n.get("attributes") or []
            label = _label_for(rec, ntype)
            if spec_n.get("label"):
                label = str(_fill_field(spec_n["label"], rec) or label)[:200]
            rec_label.setdefault(ri, label)
            if ri < len(rec_nodes_by_type):
                rec_nodes_by_type[ri][ntype] = label
            rctx = {**ctx, **{k: v for k, v in rec.items() if isinstance(v, (str, int, float))}, "label": label}
            keys = declared or [k for k in rec.keys() if not str(k).startswith("__")]
            from_source = set(spec_n.get("from_source") or [])
            attrs_out = []
            for k in keys:
                mm, model = _meta(k)
                # a `from_source` field is not the model's to produce — show where it comes from
                if k in from_source:
                    attrs_out.append(_row(k, (ctx or {}).get(k), "source", None))
                    continue
                val = await _value(k, rec.get(k), rctx)
                attrs_out.append(_row(k, val, mm, model))
            nodes_out.append({
                "id": f"{ntype}_{ri}",
                "type": ntype,
                "label": label,
                "rec_index": ri,
                "new": True,
                "attributes": attrs_out,
            })

    # 3) edges (descriptive labels only — no resolution, no writes)
    src_label = "Sample"
    edges_out = []
    declared_types = {s.get("type") for s in (emit.get("nodes") or []) if isinstance(s, dict) and s.get("type")}

    def _ep_id(ep, ri):
        if ep in (None, "source", "the node it read"):
            return "source"
        if ep == "emitted":
            return f"emitted_{ri}"
        if isinstance(ep, str) and ep in declared_types:
            return f"{ep}_{ri}"
        if isinstance(ep, dict):
            t = ep.get("match") or ep.get("node") or ep.get("node_type") or ep.get("type")
            if t and t in declared_types:
                return f"{t}_{ri}"
        return None

    for espec in _emit_edges(emit):
        if not isinstance(espec, dict):
            continue
        etype = espec.get("type") or espec.get("rel")
        mode = espec.get("mode") or "relate.fixed"
        if not etype and mode != "relate.llm":
            continue
        frm, to = espec.get("from", "source"), espec.get("to", "emitted")
        is_rec = (espec.get("per") == "record" or isinstance(frm, dict) or isinstance(to, dict)
                  or frm in declared_types or to in declared_types
                  or (isinstance(frm, dict) and (frm.get("node") in declared_types or frm.get("node_type") in declared_types))
                  or (isinstance(to, dict) and (to.get("node") in declared_types or to.get("node_type") in declared_types)))
        if is_rec:
            for ri, rec in enumerate(recs[:max_nodes]):
                rn = rec_nodes_by_type[ri] if ri < len(rec_nodes_by_type) else {}
                rec_etype = str(_fill_field(etype, rec) or etype or "").strip() if (etype and etype.startswith("$")) else (etype or "")
                if not rec_etype and mode == "relate.llm":
                    rec_etype = str(rec.get("predicate") or rec.get("relation") or "(LLM generated edge)").strip()
                edges_out.append({
                    "rel": rec_etype or "(relationship)",
                    "from": _endpoint_label(frm, rec, src_label, rec_label.get(ri), rn),
                    "to": _endpoint_label(to, rec, src_label, rec_label.get(ri), rn),
                    "from_id": _ep_id(frm, ri),
                    "to_id": _ep_id(to, ri),
                    "rec_index": ri,
                })
        else:
            emitted_label = nodes_out[0]["label"] if nodes_out else "new node"
            rec_etype = etype or ("(LLM generated edge)" if mode == "relate.llm" else "RELATES_TO")
            edges_out.append({
                "rel": rec_etype,
                "from": src_label if frm == "source" else emitted_label,
                "to": emitted_label if to == "emitted" else src_label,
                "from_id": "source" if frm == "source" else (nodes_out[0]["id"] if nodes_out else None),
                "to_id": (nodes_out[0]["id"] if nodes_out else None) if to == "emitted" else "source",
            })

    warnings: list = []
    _p = str(mech.get("prompt") or "")
    _ref = str(mech.get("ref") or "")
    if _p and ("llm" in _ref or not _ref) and not re.search(r"\{[a-z_][a-z0-9_]*\}", _p):
        warnings.append(
            "This prompt contains no {placeholder}, so the sample is never sent to the model — "
            "the output below is invented, not extracted. Add `TEXT:\\n{text}` to the prompt.")
    _emitted = bool(emit.get("nodes"))
    for _e in (emit.get("edges") or []):
        if isinstance(_e, dict) and "emitted" in (str(_e.get("to")), str(_e.get("from"))) \
                and not _emitted:
            warnings.append(
                "An edge points at `emitted`, but this extractor emits no node — that endpoint can "
                "never resolve, so the edge is never written.")
            break
    if emit.get("attributes") and _emitted:
        warnings.append(
            "This extractor both emits nodes AND enriches the source. Both run; the source "
            "attributes take their values from the FIRST record only.")

    _fields = {k for r in _records(parsed)[:5] if isinstance(r, dict) for k in r}
    for _n in (emit.get("nodes") or []):
        if not isinstance(_n, dict):
            continue
        _nt = _n.get("type") or "?"
        _declared = list(_n.get("attributes") or [])
        if not _declared:
            warnings.append(
                f"The `{_nt}` node declares no attributes, so it takes whatever fields the model "
                f"returns — right now {', '.join(sorted(_fields)) or 'none'}. It works, but the "
                f"node's shape is not pinned and per-attribute controls are unavailable.")
        _ids = _n.get("id")
        _ids = [_ids] if isinstance(_ids, str) else list(_ids or [])
        _unknown = [i for i in _ids if i not in _declared and i not in _fields]
        if _unknown:
            warnings.append(
                f"`{_nt}` keys its identity on {', '.join(_unknown)}, which is neither one of its "
                f"attributes nor a field the model returns — identity falls back to the label, so "
                f"records with the same wording merge into one node.")

    return {"sample": text, "model": mech.get("model") or None, "error": error,
            "warnings": warnings,
            "source_attributes": source_attributes, "nodes": nodes_out, "edges": edges_out,
            "counts": {"nodes": len(nodes_out), "edges": len(edges_out),
                       "attributes": len(source_attributes) + sum(len(n["attributes"]) for n in nodes_out)}}


def _text_key(text: str) -> str:
    """Content key for extraction deduplication.

    Whitespace is normalised because a chunk that differs only in indentation or line endings is the
    same text for extraction purposes — and log/export corpora differ in exactly that way. Hashing
    (rather than keying on the text itself) keeps the map small when chunks are large.
    """
    import hashlib
    return hashlib.blake2b(" ".join((text or "").split()).encode("utf-8"),
                           digest_size=16).hexdigest()


def _section_key(name: str) -> str:
    """Stable short JSON key for a template inside a folded group call: 'builtin:struct.segment' → 'struct_segment'."""
    import re
    return re.sub(r"[^a-z0-9]+", "_", (name or "").split(":", 1)[-1].lower()).strip("_") or "task"


def _is_llm_chunk_template(t) -> bool:
    """Prompt-based per-chunk LLM extractor — the only kind that can be folded into a group call."""
    mech = t.mechanism or {}
    ref = str(mech.get("ref") or "")
    return bool(mech.get("prompt")) and (ref in _LLM_REFS or not ref)


def _group_prompt(members, ctx, text: str) -> str:
    """ONE prompt that runs several extractors over the same text. Each member's own (editable)
    prompt becomes a task section — the text is included ONCE at the end instead of once per
    extractor, and the model returns one JSON object keyed by task."""
    keys = ", ".join(f'"{_section_key(t.name)}"' for t in members)
    parts = [
        "You will perform SEVERAL independent extraction tasks over the SAME text.",
        "Follow each task's instructions exactly. Where a task says to 'Return ONLY JSON' of some "
        "shape, that shape becomes the VALUE under that task's key in the combined object.",
        f"Return ONLY one JSON object whose keys are exactly: {keys}. Include EVERY key — use "
        'the task\'s empty form (e.g. {"records":[]}) when nothing is found.',
    ]
    marker_ctx = {**ctx, "chunk_text": "(see the shared TEXT at the end)"}
    for t in members:
        parts.append(f"\n### TASK {_section_key(t.name)}\n{_fill((t.mechanism or {}).get('prompt') or '', marker_ctx)}")
    parts.append(f"\nTEXT:\n{(text or '')[:8000]}\n")
    return "\n".join(parts)


async def existing_template_names(workspace_id: uuid.UUID, names) -> dict:
    """{name: status} for those of `names` that exist as extractors for this org — one query, for
    callers that must tell a removed or switched-off extractor from one that ran and found nothing."""
    names = [n for n in dict.fromkeys(names or []) if n]
    if not names:
        return {}
    async with V2Session() as db:
        rows = (await db.execute(select(ExtractionTemplate.name, ExtractionTemplate.status).where(
            ExtractionTemplate.workspace_id == workspace_id, ExtractionTemplate.name.in_(names)))).all()
    return {n: (st or "active") for n, st in rows}


async def run_named_templates_on_chunks(workspace_id: uuid.UUID, names, pairs, collection_id=None,
                                        extra_ctx: dict | None = None,
                                        breakdown: dict | None = None,
                                        mech_overrides: dict | None = None,
                                        records: dict | None = None) -> int:
    """Run named declarative templates over a SINGLE document's chunks. Scoped to `pairs` (this
    doc's chunks) so it doesn't touch other documents; reuses the same emit machinery as the
    generic engine. Two throughput levers (Settings → Extraction), neither of which changes WHAT
    is extracted — the same editable prompts and the same emit specs run either way:

      • CALL GROUPS (`extraction_call_groups`): extractors folded into ONE LLM call per chunk via
        `_group_prompt`. Groups run strictly in configured order — a later group starts only after
        every earlier group's output is committed, so dependent extractors see their inputs.
        Ungrouped extractors keep their own call. A missing/unparseable section falls back to that
        extractor's own individual call, so folding can only save calls, never lose extractions.
      • PARALLELISM (`llm_concurrency`): within one unit (a group or a single extractor), the
        per-chunk LLM calls run concurrently under a semaphore. DB writes stay sequential — only
        the network-bound LLM production is parallel.

    `records` ({chunk id: node-shaped record}) are the chunks when they are not graph nodes - a
    workspace whose data is in Delta reads them from the data store (or from memory at ingest).

    Returns the scalar total (nodes + edges + attributes) that every caller already expects. Pass
    `breakdown` (a dict) to ALSO receive per-type attribution, filled in place:
    `{"node_types": {...}, "edge_types": {...}, "attributes": n}`. That lets a stage report what it
    actually created ("12 entities, 7 claims") instead of a bare total, without this function having
    to know which extractor means what.
    """
    import asyncio
    from opencortyx.core.config import runtime as rc
    if not names or not pairs:
        return 0
    _cap = rc.extraction_max_chunks()
    if _cap and len(pairs) > _cap:
        log.info("extraction.chunks_capped", chunks=len(pairs), cap=_cap,
                 knob="runtime_config.extraction_max_chunks",
                 note="chunks stay searchable; only per-chunk LLM extraction is bounded")
        if breakdown is not None:
            breakdown["chunks_capped"] = len(pairs) - _cap
            breakdown["chunks_considered"] = _cap
        pairs = pairs[:_cap]
    async with V2Session() as db:
        tmpls = (await db.execute(select(ExtractionTemplate).where(
            ExtractionTemplate.workspace_id == workspace_id,
            ExtractionTemplate.name.in_(list(names))))).scalars().all()
        missing = sorted(set(names) - {t.name for t in tmpls})
        if missing:
            log.warning("extraction.templates_missing", workspace_id=str(workspace_id), missing=missing)
        tmpls = [t for t in tmpls if is_executable(t)]
        if not tmpls:
            return 0
    chunk_ids = [cid for cid, _ in pairs]
    recs = records or {}
    _graph_ids = [c for c in chunk_ids if c not in recs]
    nodes = {**({n.id: n for n in await _repo.get_many(workspace_id, _graph_ids)} if _graph_ids else {}),
             **recs}
    stage_ctx = {"words": "60-100", "per_node": 3, "top_n": 8, "label": "topic", **(extra_ctx or {})}
    ctxs = {}
    for cid, txt in pairs:
        n = nodes.get(cid)
        if n is not None:
            c = {**(await _node_ctx(n)), **stage_ctx}
            if (txt or "").strip():
                c["chunk_text"] = txt
            ctxs[cid] = c

    try:
        from opencortyx.core.inference.embedding import embed as _embed_warm
        await _embed_warm("warmup")
    except Exception as e:                            # noqa: BLE001 — never block extraction on this
        log.warning("enrich.embedder_warmup_skipped", error=str(e))

    by_name = {t.name: t for t in tmpls}
    relate_t = [t for t in tmpls if str((t.mechanism or {}).get("ref") or "").startswith("relate.")]
    indep_t = [t for t in tmpls if t not in relate_t
               and (str((t.mechanism or {}).get("ref") or "") == "seed.literal" or not t.source)]
    chunk_t = [t for t in tmpls if t not in relate_t and t not in indep_t]

    # ── partition per-chunk templates into ordered units: configured groups first, then singles ──
    grouped_names: set = set()
    units: list = []                                  # [(kind, [templates])], kind ∈ group|single
    for g in rc.extraction_call_groups():
        members = [by_name[n] for n in g["extractors"]
                   if n in by_name and by_name[n] in chunk_t
                   and _is_llm_chunk_template(by_name[n]) and n not in grouped_names]
        if len(members) >= 2:
            units.append(("group", members))
            grouped_names.update(t.name for t in members)
        elif len(members) == 1:                       # a 1-extractor group is just a single
            grouped_names.add(members[0].name)
            units.append(("single", members))
    units.extend(("single", [t]) for t in chunk_t if t.name not in grouped_names)

    made = 0
    sem = asyncio.Semaphore(rc.llm_concurrency())
    valid_pairs = [(cid, txt) for cid, txt in pairs if cid in ctxs]

    def _tally(name: str, counts: dict):
        """Fold one emit result into the caller's `breakdown` (no-op if unused). Recorded twice:
        globally (per node/edge TYPE) and per TEMPLATE — a stage needs the latter because an
        extractor's node type can be dynamic (`type_field`), so type alone can't say who made it."""
        if breakdown is None:
            return
        per = breakdown.setdefault("by_template", {}).setdefault(
            name, {"nodes": 0, "edges": 0, "attributes": 0, "node_types": {}, "edge_types": {}})
        for bucket in ("node_types", "edge_types"):
            dst = breakdown.setdefault(bucket, {})
            for k, v in (counts.get(bucket) or {}).items():
                dst[k] = dst.get(k, 0) + v
                per[bucket][k] = per[bucket].get(k, 0) + v
        for scalar in ("nodes", "edges", "attributes"):
            v = int(counts.get(scalar) or 0)
            if v:
                per[scalar] += v
                breakdown[scalar] = breakdown.get(scalar, 0) + v

    async def _produce_group(members, specs, cid, text):
        """One folded call for this chunk → {template_name: parsed_section}. Per-member fallback."""
        ctx = ctxs[cid]
        out: dict = {}
        parsed = None
        try:
            async with sem:
                raw = await complete(
                    _group_prompt(members, ctx, text),
                    system="You extract precise structured data. JSON only.",
                    max_tokens=min(4000, 800 + 400 * len(members)),
                    json_mode=True, temperature=0.1, model_spec=specs[members[0].name])
            parsed = _parse_json(raw)
        except Exception as e:                        # noqa: BLE001 — whole call failed → all fall back
            log.warning("enrich.group_call_failed", chunk_id=str(cid), error=str(e))
        for t in members:
            sec = parsed.get(_section_key(t.name)) if isinstance(parsed, dict) else None
            if isinstance(sec, (dict, list)):
                out[t.name] = sec
            else:                                     # missing section → this extractor's own call
                try:
                    async with sem:
                        out[t.name] = await _produce_parsed(t.mechanism or {}, ctx, text,
                                                            model_spec=specs[t.name], workspace_id=workspace_id,
                                                            node_id=cid, collection_id=collection_id,
                                                            template_name=t.name)
                except Exception as e:                # noqa: BLE001
                    log.warning("enrich.group_fallback_failed", template=str(t.name), chunk_id=str(cid), error=str(e))
        return out

    async def _produce_single(t, spec, cid, text):
        try:
            async with sem:
                parsed = await _produce_parsed(t.mechanism or {}, ctxs[cid], text, model_spec=spec,
                                               workspace_id=workspace_id, node_id=cid, collection_id=collection_id,
                                               template_name=t.name)
            return {t.name: parsed} if isinstance(parsed, (dict, list)) else {}
        except Exception as e:                        # noqa: BLE001 — isolate per-chunk failures
            log.warning("enrich.chunk_template_failed", template=str(t.name), chunk_id=str(cid), error=str(e))
            return {}

    for kind, members in units:
        specs = {t.name: await resolve_model(workspace_id, "extraction", (t.mechanism or {}).get("model"))
                 for t in members}
        uniq: dict = {}
        for cid, txt in valid_pairs:
            uniq.setdefault(_text_key(txt), []).append((cid, txt))
        reps = [members_[0] for members_ in uniq.values()]          # one representative per text
        if kind == "group":
            rep_out = await asyncio.gather(*(
                _produce_group(members, specs, cid, txt) for cid, txt in reps))
        else:
            t = members[0]
            rep_out = await asyncio.gather(*(
                _produce_single(t, specs[t.name], cid, txt) for cid, txt in reps))
        by_key = {key: out for key, out in zip(uniq.keys(), rep_out)}
        produced = [by_key[_text_key(txt)] for _cid, txt in valid_pairs]
        _saved = len(valid_pairs) - len(reps)
        if _saved:
            log.info("extraction.deduplicated", distinct_texts=len(reps), chunks=len(valid_pairs),
                     llm_calls_avoided=_saved)
            if breakdown is not None:
                breakdown["duplicate_chunks_skipped"] = (
                    breakdown.get("duplicate_chunks_skipped", 0) + _saved)
        nmap = {**({n.id: n for n in await _repo.get_many(workspace_id, _graph_ids)} if _graph_ids else {}),
                **recs}
        async with V2Session() as db:
            for (cid, _txt), sections in zip(valid_pairs, produced):
                node = nmap.get(cid)
                if node is None:
                    continue
                for t in members:
                    parsed = sections.get(t.name)
                    if not isinstance(parsed, (dict, list)):
                        continue
                    c = await _guarded(
                        db, f"chunk template {t.name} apply on {cid}",
                        lambda _t=t, _n=node, _p=parsed, _c=cid: _apply_emit(
                            db, workspace_id, _t, source_node=_n, parsed=_p,
                            ctx=ctxs[_c], collection_id=collection_id),
                        workspace_id=workspace_id, asset=str(cid))
                    if c is not None:
                        made += c["nodes"] + c["edges"] + c["attributes"]
                        _tally(t.name, c)
                # commit this node's work before starting the next one's model calls
                try:
                    await db.commit()
                except Exception as e:              # noqa: BLE001 — a dead session must not end the run
                    log.warning("enrich.apply_commit_failed", chunk_id=str(cid), error=str(e))
                    await db.rollback()
            await db.commit()

    # relationship extraction LAST — it connects entities the units above created
    async with V2Session() as db:
        for t in relate_t:
            t_spec = await resolve_model(workspace_id, "extraction", (t.mechanism or {}).get("model"))
            ov = (mech_overrides or {}).get(t.name) or {}
            if ov:
                t = _with_mechanism(t, {**(t.mechanism or {}), **ov})
            try:
                r = await _run_relate(workspace_id, t, collection_id=collection_id,
                                      node_ids=chunk_ids, model_spec=t_spec)
                made += int(r.get("edges") or 0)
                _tally(t.name, r)
            except Exception as e:                    # noqa: BLE001
                log.warning("enrich.chunk_relate_failed", template=str(t.name), error=str(e))
        # seed.literal / source=null → emit once for the document, not per chunk
        for t in indep_t:
            mech = t.mechanism or {}
            t_spec = await resolve_model(workspace_id, "extraction", mech.get("model"))
            try:
                parsed = ({"records": _seed_records(mech)} if str(mech.get("ref") or "") == "seed.literal"
                          else await _produce_parsed(mech, {}, "", model_spec=t_spec, workspace_id=workspace_id,
                                                     collection_id=collection_id))
            except Exception as e:                    # noqa: BLE001 — LLM/parse failure, no DB involved
                log.warning("enrich.chunk_independent_failed", template=str(t.name), error=str(e))
                continue
            # Same SAVEPOINT guard as the chunk loop: these templates write the shared global hubs too,
            # so an unguarded failure here would abort the session and drop every template after it.
            c = await _guarded(
                db, f"chunk template {t.name} (independent)",
                lambda _t=t, _p=parsed: _apply_emit(db, workspace_id, _t, source_node=None, parsed=_p,
                                                    ctx={}, collection_id=collection_id),
                workspace_id=workspace_id, asset=t.name)
            if c is not None:
                made += c["nodes"] + c["edges"] + c["attributes"]
                _tally(t.name, c)
        await db.commit()
    return made


async def emitted_pairs_for(workspace_id: uuid.UUID, template, input_ids) -> list:
    """(node_id, text) pairs of what `template` emitted for these input nodes, found through the
    template's own declared emit edge, so a DAG stage can chain over this stage's exact output."""
    emit = template.emit or {}
    nspec = next((n for n in (emit.get("nodes") or []) if isinstance(n, dict) and n.get("type")), None)
    espec = next((e for e in (emit.get("edges") or []) if isinstance(e, dict) and (e.get("type") or e.get("rel"))), None)
    if not nspec or not espec or not input_ids:
        return []
    etype = espec.get("type") or espec.get("rel")
    in_set = {str(i) for i in input_ids}
    edges = await _repo.edges(workspace_id, node_ids=list(in_set), types=[etype])
    other = {str(e.target_id) if str(e.source_id) in in_set else str(e.source_id) for e in edges} - in_set
    if not other:
        # Inputs that are chunks in the data store have no edges; what was extracted from them is
        # found through the evidence that names them.
        other = await _evidenced_by(workspace_id, in_set)
    nodes = [n for n in await _repo.get_many(workspace_id, list(other)) if n.type == nspec["type"]]
    return [(n.id, (n.properties or {}).get("text") or n.label or "") for n in nodes]


async def _evidenced_by(workspace_id, unit_keys) -> set:
    """Nodes whose evidence points at any of these units."""
    from opencortyx.domain.data import Evidence
    from opencortyx.core.dataplane import evidence
    keys = [str(k) for k in unit_keys]
    if not keys:
        return set()
    await evidence.flush()                 # an earlier stage of this job may still be buffered
    async with V2Session() as db:
        rows = (await db.execute(select(Evidence.target_id).where(
            Evidence.workspace_id == workspace_id, Evidence.target_kind == "node",
            Evidence.unit_key.in_(keys)))).scalars().all()
    return {str(r) for r in rows}


async def emitted_pairs_by_name(workspace_id: uuid.UUID, name: str, input_ids) -> list:
    """emitted_pairs_for by template NAME — used by the DAG to publish a template stage's output nodes
    (id, text) so the next stage can chain over them (e.g. temporal detector → date normalizer)."""
    if not input_ids:
        return []
    async with V2Session() as db:
        t = (await db.execute(select(ExtractionTemplate).where(
            ExtractionTemplate.workspace_id == workspace_id, ExtractionTemplate.name == name))).scalar_one_or_none()
    return await emitted_pairs_for(workspace_id, t, input_ids) if t else []


async def apply_output(workspace_id: uuid.UUID, template, source_id, parsed, collection_id=None) -> dict:
    """Write an extractor's output that did not come from its model — a quarantined reply someone
    fixed by hand — through the same emit spec a run uses, so it lands as the extractor declares."""
    source_node = await _repo.get(workspace_id, source_id) if source_id else None
    ctx = await _node_ctx(source_node) if source_node is not None else {}
    async with V2Session() as db:
        c = await _apply_emit(db, workspace_id, template, source_node=source_node, parsed=parsed,
                              ctx=ctx, collection_id=collection_id)
        await db.commit()
    return c


async def run_template_by_id(workspace_id: uuid.UUID, template_id: uuid.UUID, collection_id=None,
                             source_key: str | None = None) -> dict:
    async with V2Session() as db:
        t = await db.get(ExtractionTemplate, template_id)
        if not t or t.workspace_id != workspace_id:
            return {"status": "not_found"}
    return await run_template(workspace_id, t, collection_id=collection_id, source_key=source_key)


async def run_template_by_name(workspace_id: uuid.UUID, name: str, collection_id=None,
                               source_key: str | None = None, node_ids: list | None = None,
                               attrs=None) -> dict:
    """Run a template by NAME over the existing nodes it selects (used by the Structured Pipeline
    Builder when a declarative template is added as a stage). `source_key` scopes it to one source;
    `node_ids` runs it over a prior step's exact output (DAG chaining); `attrs` (optional set) limits
    which attributes the template merges (per-stage enable/disable)."""
    async with V2Session() as db:
        t = (await db.execute(select(ExtractionTemplate).where(
            ExtractionTemplate.workspace_id == workspace_id, ExtractionTemplate.name == name))).scalar_one_or_none()
        if not t:
            return {"status": "not_found", "name": name}
    return await run_template(workspace_id, t, collection_id=collection_id,
                              source_key=source_key, node_ids=node_ids, attrs=attrs)


_READING_UNITS = ("Chunk", "Record")
_DERIVED_TYPES = ("Summary", "Topic", "Keyword", "Question", "Community")


async def run_relationship_templates_for_source(workspace_id: uuid.UUID, source_id, collection_id=None) -> dict:
    """Auto-run every user relationship extractor (relate.*) over a freshly-ingested document's
    chunks, so entity↔entity edges appear without a manual Run. Scoped to THIS source's chunks, so
    an upload doesn't re-scan the whole graph. Only Chunk-anchored (or any-anchor) relate templates
    apply here — others run on their own anchor types via the Run button / pipeline."""
    from opencortyx.stores.graph.query import select_nodes
    children = await _repo.find(workspace_id, source_refs=[source_id])
    chunk_ids = [n.id for n in select_nodes(children, NodeQuery(types=list(_READING_UNITS)))]
    if not chunk_ids:                           # mapping renamed the unit → structural fallback
        chunk_ids = [n.id for n in children
                     if (n.properties or {}).get("text") and n.type not in _DERIVED_TYPES
                     and not n.type.endswith("Profile")]
    if not chunk_ids:
        return {"ran": 0, "edges": 0}
    async with V2Session() as db:
        tmpls = (await db.execute(select(ExtractionTemplate).where(
            ExtractionTemplate.workspace_id == workspace_id))).scalars().all()
    relate = [t for t in tmpls
              if str((t.mechanism or {}).get("ref") or "").startswith("relate.") and is_executable(t)]
    ran, edges = 0, 0
    for t in relate:
        types = _select_types(t.source)
        if types and "Chunk" not in types:
            continue
        r = await run_template(workspace_id, t, collection_id=collection_id, node_ids=list(chunk_ids))
        edges += int(r.get("edges") or 0)
        ran += 1
    return {"ran": ran, "edges": edges}


async def run_all(workspace_id: uuid.UUID, collection_id=None) -> dict:
    """Run every executable template whose source is an existing node type (excludes Chunk —
    chunk extraction runs during document ingestion, not here)."""
    async with V2Session() as db:
        tmpls = (await db.execute(
            select(ExtractionTemplate).where(ExtractionTemplate.workspace_id == workspace_id))).scalars().all()
    results = []
    for t in tmpls:
        if not is_executable(t):
            continue
        src = t.source if isinstance(t.source, dict) else {}
        if src.get("bound") == "text":
            continue
        types = _select_types(t.source)
        if types and "Chunk" in types and len(types) == 1:
            continue                                     # legacy spelling of the same rule
        results.append(await run_template(workspace_id, t, collection_id=collection_id))
    return {"status": "ok", "ran": len(results), "results": results}
