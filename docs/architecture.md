# OpenCortyx — Knowledge Layer Architecture (v2 Reference)

> **Vision:** the **knowledge layer for AI** — point it at your data, get **grounded, cited, token-efficient context** for any AI app or agent. Graph-based, flexible (anyone can add custom nodes/edges), and trustworthy by construction.

This is the north-star design. It supersedes the sprawling v1 (15 features, ad-hoc storage). The whole architecture rests on one idea:

> **A tiny fixed core + everything else is a plugin.** Flexibility comes from a *plugin contract* and an *open ontology*, not from a big feature list.

---

## 1. Design goals → the mechanism that delivers each

| Goal | How it is achieved (the actual mechanism) |
|---|---|
| **≥99.7% accuracy / no hallucination** | Grounding + provenance on every node + **abstain when unsupported** + citation verification + conflict/freshness guard. Accuracy = *faithfulness* (every answer claim traces to a cited source), measured on an eval set — not "the LLM is always right." |
| **Least tokens** | Top-down retrieval (summaries first), sentence-level strips, dedup, confidence-ranked truncation, per-layer token budget, cached context packages |
| **Precise + high recall** | Recall = fuse complementary patterns (vector + graph + lexical) in the Planner. Precision = rerank + trust filter. (Different layers — see §10.) |
| **Fast (milliseconds)** | Traversal in a graph store built for it, ANN and BM25 in one vector store round trip, precomputed embeddings + materialized summaries, parallel pattern retrieval, rerank only top-K, query-package cache |
| **Flexible (custom nodes/edges)** | Open ontology in a **type registry** + ontology-guided extraction + candidate→promote curation |
| **Works everywhere / no confusion** | One consistent substrate behind two ports, canonical entities (resolution), stable IDs, edges written only between nodes that exist (no ghosts) |

> **Honest note on latency & accuracy.** *Retrieval + assembly* is the millisecond path (target p99 < ~50ms). *Ingestion* and *answer generation* are model-bound (async, not ms). "99.7%" is a **faithfulness target you engineer toward and measure** via the eval harness (§14) — guaranteed by the abstain rule, not by trusting the model.

---

## 2. Core principles
1. **Tiny core, everything else a plugin.** ~7 structural types + an open entity vocabulary + a dozen edges. RAPTOR/communities/NL-SQL/etc. are optional plugins, never core.
2. **Two planes.** *Knowledge plane* = the graph (content). *Control plane* = relational tables (users, keys, billing, audit). They connect by reference, never by graph edges.
3. **One substrate, two ports.** `GraphStore` holds nodes and edges (NebulaGraph); `VectorStore` holds their search surfaces (Qdrant). Everything is written through one gateway, `NodeRepo`, which refuses an edge whose endpoints do not exist — **a ghost edge cannot be written, it is quarantined for replay.**
4. **Provenance is mandatory.** Every derived node carries `source_ref` + `confidence`. No citation → not served.
5. **Abstain over hallucinate.** If nothing is retrieved above the confidence threshold, the answer is "I don't have that."

---

## 3. Architecture — 4 layers + progressive exposure

```
┌──────────────────────────────────────────────────────────────┐
│ Layer 4  TRUST     provenance · confidence · conflict ·        │
│                    freshness · citation-verify · ABSTAIN        │ ← accuracy / no hallucination
├──────────────────────────────────────────────────────────────┤
│ Layer 3  PLANNER   query → pick patterns → parallel retrieve → │
│                    fuse (RRF) → rerank → token-budget assemble  │ ← recall + precision + tokens + speed
├──────────────────────────────────────────────────────────────┤
│ Layer 2  PATTERNS  composable graph recipes (build + retrieve): │
│                    Lexical · EntityGraph · Hierarchy ·          │
│                    Community · HypotheticalQ · AgentMemory · …   │ ← the "many graphs", as plugins
├──────────────────────────────────────────────────────────────┤
│ Layer 1  SUBSTRATE node · edge · type_registry · search surfaces│ ← universal physics (two ports)
└──────────────────────────────────────────────────────────────┘

Exposure:  Profiles  →  Toggles  →  Custom
           (presets)    (on/off)     (write your own Pattern/type)
```

