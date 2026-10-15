"""(E) Turn a plain-English goal into a runnable extraction template.

"Track which vendors we owe money to" becomes an extractor that emits Vendor nodes carrying the
amount and currency, linked to whatever text said so. The Studio already has every piece this
needs — a mechanism vocabulary, an emit model, a dry run — and a person still has to assemble
them across thirty-four decisions before they find out whether the idea was any good.

THE MODEL NAMES THINGS; THE CODE BUILDS THE TEMPLATE. This is the whole design, and it is not a
style preference. If a model wrote the template JSON directly it could emit a mechanism `ref` the
engine has no branch for, and an extractor built on one of those *saves, runs, reports success and
writes nothing* — the exact silent failure `tests/test_mechanism_vocabulary.py` was written after.
It could also emit an `emit.edges[].from` that resolves to nothing, which quarantines every edge
at ingest rather than failing here where somebody is watching.

So the model is asked for a deliberately narrow object — an entity name, its fields, an optional
relationship, and one sentence of instruction — and `_assemble` turns that into the template. The
worst a confused model can now do is name a field badly, which is visible in the preview and
fixable in one click. It cannot produce something unrunnable.

Nothing is saved. The draft is previewed through `enrich_templates.preview_template`, the same dry
run the Studio's live preview uses, so what the user approves is output rather than a form.
"""
from __future__ import annotations

import re

from opencortyx.drafting.llm import ask_json, sample_block
from opencortyx.drafting.models import Confidence, Draft, DraftKind, DraftRequest, Evidence
from opencortyx.drafting.port import Drafter
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

SAVE_ENDPOINT = "/api/v2/extraction-templates"

#: The mechanism this drafter builds on — a member of the engine's own
#: `DEPENDENT_MECHANISM_REFS`. Held to that list by `tests/test_authoring_drafts.py` rather
#: than checked at import, so the guarantee does not cost every process the ingest stack.
MECHANISM_REF = "unstructured.llm"

#: A node or edge type is written into the ontology and spelled out by every later query, so the
#: names a model proposes are matched against these rather than cleaned up: a name that needs
#: repairing is a name the model was not confident about.
_NODE_TYPE = re.compile(r"[A-Z][A-Za-z0-9]{1,39}")
_EDGE_TYPE = re.compile(r"[A-Z][A-Z0-9_]{1,39}")
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")

MAX_FIELDS = 12
#: How much sample text the preview runs over. Enough to show real output, small enough that
#: drafting stays interactive.
PREVIEW_CHARS = 4_000


class GoalExtractorDrafter(Drafter):
    """A goal sentence to a previewed extraction template."""

    name = "extractor.goal"
    kind = DraftKind.EXTRACTOR
    description = "Turns a plain-English goal into an extractor and shows what it finds"

    def supports(self, req: DraftRequest) -> bool:
        return bool((req.goal or "").strip())

    async def draft(self, req: DraftRequest) -> Draft:
        goal = (req.goal or "").strip()
        text = _sample_text(req)

        plan, note = await _plan(req, goal, text)
        if note or not plan:
            return _cannot(self.name, goal, note or "the model did not describe an extractor")

        spec, evidence, warnings = _assemble(goal, plan)
        confidence = Confidence.MEDIUM

        if text:
            found, preview_note = await _preview(req, spec, text)
            if preview_note:
                warnings.append(preview_note)
            elif found:
                confidence = Confidence.HIGH
                evidence.insert(0, Evidence(
                    claim=f"found {found} on the sample you provided",
                    basis="run as a dry run; nothing was written"))
            else:
                confidence = Confidence.LOW
                warnings.append("this found nothing in the sample — either the sample does not "
                                "contain what you asked for, or the instruction needs to be more "
                                "specific. Edit the prompt and preview again before saving.")
        else:
            warnings.append("no sample text was provided, so this has not been run against "
                            "anything — preview it before saving")

        return Draft(
            kind=DraftKind.EXTRACTOR,
            name=spec["name"],
            payload=spec,
            drafter=self.name,
            confidence=confidence,
            summary=_summary(plan),
            evidence=evidence,
            warnings=warnings,
            save_endpoint=SAVE_ENDPOINT,
        )


