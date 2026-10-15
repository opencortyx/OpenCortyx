"""Self-contained LLM access for v2 (no v1 import).

ONE dispatcher, because every provider this platform speaks — vLLM (the default, self-hosted),
OpenAI, Azure, Groq, Together — speaks the OpenAI wire format. Anthropic and the HuggingFace
Inference API are the two exceptions and get their own small branches.

There is no local-runtime special case any more: vLLM is reached with the same AsyncOpenAI client as
a hosted endpoint, pointed at `LLM_BASE_URL`. That is the whole reason vLLM needs no bespoke code.

`repair_truncated_json` ports the v1 helper used by the structured catalogue prompts.
"""
import re
from typing import Optional

import httpx

from opencortyx.core import http
from opencortyx.core.config import settings, OPENAI_COMPATIBLE_PROVIDERS

import asyncio
import weakref

_OPENAI_BY_LOOP: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _limits() -> "httpx.Limits":
    return http.limits()


def _timeout() -> "httpx.Timeout":
    # No read deadline shorter than LLM_TIMEOUT_S: a request queued behind a deep vLLM batch is slow,
    # not hung, and cancelling it throws away work the server has already scheduled.
    return httpx.Timeout(settings.LLM_TIMEOUT_S, connect=settings.LLM_CONNECT_TIMEOUT_S)


def _client() -> httpx.AsyncClient:
    return http.get_client(timeout=_timeout())


def _openai_client(api_key=None, base_url=None):
    """Pooled AsyncOpenAI, per (loop, key, base_url).

    The explicit `http_client` is the point: AsyncOpenAI otherwise builds its own httpx client with
    httpx's default 20 keep-alive connections, so the transport quietly became the ceiling on how
    many extraction calls could be in flight — the opposite of what vLLM is for.
    """
    from openai import AsyncOpenAI
    loop = asyncio.get_running_loop()
    by_key = _OPENAI_BY_LOOP.get(loop)
    if by_key is None:
        by_key = {}
        _OPENAI_BY_LOOP[loop] = by_key
    key = (api_key or "", base_url or "")
    c = by_key.get(key)
    if c is None:
        c = AsyncOpenAI(
            api_key=api_key or settings.LLM_API_KEY or "sk-none",
            base_url=base_url or None,
            http_client=httpx.AsyncClient(timeout=_timeout(), limits=_limits()),
            timeout=settings.LLM_TIMEOUT_S,
            max_retries=3)
        by_key[key] = c
    return c


def _vllm_base(explicit: str | None = None) -> str | None:
    """The endpoint an OpenAI-compatible request should go to.

    A self-hosted server has no public endpoint, so with nothing configured the OpenAI SDK silently
    defaults to api.openai.com and fails on auth — an error that reads like a bad key rather than a
    missing base URL.
    """
    return explicit or settings.LLM_BASE_URL or None


async def _openai_complete(prompt, system, max_tokens, json_mode, model, temperature,
                           base_url=None, api_key=None) -> str:
    """vLLM + every other OpenAI-compatible endpoint (Together, Groq, Azure) via base_url.

    The system prompt is sent as its own message rather than concatenated into the user turn, and
    that is a THROUGHPUT decision as much as a correctness one: vLLM's automatic prefix caching keys
    on the token prefix, so an extractor whose instructions are byte-identical across every chunk of
    every document reuses the prefill for all of them. Fold the invariant text in behind the chunk
    and the prefix stops matching, so each call re-prefills the whole instruction block.
    """
    client = _openai_client(api_key, base_url)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    kwargs = {"model": model or settings.LLM_MODEL, "messages": messages,
              "max_tokens": max_tokens, "temperature": temperature}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def _anthropic_complete(prompt, system, max_tokens, model, temperature, api_key=None) -> str:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=api_key or settings.LLM_API_KEY)
    msg = await client.messages.create(
        model=model or "claude-3-5-sonnet-latest", max_tokens=max_tokens, temperature=temperature,
        system=(system or ""), messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in (msg.content or []) if getattr(b, "type", "") == "text")


