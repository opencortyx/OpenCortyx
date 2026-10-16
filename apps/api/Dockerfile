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
    poppler-utils \
    tesseract-ocr \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
# libgl1 + libglib2.0-0: OpenCV, which Docling's layout models load on the first PDF. Without them
# every PDF failed with ImportError('libGL.so.1') and was read by the plain-text fallback instead.

# Set to 1 to install the local cross-encoder reranker. Torch is already installed (Docling needs it)
# at the locked version; installing it again here would break the torch/torchvision pair.
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
    grep -E '^(torch|torchvision)==' /tmp/requirements.txt | sed 's/ *;.*//' > /tmp/torch.txt && \
    pip install --index-url https://download.pytorch.org/whl/cpu -r /tmp/torch.txt && \
    grep -vE '^(nvidia-[a-z0-9-]+|cuda-(bindings|pathfinder|toolkit)|triton)(\[|==| )' /tmp/requirements.txt > /tmp/requirements.cpu.txt && \
    pip install -r /tmp/requirements.cpu.txt && \
    rm /tmp/requirements.txt /tmp/requirements.cpu.txt /tmp/torch.txt
# torch/torchvision go first, from the CPU index, at the locked versions. Docling depends on torch,
# and from PyPI that resolves to the CUDA build — gigabytes of NVIDIA libraries in an image that
# never touches a GPU (text generation runs on a model server you connect). `==2.13.0` in requirements is then
# already satisfied by `2.13.0+cpu`, so the second install leaves it alone.
# The export also lists the CUDA build's own dependencies (nvidia-*, cuda-*, triton) as separate
# lines marked `platform_system == "Linux"`, which every container is, so they are dropped: only
# torch depends on them, and the CPU build does not.

# Docling's layout and table models, baked in. Otherwise they download from Hugging Face on the
# first document in EVERY fresh container, and an offline install silently reads every document
# with the fallback parser instead.
ENV DOCLING_ARTIFACTS_PATH=/opt/docling-models
RUN docling-tools models download layout tableformer -o /opt/docling-models

RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "$INSTALL_RERANK" = "1" ]; then \
      export PIP_DEFAULT_TIMEOUT=200 && \
      pip install sentence-transformers ; \
    fi

COPY . .

EXPOSE 8000

CMD ["uvicorn", "opencortyx.main:app", "--host", "0.0.0.0", "--port", "8000"]
