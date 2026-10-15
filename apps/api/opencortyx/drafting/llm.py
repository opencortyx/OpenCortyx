"""The one model call authoring makes, and the rules it obeys.

Every drafter refines a heuristic result with a language model, and every drafter has the same
three problems with doing so: the model may be unreachable, it may answer with prose around its
JSON, and it may answer with JSON that is valid but wrong — a key that is not in the sample, a
mechanism the engine has no branch for, a node type nobody declared. Solved once, here, rather
than four times with four different failure modes.

`ask_json` therefore NEVER RAISES and never returns a partially-trusted object. It returns
`(parsed, note)`: a dict on success, and on any failure `({}, "<why>")` so the caller keeps its
heuristic answer and can put the reason in `Draft.warnings`. A drafter that has to decide whether
an exception from here is fatal is a drafter that will one day decide wrong and 500 on someone
who is holding a file.

Validation of the CONTENT is not here — it belongs to whoever owns the vocabulary being checked
(the engine owns mechanism refs, the ontology owns type names). This module only guarantees you
are looking at a dict that came back from the model.
"""
from __future__ import annotations

import json
from typing import Any

from opencortyx.core.observability import get_logger

log = get_logger(__name__)

#: Authoring is interactive: a person is watching a spinner. Well under the ingest-time budgets.
MAX_TOKENS = 1_500
#: Near-deterministic. A draft that differs each time it is asked cannot be reviewed or reproduced.
TEMPERATURE = 0.1

_SYSTEM = ("You configure a knowledge-extraction system. You answer with JSON only — no prose, "
           "no markdown fences. You never invent field names, keys or types that are not present "
           "in the material you were given.")


async def ask_json(workspace_id, prompt: str, *, model_ref: str | None = None,
                   max_tokens: int = MAX_TOKENS, role: str = "generation") -> tuple[dict, str]:
    """One JSON-mode completion. Returns `(parsed_object, failure_note)`; exactly one is truthy.

    A non-dict top-level answer (a bare list, a string) counts as a failure: every authoring prompt
    asks for an object, and silently accepting a list would push the shape error into a drafter
    that is about to `.get()` on it.
    """
    from opencortyx.core.inference.llm import complete, repair_truncated_json
    from opencortyx.core.inference.registry import resolve_model

    try:
        spec = await resolve_model(workspace_id, role, model_ref)
    except Exception as exc:                        # noqa: BLE001 — resolution never blocks a draft
        log.warning("authoring.model_unresolved", error=str(exc))
        spec = None

    try:
        raw = await complete(prompt, system=_SYSTEM, max_tokens=max_tokens, json_mode=True,
                             temperature=TEMPERATURE, model_spec=spec)
    except Exception as exc:                        # noqa: BLE001 — see module docstring
        note = f"the model was not reachable ({type(exc).__name__}); kept the measured reading"
        log.warning("authoring.model_failed", error=f"{type(exc).__name__}: {exc}")
        return {}, note

    parsed = _loads(raw, repair_truncated_json)
    if not isinstance(parsed, dict):
        log.warning("authoring.model_unparseable", head=str(raw)[:200])
        return {}, "the model's answer was not usable JSON; kept the measured reading"
    return parsed, ""


def model_name(spec: dict | None) -> str:
    """How a draft records which model touched it. Never the api key."""
    if not spec:
        return ""
    return str(spec.get("model_id") or spec.get("name") or "")


def _loads(raw: str, repair) -> Any:
    """Parse, then retry through the shared truncation repair, then give up."""
    text = (raw or "").strip()
    if text.startswith("```"):                      # a fenced block despite the system prompt
        text = text.strip("`")
        text = text[text.index("\n") + 1:] if "\n" in text else text
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return json.loads(repair(text))
    except Exception:                               # noqa: BLE001 — repair is itself best-effort
        return None


def sample_block(obj: Any, limit: int = 2_000) -> str:
    """Render part of a sample for a prompt, bounded, without letting a serialisation error through.

    `default=str` matters: samples routinely carry datetimes and Decimals from DuckDB, and a
    TypeError raised while BUILDING the prompt would bypass every guarantee `ask_json` makes.
    """
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)[:limit]
    except Exception:                               # noqa: BLE001
        return str(obj)[:limit]


__all__ = ["MAX_TOKENS", "TEMPERATURE", "ask_json", "model_name", "sample_block"]
