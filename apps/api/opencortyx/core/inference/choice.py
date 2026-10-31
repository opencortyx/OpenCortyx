"""Choosing, not writing: the model picks one of a closed list of options.

The planners that use it (retrieval/l2_data/semantic.py, retrieval/l0_understanding/tools.py) never
ask a model to write SQL or a query. They build the options from the data and the ontology - the
tables, columns, real category values, metrics and relationships that exist - and the model only
says which one the question means. A small model cannot write reliable SQL; it can rank a handful of
short phrases, and whatever it picks is valid by construction.

    in-process model (`transformers`)   every option is scored by the model's own likelihood of it as
                                        the answer (mean log-probability of its tokens after the
                                        prompt), the prompt encoded once and its cache reused. With a
                                        `baseline` prompt (the same prompt without the question) the
                                        score is how much MORE likely the question makes an option
                                        than it is anyway - a small model otherwise favours whatever
                                        option is common in general ("surface form competition",
                                        Holtzman et al. 2021)
    any other provider                  the options are numbered and the model answers with a number;
                                        an answer that is no number is matched to the closest option

`choose` returns the options ranked, with a score each, so a caller can see how sure the choice is
and fall back when it is not.
"""
from __future__ import annotations

import asyncio
import difflib
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class Choice:
    index: int
    option: str
    scores: list            # one per option, same order; higher is more likely
    margin: float           # best minus second best (0 when there is one option)


def _ranked(options: list[str], scores: list[float]) -> Choice:
    order = sorted(range(len(options)), key=lambda i: -scores[i])
    best = order[0]
    margin = scores[best] - scores[order[1]] if len(order) > 1 else 0.0
    return Choice(index=best, option=options[best], scores=list(scores), margin=float(margin))


def _score_local(model_id: str, prompt: str, options: list[str], system: Optional[str]) -> list[float]:
    """Mean log-probability of each option as the model's answer to `prompt`."""
    import torch

    from opencortyx.core.inference import local_llm
    tok, model = local_llm._load(model_id)
    enc = local_llm._encode(tok, prompt, system)
    ids = enc["input_ids"]
    out: list[float] = []
    with local_llm._gen_lock, torch.inference_mode():
        first = model(input_ids=ids, attention_mask=enc.get("attention_mask"), use_cache=True)
        cache, last_logits = first.past_key_values, first.logits[0, -1]
        for opt in options:
            o = tok(opt, add_special_tokens=False, return_tensors="pt")["input_ids"]
            if o.shape[-1] == 0:
                out.append(float("-inf"))
                continue
            logp = torch.log_softmax(last_logits.float(), dim=-1)[o[0, 0]].item()
            total, count = logp, 1
            if o.shape[-1] > 1:
                step = model(input_ids=o[:, :-1], past_key_values=cache, use_cache=True)
                lp = torch.log_softmax(step.logits[0].float(), dim=-1)
                total += sum(lp[i, o[0, i + 1]].item() for i in range(o.shape[-1] - 1))
                count = o.shape[-1]
                cache.crop(-(o.shape[-1] - 1))      # back to the prompt alone for the next option
            out.append(total / count)
    return out


_NUM = re.compile(r"\d+")
#: Baseline scores do not depend on the question: (model, baseline prompt, options) -> scores.
_priors: dict = {}


async def _ask_remote(prompt: str, options: list[str], system: Optional[str], model_spec: dict) -> list[float]:
    from opencortyx.core.inference.llm import complete
    listing = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    text = await complete(f"{prompt}\n\nOptions:\n{listing}\n\nAnswer with the number of one option only.",
                          system=system, max_tokens=8, temperature=0.0, model_spec=model_spec)
    m = _NUM.search(text or "")
    scores = [0.0] * len(options)
    if m and 1 <= int(m.group(0)) <= len(options):
        scores[int(m.group(0)) - 1] = 1.0
    else:
        sims = [difflib.SequenceMatcher(None, (text or "").lower(), o.lower()).ratio() for o in options]
        scores[max(range(len(options)), key=lambda i: sims[i])] = 0.5
    return scores


async def choose(prompt: str, options: list[str], *, model_spec: Optional[dict],
                 system: Optional[str] = None, baseline: Optional[str] = None) -> Optional[Choice]:
    """The option the model takes `prompt` to mean, or None (no options, or no model)."""
    options = [str(o) for o in options if str(o).strip()]
    if not options or not model_spec:
        return None
    if len(options) == 1:
        return Choice(index=0, option=options[0], scores=[0.0], margin=0.0)
    from opencortyx.core.config import LOCAL_LLM_PROVIDERS
    prov = (model_spec.get("provider") or "").lower()
    if prov in LOCAL_LLM_PROVIDERS:
        mid = str(model_spec.get("model_id") or "")
        scores = await asyncio.to_thread(_score_local, mid, prompt, options, system)
        if baseline:
            key = (mid, baseline, system, tuple(options))
            cached = _priors.get(key)
            if cached is not None:
                prior = cached
            else:
                prior = await asyncio.to_thread(_score_local, mid, baseline, options, system)
                if len(_priors) > 5000:
                    _priors.clear()
                _priors[key] = prior
            scores = [s - p for s, p in zip(scores, prior)]
    else:
        scores = await _ask_remote(prompt, options, system, model_spec)
    return _ranked(options, scores)


async def workspace_model(workspace_id, role: str = "utility") -> Optional[dict]:
    """The workspace's model for `role`, or None when it has none (choosing then falls back)."""
    from opencortyx.core.inference.registry import resolve_model
    try:
        return await resolve_model(workspace_id, role)
    except Exception:                                         # noqa: BLE001 - no model, no choice
        return None