Nobody hand-designs a graph. They pick a **Profile** (`docs_qa`, `agent_memory`, `research`); power users flip **Toggles**; advanced users register **Custom** types/patterns.

---

## 4. Layer 1 — Substrate (the universal physics)

Two ports, one gateway. `core/substrate/repo.py::NodeRepo` is the only writer; nothing else touches a store.

```
                    ┌────────────────────────────────────────────┐
  ingestion ───────▶│ NodeRepo  audit stamps · date links ·        │
  authoring  ──────▶│           edge quarantine · write accounting │
  capabilities ────▶└──────────┬───────────────────┬──────────────┘
                               ▼                   ▼
                       GraphStore port      VectorStore port
                       (NebulaGraph)        (Qdrant)
                       nodes · edges        dense per attribute
                       traversal · paths    BM25 document per node
```

**A node's type is a property, never a table.** A `Table`, an `Attribute`, a `Chunk` and a custom
`Aadhaar` are all `Node` records that differ in `type` and `properties`, so a new type needs no
migration and no new storage.

```python
Node(id, workspace_id, type, label, entity_type, properties,      # properties: anything, no schema
     collection_id, source_ref, confidence, level,          # source_ref: provenance
     sensitivity, x, y, lod, degree, created_at, updated_at)

Edge(id, workspace_id, source_id, target_id, type, weight,        # id = uuid5(source|type+relation|target),
     properties, confidence, created_at)                    # so writing the same edge twice is one edge
```

- **Identity is deterministic.** An edge's id is derived from its endpoints, type and `relation`, so
  re-ingesting converges instead of duplicating, and two facts between the same pair stay two edges.
- **Endpoints must exist.** `NodeRepo.add_edges` writes only edges whose endpoints are present and
  records the rest in `write_quarantine` for replay — the common case is extraction naming an entity
  a later chunk creates.
- **Every write is counted.** `write_accounting` tracks attempted, written, created, updated and
  deleted per organization, and the integrity audit alarms when the counters and the graph disagree:
  a row nothing accounted for means something bypassed the gateway.

**Search surfaces (Qdrant).** One point per attribute carries that attribute's dense vector, and one
`__doc` point per node carries its BM25 document — so a node is found by the one field that matches
rather than by an average of all of them. Every point carries the fields searches filter on (types,
collection, sensitivity, confidence, superseded, structural, internal, memory), so filtering happens
inside the index rather than after it.

**The ontology stays relational**, because it is a rulebook rather than content:

```sql
CREATE TABLE type_registry (
  name        text NOT NULL,
  kind        text NOT NULL,          -- 'node' | 'edge' | 'entity'
  description text NOT NULL,          -- THIS drives ontology-guided extraction
  properties_schema jsonb DEFAULT '{}',
  allowed_source_types text[],        -- edges only
  allowed_target_types text[],        -- edges only
  is_open     boolean DEFAULT true,
  source      text DEFAULT 'system',  -- 'system' | 'user' | 'discovered'
  status      text DEFAULT 'active',  -- 'active' | 'candidate'
  PRIMARY KEY (kind, name)
);
```

Traversal is a graph query (`GO`/`FIND PATH`), not a recursive CTE. An in-memory adapter implements
both ports for tests, so the semantics — what `types` matches, what counts as evidence, how a status
hides a node — live in one shared module and are tested without either service running.

---

## 5. Ontology vs Taxonomy (the distinction that ends the confusion)

| | What it is | Where it lives |
|---|---|---|
| **Ontology** | the **rulebook**: which node/edge types may exist + their rules | `type_registry` (meta-layer, *not* nodes) |
| **Taxonomy** | a **classification tree**: categories content is filed under | `Topic` nodes + `PARENT_OF` edges (*data in the graph*) |

Changing the **ontology** changes what *can* exist. Changing the **taxonomy** changes how existing content is *categorized*.

### Creating custom nodes/edges = adding to the ontology
1. Register a type, e.g. node `Drug {generic_name, dosage}` + edge `INTERACTS_WITH (Drug→Drug)`.
2. The extractor reads the **active** ontology and injects each type's `name + description` into the LLM prompt → it now extracts those.
3. Stored as `node(type='Drug', properties={...})` + `edge(type='INTERACTS_WITH')`. JSONB props → **zero migrations**.
4. Retrieval/patterns treat them like any node/edge.

