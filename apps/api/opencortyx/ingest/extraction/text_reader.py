"""Entities and relationships read from text by a small model that runs in-process - no LLM.

The workspace's `entities` model (spaCy, MIT; by default `spacy/en_core_web_sm` from Hugging Face,
changeable on the Models page) reads each text once:

    entities       the spans its named-entity recogniser finds. The node type is the recogniser's own
                   label (PERSON -> Person, ORG -> Org), so nothing is listed here; a span that is only
                   a number or punctuation ("2", "three") is a value, not a thing, and is skipped
    relationships  from each sentence's grammar: an entity that is the subject of a verb and one in
                   that verb's object (directly, or through a preposition) - "Alice joined Acme in
                   2021" gives Alice -JOIN-> Acme. The relationship is named by the verb's lemma and
                   its preposition, read from the sentence, never from a list

Every entity is one node per (type, normalised name) across the workspace, so "Acme" in two documents
is one Acme. Each node and edge carries evidence - the passage or row it was read from and the
sentence - and the entities are then resolved by name onto the published records they name
(`resolution/mentions.py`).
"""
from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

from opencortyx.core.observability import get_logger

log = get_logger(__name__)
ROLE = "entities"
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/text-reader")
#: The model's own files: the pipeline directory, not the wheel the repository also carries.
_FILES = ["*.json", "*.cfg", "*/cfg", "*/model", "*/moves", "*/patterns", "*.bin", "*/*.bin",
          "tokenizer", "vocab/*", "meta.json", "config.cfg"]
#: Texts read per batch, and at most this many characters of one text (a model reads sentences, and
#: a huge field is cut rather than held in memory whole).
BATCH = 64
MAX_CHARS = 100_000
#: The grammar of "is the subject of" and "is in the object of" - the parser's dependency labels.
_SUBJECT = ("nsubj", "nsubjpass", "csubj")
_OBJECT = ("dobj", "obj", "pobj", "attr", "dative", "oprd", "iobj", "agent")

_models: dict = {}
_lock = threading.Lock()


@dataclass
class Found:
    """What one text holds."""
    entities: list = field(default_factory=list)      # [(text, label)]
    relations: list = field(default_factory=list)     # [(subject index, relation, object index, sentence)]
    sentences: dict = field(default_factory=dict)     # entity index -> the sentence it was read in


def _local_dir(repo: str) -> str:
    from huggingface_hub import snapshot_download
    return snapshot_download(repo, allow_patterns=_FILES)


def load(model_id: str):
    """The spaCy pipeline for `model_id` (a Hugging Face repository holding the pipeline directory),
    downloaded on first use into the shared model cache and kept in memory."""
    with _lock:
        nlp = _models.get(model_id)
        if nlp is None:
            import spacy
            nlp = _models[model_id] = spacy.load(_local_dir(model_id))
        return nlp


def _is_value(span) -> bool:
    return all(t.like_num or t.is_punct or t.is_space or t.is_currency for t in span)


def _type(label: str) -> str:
    return label[:1].upper() + label[1:].lower() if label else "Entity"


def _relation(verb, prep) -> str:
    name = verb.lemma_ + (f"_{prep.lemma_}" if prep is not None else "")
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def _object_of(span, verb):
    """Whether the entity sits in `verb`'s object (directly, or as the object of a preposition on the
    verb); returns (True, preposition or None)."""
    tok = span.root
    if tok.head == verb and tok.dep_ in _OBJECT:
        return True, None
    if tok.dep_ in ("pobj", "pcomp") and tok.head.dep_ in ("prep", "agent") and tok.head.head == verb:
        return True, tok.head
    return False, None


def read(nlp, texts: list[str]) -> list[Found]:
    """Entities and grammatical relationships in each of `texts`."""
    out = []
    for doc in nlp.pipe([(t or "")[:MAX_CHARS] for t in texts], batch_size=BATCH):
        f = Found()
        index = {}
        for ent in doc.ents:
            if _is_value(ent) or not ent.text.strip():
                continue
            index[(ent.start, ent.end)] = len(f.entities)
            f.entities.append((" ".join(ent.text.split()), ent.label_))
            f.sentences[len(f.entities) - 1] = ent.sent.text.strip()
        if doc.has_annotation("DEP") and len(f.entities) > 1:
            for sent in doc.sents:
                ents = [(index[(e.start, e.end)], e) for e in sent.ents if (e.start, e.end) in index]
                if len(ents) < 2:
                    continue
                for verb in (t for t in sent if t.pos_ in ("VERB", "AUX")):
                    subjects = [i for i, e in ents if e.root.head == verb and e.root.dep_ in _SUBJECT]
                    if not subjects:
                        continue
                    for j, e in ents:
                        is_obj, prep = _object_of(e, verb)
                        if not is_obj:
                            continue
                        for i in subjects:
                            if i != j:
                                f.relations.append((i, _relation(verb, prep), j, sent.text.strip()))
        out.append(f)
    return out


