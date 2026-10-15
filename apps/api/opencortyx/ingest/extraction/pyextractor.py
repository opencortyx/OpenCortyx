"""Python-plugin extractor mechanism — run author-written Python as an extractor step.

A Python extractor is a small function the author writes in the Studio. It receives the
selected input attributes (the node's own fields + its text) and returns records — exactly
like the LLM / regex / dictionary mechanisms, but the logic is arbitrary Python instead of a
prompt or a pattern. This is the coded-component escape hatch (à la Haystack's `@component`):
when a rule is deterministic and known (date normalization, unit parsing, ID checksums …), you
write it ONCE instead of paying an LLM per node. Multi-language is future-proofed via
`mechanism.language` (only "python" is implemented today; "js"/"wasm" can slot in later).

CONTRACT — the author's code must define a top-level function `extract`:

    def extract(inputs: dict, cortex=None) -> list[dict]:
        # inputs: {"text": <node text>, plus every node attribute by its own name}
        # cortex: the OpenCortyx extractor SDK handle (see ExtractorSDK) — present when the extractor
        #         is model-capable; use cortex.generate(...) to call a model. Omit the 2nd parameter
        #         (def extract(inputs):) if the extractor is pure code; `cortex` is then a module global.
        # return: a list of records; each record is a dict of the emit attributes.
        ...
        return [{"phrase": "last tuesday", "normalized_date": "2024-03-12", "kind": "relative"}]

Also accepted and normalized: a bare dict (one record), or {"records": [...]}. `run` is an
alias for `extract`. Whatever it returns is fed to the SAME emit pipeline as every other
mechanism, so idempotent node ids, edges, embeddings and per-attribute weights all come free.

THE SDK (`cortex`) — this is the surface the future OpenCortyx Plugin SDK formalises. Today it is an
in-process ExtractorSDK injected into the sandbox; tomorrow the SAME method names are a published
package backed by the hardened runtime. Extractors depend on THIS abstraction, never on engine
internals (Dependency Inversion). Two capability families:

  • MODEL      — `cortex.generate(prompt, …)`. Available only when the extractor is `model_capable`.
  • GRAPH READ — `cortex.node/children/neighbors/find/edges/count/retrieval_count`. ALWAYS available:
                 a deterministic extractor that has to look at what is already in the graph (how many
                 chunks this asset has, which entity a fact is about, which columns are mostly NULL)
                 must not need an LLM to do it. Read-only and bounded by construction — the SDK
                 returns plain dicts, never store records, so author code cannot write through them.

`cortex` is therefore never None. Its TRUTHINESS still means "can I call a model?" (`__bool__` →
model_capable), so the long-standing `if cortex:` idiom in author code keeps its original meaning
while `cortex.children()` works either way.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECURITY — READ BEFORE EXPOSING THIS TO UNTRUSTED USERS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Author Python runs IN-PROCESS. The guards below (stripped builtins, an import whitelist, no
  `open`/`eval`/`exec`/dunder access, a wall-clock timeout, an output cap) RAISE the bar but are
  NOT a real sandbox — a determined author can still escape. This is acceptable ONLY because the
  platform is single-tenant / self-hosted today: the extractor author IS the operator, so this is
  "trusted operator code", the same trust level as editing a config file.

  Before Python extractors are offered to untrusted / multi-tenant tenants they MUST move behind a
  real isolation boundary — subprocess + seccomp, a WASM/Pyodide runtime, or a remote execution
  service. That hardened boundary is the planned Plugin SDK; this module is the interim, and its
  public surface (`run_python_extractor`) is designed so the SDK can replace the body without
  touching the engine. Gate exposure with OPENCORTYX_ALLOW_PYTHON_EXTRACTORS (default on for the
  self-hosted build; set to 0 to disable the mechanism entirely).
"""
from __future__ import annotations

import asyncio
from opencortyx.core.config import settings
import builtins as _builtins
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
from opencortyx.core.observability import get_logger
from opencortyx.core.substrate import NodeRepo

log = get_logger(__name__)

_TIMEOUT_S = settings.OPENCORTYX_PYEXTRACTOR_TIMEOUT
# Model-capable extractors call the LLM, so they get a much larger budget than pure-code ones.
_MODEL_TIMEOUT_S = settings.OPENCORTYX_PYEXTRACTOR_MODEL_TIMEOUT
_LLM_CALL_TIMEOUT_S = settings.OPENCORTYX_PYEXTRACTOR_LLM_TIMEOUT
_MAX_CODE = 40_000          # reject implausibly large snippets outright
_MAX_RECORDS = 2_000        # cap output so a bug can't flood the graph
_ENTRYPOINTS = ("extract", "run")

