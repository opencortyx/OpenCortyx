# ADR-005: Module boundaries and dependency direction

## Status
Accepted (2026-09-18)

## Context
`opencortyx` grew capability-first: each new concern was added where it was convenient, which for
a long time was `core`. The result was a kernel that imported its own callers. `core.db` reached
into `retrieve` to seed the ontology; `core` imported `control` for the ORM row types while
`control` imported `core` for the declarative base; `core.warmup` imported a private function out
of `retrieve.rerank`.

Two costs, both paid daily. Nothing could be loaded or tested in isolation, because importing any
part pulled in most of the rest. And the cycles were worked around rather than fixed — 786
internal imports had been pushed inside function bodies, 408 of them the same import repeated in
several functions of one file, which made the real dependency graph invisible to a reader and to
static analysis.

## Decision
Dependencies point one way. The layers, bottom first:

```
domain/      definitions only — the tables, the ontology types. Imports nothing from the app.
core/        the runtime kernel: config, db, substrate, inference, security, worker, observability
ontology/    ingest/  retrieval/  capabilities/  graph_query/
bootstrap.py api/  tasks.py
```

Rules:

1. **`domain` imports nothing from the application.** It defines what a row is; it does not open a
   session or know what reads it.
2. **`core` may not import the layers built on it.** A kernel that reaches up into its callers
   cannot be reasoned about, tested or replaced.
3. **Composition happens at the top.** Deciding that a fresh install ships with a particular
   ontology is a product decision, so it lives in `bootstrap`, not in `core.db`. This is what lets
   `core.db` stay a database module that the ontology can depend on.
4. **Modules are grouped by responsibility, with an explicit public surface.** `core/substrate/`,
   `core/inference/`, `core/security/`, `core/config/`, `core/worker/` each export a named set;
   other layers depend on that, not on the files behind it.
5. **Extension points are registries, not conditionals.** Walkers, capabilities, parsers, and the
   graph and vector adapters are all registered and resolved by name.

Rules 1 and 2 are enforced by `apps/api/tests/test_module_boundaries.py`, not by convention, and
with no allowlist: `core` importing anything above it fails the build outright.

## Consequences
- `domain` and most of `core` can be imported and tested without infrastructure.
- Deferred imports become a deliberate choice (deferring an expensive load) rather than the
  default way to dodge a cycle.
- Backward compatibility is preserved by re-export: `control`, `core.models`, `core.substrate` and
  `core.config` still resolve the names the application has always used, so a boundary can move
  without touching every call site.
- Two modules that were exempt while this landed — `asset_purge` and `progress` — turned out to be
  ingest-shaped jobs rather than kernel concerns, and moved to `ingest/`. That emptied the
  allowlist, which is what removed the last place a violation could hide.
