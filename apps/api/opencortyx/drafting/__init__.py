"""Authoring — the layer that writes OpenCortyx's own configuration by looking at real data.

A mapping, a pipeline and an extractor are all things a person can author by hand, and the
builders for doing so are the deepest screens in the product. They are also, nearly always,
derivable: the shape of a JSON file is IN the JSON file, the right pipeline for a CSV is the
Universal one minus the stages its columns cannot feed, and "track which vendors we owe money to"
describes an extractor completely. This layer derives them and hands back a DRAFT — a proposal
with its reasons attached, which a person corrects rather than composes.

    from opencortyx.drafting import build_sample, registry
    from opencortyx.drafting.models import DraftKind, DraftRequest

    sample = build_sample(content, filename)
    req = DraftRequest(workspace_id=org, kind=DraftKind.MAPPING, sample=sample)
    drafts = [await d.draft(req) for d in registry.candidates(req)]

Importing this package registers the built-in drafters as a side effect, the same contract
`capabilities` uses, so a new drafter is a class plus one `register()` line and no route changes.

Two invariants hold across everything here and are the reason it is safe to run on an upload:

  NOTHING IS WRITTEN. Every drafter is a pure read. A draft becomes real only when someone POSTs
  it to the CRUD that already owns that object — which is also why `Draft.payload` is that
  endpoint's exact request body and not an intermediate format of our own.

  NOTHING DEPENDS ON THE MODEL BEING UP. Each drafter produces its measured result first and uses
  a language model only to refine it, so authoring degrades to "slightly worse names" when
  inference is unavailable rather than to an error on someone who is holding a file.
"""
from opencortyx.drafting.drafters import (
    DefaultPipelineDrafter, DocumentMappingDrafter, GoalExtractorDrafter,
    SemistructuredMappingDrafter, StructuredMappingDrafter,
)
from opencortyx.drafting.models import (
    Confidence, Draft, DraftKind, DraftRequest, Evidence, Sample,
)
from opencortyx.drafting.port import Drafter
from opencortyx.drafting.registry import registry
from opencortyx.drafting.sampling import build_sample

# ── register built-ins ──
# Registration order is selection order for a caller that does not name a drafter. The three mapping
# drafters are mutually exclusive by route, so their order between themselves does not matter.
registry.register(SemistructuredMappingDrafter())
registry.register(StructuredMappingDrafter())
registry.register(DocumentMappingDrafter())
registry.register(DefaultPipelineDrafter())
registry.register(GoalExtractorDrafter())

__all__ = ["Confidence", "Draft", "DraftKind", "DraftRequest", "Drafter", "Evidence", "Sample",
           "build_sample", "registry"]