async def _plan(req: DraftRequest, goal: str, text: str) -> tuple[dict, str]:
    """Ask for the narrow object, then refuse anything that is not exactly it.

    Validation is rejection, never repair. A field name that has to be sanitised into shape is one
    the model was guessing at, and silently correcting it produces an extractor that looks
    considered and pulls the wrong thing.
    """
    sample = f"\nHere is a sample of the text it will run over:\n{text[:1200]}\n" if text else ""
    prompt = (
        f"Someone wants to pull structured facts out of their documents. Their goal, in their own "
        f'words: "{goal}"\n{sample}\n'
        f"Describe the ONE thing they want recorded.\n"
        f"- entity: the real-world thing, as one capitalised singular noun (Vendor, Invoice, Risk)\n"
        f"- fields: up to {MAX_FIELDS} facts to capture about it, each a lower_snake_case name "
        f"with a short description. Include the field that names the entity itself.\n"
        f"- relationship: how the text relates to the entity, as one SCREAMING_SNAKE verb phrase "
        f"(OWES, MENTIONS, REPORTS), or null if there is no natural one\n"
        f"- instruction: one sentence telling an assistant what to look for\n\n"
        f'Return ONLY JSON: {{"entity": "Vendor", "fields": [{{"name": "vendor_name", '
        f'"description": "the company owed money"}}], "relationship": "OWES", '
        f'"instruction": "Find every supplier the document says money is owed to."}}')

    parsed, note = await ask_json(req.workspace_id, prompt, model_ref=req.model_ref, max_tokens=900)
    if note:
        return {}, note

    entity = str(parsed.get("entity") or "").strip()
    if not _NODE_TYPE.fullmatch(entity):
        return {}, (f"the model proposed {entity!r} as the thing to record, which is not a usable "
                    f"type name — rephrase the goal around a single noun")

    fields = _clean_fields(parsed.get("fields"))
    if not fields:
        return {}, ("the model did not name any facts worth capturing — say what you want to know "
                    "about each one")

    relationship = str(parsed.get("relationship") or "").strip().upper()
    if relationship and not _EDGE_TYPE.fullmatch(relationship):
        relationship = ""                          # unusable: emit the nodes without the edge

    instruction = str(parsed.get("instruction") or goal).strip()[:600]
    return {"entity": entity, "fields": fields, "relationship": relationship,
            "instruction": instruction, "goal": goal}, ""


def _clean_fields(raw) -> list[dict]:
    """Keep the well-formed fields, in order, without duplicates."""
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw or []:
        name = str((item or {}).get("name") or "").strip().lower() if isinstance(item, dict) else ""
        if not _FIELD_NAME.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        out.append({"name": name,
                    "description": str(item.get("description") or "").strip()[:200]})
        if len(out) >= MAX_FIELDS:
            break
    return out


def _assemble(goal: str, plan: dict) -> tuple[dict, list[Evidence], list[str]]:
    """The plan becomes a `TemplateSpec` body. Every mechanical choice is made here, not by a model."""
    entity, fields = plan["entity"], plan["fields"]
    names = [f["name"] for f in fields]
    label_field = _label_field(names, entity)

    node_spec = {
        "type": entity,
        "label": f"${label_field}",
        "attributes": names,
        # Two mentions of one vendor across two documents are one vendor. Without this the graph
        # grows a node per mention and every count over it is wrong.
        "resolve": "entity",
        "id": [label_field],
    }
    emit: dict = {"nodes": [node_spec]}
    if plan["relationship"]:
        # from=source, to=emitted: the text that said it, pointing at the thing it said. `per:
        # record` gives one edge per extracted item rather than one for the whole chunk.
        emit["edges"] = [{"type": plan["relationship"], "from": "source", "to": "emitted",
                          "per": "record"}]

    spec = {
        "name": _template_name(entity, goal),
        # `select: *` with `bound: text` is "every node that carries prose", which is what a goal
        # like this means: the user asked about their content, not about one paradigm's node type.
        "source": {"select": "*", "bound": "text"},
        "mechanism": {"ref": MECHANISM_REF, "category": "custom",
                      "prompt": _build_prompt(plan), "_drafted_from": goal[:300]},
        "emit": emit,
    }

    evidence = [
        Evidence(claim=f"each result becomes a {entity} node",
                 basis=f"labelled by {label_field}, and merged with any {entity} already in the "
                       f"graph with the same name"),
        Evidence(claim=f"captures {', '.join(names)}",
                 basis=plan["instruction"]),
        Evidence(claim="runs over every node that carries text",
                 basis="documents, JSON records and table rows alike, as they are ingested"),
    ]
    warnings: list[str] = []
    if plan["relationship"]:
        evidence.append(Evidence(
            claim=f"links the text to the {entity} with {plan['relationship']}",
            basis="so an answer can cite the passage the fact came from"))
    else:
        warnings.append(f"no relationship was proposed, so a {entity} will not be linked back to "
                        f"the text it came from — add one in the Studio if you need citations")
    return spec, evidence, warnings


