# Example data

Small files to try the three kinds of data: tables, JSON and documents. Add them in **Add data**,
check what was found in the **Mapper**, and see the result in **Graph**.

## Files
| Path | Kind | Use for |
|---|---|---|
| `csv/customers.csv` | Table | rows → Customer records, identified by `id`, shown as `full_name` |
| `json/orders.json` | JSON | flat records → Order records; `customer_id` names a customer in `customers.csv` |
| `json/sessions.json` | JSON | nested `turns[]` → Session and Turn, each turn linked to its session |
| `json/devices.json` | JSON | another shape (nested `specs`, `tags[]`): detection reads new shapes too |
| `json/benchmark_deep.json`, `json/benchmark_deep_2.json` | JSON | deeply nested payloads |
| `sql/schema.sql` | Database | orders, customers, products and their foreign keys (load it into Postgres or MySQL and add a Connection) |
| `docs/onboarding.md` | Document | two headings → two sections, for extractors |
| `text/*.txt` | Document | short articles for document reading and questions |

## A. A table and a JSON file that refer to each other
1. **Add data** → choose `csv/customers.csv` and `json/orders.json` together → **Extract knowledge**
   → **Add 2 files**.
2. **Mapper** → pick `customers`: one kind, Customer (detection may call it *Customers*: click the
   name to rename it), identified by `id`, shown as `full_name`.
3. Pick `orders.json`: one kind, Order, identified by `order_id`. Three orders are too few for
   detection to be sure `customer_id` names a customer, so add it: **Add a relationship** → each
   *Order* refers to a *Customer* through `customer_id` → **Add**.
4. **Save**. **Graph** now holds 3 Customer and 3 Order nodes, each order linked to its customer.

## B. Nested JSON
1. **Add data** → `json/sessions.json` → **Extract knowledge** → **Add**.
2. **Mapper** → `sessions.json`: Session (identified by `session_id`) and Turns (events: each turn
   is new), with a relationship from each turn to the Session it sits in. Rename *Turns* to *Turn*
   if you like: the relationship follows. **Advanced** shows where each is read from (`$.turns[*]`).

## C. Documents
1. **Add data** → `docs/onboarding.md` and `text/mars-exploration.txt` → **Add 2 files**.
2. **Mapper** → `onboarding.md`: read by its layout into two sections, Onboarding and Benefits, one
   paragraph each. `mars-exploration.txt` is plain text, so it has no headings: five paragraphs.
3. On `mars-exploration.txt`, set **Split paragraphs longer than** to 300 and press **Save and read
   this document again**: its three longest paragraphs become two passages each (eight in all). Set
   it back to 800 the same way.
4. **Customize → Extractors** → create an extractor that finds people, places and things (the model).
   Back in the Mapper, on the document, **Run an extractor on this file** → choose it → **Run**. A
   few minutes later (Refresh) the document shows what it found, for example Product, Place,
   Organisation, Person, and **Found by extractors** lists those kinds with a switch each.

## D. A database
Load `sql/schema.sql` into Postgres or MySQL, add it under **Connections**, and read it. Its tables
appear in the Mapper like files.