The `description` field is the magic — it turns a registry row into precise, domain-specific extraction ("ontology-guided extraction").

### Three modes (ship **Hybrid** as default)
- **Open** — LLM invents types freely. Flexible, messy.
- **Guided** — only registered types allowed. Precise, more setup.
- **Hybrid** — ship a small base ontology; LLM proposes new types as `status='candidate'`; user **promotes / renames / merges** them. Flexible *and* clean. The `discovered → candidate → promoted` loop *is* the "add your own nodes/edges" UX.

---

## 6. Base ontology seed (Phase 1 registry contents)

### Structural node types (fixed — framework owns)
| Type | Description (extraction guidance) | Key properties |
|---|---|---|
| `Organization` | The tenant that owns all data | `name` |
| `Source` | A single ingested document | `title, uri, author, published_at, mime, language` |
| `Chunk` | A contiguous text segment of a source | `text, ordinal, section_path, token_count` |
| `Claim` | One atomic, checkable fact stated in the text | `text, polarity, confidence` |
| `Summary` | A condensed overview of other nodes | `text, level, scope` |
| `Topic` | A category/subject used to classify content | `name, path` |
| `Question` | A hypothetical question this content answers (HyDE) | `text` |
| `Collection` | Logical partition (per-app/project/tenant) for scoped retrieval | `name` |

### Entity types — the open vocabulary (seed; users extend freely)
| Entity type | Description (what the LLM extracts) |
|---|---|
| `Person` | A named individual |
| `Organization` | A company, agency, team, institution |
| `Location` | A place — country, city, region, address |
| `Product` | A named product, service, or offering |
| `Concept` | An idea, theory, or topic treated as a thing |
| `Technology` | A tool, system, technique, or standard |
| `Event` | Something that happened at a time |
| `Work` | A referenced document, paper, law, artifact |

### Edge types
| Edge | From → To | Description |
|---|---|---|
| `OWNS` | Organization → Source | tenancy |
| `BELONGS_TO` | Source → Collection | scoping/isolation |
| `CONTAINS` | Source → Chunk | document structure |
| `NEXT` | Chunk → Chunk | reading order (sequence) |
| `PART_OF` | Chunk → Chunk | parent-child / section nesting |
| `MENTIONS` | Chunk → Entity | passage references entity |
| `EXTRACTED_FROM` | Entity/Claim/Summary → Chunk/Source | **provenance / citation** |
| `RELATES_TO` | Entity → Entity | open relationship; meaning in `properties.relation` + `weight` |
| `HAS_CLAIM` | Entity → Claim | facts about an entity |
| `ALIAS_OF` | Entity → Entity | duplicate → canonical (entity resolution) |
| `SUMMARIZES` | Summary → Chunk/Entity/Summary | what a summary covers (recursive = hierarchy) |
| `HAS_TOPIC` | Source/Chunk/Entity → Topic | classification |
| `PARENT_OF` | Topic → Topic | taxonomy hierarchy |
| `ANSWERS` | Question → Chunk | hypothetical-question index |
| `TAGGED_WITH` | any → Label | governance labels (optional) |

### Two rules that stop bloat forever
1. **Attributes are NOT nodes** — custom fields live in `node.properties` (JSONB).
2. **Relationship meanings are NOT edge types** — `WORKS_AT`, `LOCATED_IN`, etc. are `RELATES_TO` with `properties.relation`. Promote to a typed edge only when heavily queried. The edge list stays ~15 forever; the entity vocabulary grows.

---

## 7. Layer 2 — Patterns (the "many graphs," as plugins)

Every published GraphRAG pattern is just node+edge structure over the substrate — they are **composable, not mutually exclusive**:

| Pattern | What it builds on the substrate |
|---|---|
| Lexical / Text Sequence | `Source-CONTAINS->Chunk`, `Chunk-NEXT->Chunk` |
| Parent-Child / Sibling | `Chunk-PART_OF->Chunk` |
| Domain Graph | `Entity-RELATES_TO->Entity` (+ Claims) |
| Lexical + Entities | `Chunk-MENTIONS->Entity` |
| Hierarchy (RAPTOR) | `Summary-SUMMARIZES->Chunk/Summary` |
| Community Summaries | `Summary(scope=community)-SUMMARIZES->Entity` |
| Hypothetical Questions | `Question-ANSWERS->Chunk` |
| Memory (episodic/semantic/procedural/temporal) | nodes scoped `collection=agent_memory` + time props + `PRECEDES` |

