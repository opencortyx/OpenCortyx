"""System Default Pipelines — the 3 zero-config ingestion pipelines, and the ONE place a
pipeline id is resolved into runnable stages.

  1. Default Pipeline 1: Universal SQL Ingestion Engine       (structured)
  2. Default Pipeline 2: Universal JSON Ingestion Engine      (semistructured)
  3. Default Pipeline 3: Universal Document Ingestion Engine  (document / "unstructured")

These are VIRTUAL: they are never rows in the pipeline table. The dashboard offers them under fixed
sentinel UUIDs, an ingestion stores that id like any other, and every ingest path resolves it through
`resolve_pipeline()` here. (An earlier build seeded them per-org under `uuid5(org, sentinel)`, which
produced a DIFFERENT id from the one the UI sends — so an ingestion assigned "Default Pipeline 1"
resolved to nothing and the run ended with *"pipeline … not found or has no stages"* and an empty
graph. `default_spec()` still recognises those legacy uuid5 ids so old ingestions keep working.)

STAGE VOCABULARY. Each engine reads the Step-1 card under its own token, and the executors read
`type` while the structured DAG reads `action`. `normalize_stages()` canonicalises both so one spec
runs everywhere:

    structured     → base_schema        (ingest_structured._parse_structured_plan, action)
    semistructured → json_structures    (semistructured.runner.STRUCTURES_STAGE, type)
    document       → chunking           (ingest_docs._stage_kind → "chunk", type)
"""

import uuid as _uuid

DEFAULT_PIPELINE_SQL_ID = "00000000-0000-4000-8000-000000000001"
DEFAULT_PIPELINE_JSON_ID = "00000000-0000-4000-8000-000000000002"
DEFAULT_PIPELINE_DOC_ID = "00000000-0000-4000-8000-000000000003"

DEFAULT_PIPELINES = [
    {
        "id": DEFAULT_PIPELINE_SQL_ID,
        "name": "Default Pipeline 1: Universal SQL Ingestion Engine",
        "description": "System default pipeline for structured SQL ingestion. Automatically maps Tables to Table Nodes, PKs to Identities, and Foreign Keys to Edges.",
        "kind": "structured",
        "is_system_default": True,
        "stages": [
            {
                "id": "stage_0",
                "action": "base_schema",
                "config": {
                    # auto → one node per ROW (auto_mode "row"), column roles auto-detected.
                    # Without this the tabular reader defaults to "catalog" (schema nodes only).
                    "mode": "auto",
                    "auto_mode": "row",
                    "auto_detect": True,
                    "auto_accept": True,
                    "mapper_class": "UniversalTabularMapper"
                }
            }
        ]
    },
    {
        "id": DEFAULT_PIPELINE_JSON_ID,
        "name": "Default Pipeline 2: Universal JSON Ingestion Engine",
        "description": "System default pipeline for semi-structured JSON payloads. Uses prose density heuristics to find the text in each record.",
        "kind": "semistructured",
        "is_system_default": True,
        "stages": [
            {
                "id": "stage_0",
                "action": "structure_card",
                "config": {
                    "auto_accept": True,
                    "auto_min_chars": 40,
                    "mapper_class": "UniversalJsonMapper"
                }
            }
        ]
    },
    {
        "id": DEFAULT_PIPELINE_DOC_ID,
        "name": "Default Pipeline 3: Universal Document Ingestion Engine",
        "description": "System default pipeline for unstructured PDF/Docx files. Layout AST chunking.",
        # "document" is the pipeline kind the builders save and the Ingestion Hub filters on;
        # "unstructured" is the same paradigm's user-facing word (see normalize_kind).
        "kind": "document",
        "is_system_default": True,
        "stages": [
            {
                "id": "stage_0",
                "action": "chunking_rules",
                "config": {
                    "auto_accept": True,
                    # read by layout: structure becomes node types, not equal-size chunks
                    "strategy": "layout",
                    "mapper_class": "UniversalDocMapper"
                }
            }
        ]
    }
]


DEFAULT_PIPELINES_MAP = {p["id"]: p for p in DEFAULT_PIPELINES}

# Readable aliases the UI may send instead of the sentinel UUID.
_SLUG_MAP = {
    "default-pipeline-sql": DEFAULT_PIPELINES_MAP[DEFAULT_PIPELINE_SQL_ID],
    "default-pipeline-json": DEFAULT_PIPELINES_MAP[DEFAULT_PIPELINE_JSON_ID],
    "default-pipeline-doc": DEFAULT_PIPELINES_MAP[DEFAULT_PIPELINE_DOC_ID],
}

_STEP1_CANON = {
    "base_schema": "base_schema",              # structured
    "introspect": "base_schema",
    "structure_card": "json_structures",       # semi-structured
    "structure_cards": "json_structures",
    "json_structures": "json_structures",
    "chunking_rules": "chunking",              # document
    "chunking": "chunking",
}
# Config keys the UI/specs spell differently from the engine.
_CONFIG_CANON = {"auto_min_chars": "auto_text_min_chars"}


def normalize_kind(kind) -> str:
    """Pipeline kind, canonical. "unstructured" is the paradigm's user-facing name for the same
    thing the builders save as "document" — treat them as one so a pipeline is never invisible to
    the dropdown that filters on the other spelling."""
    k = str(kind or "").strip().lower()
    if k in ("unstructured", "document", "doc", "docs"):
        return "document"
    if k in ("semistructured", "semi-structured", "semi", "json"):
        return "semistructured"
    if k in ("structured", "tabular", "sql"):
        return "structured"
    return k


