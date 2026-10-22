"""The image bakes in the models a new workspace uses; the names in the Dockerfile must be the config's.

The Dockerfile cannot import the config (the bake layer comes before the code is copied, so a code
change does not download 650 MB again), so the names are written twice. If they drift, a fresh
installation downloads the new default on its first upload while the baked one sits unused.
"""
from pathlib import Path

from opencortyx.core.config import BUILTIN_EMBED_MODEL, BUILTIN_RERANK_MODEL, SPARSE_MODEL

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def test_dockerfile_bakes_the_default_models():
    text = DOCKERFILE.read_text(encoding="utf-8")
    for name in (BUILTIN_EMBED_MODEL, SPARSE_MODEL, BUILTIN_RERANK_MODEL):
        assert f"'{name}'" in text, f"the Dockerfile does not bake {name}"