# Modules an extractor may import. Deliberately small: data/text/date utilities only — nothing that
# touches the filesystem, network, subprocess, or the interpreter internals.
_ALLOWED_IMPORTS = {
    "re", "json", "math", "random", "string", "datetime", "time", "calendar",
    "collections", "itertools", "functools", "statistics", "textwrap", "unicodedata",
    "decimal", "fractions", "hashlib", "base64", "uuid",
    # date/NLP helpers the platform already ships and the temporal extractor relies on
    "dateparser", "parsedatetime", "dateutil",
}


def is_enabled() -> bool:
    """Python extractors are opt-out via env. Default ON for the self-hosted single-tenant build."""
    return settings.OPENCORTYX_ALLOW_PYTHON_EXTRACTORS


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """__import__ replacement — only the whitelist (and their submodules) may be imported."""
    root = (name or "").split(".", 1)[0]
    if level != 0 or root not in _ALLOWED_IMPORTS:
        raise ImportError(f"import of '{name}' is not allowed in a Python extractor "
                          f"(allowed: {', '.join(sorted(_ALLOWED_IMPORTS))})")
    return _builtins.__import__(name, globals, locals, fromlist, level)


# A curated builtins subset. No open/exec/eval/compile/input/__import__(raw)/globals/vars/getattr-abuse.
_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "callable", "chr",
    "complex", "dict", "divmod", "enumerate", "filter", "float", "format", "frozenset",
    "hash", "hex", "int", "isinstance", "issubclass", "iter", "len", "list", "map", "max",
    "min", "next", "oct", "ord", "pow", "print", "range", "repr", "reversed", "round", "set",
    "slice", "sorted", "str", "sum", "tuple", "type", "zip", "True", "False", "None",
    "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "AttributeError",
    "StopIteration", "ZeroDivisionError", "RuntimeError",
)


# The name the SDK handle is injected under, inside author code. Branded (OpenCortyx), not a bare
# `llm` — this is the extractor's window onto the platform, like Haystack's own component API.
_SDK_GLOBAL = "cortex"


def _safe_globals(sdk=None) -> dict:
    """Sandbox namespace. The SDK handle (`cortex`) is ALWAYS defined (None for pure-code extractors,
    an ExtractorSDK for model-capable ones) so author code can branch on `if cortex:` without a
    NameError."""
    safe = {n: getattr(_builtins, n) for n in _SAFE_BUILTIN_NAMES if hasattr(_builtins, n)}
    safe["__import__"] = _safe_import
    # `__build_class__` lets authors define helper classes; keep it so `class Foo: ...` works.
    safe["__build_class__"] = getattr(_builtins, "__build_class__")
    return {"__builtins__": safe, _SDK_GLOBAL: sdk}


# Read bounds for the graph surface. An extractor must not be able to pull the whole graph into the
# sandbox: these are the ceilings every read is clamped to, whatever `limit` the author passes.
_MAX_NODE_ROWS = 5_000
_MAX_EDGE_ROWS = 50_000
_GRAPH_CALL_TIMEOUT_S = settings.OPENCORTYX_PYEXTRACTOR_GRAPH_TIMEOUT


def _node_view(n) -> dict:
    """A graph node → the plain dict author code sees. Deliberately a COPY, and deliberately not the
    record itself: the sandbox gets to read the graph, never to write through it."""
    return {
        "id": str(n.id),
        "type": n.type,
        "entity_type": getattr(n, "entity_type", None),
        "label": n.label or "",
        "properties": dict(n.properties or {}),
        "confidence": float(n.confidence) if n.confidence is not None else None,
        "level": getattr(n, "level", None),
        "source_ref": str(n.source_ref) if getattr(n, "source_ref", None) else None,
        "collection_id": str(n.collection_id) if getattr(n, "collection_id", None) else None,
        "created_at": n.created_at.isoformat() if getattr(n, "created_at", None) else None,
        "updated_at": n.updated_at.isoformat() if getattr(n, "updated_at", None) else None,
    }