A Pattern is a plugin with two halves:
```python
class Pattern(Protocol):
    name: str
    async def build(self, source, ctx) -> None        # create its nodes/edges at ingest
    async def retrieve(self, query, budget, ctx) -> list[ScoredNode]   # traverse it
```

**Profiles** bundle patterns for a use case:
- `docs_qa` = Lexical + ParentChild + EntityGraph
- `research` = + Hierarchy + CommunitySummary
- `agent_memory` = AgentMemory(episodic+semantic+temporal)

---

## 8. Layer 3 — Planner (recall + precision + tokens + speed)

The retrieval loop (this is the engine for the goals):
```
1. identity check  → resolve allowed collections + max sensitivity
2. plan            → pick patterns/retrievers for this query (+ profile)
3. retrieve        → run chosen retrievers IN PARALLEL (vector + graph + lexical)
4. fuse            → reciprocal-rank fusion across results        ← RECALL
5. rerank          → cross-encoder on top-K only                  ← PRECISION
6. top-down assemble → summaries first, drill to chunks on demand ← FEWER TOKENS
   · sentence-level strips · dedup · confidence-ranked truncation · per-layer budget
7. trust filter    → see Layer 4
8. cache           → store context package keyed by (query_hash, identity, ontology_ver)
```

**Recall is a Layer-3 property** (fuse complementary patterns). **Precision is a Layer-4 property** (rerank + trust). Keeping them separate is what lets you push both high.

---

## 9. Layer 4 — Trust (accuracy / no hallucination)

The mechanisms that deliver ≥99.7% faithfulness:
1. **Provenance** — every Entity/Claim/Summary has `EXTRACTED_FROM`; answers cite node IDs.
2. **Confidence gating** — drop nodes below threshold.
3. **Conflict guard** — if two retrieved claims contradict, flag/withhold or present both with sources.
4. **Freshness guard** — warn/down-rank stale items (TTL per type).
5. **Citation verification** — after generation, check every answer sentence maps to a provided source; drop/flag any that don't.
6. **Abstain rule** — if no grounded context above threshold → return "I don't have that." (This single rule is what makes "no hallucination" achievable.)
7. **Entity resolution** — canonical entities prevent duplicate/contradictory confusion.

---

## 10. The two planes — Control plane (relational tables, NOT graph)

Developers/enterprises/founders need these — they ride **beside** the graph and reference it by id. **Never** make them graph nodes (that re-pollutes the graph).

```sql
-- Tenancy / product (founders)
workspace(id, name, plan, created_at)
project(id, workspace_id, name)            -- maps 1:1 to a Collection in the graph
usage(id, workspace_id, metric, value, period)
quota(workspace_id, metric, limit)
billing_event(id, workspace_id, type, amount, ts)
invite(id, workspace_id, email, role, token, expires_at)

-- Identity / access (enterprise)
app_user(id, workspace_id, email, name)            -- 'user' is reserved in PG
team(id, workspace_id, name)
role(id, name, permissions jsonb)
membership(user_id, team_id, role_id)
api_key(id, workspace_id, app_id, hash, scopes, created_at, revoked_at)
app(id, workspace_id, name, kind)                  -- consumer identity (agent/app)
policy(id, workspace_id, group_id, collection_id, max_sensitivity, permission)
audit_log(id, workspace_id, actor_id, action, node_id, source_id, ts, meta jsonb)

-- Quality / governance (enterprise)
retention_policy(id, workspace_id, collection_id, ttl_days, action)
freshness_policy(id, workspace_id, type, ttl_days, warn_before_days)
classification(id, node_id, label, detector, score)   -- PII/sensitivity scan results

-- Developer feedback / eval loop
query_log(id, workspace_id, app_id, query, latency_ms, token_count, package_id, ts)
feedback(id, query_log_id, verdict, note)              -- 👍/👎 → retrieval tuning
eval_run(id, workspace_id, dataset, faithfulness, precision, recall, ts)
webhook(id, workspace_id, event, url, secret)
```

