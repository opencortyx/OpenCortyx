"""Resolution: after an asset is ingested and extracted, decide what its nodes are to the rest of
the organization's graph — the same thing as an existing node, extra detail about it, agreement
with it, or a conflict — and record every decision.

  spec.py       per-type settings (identify by / compare / ignore), stored on the ontology type
  normalize.py  values compared by meaning, and the identity key
  records.py    the decisions, as a pure function
  engine.py     resolve_asset(): load, decide, write — in bulk
"""
from opencortyx.resolution.engine import resolve_asset
from opencortyx.resolution.spec import ResolutionSpec

__all__ = ["ResolutionSpec", "resolve_asset"]
