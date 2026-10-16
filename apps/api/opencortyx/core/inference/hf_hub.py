"""Download Hugging Face models into the shared model cache, for the `transformers` provider.

The cache is `HF_HOME` on the `model_cache` volume, which the API and every worker mount, so a model
downloaded once (here, by the API) is loaded from disk by whichever process generates with it
(`local_llm.py`). Only what `transformers` needs is fetched: config, tokenizer and safetensors
weights, not the GGUF, ONNX or PyTorch-pickle copies many repos also carry.

A download runs in a thread of the API process and can take many minutes, so it is started here and
polled; the progress is the bytes already on disk, which also survives an API restart (pressing
Download again resumes from what is there).
"""
import fnmatch
import threading
from pathlib import Path

#: The files a causal language model needs to load with `transformers`.
ALLOW = ["*.json", "*.safetensors", "*.txt", "*.model", "*.tiktoken", "*.jinja"]

_jobs: dict = {}
_lock = threading.Lock()


def _wanted(name: str) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in ALLOW)


def _repo_dir(repo: str) -> Path:
    from huggingface_hub import constants
    return Path(constants.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))


def info(repo: str, token: str | None = None) -> dict:
    """What `repo` is and whether it can run in OpenCortyx, from the Hub (no download)."""
    from huggingface_hub import HfApi
    from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError
    try:
        mi = HfApi().model_info(repo, files_metadata=True, token=token or None)
    except GatedRepoError:
        raise ValueError(f"'{repo}' is gated: accept its terms on huggingface.co, then enter a "
                         "Hugging Face token with access to it.")
    except RepositoryNotFoundError:
        raise ValueError(f"No model '{repo}' on Hugging Face. Use its full name, e.g. "
                         "Qwen/Qwen2.5-0.5B-Instruct.")
    files = [s for s in (mi.siblings or []) if _wanted(s.rfilename)]
    total = sum(s.size or 0 for s in files)
    weights = any(s.rfilename.endswith(".safetensors") for s in files)
    gguf = any(s.rfilename.endswith(".gguf") for s in (mi.siblings or []))
    task = mi.pipeline_tag
    why = None
    if task != "text-generation":
        why = f"it is a {task or 'model of unknown type'} model, not a text-generation one"
    elif not weights:
        why = "it has no safetensors weights" + (" (it is a GGUF repository: download it into Ollama)" if gguf else "")
    return {"repo": mi.id, "task": task, "bytes": total, "gguf": gguf, "gated": bool(mi.gated),
            "runs_in_opencortyx": why is None, "why_not": why,
            # weights are loaded at their stored precision, plus working memory while generating
            "memory_bytes": int(total * 1.25)}


def is_downloaded(repo: str) -> bool:
    from huggingface_hub import snapshot_download
    try:
        path = Path(snapshot_download(repo, allow_patterns=ALLOW, local_files_only=True))
    except Exception:                                   # noqa: BLE001 — not in the cache
        return False
    return (path / "config.json").exists() and any(path.glob("*.safetensors"))


def _bytes_on_disk(repo: str) -> int:
    blobs = _repo_dir(repo) / "blobs"
    if not blobs.is_dir():
        return 0
    return sum(f.stat().st_size for f in blobs.iterdir() if f.is_file())


def status(repo: str) -> dict:
    job = _jobs.get(repo) or {}
    state = job.get("status") or ("done" if is_downloaded(repo) else "none")
    return {"repo": repo, "status": state, "done_bytes": _bytes_on_disk(repo),
            "total_bytes": job.get("total"), "error": job.get("error")}


def _download(repo: str, token: str | None) -> None:
    from huggingface_hub import snapshot_download
    try:
        snapshot_download(repo, allow_patterns=ALLOW, token=token or None)
        _jobs[repo].update(status="done")
    except Exception as e:                              # noqa: BLE001 — reported through status()
        _jobs[repo].update(status="failed", error=str(e)[:300])


def start(repo: str, token: str | None = None) -> dict:
    """Start downloading `repo`, or report the download already running."""
    meta = info(repo, token)
    if not meta["runs_in_opencortyx"]:
        raise ValueError(f"'{repo}' cannot run in OpenCortyx: {meta['why_not']}.")
    repo = meta["repo"]
    with _lock:
        if (_jobs.get(repo) or {}).get("status") == "downloading":
            return status(repo)
        _jobs[repo] = {"status": "downloading", "total": meta["bytes"], "error": None}
    threading.Thread(target=_download, args=(repo, token), daemon=True,
                     name=f"hf-download-{repo}").start()
    return status(repo)


def downloaded() -> list[dict]:
    """The text-generation models in the cache, for the Model ID picker."""
    import json

    from huggingface_hub import scan_cache_dir
    out: list[dict] = []
    try:
        repos = scan_cache_dir().repos
    except Exception:                                   # noqa: BLE001 — no cache yet
        return out
    for r in repos:
        if r.repo_type != "model" or not r.revisions:
            continue
        snap = max(r.revisions, key=lambda v: v.last_modified).snapshot_path
        try:
            archs = json.loads((snap / "config.json").read_text()).get("architectures") or []
        except Exception:                               # noqa: BLE001 — not a transformers model
            continue
        if any(a.endswith("ForCausalLM") for a in archs) and any(snap.glob("*.safetensors")):
            out.append({"id": r.repo_id, "size": r.size_on_disk})
    return sorted(out, key=lambda x: x["id"])


def delete(repo: str) -> bool:
    """Remove `repo` from the cache. False when it is not there."""
    from huggingface_hub import scan_cache_dir
    for r in scan_cache_dir().repos:
        if r.repo_type == "model" and r.repo_id == repo:
            scan_cache_dir().delete_revisions(*[v.commit_hash for v in r.revisions]).execute()
            _jobs.pop(repo, None)
            return True
    return False
