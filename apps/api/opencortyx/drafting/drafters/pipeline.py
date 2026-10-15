"""(C) Derive a pipeline from a sample: the default engine for the route, plus what the data earns.

The starting point is always one of the three Universal pipelines, because those are what ingestion
runs when nobody picks anything and they are already correct for the general case. A drafted
pipeline is therefore never built from nothing — it is the default, adapted to one observation
about the actual file, and the adaptation is what the user is shown.

The adaptations are deliberately few and each is a measurement:

  • a mapping was derived      → bind it, so the pipeline reads the shape the mapping declares
                                 instead of re-deriving it per run.
  • a date role was found      → nothing structural, but it is reported, because temporal anchoring
                                 is the difference between "a fact" and "a fact as of a date".

A drafter that invented novel stage graphs would be a worse product: the stage vocabulary is
owned by the executors (`default_pipelines._STEP1_CANON` and the extractor catalogue), an invented
token saves, runs, reports success and writes nothing — the exact failure
`tests/test_mechanism_vocabulary.py` exists to prevent. So every stage emitted here is copied from
a shipped default, never generated, and `normalize_stages` canonicalises the result.
"""
from __future__ import annotations

import copy

from opencortyx.drafting.models import Confidence, Draft, DraftKind, DraftRequest, Evidence
from opencortyx.drafting.port import Drafter
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

SAVE_ENDPOINT = "/api/v2/pipelines"


class DefaultPipelineDrafter(Drafter):
    """The Universal engine for this file's route, adapted to what the sample actually contains."""

    name = "pipeline.default"
    kind = DraftKind.PIPELINE
    description = "Starts from the Universal engine for the file's route and binds what the sample earns"

    def supports(self, req: DraftRequest) -> bool:
        return bool(req.sample.route) and not req.sample.is_empty

    async def draft(self, req: DraftRequest) -> Draft:
        from opencortyx.ingest.pipelines.defaults import normalize_stages

        sample = req.sample
        spec = _default_for_route(sample.route)
        if spec is None:
            return _unsupported(self.name, sample.route)

        stages = copy.deepcopy(spec["stages"])
        evidence: list[Evidence] = [Evidence(
            claim=f"starts from {spec['name']}",
            basis=f"the file was routed as {sample.route}, which is what that engine reads")]
        warnings: list[str] = []

        mapping_id = str(req.context.get("mapping_id") or "").strip()
        if mapping_id:
            _bind_mapping(stages, mapping_id)
            evidence.append(Evidence(
                claim="binds the mapping drafted from this same file",
                basis="the pipeline reads the declared shape instead of re-deriving it per run"))

        dates = _date_paths(sample)
        if dates:
            evidence.append(Evidence(
                claim=f"{len(dates)} date field(s) will anchor these records in time",
                basis=f"found {', '.join(dates[:3])}"))
        elif sample.route not in ("structured", "document"):
            warnings.append("no date field was recognised, so these records carry no point in "
                            "time — answers about them cannot be ordered or filtered by date")

        name = f"{_stem(sample.filename)} pipeline".strip()
        return Draft(
            kind=DraftKind.PIPELINE,
            name=name,
            payload={"name": name,
                     "description": f"Drafted from {sample.filename or 'a sample'}: "
                                    f"{spec['name']} adapted to what the file contains.",
                     "kind": spec["kind"],
                     "stages": normalize_stages(stages)},
            drafter=self.name,
            confidence=Confidence.HIGH,
            summary=f"{len(stages)} stage(s), based on {spec['name']}",
            evidence=evidence,
            warnings=warnings,
            save_endpoint=SAVE_ENDPOINT,
        )


def _default_for_route(route: str) -> dict | None:
    """The shipped Universal pipeline for a route, or None if the route has no engine.

    Matched on `kind` rather than on a hard-coded sentinel id, so adding a fourth default pipeline
    makes it draftable without an edit here.
    """
    from opencortyx.ingest.pipelines.defaults import DEFAULT_PIPELINES, normalize_kind

    wanted = normalize_kind(route)
    for spec in DEFAULT_PIPELINES:
        if normalize_kind(spec.get("kind")) == wanted:
            return spec
    return None


def _token(stage: dict) -> str:
    return str((stage or {}).get("type") or (stage or {}).get("action") or "")


def _scalars(value, prefix: str = "", depth: int = 0):
    """Every scalar in a record with its dotted path. Bounded — a sample may nest arbitrarily."""
    if depth > 6:
        return
    if isinstance(value, dict):
        for key, item in list(value.items())[:80]:
            yield from _scalars(item, f"{prefix}.{key}" if prefix else str(key), depth + 1)
    elif isinstance(value, list):
        for item in value[:20]:
            yield from _scalars(item, f"{prefix}[]", depth + 1)
    else:
        yield prefix, value


def _date_paths(sample) -> list[str]:
    """Fields the reader will treat as a temporal anchor, using its own role classifier."""
    from opencortyx.ingest.semistructured.schema import guess_role

    if sample.columns:
        return [str(c.get("name")) for c in sample.records
                if isinstance(c, dict) and "date" in str(c.get("type") or "").lower()]

    found: dict[str, list] = {}
    for record in sample.records:
        for path, value in _scalars(record):
            found.setdefault(path, []).append(value)
    out = []
    for path, values in found.items():
        head = [v for v in values if v is not None][:3]
        if head and guess_role(path, type(head[0]).__name__, head) == "date":
            out.append(path)
    return out


def _bind_mapping(stages: list, mapping_id: str) -> None:
    """Point the pipeline's step-1 card at a saved mapping, in that engine's own spelling.

    The three engines carry a bound mapping under different keys because each step-1 card is read
    by a different executor. Writing both spellings would leave two ids that can disagree, so the
    key is chosen by the stage token the card actually uses.
    """
    for stage in stages:
        token = _token(stage)
        config = stage.setdefault("config", {})
        if token in ("json_structures", "structure_card", "structure_cards"):
            config["mapping_ids"] = [mapping_id]
            return
        if token in ("base_schema", "introspect", "chunking", "chunking_rules"):
            config["mapping_id"] = mapping_id
            return


def _stem(filename: str) -> str:
    stem = (filename or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return stem.replace("_", " ").replace("-", " ").strip() or "Drafted"


def _unsupported(drafter: str, route: str) -> Draft:
    return Draft(
        kind=DraftKind.PIPELINE, name="No pipeline", payload={}, drafter=drafter,
        confidence=Confidence.LOW,
        summary="this file's route has no default engine to start from",
        warnings=[f"no shipped pipeline reads a {route or 'unknown'} source"],
        save_endpoint=SAVE_ENDPOINT)


__all__ = ["DefaultPipelineDrafter"]
