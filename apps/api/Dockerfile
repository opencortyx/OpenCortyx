# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    g++ \
    build-essential \
    python3-dev \
    libxml2-dev \
    libz-dev \
    libpq-dev \
    tesseract-ocr \
    libreoffice-writer-nogui \
    libreoffice-impress-nogui \
    libreoffice-calc-nogui \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
# tesseract-ocr: the OCR engine and its English data, which LiteParse reads scanned pages and images
# with (TESSDATA_PREFIX below), so OCR works offline with nothing fetched on first use.
# libreoffice-*-nogui: legacy and OpenDocument office files (.doc, .ppt, .odt, .rtf, iWork) are
# converted to PDF before LiteParse reads them (layout/readers/pdf.py ConvertedReader).
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

# Set to 1 to install the local cross-encoder reranker. Torch is already installed (the in-process
# Hugging Face model provider needs it) at the locked version.
ARG INSTALL_RERANK=0

# Poetry only resolves the lock file; it runs in a throwaway venv and is not part of the image.
COPY pyproject.toml poetry.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/poetry && \
    /opt/poetry/bin/pip install poetry==1.8.2 && \
    /opt/poetry/bin/poetry export --with dev --without-hashes -f requirements.txt -o /tmp/requirements.txt && \
    rm -rf /opt/poetry && \
    pip install --upgrade pip setuptools && \
    export PIP_DEFAULT_TIMEOUT=200 && \
    grep -E '^torch==' /tmp/requirements.txt | sed 's/ *;.*//' > /tmp/torch.txt && \
    pip install --index-url https://download.pytorch.org/whl/cpu -r /tmp/torch.txt && \
    grep -vE '^(nvidia-[a-z0-9-]+|cuda-(bindings|pathfinder|toolkit)|triton)(\[|==| )' /tmp/requirements.txt > /tmp/requirements.cpu.txt && \
    pip install -r /tmp/requirements.cpu.txt && \
    rm /tmp/requirements.txt /tmp/requirements.cpu.txt /tmp/torch.txt
# torch goes first, from the CPU index, at the locked version. The in-process Hugging Face model
# provider (transformers) runs on it, and from PyPI it resolves to the CUDA build — gigabytes of NVIDIA libraries in an image that
# never touches a GPU (text generation runs on a model server you connect). `==2.13.0` in requirements is then
# already satisfied by `2.13.0+cpu`, so the second install leaves it alone.
# The export also lists the CUDA build's own dependencies (nvidia-*, cuda-*, triton) as separate
# lines marked `platform_system == "Linux"`, which every container is, so they are dropped: only
# torch depends on them, and the CPU build does not.

# The search models a new workspace uses (core/config/env.py: BUILTIN_EMBED_MODEL, SPARSE_MODEL,
# BUILTIN_RERANK_MODEL), baked in for the same reason. They go to /cache, where the `model_cache`
# volume is mounted: Docker fills a new, empty named volume with what the image has at that path, so
# a fresh installation starts with them and never downloads them. tests/test_baked_models.py keeps
# these names in step with the config.
RUN python -c "from fastembed import TextEmbedding, SparseTextEmbedding; \
from fastembed.rerank.cross_encoder import TextCrossEncoder; \
TextEmbedding('nomic-ai/nomic-embed-text-v1.5', cache_dir='/cache', lazy_load=True); \
SparseTextEmbedding('Qdrant/bm25', cache_dir='/cache', lazy_load=True); \
TextCrossEncoder('Xenova/ms-marco-MiniLM-L-12-v2', cache_dir='/cache', lazy_load=True)"

RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$INSTALL_RERANK" = "1" ]; then \
      export PIP_DEFAULT_TIMEOUT=200 && \
      pip install sentence-transformers ; \
    fi

COPY . .

EXPOSE 8000

CMD ["uvicorn", "opencortyx.main:app", "--host", "0.0.0.0", "--port", "8000"]