def entity_id(workspace_id, type_name: str, name: str) -> uuid.UUID:
    from opencortyx.stores.graph.query import normalize_name
    return uuid.uuid5(_NS, f"{workspace_id}:{type_name}:{normalize_name(name)}")


async def model(workspace_id) -> Optional[dict]:
    """The workspace's `entities` model; registered with the built-in default the first time one is
    needed (a workspace made before the role existed has no row for it)."""
    from opencortyx.core.inference.registry import ensure_role, resolve_model
    spec = await resolve_model(workspace_id, ROLE)
    if spec is None and await ensure_role(workspace_id, ROLE):
        spec = await resolve_model(workspace_id, ROLE)
    return spec


@dataclass
class Input:
    """One text to read: where it came from (for evidence) and the node its mentions hang off."""
    text: str
    asset_id: str
    unit_kind: str                 # chunk | row | record
    unit_key: str
    holder: Optional[uuid.UUID] = None     # the node that MENTIONS what is read (a document, a record)


async def read_inputs(workspace_id, inputs: list[Input], *, by: str = "text_reader") -> dict:
    """Read `inputs`, write what they hold to the graph with evidence, and resolve the entities by
    name onto the published records they name. Returns counts."""
    import anyio

    from opencortyx.core.dataplane import evidence
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.resolution.mentions import link_mentions

    counts = {"texts": 0, "entities": 0, "relations": 0, "mentions": 0, "linked": 0}
    inputs = [i for i in inputs if (i.text or "").strip()]
    if not inputs:
        return counts
    spec = await model(workspace_id)
    if not spec:
        raise RuntimeError("No entities model: add one for the Entities role on the Models page.")
    nlp = await anyio.to_thread.run_sync(load, spec["model_id"])
    repo = NodeRepo()
    written: dict = {}
    types, relations = set(), set()
    async with evidence.collect(workspace_id):
        for b in range(0, len(inputs), BATCH):
            batch = inputs[b:b + BATCH]
            found = await anyio.to_thread.run_sync(read, nlp, [i.text for i in batch])
            for inp, f in zip(batch, found):
                counts["texts"] += 1
                if not f.entities:
                    continue
                ids = []
                for k, (name, label) in enumerate(f.entities):
                    t = _type(label)
                    types.add(t)
                    nid = entity_id(workspace_id, t, name)
                    ids.append(nid)
                    with evidence.unit(inp.asset_id, inp.unit_kind, inp.unit_key, quote=f.sentences.get(k),
                                       extractor=by):
                        if nid not in written:
                            node = await repo.upsert_node(
                                node_id=nid, workspace_id=workspace_id, type=t, label=name[:300],
                                properties={"__identity": "entity", "__by": by, "entity_label": label},
                                date_link=False)        # a name read from text has no date of its own
                            written[nid] = node
                            counts["entities"] += 1
                        if inp.holder is not None:
                            await repo.add_edge(workspace_id=workspace_id, source_id=inp.holder, target_id=nid,
                                                type="MENTIONS", properties={"__by": by})
                            counts["mentions"] += 1
                for i, rel, j, sentence in f.relations:
                    relations.add(rel)
                    with evidence.unit(inp.asset_id, inp.unit_kind, inp.unit_key, quote=sentence, extractor=by):
                        if await repo.add_edge(workspace_id=workspace_id, source_id=ids[i], target_id=ids[j],
                                               type=rel, properties={"sentence": sentence[:500], "__by": by}):
                            counts["relations"] += 1
    if types or relations:              # new words in the ontology, as candidates to review
        from opencortyx.core.db import V2Session
        from opencortyx.ontology.extractors import register_emit_types
        async with V2Session() as db:
            await register_emit_types(db, workspace_id, {"nodes": [{"type": t} for t in sorted(types)],
                                                         "edges": [{"type": r} for r in sorted(relations)]},
                                      source="discovered")
            await db.commit()
    counts["linked"] = await link_mentions(workspace_id, [n for n in written.values() if n is not None], repo=repo)
    from opencortyx.resolution.names import link_similar   # near misses of a name: SAME_AS
    counts["near_names"] = await link_similar(workspace_id, list(written.values()), repo)
    from opencortyx.tasks.graph import embed_later         # merged by name: in no asset's subtree
    embed_later(workspace_id, [n.id for n in written.values() if n is not None])
    log.info("text_reader.read", workspace_id=str(workspace_id), **counts)
    return counts