class ExtractorSDK:
    """The OpenCortyx extractor SDK handle (injected as `cortex`). SINGLE RESPONSIBILITY: give author
    code safe, brokered access to platform capabilities from inside the sandbox — model generation and
    read-only graph access. It owns the async→sync bridge (the sandbox runs in a worker thread; the
    engine's services are async) and routes model calls through the Model Registry, so BYO models,
    budgets and provider routing all apply. New capabilities are added as METHODS here without touching
    the injection contract (Open/Closed). This class is the stable seam the future published Plugin SDK
    re-implements against a hardened runtime — author code never changes.

    TRUTHINESS = "a model is available" (`model_capable`), NOT "the SDK exists" — the handle is now
    always injected, so `if cortex:` in existing author code keeps meaning what it always meant."""

    def __init__(self, loop: asyncio.AbstractEventLoop, workspace_id, default_model_spec, *,
                 node_id=None, collection_id=None, model_capable: bool = False, prompt: str = "",
                 config: dict | None = None):
        self._loop = loop
        self._workspace_id = workspace_id
        self._default_spec = default_model_spec
        self._node_id = node_id
        self._collection_id = collection_id
        self._model_capable = bool(model_capable)
        self._prompt = prompt or ""
        self._config = dict(config or {})

    def __bool__(self) -> bool:
        return self._model_capable

    # ── properties author code may read ──────────────────────────────────────────────────────────
    @property
    def node_id(self):
        """The id of the node this extractor is running over (None for independent extractors)."""
        return str(self._node_id) if self._node_id else None

    @property
    def workspace_id(self):
        return str(self._workspace_id) if self._workspace_id else None

    @property
    def prompt(self) -> str:
        """This extractor's own prompt as configured in the Studio, with the node's placeholders
        already substituted. A coded extractor that calls a model MUST build its call from this —
        that is what keeps an editable prompt editable once the logic lives in code."""
        return self._prompt

    @property
    def can_generate(self) -> bool:
        """Explicit form of the truthiness test, for readability in author code."""
        return self._model_capable

    @property
    def config(self) -> dict:
        """This extractor's own mechanism configuration, as edited in the Studio, minus the code and
        the prompt (which have their own accessors). A coded extractor whose BEHAVIOUR is configured
        by structured settings — the contradiction judge's comparison passes, for instance — reads
        them here, so those settings stay editable data on the card instead of constants in code."""
        return dict(self._config)

    def setting(self, name: str, default=None):
        """Read a workspace runtime setting (Settings → …) by name, e.g. `resolution_mode`.

        Extractors that used to live in the engine could read `runtime_config` directly; moving them
        into the sandbox must not quietly detach them from the settings a user tunes in the UI. Only
        the zero-argument public getters are reachable, so this cannot be used to call arbitrary
        engine code."""
        from opencortyx.core.config import runtime as rc
        key = str(name or "").strip()
        if not key or key.startswith("_"):
            return default
        fn = getattr(rc, key, None)
        if not callable(fn):
            return default
        try:
            import inspect
            if inspect.signature(fn).parameters:          # takes arguments → not a settings getter
                return default
            value = fn()
        except Exception:                                 # noqa: BLE001 — a setting never breaks a run
            return default
        return default if value is None else value

    def log(self, *parts):
        """Print a line into the worker log, tagged so it is attributable to an extractor."""
        log.info("extractor.log", message=" ".join(str(p)[:500] for p in parts))

    def _await(self, coro, timeout=None):
        """Run an engine coroutine on the main loop from this worker thread, block for the result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(
            timeout=timeout or _LLM_CALL_TIMEOUT_S)

    # ── MODEL ────────────────────────────────────────────────────────────────────────────────────
    def generate(self, prompt, *, model=None, json=False, system=None, max_tokens=800, temperature=0.1):
        """Call a generation model. `model` (a registry name) overrides per call; omitted → the
        extractor's configured model / org default. Returns str; with `json=True` returns parsed
        records (a list) or the parsed object. This is 'call our SDK model' — the branded surface."""
        if not self._model_capable:
            raise RuntimeError(
                "cortex.generate() needs a model-capable extractor — turn on the model for this "
                "extractor in the Studio (mechanism.model_capable) before calling a model.")
        from opencortyx.core.inference.llm import complete
        from opencortyx.core.inference.registry import resolve_model
        from opencortyx.ingest.structured.catalog import _parse_json

        async def _run():
            spec = self._default_spec
            if model and self._workspace_id is not None:
                spec = await resolve_model(self._workspace_id, "generation", model)
            sysmsg = system or ("You extract precise structured data. JSON only." if json else None)
            return await complete(str(prompt), system=sysmsg, max_tokens=int(max_tokens),
                                  json_mode=bool(json), temperature=float(temperature), model_spec=spec)

        raw = self._await(_run())
        if not json:
            return raw
        parsed = _parse_json(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("records"), list):
            return parsed["records"]
        return parsed

    def _read(self, fn):
        """Run a read against its OWN session on the engine loop. A separate session (not the caller's)
        because the caller's transaction is mid-emit; a read must never join, or flush, that unit."""
        from opencortyx.core.db import V2Session

        async def _run():
            async with V2Session() as db:
                return await fn(db)

        return self._await(_run(), timeout=_GRAPH_CALL_TIMEOUT_S)

    def _graph(self, make):
        """Run a graph read on the engine loop and wait for it."""
        return self._await(make(), timeout=_GRAPH_CALL_TIMEOUT_S)

    def node(self, node_id=None):
        """One node by id (default: the node this extractor is running over) → dict, or None."""
        nid = node_id or self._node_id
        if not nid:
            return None

        async def _fn():
            n = await NodeRepo().get(self._workspace_id, _as_uuid(nid))
            return _node_view(n) if n is not None else None

        return self._graph(_fn)

    def children(self, type=None, limit=500):
        """Nodes derived from this node (`source_ref` = it) — an asset's chunks, a table's columns."""
        return self.find(type=type, limit=limit, source_ref=self._node_id)

    def neighbors(self, edge_type=None, direction="out", limit=200, node_id=None):
        """Nodes connected to this one. `direction`: out | in | both. `edge_type`: str or list."""
        nid = _as_uuid(node_id or self._node_id)
        if not nid:
            return []
        types = [edge_type] if isinstance(edge_type, str) else list(edge_type or [])
        cap = min(int(limit or 200), _MAX_NODE_ROWS)

        async def _fn():
            hops = await NodeRepo().graph.neighbors(self._workspace_id, [nid], edge_types=types or None,
                                                    direction=direction if direction in ("in", "both") else "out",
                                                    limit=cap)
            out = []
            for hop in hops:
                v = _node_view(hop.node)
                v["edge_type"] = hop.edge.type
                v["edge_direction"] = hop.direction
                out.append(v)
            return out

        return self._graph(_fn)

    def _query(self, type=None, where=None, label=None, source_ref=None, collection_id=None, limit=None):
        from opencortyx.stores.graph import NodeQuery
        types = [type] if isinstance(type, str) else list(type or [])
        return NodeQuery(exact_types=types or None, label=label or None,
                         source_refs=[_as_uuid(source_ref)] if source_ref else None,
                         collection_ids=[_as_uuid(collection_id)] if collection_id else None,
                         props={str(k): v for k, v in (where or {}).items()}, limit=limit)

    def find(self, type=None, where=None, label=None, limit=200, source_ref=None, collection_id=None):
        """Search this org's nodes. `type`: str or list. `where`: {property: value} exact match on the
        node's properties. `label`: case-insensitive exact label match."""
        query = self._query(type, where, label, source_ref, collection_id, min(int(limit or 200), _MAX_NODE_ROWS))

        async def _fn():
            return [_node_view(n) for n in await NodeRepo().find(self._workspace_id, query)]

        return self._graph(_fn)

    def edges(self, type=None, limit=10_000, node_ids=None):
        """Edges in this org as {source, target, type, properties} — for graph algorithms (clustering,
        reachability) that need the topology rather than one node's immediate neighbourhood."""
        types = [type] if isinstance(type, str) else list(type or [])
        cap = min(int(limit or 10_000), _MAX_EDGE_ROWS)
        ids = [_as_uuid(i) for i in (node_ids or [])]

        async def _fn():
            found = await NodeRepo().edges(self._workspace_id, types=types or None, node_ids=ids or None, limit=cap)
            return [{"source": str(e.source_id), "target": str(e.target_id), "type": e.type,
                     "properties": dict(e.properties or {})} for e in found]

        return self._graph(_fn)

    def count(self, type=None, where=None, source_ref=None):
        """How many nodes match — without pulling them into the sandbox."""
        query = self._query(type, where, source_ref=source_ref)

        async def _fn():
            return await NodeRepo().count(self._workspace_id, query)

        return self._graph(_fn)

    def retrieval_count(self, node_ids=None, window=300):
        """How many recent answers cited this node (or any of `node_ids`). None when the audit trail
        is unavailable — a usage extractor must report 'unknown', never a fabricated 0."""
        from sqlalchemy import select, desc

        async def _fn(db):
            from opencortyx.domain.control import AuditLog
            ids = {str(i) for i in (node_ids or [])} | ({self.node_id} if self.node_id else set())
            if not ids:
                return None
            rows = (await db.execute(
                select(AuditLog).where(AuditLog.workspace_id == self._workspace_id,
                                       AuditLog.action == "retrieve/answer")
                .order_by(desc(AuditLog.ts)).limit(int(window)))).scalars().all()
            return sum(1 for a in rows
                       if any(str((t or {}).get("node_id")) in ids
                              for t in ((a.meta or {}).get("trace") or [])))

        try:
            return self._read(_fn)
        except Exception:                                # noqa: BLE001 — usage telemetry is optional
            return None


def _as_uuid(v):
    import uuid as _uuid
    if v is None or isinstance(v, _uuid.UUID):
        return v
    try:
        return _uuid.UUID(str(v))
    except (ValueError, AttributeError, TypeError):
        return None


def _normalize_records(result) -> list:
    """Coerce whatever extract() returned into a list[dict] (the shape the emit engine expects)."""
    if result is None:
        return []
    if isinstance(result, dict):
        recs = result.get("records") if isinstance(result.get("records"), list) else [result]
    elif isinstance(result, (list, tuple)):
        recs = list(result)
    else:
        raise TypeError("extract() must return a list of dicts, a dict, or {'records': [...]}")
    out = [r for r in recs if isinstance(r, dict)]
    return out[:_MAX_RECORDS]


def _compile_and_call(code: str, inputs: dict, sdk=None) -> list:
    """Compile the snippet, find its entrypoint, call it with `inputs`, normalize the result.
    Runs in a worker thread so a slow call can be timed out. The SDK handle is injected as the
    `cortex` global AND — preferred (Dependency Injection) — passed as the 2nd argument when the
    entrypoint declares one (`def extract(inputs, cortex):`)."""
    import inspect
    ns = _safe_globals(sdk=sdk)
    exec(compile(code, "<python_extractor>", "exec"), ns, ns)   # noqa: S102 — sandboxed namespace
    fn = next((ns[e] for e in _ENTRYPOINTS if callable(ns.get(e))), None)
    if fn is None:
        raise NameError(f"define a function named {' or '.join(_ENTRYPOINTS)}(inputs) -> list[dict]")
    try:
        wants_sdk = len(inspect.signature(fn).parameters) >= 2
    except (ValueError, TypeError):
        wants_sdk = False
    result = fn(dict(inputs or {}), sdk) if wants_sdk else fn(dict(inputs or {}))
    return _normalize_records(result)


def _check_code(code: str) -> str:
    if not is_enabled():
        raise RuntimeError("Python extractors are disabled (OPENCORTYX_ALLOW_PYTHON_EXTRACTORS=0)")
    code = (code or "").strip()
    if len(code) > _MAX_CODE:
        raise ValueError(f"extractor code too large ({len(code)} > {_MAX_CODE} chars)")
    return code


async def run_python_extractor_async(code: str, inputs: dict, *, workspace_id=None, model_spec=None,
                                     model_capable: bool = False, timeout: float | None = None,
                                     node_id=None, collection_id=None, prompt: str = "",
                                     config: dict | None = None) -> list:
    """Async entrypoint used by the engine. The `cortex` SDK handle is ALWAYS injected (bound to THIS
    event loop + the org): graph reads are available to every coded extractor, while `cortex.generate`
    is unlocked only when `model_capable` is set (Way 2: detect-with-LLM, normalize-in-code). `cortex`
    is falsy for a pure-code extractor, so `if cortex:` still reads as "can I call a model?".
    `node_id` is the node being extracted from — it is what makes `cortex.children()` /
    `cortex.neighbors()` mean "of THIS node". The snippet runs in an executor thread so a blocking SDK
    call never stalls the event loop, and so a runaway snippet can be timed out."""
    code = _check_code(code)
    if not code:
        return []
    loop = asyncio.get_running_loop()
    sdk = ExtractorSDK(loop, workspace_id, model_spec, node_id=node_id, collection_id=collection_id,
                       model_capable=bool(model_capable), prompt=prompt, config=config)
    to = timeout or (_MODEL_TIMEOUT_S if model_capable else _TIMEOUT_S)
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _compile_and_call(code, inputs, sdk)), timeout=to)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Python extractor exceeded {to}s")


def run_python_extractor(code: str, inputs: dict, *, timeout: float | None = None) -> list:
    """Execute an author Python extractor safely-ish and return list[dict] records.

    Raises ValueError/ImportError/etc. with a readable message on any failure — the caller
    (the engine) catches and surfaces it to the Studio so authors see WHY their code didn't run.
    This is the stable seam the future hardened Plugin SDK will re-implement behind.
    Pure-code only (no `llm`); the engine uses run_python_extractor_async for model-capable ones."""
    code = _check_code(code)
    if not code:
        return []

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_compile_and_call, code, inputs)
        try:
            return fut.result(timeout=timeout or _TIMEOUT_S)
        except _FuturesTimeout:
            raise TimeoutError(f"Python extractor exceeded {timeout or _TIMEOUT_S}s")
