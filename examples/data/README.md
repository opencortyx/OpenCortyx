# Example data

Sample inputs to exercise **auto mode**, **auto + schema mapping**, and **bound mapping** across all
three paradigms, then view the result in the **Lineage Graph** (dashboard → Graph).

## Files
| Path | Paradigm | Use for |
|---|---|---|
| `json/orders.json` | Semi-structured | flat records → Order nodes; `customer_id`/`product_id` → REFERENCES |
| `json/sessions.json` | Semi-structured | nested `turns[]` → Session→Turn chain (auto), or a bound mapping |
| `json/devices.json` | Semi-structured | different shape again (nested `specs`, `tags[]`) — proves auto handles new schemas |
| `csv/customers.csv` | Structured (file) | rows → Customer nodes (auto column roles) |
| `sql/schema.sql` | Structured (DBMS) | orders/customers/products + FKs → nodes + FK edges (load into Postgres/MySQL, connect it) |
| `docs/onboarding.md` | Unstructured | 2 headings → 2 Chunk nodes → extractors |
| `text/*.txt` | Unstructured | short articles for document ingestion and Q&A |
| `json/benchmark_deep.json`, `json/benchmark_deep_2.json` | Semi-structured | deeply nested payloads for mapping and walker stress tests |

## A. Pure AUTO (no mapping) — the zero-config path
1. Dashboard → **Pipelines** → pick/create a semi-structured pipeline; leave **Schema mapping = none**, Auto-accept ON.
2. **Ingestion Hub** → upload `json/sessions.json` → assign the pipeline → run.
3. It auto-detects the shape, builds the graph, AND saves an **`Auto — …` mapping** (see it in
   Schema Mappings hub, status `auto`).
4. Repeat with `json/devices.json` — a *different* schema — confirm it makes its own auto mapping + nodes.
5. **Graph** tab → see Session/Turn/Device nodes and edges.

## B. AUTO + SCHEMA MAPPING (author, then bind)
1. **Schema Mappings → Semi-Structured engine** → paste `json/orders.json` (or a record) → Detect keys →
   set roles (e.g. `review_text` = text → extract; `customer_id` = id) → **Save**. It appears in the hub.
2. **Pipelines → semi-structured builder** → Step 1 **Schema mapping (starting point)** → pick your saved
   mapping. Inline authoring hides; add an extractor step (its nodes come from the mapping).
3. **Ingestion Hub** → `json/orders.json` → assign this pipeline → run.
4. **Graph** → Order nodes with the exact shape you defined; claims extracted from `review_text`.

## C. Structured (CSV / DBMS)
- **CSV**: Structured engine → upload `csv/customers.csv` → columns auto-detected → Save → bind in the
  structured pipeline builder → ingest the CSV.
- **DBMS**: load `sql/schema.sql` into a Postgres/MySQL, add a **Connector** (Connections tab), then in
  the Structured engine introspect it (source connectivity — see note), map `orders` (FKs → edges), Save,
  bind, ingest.

## D. Unstructured
- Document engine/pipeline → `docs/onboarding.md` → 2 Chunk nodes → add an entity/claim extractor →
  ingest → Graph shows Chunks + extracted entities.

## What to verify in the Lineage Graph
- Auto and mapping-bound runs both produce nodes/edges.
- A **new/different** JSON schema under auto mode creates a fresh `auto` mapping (hub) — not a failure.
- A **bound** mapping produces the exact node/edge types you declared (renames, FK/REFERENCES edges, zips).