### Access control (the one thing to get right)
- Every `node`/`Source` carries `collection_id` + `sensitivity`.
- `policy` maps **group → allowed collections + max sensitivity**.
- **Every `/context` call carries identity** (api_key → app/user → groups) → the Planner **filters the graph query before ranking**. Document-level security with no graph pollution.

---

## 11. Performance design (how to hit milliseconds + fewer tokens)
- **Each store does what it is built for** → traversal in the graph store, ANN and BM25 in the vector store, and no join between them: a search returns node ids, which the graph store fetches in one batch.
- **Filters ride on the points** → a scoped search is one filtered ANN query, not a scan and a post-filter.
- **Precompute** embeddings + summaries at ingest; retrieval reads, never computes.
- **Parallel** pattern retrievers; **rerank only top-K** (e.g., 50→8).
- **Context-package cache** keyed by `(query_hash, identity, ontology_version)` → repeat queries are a cache hit.
- **Token budget** per layer + sentence-strips + dedup → minimal tokens to the model.
- Target: **retrieval+assembly p99 < ~50ms**. (Ingestion & answer generation are async/model-bound — not part of the ms path.)

---

## 12. Accuracy & evaluation (how you actually reach 99.7%)
You don't *hope* for 99.7% — you **measure** it and gate releases on it.
- **Eval harness** (`eval_run`): a labeled Q→expected-source set per profile.
- **Metrics:** faithfulness (every answer claim cites a correct source), citation precision, retrieval recall@k, abstain-correctness (did it abstain when it should?).
- **Loop:** `feedback` + failed evals → tune retrieval/thresholds/ontology → re-run.
- **Release gate:** ship only if faithfulness ≥ target on the eval set.
- The **abstain rule** is what makes a high faithfulness target reachable: the system refuses rather than guesses.

---

## 13. Build phases (roadmap)
| Phase | Deliverable |
|---|---|
| **1 — MVP** | `node`/`edge`/`type_registry`; ingest→parse→chunk→embed→extract(Entity+Claim)→**entity resolution**→provenance; retrieve (vector + 1-hop graph)→rerank→cite→**abstain**; API `POST /sources`, `POST /context` |
| **2 — Tokens** | `Summary` + `SUMMARIZES`; top-down retrieval + token budget |
| **3 — Trust** | confidence gating, conflict + freshness guard, citation verification, eval harness |
| **4 — Flexibility** | registration API + candidate→promote curation; Pattern SDK; Profiles + Planner fusion; Taxonomy pattern |
| **5 — Scale/Enterprise** | control-plane (RBAC, policy, audit, usage/billing); access-filtered retrieval; webhooks |
| **6 — Optional plugins** | RAPTOR, Community, Hypothetical-Q, AgentMemory, NL-SQL, structured-data (Table/Column) — all as plugins |

---

## 14. Public API (keep it this small)
```
POST /sources            connect/ingest data
POST /context            THE product → { context[], citations[], confidence, abstained }
GET  /graph              explore nodes/edges (dashboard)
POST /ontology/types     register custom node/edge types
GET  /ontology/candidates  review discovered types
POST /ontology/promote   promote a candidate type
POST /patterns           register a custom Pattern (advanced)
```

---

## 15. Invariants (never violate these)
1. One gateway. An edge is written only between nodes that exist, or it is quarantined. **No ghost edges, ever.**
2. Every derived node has `source_ref` + `confidence`.
3. Knowledge plane and control plane never mix.
4. Attributes are properties; relationship meanings are `RELATES_TO.properties.relation`.
5. The core stays tiny; new capability = a Pattern or an ontology entry, not new infrastructure.
6. No grounded context → **abstain**, never fabricate.

---

### One-line summary
**A graph and its search surfaces behind two ports, plus an open ontology (Layer 1) → composable patterns (Layer 2) → a planner that fuses & reranks (Layer 3) → a trust layer that cites or abstains (Layer 4), with a control plane beside it.** That is flexible enough for anyone to add custom nodes/edges, simple enough to keep correct, and engineered toward grounded, fast, low-token, no-hallucination context.