async def _hf_complete(prompt, system, max_tokens, model, temperature, base_url=None, api_key=None) -> str:
    """HuggingFace Inference API (text-generation) over httpx."""
    url = base_url or f"https://api-inference.huggingface.co/models/{model}"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    full = (f"{system}\n\n" if system else "") + prompt
    r = await _client().post(url, headers=headers, json={
        "inputs": full,
        "parameters": {"max_new_tokens": max_tokens, "temperature": max(temperature, 0.01),
                       "return_full_text": False}})
    r.raise_for_status()
    d = r.json()
    if isinstance(d, list) and d and isinstance(d[0], dict):
        return d[0].get("generated_text", "") or ""
    if isinstance(d, dict):
        return d.get("generated_text", "") or ""
    return str(d)


_WINDOW_BY_ENDPOINT: dict = {}

_CHARS_PER_TOKEN = 2.0
#: chat scaffolding, the system message, and the JSON grammar's own overhead
_PROMPT_OVERHEAD_TOKENS = 256

_ELISION = ("\n\n[... {n} characters elided to fit the model context window ...]\n\n")


async def _endpoint_window(base_url: str | None, model_id: str | None) -> int | None:
    """The window the SERVER will actually accept, asked of the server.

    Configuration is a claim; `/v1/models` is the fact. The two drifting apart is the whole failure
    mode — a config saying 12,288 against a server started with 8,192 fails every long prompt at a
    length the app believes is legal. Asked once per (endpoint, model) per process, and never fatal:
    an endpoint that does not report a window falls back to the configured one.
    """
    key = (base_url or "", model_id or "")
    if key in _WINDOW_BY_ENDPOINT:
        return _WINDOW_BY_ENDPOINT[key]
    win = None
    try:
        base = (base_url or "").rstrip("/")
        if base:
            r = await _client().get(f"{base}/models", timeout=httpx.Timeout(5.0, connect=2.0))
            for m in (r.json().get("data") or []):
                if not model_id or m.get("id") == model_id:
                    win = int(m.get("max_model_len") or 0) or None
                    if win:
                        break
    except Exception:
        win = None
    _WINDOW_BY_ENDPOINT[key] = win
    return win


async def _fit_to_window(prompt: str, max_tokens: int, base_url: str | None,
                         model_id: str | None, discover: bool) -> str:
    """Return `prompt` unchanged, or elided middle-out so the request fits."""
    window = (await _endpoint_window(base_url, model_id)) if discover else None
    window = window or settings.LLM_MAX_MODEL_LEN
    budget_tokens = window - int(max_tokens or 0) - _PROMPT_OVERHEAD_TOKENS
    if budget_tokens <= 0:
        # max_tokens alone exceeds the window: nothing this function does can help, and silently
        # shrinking the caller's requested output would be a different lie. Let the provider say so.
        return prompt
    budget = int(budget_tokens * _CHARS_PER_TOKEN)
    if len(prompt) <= budget:
        return prompt

    dropped = len(prompt) - budget
    marker = _ELISION.format(n=dropped)
    keep = max(0, budget - len(marker))
    head = keep * 2 // 3          # the instructions and the start of the text
    tail = keep - head            # the closing contract ("Return ONLY JSON …")
    out = prompt[:head] + marker + (prompt[-tail:] if tail else "")

    try:
        from opencortyx.core import observability as obs
        label = model_id or settings.LLM_MODEL or "unknown"
        obs.PROMPT_ELISIONS.labels(label).inc()
        obs.PROMPT_CHARS_DROPPED.labels(label).inc(dropped)
        obs.get_logger(__name__).warning(
            "prompt_elided", model=label, window=window, budget_chars=budget,
            original_chars=len(prompt), dropped_chars=dropped)
    except Exception:
        pass
    return out


