"""Hugging Face models that run INSIDE OpenCortyx, on CPU — the `transformers` provider.

For a machine without a model server. The Models page downloads a model from the Hugging Face Hub
into the shared model cache (`HF_HOME`, the `model_cache` volume, see `hf_hub.py`), and generation
runs here with `transformers`. No server and no GPU, and slow to match: a 0.5B model writes a few
tokens a second on a laptop CPU, and every process that generates (the API for answers, the workers
for extraction) loads its own copy into memory. A model server such as Ollama is the fast path;
this is the one that needs nothing installed.

Loading never downloads. A model missing from the cache is an error that says to download it, not
a multi-gigabyte fetch hidden inside a chat request.
"""
import asyncio
import threading

#: One model per process. Each is hundreds of MB to several GB of RAM, and a process that switched
#: between two would otherwise keep both.
_loaded: dict = {}
_load_lock = threading.Lock()
#: One generation at a time per process: it is CPU-bound and torch already spreads it over every core,
#: so a second one in parallel only makes both slower.
_gen_lock = threading.Lock()


def _not_downloaded(model_id: str):
    from opencortyx.core.inference.registry import NoModelConfigured
    return NoModelConfigured("generation", (
        f"The model '{model_id}' is not downloaded yet. On the Models page, edit it and press "
        "Download under the Model ID."))


def _load(model_id: str):
    """(tokenizer, model) for `model_id`, from the local cache only."""
    with _load_lock:
        hit = _loaded.get(model_id)
        if hit:
            return hit
        from transformers import AutoModelForCausalLM, AutoTokenizer
        try:
            tok = AutoTokenizer.from_pretrained(model_id, local_files_only=True)
            # safetensors only: a pickled checkpoint (.bin/.pt) can run code when it is loaded. Remote code
            # (`trust_remote_code`) stays off, its default.
            model = AutoModelForCausalLM.from_pretrained(model_id, local_files_only=True, dtype="auto",
                                                         use_safetensors=True)
        except OSError as e:
            raise _not_downloaded(model_id) from e
        model.eval()
        _loaded.clear()
        _loaded[model_id] = (tok, model)
        return tok, model


def _encode(tok, prompt: str, system: str | None):
    """The chat-formatted input, or the plain text for a model without a chat template."""
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}]
    if getattr(tok, "chat_template", None):
        return tok.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt",
                                       return_dict=True)
    return tok((system + "\n\n" if system else "") + prompt, return_tensors="pt")


def _options(tok, max_tokens: int, temperature: float) -> dict:
    opts = {"max_new_tokens": int(max_tokens or 256),
            "pad_token_id": tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id}
    if temperature and temperature > 0:
        opts.update(do_sample=True, temperature=float(temperature))
    else:
        opts["do_sample"] = False
    return opts


def _generate(model_id: str, prompt: str, system: str | None, max_tokens: int, temperature: float) -> str:
    import torch
    tok, model = _load(model_id)
    enc = _encode(tok, prompt, system)
    with _gen_lock, torch.inference_mode():
        out = model.generate(**enc, **_options(tok, max_tokens, temperature))
    return tok.decode(out[0][enc["input_ids"].shape[-1]:], skip_special_tokens=True)


async def complete(model_id: str, prompt: str, system: str | None = None, max_tokens: int = 256,
                   temperature: float = 0.2) -> str:
    return await asyncio.to_thread(_generate, model_id, prompt, system, max_tokens, temperature)


async def stream(model_id: str, prompt: str, system: str | None = None, max_tokens: int = 256,
                 temperature: float = 0.2):
    """Yield text as it is generated. On a CPU the first words arrive long before the last."""
    import torch
    from transformers import TextIteratorStreamer

    tok, model = await asyncio.to_thread(_load, model_id)
    enc = _encode(tok, prompt, system)
    streamer = TextIteratorStreamer(tok, skip_prompt=True, skip_special_tokens=True)
    failed: list = []

    def run():
        try:
            with _gen_lock, torch.inference_mode():
                model.generate(**enc, streamer=streamer, **_options(tok, max_tokens, temperature))
        except Exception as e:                      # noqa: BLE001 — re-raised in the caller below
            failed.append(e)
            streamer.end()                          # or the reader below waits forever

    threading.Thread(target=run, daemon=True).start()
    chunks = iter(streamer)
    while True:
        text = await asyncio.to_thread(next, chunks, None)
        if text is None:
            break
        if text:
            yield text
    if failed:
        raise failed[0]
