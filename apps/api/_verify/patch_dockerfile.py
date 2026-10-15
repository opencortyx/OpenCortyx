import io

p = "Dockerfile"
s = io.open(p, encoding="utf-8").read()


def swap(old, new):
    global s
    assert old in s, old[:60]
    s = s.replace(old, new, 1)


swap("""    tesseract-ocr \\
    && rm -rf /var/lib/apt/lists/*""", """    tesseract-ocr \\
    libgl1 \\
    libglib2.0-0 \\
    && rm -rf /var/lib/apt/lists/*
# libgl1 + libglib2.0-0: OpenCV, which Docling's layout models load on the first PDF. Without them
# every PDF failed with ImportError('libGL.so.1') and was read by the plain-text fallback instead.""")

swap("""    export PIP_DEFAULT_TIMEOUT=200 && \\
    pip install -r /tmp/requirements.txt && \\
    rm /tmp/requirements.txt""", """    export PIP_DEFAULT_TIMEOUT=200 && \\
    grep -E '^(torch|torchvision)==' /tmp/requirements.txt | sed 's/ *;.*//' > /tmp/torch.txt && \\
    pip install --index-url https://download.pytorch.org/whl/cpu -r /tmp/torch.txt && \\
    pip install -r /tmp/requirements.txt && \\
    rm /tmp/requirements.txt /tmp/torch.txt
# torch/torchvision go first, from the CPU index, at the locked versions. Docling depends on torch,
# and from PyPI that resolves to the CUDA build — gigabytes of NVIDIA libraries in an image that
# never touches a GPU (inference runs in the vllm container). `==2.13.0` in requirements is then
# already satisfied by `2.13.0+cpu`, so the second install leaves it alone.

# Docling's layout and table models, baked in. Otherwise they download from Hugging Face on the
# first document in EVERY fresh container, and an offline install silently reads every document
# with the fallback parser instead.
ENV DOCLING_ARTIFACTS_PATH=/opt/docling-models
RUN docling-tools models download layout tableformer -o /opt/docling-models""")
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("patched")