_LEN_400 = re.compile(r"maximum context length is (\d+) tokens.*?request has (\d+) ", re.S)


def _length_overflow(err: Exception):
    """(window, actual_tokens) when this exception is a context-length rejection, else None."""
    m = _LEN_400.search(str(err))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _reelide(prompt: str, window: int, actual: int, max_tokens: int) -> str:
    """Re-cut `prompt` using the ratio the provider's own count implies."""
    ratio = max(0.5, len(prompt) / max(actual, 1))          # true chars-per-token for THIS prompt
    budget_tokens = max(1, window - int(max_tokens or 0) - _PROMPT_OVERHEAD_TOKENS)
    budget = int(budget_tokens * ratio * 0.95)              # 5% below the measured fit
    if budget >= len(prompt):
        budget = int(len(prompt) * 0.8)
    marker = _ELISION.format(n=len(prompt) - budget)
    keep = max(0, budget - len(marker))
    head = keep * 2 // 3
    return prompt[:head] + marker + (prompt[-(keep - head):] if keep - head else "")


async def _registry_default() -> dict | None:
    """The Models page's generation choice, for a call that did not resolve its own spec — so no call
    silently bypasses the registry and goes wherever the environment happens to point."""
    from opencortyx.core.inference.registry import active_generation_spec
    return await active_generation_spec()


async def complete(prompt: str, system: Optional[str] = None, max_tokens: int = 500,
                   json_mode: bool = False, model: Optional[str] = None,
                   temperature: float = 0.2, provider: Optional[str] = None,
                   model_spec: Optional[dict] = None) -> str:
    """Completion entry point. `model_spec` (from the Model Registry) routes to the right provider
    (vllm / openai / openai_compatible / anthropic / huggingface). Without it, behaviour is the
    env-configured provider — vLLM by default."""
    if model_spec is None and not provider:
        model_spec = await _registry_default()
    if model_spec:
        prov = (model_spec.get("provider") or settings.LLM_PROVIDER or "vllm").lower()
        mid = model_spec.get("model_id") or model
        base = model_spec.get("base_url")
        key = model_spec.get("api_key")
        temp = (model_spec.get("params") or {}).get("temperature", temperature)
    else:
        prov = (provider or settings.LLM_PROVIDER or "vllm").lower()
        mid, base, key, temp = model, None, None, temperature

    prompt = await _fit_to_window(prompt, max_tokens, _vllm_base(base), mid or settings.LLM_MODEL,
                                  discover=prov in OPENAI_COMPATIBLE_PROVIDERS)

    if prov == "anthropic":
        return await _anthropic_complete(prompt, system, max_tokens, mid, temp, api_key=key)
    if prov in ("huggingface", "hf"):
        return await _hf_complete(prompt, system, max_tokens, mid, temp, base_url=base, api_key=key)
    if prov == "groq" and not base:
        base = "https://api.groq.com/openai/v1"
    if prov not in OPENAI_COMPATIBLE_PROVIDERS:
        raise ValueError(
            f"unsupported LLM provider {prov!r}. Text generation runs on vLLM (or any "
            f"OpenAI-compatible endpoint), Anthropic, or HuggingFace Inference — see "
            f"config.OPENAI_COMPATIBLE_PROVIDERS.")
    try:
        return await _openai_complete(prompt, system, max_tokens, json_mode, mid, temp,
                                      base_url=_vllm_base(base), api_key=key)
    except Exception as e:
        over = _length_overflow(e)
        if not over:
            raise
        window, actual = over
        retry = _reelide(prompt, window, actual, max_tokens)
        try:
            from opencortyx.core import observability as obs
            obs.get_logger(__name__).warning(
                "prompt_refit_after_400", model=(mid or settings.LLM_MODEL or "unknown"),
                window=window, reported_tokens=actual,
                original_chars=len(prompt), retry_chars=len(retry))
        except Exception:                                   # noqa: BLE001 — never fail on telemetry
            pass
        return await _openai_complete(retry, system, max_tokens, json_mode, mid, temp,
                                      base_url=_vllm_base(base), api_key=key)


