"""Services: turn an HTTP request into a `DraftRequest`, run the drafters, shape the reply.

Thin by design. Everything that decides anything lives in `opencortyx.drafting`; this exists so
the route handler has no logic in it and so the same orchestration serves the multipart upload,
the inline-content call and the MCP tool without any of them re-implementing "which drafters, in
what order, with what failure behaviour".
"""
from __future__ import annotations

import uuid

from opencortyx.drafting import build_sample, registry
from opencortyx.drafting.models import Draft, DraftKind, DraftRequest, Sample
from opencortyx.core.observability import get_logger

log = get_logger(__name__)


def make_request(workspace_id: uuid.UUID, *, kind: str, content: bytes | None = None,
                 filename: str = "", mime: str = "", goal: str = "",
                 use_model: bool = True, model_ref: str | None = None,
                 context: dict | None = None) -> DraftRequest:
    """Build the request, sampling the content when there is any.

    An extractor drafted from a goal alone has no file, so an empty `Sample` is a valid input
    rather than an error — the drafter reports that it could not be previewed.
    """
    sample = build_sample(content, filename, mime=mime) if content else Sample(filename=filename)
    return DraftRequest(workspace_id=workspace_id, kind=DraftKind(kind), sample=sample, goal=goal or "",
                        context=dict(context or {}), model_ref=model_ref, use_model=use_model)


async def run_drafters(req: DraftRequest, *, drafter: str | None = None) -> list[Draft]:
    """Every drafter that claims the request, or just the one named. Failures are skipped.

    One drafter raising must not lose the others' work: a request that asks for a mapping gets
    whichever readings succeeded, and the one that broke is logged rather than turned into a 500
    for a user who is holding a file. A drafter that fails for everyone shows up as an empty
    `drafts` list, which the route reports as "nothing could read this".
    """
    if drafter:
        chosen = registry.get(drafter)
        if chosen is None:
            raise LookupError(f"unknown drafter {drafter!r}")
        candidates = [chosen]
    else:
        candidates = registry.candidates(req)

    drafts: list[Draft] = []
    for candidate in candidates:
        try:
            drafts.append(await candidate.draft(req))
        except Exception as exc:                    # noqa: BLE001 — see docstring
            log.warning("authoring.drafter_failed", drafter=candidate.name,
                        kind=req.kind.value, error=f"{type(exc).__name__}: {exc}")
    return drafts


def response(req: DraftRequest, drafts: list[Draft]) -> dict:
    return {
        "drafts": [d.to_dict() for d in drafts],
        "sample": req.sample.to_dict(),
        "available": [d.describe() for d in registry.for_kind(req.kind)],
    }


async def preview_payload(workspace_id: uuid.UUID, kind: str, payload: dict, sample_text: str) -> dict:
    """Run an edited draft against a sample and return what it would produce.

    Only `extractor` has something to run — a mapping or a pipeline is verified by the tools that
    already own that job (`/schema-mappings/{id}/conformance` against a real asset, and an actual
    run for a pipeline), and a second preview path here would be a second opinion about the same
    payload. Saying so is better than a silent empty result.
    """
    if DraftKind(kind) is not DraftKind.EXTRACTOR:
        return {"supported": False,
                "reason": f"a {kind} draft is verified by ingesting with it, not by a dry run"}

    from opencortyx.ingest.extraction.templates import preview_template

    result = await preview_template(workspace_id, {
        "name": payload.get("name"), "source": payload.get("source"),
        "mechanism": payload.get("mechanism") or {}, "emit": payload.get("emit") or {},
    }, sample_text or "")
    return {"supported": True, **result}


__all__ = ["make_request", "preview_payload", "response", "run_drafters"]
