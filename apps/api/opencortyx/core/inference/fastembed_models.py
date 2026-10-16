"""fastembed models for embedding and reranking: the catalogue, and downloading one ahead of use.

fastembed runs ONNX models on the CPU inside OpenCortyx, so an embedder or reranker needs no server.
It downloads a model the first time something loads it; doing that from the Models page instead
means the first ingest or search does not stall on a download, and the page can say how far along
it is. Files go to the same cache the loaders read (`/cache`, the `model_cache` volume).

Only the models fastembed itself supports can run this way; the catalogue lists them, with their
vector size, which for an embedder has to match the index (`EMBED_DIMENSIONS`).
"""
import os
import threading
from pathlib import Path

from opencortyx.core.config import settings

_jobs: dict = {}
_lock = threading.Lock()


def cache_dir() -> str | None:
    """Where fastembed keeps models. The loaders in embedding.py use the same rule."""
    return settings.FASTEMBED_CACHE_DIR or ("/cache" if os.path.exists("/cache") else None)


def _catalogue(role: str) -> list[dict]:
    if role == "rerank":
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        return TextCrossEncoder.list_supported_models()
    from fastembed import TextEmbedding
    return TextEmbedding.list_supported_models()


def describe(role: str, model: str) -> dict | None:
    """fastembed's description of `model` for `role`, or None when fastembed does not support it."""
    want = (model or "").strip().lower()
    return next((d for d in _catalogue(role) if d["model"].lower() == want), None)


def _model_dir(d: dict) -> Path | None:
    src = (d.get("sources") or {}).get("hf")
    root = cache_dir()
    if not src or not root:
        return None
    return Path(root) / ("models--" + src.replace("/", "--"))


def is_downloaded(d: dict) -> bool:
    folder = _model_dir(d)
    snaps = folder / "snapshots" if folder else None
    return bool(snaps and snaps.is_dir()
                and any((s / d["model_file"]).exists() for s in snaps.iterdir()))


def catalogue(role: str) -> list[dict]:
    """Every model fastembed can run for `role`, for the Model ID picker."""
    out = []
    for d in _catalogue(role):
        dim = d.get("dim")
        out.append({"id": d["model"], "size": int(float(d.get("size_in_GB") or 0) * 1e9),
                    "dim": dim, "downloaded": is_downloaded(d),
                    "detail": f"{dim} dims" if dim else None})
    return sorted(out, key=lambda x: x["id"].lower())


def _bytes_on_disk(d: dict) -> int:
    folder = _model_dir(d)
    blobs = folder / "blobs" if folder else None
    if not blobs or not blobs.is_dir():
        return 0
    return sum(f.stat().st_size for f in blobs.iterdir() if f.is_file())


def status(role: str, model: str) -> dict:
    d = describe(role, model)
    if d is None:
        return {"model": model, "status": "none", "done_bytes": 0, "total_bytes": None, "error": None}
    job = _jobs.get(d["model"]) or {}
    state = job.get("status") or ("done" if is_downloaded(d) else "none")
    return {"model": d["model"], "status": state, "done_bytes": _bytes_on_disk(d),
            "total_bytes": int(float(d.get("size_in_GB") or 0) * 1e9) or None,
            "error": job.get("error")}


def _download(role: str, name: str) -> None:
    # `lazy_load` fetches the files without opening an ONNX session: the API process downloads the
    # model, it does not need to hold it in memory too.
    kwargs = {"cache_dir": cache_dir(), "lazy_load": True} if cache_dir() else {"lazy_load": True}
    try:
        if role == "rerank":
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            TextCrossEncoder(model_name=name, **kwargs)
        else:
            from fastembed import TextEmbedding
            TextEmbedding(model_name=name, **kwargs)
        _jobs[name].update(status="done")
    except Exception as e:                              # noqa: BLE001 — reported through status()
        _jobs[name].update(status="failed", error=str(e)[:300])


def start(role: str, model: str) -> dict:
    """Start downloading a fastembed model, or report the download already running."""
    d = describe(role, model)
    if d is None:
        kind = "reranker" if role == "rerank" else "embedding model"
        raise ValueError(f"'{model}' is not a fastembed {kind}. Pick one from the list.")
    name = d["model"]
    with _lock:
        if (_jobs.get(name) or {}).get("status") == "downloading":
            return status(role, name)
        _jobs[name] = {"status": "downloading", "error": None}
    threading.Thread(target=_download, args=(role, name), daemon=True,
                     name=f"fastembed-download-{name}").start()
    return status(role, name)