async def _openai_stream(prompt, system, max_tokens, model, temperature, base_url=None, api_key=None):
    client = _openai_client(api_key, base_url)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    stream = await client.chat.completions.create(
        model=model or settings.LLM_MODEL, messages=messages, max_tokens=max_tokens,
        temperature=temperature, stream=True)
    async for chunk in stream:
        delta = (chunk.choices[0].delta.content if chunk.choices else None) or ""
        if delta:
            yield delta


async def _anthropic_stream(prompt, system, max_tokens, model, temperature, api_key=None):
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=api_key or settings.LLM_API_KEY)
    async with client.messages.stream(
            model=model or "claude-3-5-sonnet-latest", max_tokens=max_tokens, temperature=temperature,
            system=(system or ""), messages=[{"role": "user", "content": prompt}]) as stream:
        async for text in stream.text_stream:
            if text:
                yield text


async def stream_complete(prompt: str, system: Optional[str] = None, max_tokens: int = 600,
                          model: Optional[str] = None, temperature: float = 0.2,
                          provider: Optional[str] = None, model_spec: Optional[dict] = None):
    """Async generator that yields answer text chunks as the model produces them. Mirrors `complete`'s
    provider routing. Any streaming failure (or a provider without a streaming path) falls back to a
    single non-streamed chunk so the caller always gets a complete answer."""
    if model_spec is None and not provider:
        model_spec = await _registry_default()
    if model_spec:
        prov = (model_spec.get("provider") or settings.LLM_PROVIDER or "vllm").lower()
        mid = model_spec.get("model_id") or model
        base = model_spec.get("base_url")
        key = model_spec.get("api_key")
        temp = (model_spec.get("params") or {}).get("temperature", temperature)
    else:
        prov = (provider or settings.LLM_PROVIDER or "vllm").lower()
        mid, base, key, temp = model, None, None, temperature
    try:
        if prov == "anthropic":
            async for c in _anthropic_stream(prompt, system, max_tokens, mid, temp, key):
                yield c
        elif prov in ("huggingface", "hf"):
            # No token stream on this API — one chunk is the honest representation.
            yield await _hf_complete(prompt, system, max_tokens, mid, temp, base, key)
        else:
            if prov == "groq" and not base:
                base = "https://api.groq.com/openai/v1"
            async for c in _openai_stream(prompt, system, max_tokens, mid, temp, _vllm_base(base), key):
                yield c
    except Exception:
        # graceful degradation: deliver the whole answer in one chunk rather than failing the stream.
        yield await complete(prompt, system=system, max_tokens=max_tokens, model=mid,
                             temperature=temp, provider=(None if model_spec else provider),
                             model_spec=model_spec)


def repair_truncated_json(json_str: str) -> str:
    """Brace-balance a truncated LLM JSON string (ported from v1)."""
    json_str = (json_str or "").strip()
    if not json_str:
        return "{}"
    while json_str and json_str[-1] not in ("}", "]"):
        json_str = json_str[:-1]
    stack = []
    is_in_string = False
    escape_next = False
    for char in json_str:
        if escape_next:
            escape_next = False
            continue
        if char == "\\":
            escape_next = True
            continue
        if char == '"':
            is_in_string = not is_in_string
            continue
        if not is_in_string:
            if char == "{":
                stack.append("}")
            elif char == "[":
                stack.append("]")
            elif char == "}" and stack and stack[-1] == "}":
                stack.pop()
            elif char == "]" and stack and stack[-1] == "]":
                stack.pop()
    if is_in_string:
        json_str += '"'
    return json_str + "".join(reversed(stack))
