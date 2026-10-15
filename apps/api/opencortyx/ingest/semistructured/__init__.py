"""Semi-structured (JSON) pipelines — a self-contained package.

Public API:
  ingest_json / ingest_json_stream — read records into the graph (the walk itself)
  run_semistructured(...)          — the ingestion runtime (walk + match + read + extract)
  build_structure / match_structure / scout_structure — structure registry primitives
  STRUCTURES_STAGE                 — the pseudo-stage type that carries structure cards in a pipeline

`ingest.py` was `ingest/ingest_semistructured.py` until 2026-09-19, living beside this package
rather than in it while the package imported back into it. One capability, one home.
"""
from opencortyx.ingest.semistructured.runner import (   # noqa: F401
    run_semistructured, structures_config, runnable_stages, STRUCTURES_STAGE)
from opencortyx.ingest.semistructured.schema import (   # noqa: F401
    build_structure, match_structure, scout_structure, fingerprint, key_paths, ROLES)
from opencortyx.ingest.semistructured.registry import (   # noqa: F401
    register_structures, structure_types)
from opencortyx.ingest.semistructured.ingest import (   # noqa: F401
    ingest_json, ingest_json_stream, evaluate_key_filters, extract_prose_context)
