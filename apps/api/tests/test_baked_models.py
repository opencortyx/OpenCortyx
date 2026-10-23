"""The image bakes in the models a new workspace uses; the names in the Dockerfile must be the config's.

The Dockerfile cannot import the config (the bake layer comes before the code is copied, so a code
change does not download 650 MB again), so the names are written twice. If they drift, a fresh
installation downloads the new default on its first upload while the baked one sits unused.
"""
from pathlib import Path

import re

import pytest

from opencortyx.core.config import BUILTIN_GENERATION_MODEL, BUILTIN_RERANK_MODEL, SPARSE_MODEL
from opencortyx.core.config.env import BUILTIN_EMBED_MODELS

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def test_dockerfile_bakes_the_default_models():
    # A new installation's embedder is the one that fits the width its .env starts with.
    example = next((p / ".env.example" for p in Path(__file__).resolve().parents
                    if (p / ".env.example").exists()), None)
    if example is None:
        pytest.skip("runs from a checkout of the repository")
    width = int(re.search(r"^EMBED_DIMENSIONS=(\d+)", example.read_text(encoding="utf-8"), re.M)[1])
    text = DOCKERFILE.read_text(encoding="utf-8")
    for name in (BUILTIN_EMBED_MODELS[width], SPARSE_MODEL, BUILTIN_RERANK_MODEL, BUILTIN_GENERATION_MODEL):
        assert f"'{name}'" in text, f"the Dockerfile does not bake {name}"
