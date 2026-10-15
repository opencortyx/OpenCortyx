# Memory budget

Every service has a `mem_limit` (with `memswap_limit` equal to it, so an overrun fails fast instead of
swapping). Per-container limits protect services from each other; they do not protect the Docker VM
from running out, so keep the sum of the ceilings in view.

Check it against your Docker VM:

    python scripts/check-memory-budget.py            # exits 1 if the ceilings exceed the VM
    python scripts/check-memory-budget.py --stats    # also shows live usage per service

| Service | last measured | ceiling | notes |
|---|---|---|---|
| api | 1136 MiB | 3072m | embedding model plus retrieval working memory; 2048m was OOM-killed under load |
| embedding-worker | 1034 MiB | 1792m | embedding model |
| extraction-worker | 1041 MiB | 1664m | entity resolution loads the embedding model; 768m was OOM-killed |
| worker | 234 MiB | 3072m | reads PDFs with Docling (layout + TableFormer models); at 1664m a PDF OOM-killed the child on every attempt |
| maintenance-worker | 200 MiB | 832m | one child up to 293 MiB, plus the 512 MiB headroom rule below |
| postgres | 74 MiB | 1024m | control plane + ontology only since ADR-004 |
| nebulagraph | 36 MiB | 1024m | graphd |
| nebula-storaged | 64 MiB | 1024m | grows with the graph |
| nebula-metad | 52 MiB | 512m | |
| qdrant | 132 MiB | 1024m | grows with the number of indexed surfaces |
| web | 85 MiB | 512m | production build |
| mcp | 65 MiB | 192m | |
| redis | 20 MiB | 192m | |
| vllm | 2351 MiB | 2560m | `gpu` profile only — **92% of its ceiling** |
| setup | exits | 768m | one-shot: graph `ADD HOSTS` + schema, vector collection, database migrations |
| **sum** | **6.4 GiB** | **19.31 GiB** | |

The graph and vector stores were missing from this table until 2026-09-19: it still described the
single-Postgres world from ADR-001, which is why the documented ceiling total read 13.25 GiB while
`compose.yaml` had long since totalled 17.62.

Measured on a FRESHLY WIPED install with no ingested corpus, so the store rows are floors rather
than working sets — postgres, nebula-storaged and qdrant all grow with the data. Re-measure under
your own load before trusting them.

Two things this table is currently telling you:

* **vllm sits at 92% of its ceiling while loading.** That is why its engine core dies with
  `EngineCore_DP0: -9` (SIGKILL) on a constrained VM, and why the model server never comes up.
  Raising it means lowering something else, or giving the VM more.
* **The ceilings total 19.31 GiB and the measured working set is 6.4 GiB.** The stack fits a 10 GiB
  VM in practice and does not fit on paper, which is exactly the condition where the kernel, not
  Docker, decides what dies.

Without the `gpu` profile the ceilings total 16.81 GiB and `setup` exits after start-up. Give Docker at least 8 GiB (12 GiB with a local vLLM). On Windows set `memory=` in
`%USERPROFILE%\.wslconfig` and run `wsl --shutdown`.

## Setting the limits

Every ceiling above is a default. Override any of them in `.env`, then run `docker compose up -d`
(or `bin/opencortyx up`) to apply it:

| Variable | Default | Sets |
|---|---|---|
| `API_MEM_LIMIT` | 3072m | api |
| `WORKER_MEM_LIMIT` | 3072m | worker (file reading, Docling) |
| `EXTRACTION_WORKER_MEM_LIMIT` | 1664m | extraction-worker |
| `EMBEDDING_WORKER_MEM_LIMIT` | 1792m | embedding-worker |
| `MAINTENANCE_WORKER_MEM_LIMIT` | 832m | maintenance-worker |
| `POSTGRES_MEM_LIMIT`, `QDRANT_MEM_LIMIT` | 1024m | postgres, qdrant |
| `NEBULAGRAPH_MEM_LIMIT`, `NEBULA_STORAGED_MEM_LIMIT`, `NEBULA_METAD_MEM_LIMIT` | 1024m, 1024m, 512m | the graph store |
| `WEB_MEM_LIMIT`, `MCP_MEM_LIMIT`, `REDIS_MEM_LIMIT` | 512m, 192m, 192m | web, mcp, redis |
| `SETUP_MEM_LIMIT`, `VLLM_MEM_LIMIT` | 768m, 2560m | the one-shot setup, vLLM |
| `CELERY_WORKER_MAX_MEMORY_KB` | 1500000 | worker, per child |
| `EXTRACTION_WORKER_MAX_MEMORY_KB` | 1000000 | extraction-worker, per child |
| `MAINTENANCE_WORKER_MAX_MEMORY_KB` | 300000 | maintenance-worker, per child |

Each limit sets both `mem_limit` and `memswap_limit`, so a container never swaps. Two rules keep an
override safe, and `python scripts/check-memory-budget.py` checks both against your `.env`:

* a worker's `*_MEM_LIMIT` must exceed its per-child cap by at least 512 MiB (below);
* the sum should fit the Docker VM — raise the VM first (Docker Desktop → Settings → Resources →
  Memory on macOS; `memory=` in `%USERPROFILE%\.wslconfig` on Windows), then the ceilings.

Celery workers also cap each child process with `--max-memory-per-child`; a child over its cap
finishes its task and is replaced. The cap is only checked BETWEEN tasks, so leave room above it:
the container must hold the pool parent plus one task's growth past the cap (the guard test asks for
512 MiB). With the old 1.4 GB cap in a 1.6 GiB container, `extraction-worker` was OOM-killed mid-task
hundreds of times on an 8 GB Mac instead of being recycled; `cat /sys/fs/cgroup/memory.events` in a
container shows the count (`oom_kill`). `apps/api/tests/test_memory_guards.py` checks that every service
has a ceiling, that every child cap fits its container, and that this table's sum matches
`compose.yaml`.

If a service is killed for memory, measure its working set (`anon` in the cgroup `memory.stat`)
before raising its ceiling, and lower another ceiling to keep the sum within the VM.
