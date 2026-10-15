"""Built-in drafters. Import order here is the order `registry.candidates` returns them in."""
from opencortyx.drafting.drafters.extractor import GoalExtractorDrafter
from opencortyx.drafting.drafters.mapping import (
    DocumentMappingDrafter, SemistructuredMappingDrafter, StructuredMappingDrafter,
)
from opencortyx.drafting.drafters.pipeline import DefaultPipelineDrafter

__all__ = ["DefaultPipelineDrafter", "DocumentMappingDrafter", "GoalExtractorDrafter",
           "SemistructuredMappingDrafter", "StructuredMappingDrafter"]