def normalize_stage(stage):
    """One stage → canonical form: `type` and `action` both set to the token its engine reads, and
    config keys canonicalised. Non-dict stages pass through untouched."""
    if not isinstance(stage, dict):
        return stage
    s = dict(stage)
    token = str(s.get("type") or s.get("action") or "").strip()
    canon = _STEP1_CANON.get(token.lower())
    if canon:
        s["type"] = canon
        s["action"] = canon
    elif token:                                # an extractor / generator stage: mirror the token
        s.setdefault("type", token)
        s.setdefault("action", token)
    cfg = dict(s.get("config") or {})
    for old, new in _CONFIG_CANON.items():
        if old in cfg and new not in cfg:
            cfg[new] = cfg.pop(old)
    s["config"] = cfg
    return s


def normalize_stages(stages) -> list:
    """A pipeline's stages → canonical form. Safe on user-authored pipelines (they already use the
    canonical tokens, so this is a no-op for them)."""
    return [normalize_stage(s) for s in (stages or [])]


# ── resolution ───────────────────────────────────────────────────────────────────────────────────
def default_spec(pipeline_id, workspace_id=None):
    """The system-default spec a pipeline id names, or None. Accepts:
      • the sentinel UUID              (what the dashboard sends today)
      • the readable slug              ("default-pipeline-sql" — an ingestion saved before the UUIDs)
      • `uuid5(org, <either of those>)` — what `_safe_uuid()` / the old per-org seeding turned a
        non-row id into when it was written onto an Ingestion.
    Saved ingestions exist in the wild with every one of these, and each one that fails to resolve is
    a run that ends with "pipeline … not found or has no stages" and an empty graph."""
    if not pipeline_id:
        return None
    key = str(pipeline_id).strip().lower()
    spec = DEFAULT_PIPELINES_MAP.get(key) or _SLUG_MAP.get(key)
    if spec:
        return spec
    if workspace_id:
        try:
            ns = workspace_id if isinstance(workspace_id, _uuid.UUID) else _uuid.UUID(str(workspace_id))
        except (ValueError, TypeError, AttributeError):
            return None
        for name, s in list(DEFAULT_PIPELINES_MAP.items()) + list(_SLUG_MAP.items()):
            if key == str(_uuid.uuid5(ns, name)):
                return s
    return None


def is_default_pipeline_id(pipeline_id, workspace_id=None) -> bool:
    return default_spec(pipeline_id, workspace_id) is not None


class ResolvedPipeline:
    """Read-only stand-in for a `control.Pipeline` row, so callers can treat a system default and a
    saved pipeline identically (`.id` / `.kind` / `.stages`)."""

    __slots__ = ("id", "workspace_id", "name", "description", "kind", "stages", "is_system_default",
                 "created_at", "updated_at")

    def __init__(self, *, id, workspace_id, name, description, kind, stages, is_system_default=False,
                 created_at=None, updated_at=None):
        self.id = id
        self.workspace_id = workspace_id
        self.name = name
        self.description = description or ""
        self.kind = normalize_kind(kind)
        self.stages = stages or []
        self.is_system_default = is_system_default
        self.created_at = created_at
        self.updated_at = updated_at

    def view(self):
        return {"id": str(self.id), "name": self.name, "description": self.description,
                "kind": self.kind, "stages": self.stages, "stage_count": len(self.stages),
                "is_system_default": self.is_system_default,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None}


def default_pipeline(pipeline_id, workspace_id) -> "ResolvedPipeline | None":
    """A system default as a ResolvedPipeline (stages normalized), or None."""
    spec = default_spec(pipeline_id, workspace_id)
    if not spec:
        return None
    return ResolvedPipeline(id=spec["id"], workspace_id=workspace_id, name=spec["name"],
                            description=spec.get("description"), kind=spec.get("kind"),
                            stages=normalize_stages(spec.get("stages")), is_system_default=True)


def default_pipelines(workspace_id=None, kind=None) -> list:
    """Every system default (optionally filtered by kind) as ResolvedPipelines — what the pipelines
    list endpoint merges in so the UI does not have to invent them client-side."""
    want = normalize_kind(kind) if kind else None
    out = []
    for spec in DEFAULT_PIPELINES:
        if want and normalize_kind(spec.get("kind")) != want:
            continue
        out.append(default_pipeline(spec["id"], workspace_id))
    return out


async def resolve_pipeline(db, workspace_id, pipeline_id) -> "ResolvedPipeline | None":
    """THE pipeline resolver. `pipeline_id` → a saved Pipeline row (org-checked) or a system default,
    with stages normalized; None when it names neither.

    Every ingest path must go through this: a system default is not a row, so a bare
    `db.get(Pipeline, id)` returns None for it and the run either refuses to start or silently drops
    the pipeline's stages."""
    if not pipeline_id:
        return None
    from opencortyx.domain.control import Pipeline
    row = None
    try:
        row = await db.get(Pipeline, _uuid.UUID(str(pipeline_id)))
    except (ValueError, TypeError):              # not a UUID → the id-from-name form
        try:
            row = await db.get(Pipeline, _uuid.uuid5(_uuid.UUID(str(workspace_id)), str(pipeline_id)))
        except (ValueError, TypeError):
            row = None
    if row is not None and str(row.workspace_id) == str(workspace_id):
        return ResolvedPipeline(id=row.id, workspace_id=row.workspace_id, name=row.name,
                                description=row.description, kind=row.kind,
                                stages=normalize_stages(row.stages),
                                created_at=getattr(row, "created_at", None),
                                updated_at=getattr(row, "updated_at", None))
    return default_pipeline(pipeline_id, workspace_id)