def _build_prompt(plan: dict) -> str:
    """The extraction prompt. `{text}` is required, or the mechanism reads nothing.

    `_produce_parsed` short-circuits on an empty chunk only when the prompt actually references
    the text, and appends its own `Return ONLY JSON` contract when one is not already present —
    so the placeholder is not decoration, it is what makes the template read the node it runs on.
    """
    lines = "\n".join(f'- {f["name"]}: {f["description"] or f["name"].replace("_", " ")}'
                      for f in plan["fields"])
    return (f'{plan["instruction"]}\n\n'
            f'For each {plan["entity"]} you find, record:\n{lines}\n\n'
            f"Use only what the text states. Omit a field the text does not give; do not guess it. "
            f"If the text contains none, return an empty list.\n\nTEXT:\n{{text}}")


def _label_field(names: list[str], entity: str) -> str:
    """Which field names the thing. The label is what a person sees in the graph and what entity
    resolution merges on, so a bad choice here shows up as duplicate nodes, not as a typo."""
    stem = entity.lower()
    for candidate in (f"{stem}_name", stem, "name", "title", "label"):
        if candidate in names:
            return candidate
    for name in names:
        if name.endswith("_name"):
            return name
    return names[0]


def _template_name(entity: str, goal: str) -> str:
    words = " ".join(goal.split())[:60].rstrip(" .,")
    return f"{entity}: {words}" if words else entity


def _sample_text(req: DraftRequest) -> str:
    """Prose to preview against — the document's text, or the longest text in a record sample."""
    explicit = str(req.context.get("sample_text") or "").strip()
    if explicit:
        return explicit[:PREVIEW_CHARS]
    if req.sample.text.strip():
        return req.sample.text[:PREVIEW_CHARS]
    best = ""
    for record in req.sample.records:
        block = sample_block(record, PREVIEW_CHARS)
        if len(block) > len(best):
            best = block
    return best


async def _preview(req: DraftRequest, spec: dict, text: str) -> tuple[str, str]:
    """Dry-run the unsaved template. Returns `(what_it_found, warning)`.

    Uses the Studio's own preview so a draft is judged by exactly the machinery that will run it —
    a second, kinder execution path here would let a draft pass a check the real run then fails.
    """
    from opencortyx.ingest.extraction.templates import preview_template

    try:
        result = await preview_template(req.workspace_id, spec, text)
    except Exception as exc:                        # noqa: BLE001 — a draft survives a failed run
        log.warning("authoring.preview_failed", error=f"{type(exc).__name__}: {exc}")
        return "", f"could not run this against the sample ({type(exc).__name__}); it is unverified"

    if result.get("error"):
        return "", f"running it against the sample reported: {str(result['error'])[:200]}"
    nodes = result.get("nodes") or []
    edges = result.get("edges") or []
    if not nodes:
        return "", ""
    parts = [f"{len(nodes)} {spec['emit']['nodes'][0]['type']}"]
    if edges:
        parts.append(f"{len(edges)} relationship(s)")
    return " and ".join(parts), ""


def _summary(plan: dict) -> str:
    count = len(plan["fields"])
    tail = f", linked with {plan['relationship']}" if plan["relationship"] else ""
    return f"{plan['entity']} with {count} field(s){tail}"


def _cannot(drafter: str, goal: str, reason: str) -> Draft:
    return Draft(
        kind=DraftKind.EXTRACTOR, name=goal[:80] or "Extractor", payload={}, drafter=drafter,
        confidence=Confidence.LOW, summary="could not draft this one",
        warnings=[reason], save_endpoint=SAVE_ENDPOINT)


__all__ = ["GoalExtractorDrafter", "MECHANISM_REF"]
