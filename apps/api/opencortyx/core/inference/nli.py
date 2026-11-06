"""Does this passage say this sentence? A natural-language-inference model, run in this process.

Grounding by similarity cannot tell "the board approved the budget" from "the board did not approve
the budget": the words are the same. An NLI model reads the two together and answers entailment /
neutral / contradiction. It is the `verification` role of the Model Registry - any Hugging Face
sequence-classification model with those labels (an MNLI cross-encoder), downloaded on the Models
page like the in-process generation model. Without one, citations are still checked by numbers,
words and similarity (retrieval/answering/trust.py), and the answer says which check ran.

`entails(pairs)` returns, per (premise, hypothesis), {"entail", "contradict"} probabilities.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Optional

_lock = threading.Lock()
_loaded: dict = {}
#: Characters of a passage read per pair: NLI models take ~512 tokens.
PREMISE_CHARS = 1600


def _load(model_id: str):
    with _lock:
        hit = _loaded.get(model_id)
        if hit:
            return hit
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        tok = AutoTokenizer.from_pretrained(model_id, local_files_only=True)
        model = AutoModelForSequenceClassification.from_pretrained(model_id, local_files_only=True,
                                                                   use_safetensors=True)
        model.eval()
        labels = {str(v).lower(): int(k) for k, v in (model.config.id2label or {}).items()}
        ent = next((i for name, i in labels.items() if name.startswith("entail")), None)
        con = next((i for name, i in labels.items() if name.startswith("contra")), None)
        if ent is None or con is None:
            raise ValueError(f"{model_id} is not an NLI model (its labels are {sorted(labels)})")
        _loaded.clear()
        _loaded[model_id] = (tok, model, ent, con)
        return _loaded[model_id]


def _score(model_id: str, pairs: list) -> list:
    import torch
    tok, model, ent, con = _load(model_id)
    out = []
    for i in range(0, len(pairs), 16):
        batch = pairs[i:i + 16]
        enc = tok([p[:PREMISE_CHARS] for p, _ in batch], [h for _, h in batch], truncation=True,
                  padding=True, max_length=512, return_tensors="pt")
        with torch.inference_mode():
            probs = model(**enc).logits.softmax(-1)
        out.extend({"entail": float(r[ent]), "contradict": float(r[con])} for r in probs)
    return out


async def model_for(workspace_id) -> Optional[str]:
    """The workspace's verification model id, when the citation check is on (the `citation_check`
    setting) and a model is registered for it that runs in this process."""
    try:
        from opencortyx.core.config import runtime as rc
        if not rc.get("citation_check"):
            return None
    except Exception:                                         # noqa: BLE001 - settings not loaded: on
        pass
    try:
        from opencortyx.core.inference.registry import resolve_model
        spec = await resolve_model(workspace_id, "verification")
    except Exception:                                         # noqa: BLE001
        return None
    if not spec or (spec.get("provider") or "").lower() not in ("transformers", "local"):
        return None
    return spec.get("model_id") or None


async def entails(model_id: str, pairs: list) -> Optional[list]:
    """[{entail, contradict}] for each (premise, hypothesis), or None when the model cannot run."""
    if not pairs:
        return []
    try:
        return await asyncio.to_thread(_score, model_id, pairs)
    except Exception:                                         # noqa: BLE001 - checked without it
        return None


__all__ = ["entails", "model_for"]
